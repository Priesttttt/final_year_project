"""A readable subset of Raft (leader election + log replication).

Implemented
-----------
* randomised election timeouts, terms, one vote per term;
* log up-to-date check for votes;
* AppendEntries replication with consistency check and conflict truncation;
* majority commit (only entries from the current term are committed directly,
  which is the standard Raft rule);
* leader crash and follower recovery/catch-up.

Deliberately omitted (documented): cluster membership changes, log
snapshots/compaction, pre-vote, and forwarding client requests from followers.
A follower returns ``NotLeader`` and the caller may retry at the leader.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from dimf.l2_sync.network import SimulatedNetwork


class NotLeader(Exception):
    def __init__(self, leader_id: str | None) -> None:
        self.leader_id = leader_id
        super().__init__(f"not leader; known leader={leader_id}")


@dataclass(frozen=True)
class LogEntry:
    index: int
    term: int
    command: Any

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "term": self.term, "command": self.command}


ApplyCallback = Callable[[LogEntry], Any | Awaitable[Any]]


class RaftNode:
    protocol = "raft"

    def __init__(
        self,
        name: str,
        peers: list[str],
        network: SimulatedNetwork,
        *,
        heartbeat_s: float = 0.10,
        election_min_s: float = 0.30,
        election_max_s: float = 0.60,
        seed: int = 1,
        apply_callback: ApplyCallback | None = None,
    ) -> None:
        self.name = name
        self.peers = peers
        self.network = network
        self.heartbeat_s = heartbeat_s
        self.election_min_s = election_min_s
        self.election_max_s = election_max_s
        self.apply_callback = apply_callback
        self._rng = random.Random(seed)

        self.role = "follower"  # follower | candidate | leader
        self.current_term = 0
        self.voted_for: str | None = None
        self.leader_id: str | None = None
        self.log: list[LogEntry] = []
        self.commit_index = 0
        self.last_applied = 0

        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self._votes: set[str] = set()
        self._last_contact = time.monotonic()
        self._last_heartbeat = 0.0
        self._election_timeout = self._new_timeout()
        self._commit_waiters: dict[int, asyncio.Future[LogEntry]] = {}
        self._tick_task: asyncio.Task[None] | None = None
        self._running = False
        self._alive = True
        self._started_at = time.time()
        self.applied: list[LogEntry] = []
        self.elections_started = 0
        self.last_election_time: float | None = None
        network.register(self)

    # -- lifecycle --------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def cluster_size(self) -> int:
        return len(self.peers) + 1

    @property
    def majority(self) -> int:
        return self.cluster_size // 2 + 1

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_contact = time.monotonic()
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        self._running = False
        if self._tick_task is not None:
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
            self._tick_task = None

    def crash(self) -> None:
        """Simulate a crash: stop acting and drop in-flight client waits."""
        self._alive = False
        self.network.crash(self.name)
        self._fail_waiters(NotLeader(None))

    def recover(self) -> None:
        """Recover as a follower; the cluster will catch it up."""
        self._alive = True
        self.network.recover(self.name)
        self.role = "follower"
        self.leader_id = None
        self.voted_for = None
        self._last_contact = time.monotonic()
        self._election_timeout = self._new_timeout()
        if self._running:
            # Restart the tick loop if it was left running; it checks _alive.
            pass

    def _new_timeout(self) -> float:
        return self._rng.uniform(self.election_min_s, self.election_max_s)

    # -- log helpers ------------------------------------------------------
    @property
    def last_index(self) -> int:
        return len(self.log)

    @property
    def last_term(self) -> int:
        return self.log[-1].term if self.log else 0

    def term_at(self, index: int) -> int:
        if index == 0:
            return 0
        if 1 <= index <= len(self.log):
            return self.log[index - 1].term
        return -1

    @property
    def is_leader(self) -> bool:
        return self.role == "leader" and self._alive

    # -- tick / election --------------------------------------------------
    async def _tick_loop(self) -> None:
        tick = max(0.005, min(0.02, self.heartbeat_s / 2))
        while self._running:
            await asyncio.sleep(tick)
            if not self._alive:
                continue
            now = time.monotonic()
            if self.role == "leader":
                if now - self._last_heartbeat >= self.heartbeat_s:
                    self._last_heartbeat = now
                    self._broadcast_append()
            else:
                if now - self._last_contact >= self._election_timeout:
                    await self._start_election()

    async def _start_election(self) -> None:
        if not self._alive:
            return
        self.role = "candidate"
        self.current_term += 1
        self.voted_for = self.name
        self.leader_id = None
        self._votes = {self.name}
        self._last_contact = time.monotonic()
        self._election_timeout = self._new_timeout()
        self.elections_started += 1
        self.last_election_time = time.time()
        self._fail_waiters(NotLeader(None))

        request = {
            "type": "request_vote",
            "term": self.current_term,
            "candidate": self.name,
            "last_log_index": self.last_index,
            "last_log_term": self.last_term,
        }
        for peer in self.peers:
            self.network.dispatch(self.protocol, self.name, peer, request)
        # A single-node cluster wins immediately.
        if len(self._votes) >= self.majority:
            self._become_leader()

    def _become_leader(self) -> None:
        if not self._alive:
            return
        self.role = "leader"
        self.leader_id = self.name
        self.next_index = {p: self.last_index + 1 for p in self.peers}
        self.match_index = {p: 0 for p in self.peers}
        self._last_heartbeat = 0.0
        # Commit an entry from the current term if a previous leader already
        # replicated it; then send an immediate heartbeat.
        self._advance_commit()
        self._broadcast_append()

    # -- RPC send ---------------------------------------------------------
    def _broadcast_append(self) -> None:
        for peer in self.peers:
            self._send_append(peer)

    def _send_append(self, peer: str) -> None:
        if not self.is_leader:
            return
        next_idx = self.next_index.get(peer, self.last_index + 1)
        prev_index = next_idx - 1
        prev_term = self.term_at(prev_index)
        if prev_term == -1:
            next_idx = self.last_index + 1
            prev_index = self.last_index
            prev_term = self.last_term
            self.next_index[peer] = next_idx
        entries = [e.to_dict() for e in self.log[prev_index:]]
        self.network.dispatch(
            self.protocol,
            self.name,
            peer,
            {
                "type": "append_entries",
                "term": self.current_term,
                "leader": self.name,
                "prev_index": prev_index,
                "prev_term": prev_term,
                "entries": entries,
                "leader_commit": self.commit_index,
            },
        )

    # -- receive ----------------------------------------------------------
    async def receive(self, message: dict[str, Any]) -> None:
        if not self._alive:
            return
        msg_type = message.get("type")
        if msg_type == "request_vote":
            await self._handle_request_vote(message)
        elif msg_type == "request_vote_response":
            self._handle_request_vote_response(message)
        elif msg_type == "append_entries":
            await self._handle_append_entries(message)
        elif msg_type == "append_entries_response":
            self._handle_append_response(message)

    def _step_down(self, term: int) -> None:
        if term > self.current_term:
            self.current_term = term
            self.voted_for = None
            self.role = "follower"
            self.leader_id = None
            self._last_contact = time.monotonic()
            self._election_timeout = self._new_timeout()
            self._fail_waiters(NotLeader(None))

    async def _handle_request_vote(self, msg: dict[str, Any]) -> None:
        term = int(msg["term"])
        candidate = str(msg["candidate"])
        granted = False
        if term > self.current_term:
            self._step_down(term)
        if term == self.current_term:
            up_to_date = (int(msg["last_log_term"]), int(msg["last_log_index"])) >= (
                self.last_term,
                self.last_index,
            )
            if (self.voted_for is None or self.voted_for == candidate) and up_to_date:
                self.voted_for = candidate
                self._last_contact = time.monotonic()
                granted = True
        await self.network.send(
            self.protocol,
            self.name,
            candidate,
            {
                "type": "request_vote_response",
                "term": self.current_term,
                "voter": self.name,
                "vote_granted": granted,
            },
        )

    def _handle_request_vote_response(self, msg: dict[str, Any]) -> None:
        term = int(msg["term"])
        if term > self.current_term:
            self._step_down(term)
            return
        if self.role != "candidate" or term != self.current_term:
            return
        if msg.get("vote_granted"):
            self._votes.add(str(msg["voter"]))
            if len(self._votes) >= self.majority:
                self._become_leader()

    async def _handle_append_entries(self, msg: dict[str, Any]) -> None:
        term = int(msg["term"])
        if term < self.current_term:
            await self._reply_append(msg, success=False, match_index=0, hint=None)
            return
        if term > self.current_term:
            self._step_down(term)
        if self.role != "follower":
            self.role = "follower"
        self.leader_id = str(msg["leader"])
        self._last_contact = time.monotonic()
        self._election_timeout = self._new_timeout()

        prev_index = int(msg["prev_index"])
        prev_term = int(msg["prev_term"])
        if prev_index > self.last_index:
            await self._reply_append(msg, success=False, match_index=0, hint=self.last_index + 1)
            return
        if prev_index > 0 and self.term_at(prev_index) != prev_term:
            # Hint the leader to back up to the start of this conflicting term.
            conflict_term = self.term_at(prev_index)
            idx = prev_index
            while idx > 1 and self.term_at(idx - 1) == conflict_term:
                idx -= 1
            await self._reply_append(msg, success=False, match_index=0, hint=idx)
            return

        entries = msg.get("entries", [])
        for raw in entries:
            entry = LogEntry(int(raw["index"]), int(raw["term"]), raw["command"])
            if entry.index <= self.last_index and self.term_at(entry.index) != entry.term:
                del self.log[entry.index - 1 :]
            if entry.index > self.last_index:
                self.log.append(entry)

        leader_commit = min(int(msg["leader_commit"]), self.last_index)
        if leader_commit > self.commit_index:
            self.commit_index = leader_commit
            self._apply_committed()
        await self._reply_append(
            msg,
            success=True,
            match_index=prev_index + len(entries),
            hint=None,
        )

    async def _reply_append(
        self,
        request: dict[str, Any],
        *,
        success: bool,
        match_index: int,
        hint: int | None,
    ) -> None:
        await self.network.send(
            self.protocol,
            self.name,
            str(request["leader"]),
            {
                "type": "append_entries_response",
                "term": self.current_term,
                "follower": self.name,
                "success": success,
                "match_index": match_index,
                "hint": hint,
            },
        )

    def _handle_append_response(self, msg: dict[str, Any]) -> None:
        term = int(msg["term"])
        if term > self.current_term:
            self._step_down(term)
            return
        if not self.is_leader or term != self.current_term:
            return
        peer = str(msg["follower"])
        if msg.get("success"):
            self.match_index[peer] = max(self.match_index.get(peer, 0), int(msg["match_index"]))
            self.next_index[peer] = max(self.next_index.get(peer, 1), int(msg["match_index"]) + 1)
            self._advance_commit()
            # If the follower is still behind, send the missing suffix now.
            if self.next_index[peer] <= self.last_index:
                self._send_append(peer)
        else:
            hint = msg.get("hint")
            if hint is not None:
                self.next_index[peer] = max(1, min(self.next_index.get(peer, 1) - 1, int(hint)))
            else:
                self.next_index[peer] = max(1, self.next_index.get(peer, 1) - 1)
            self._send_append(peer)

    # -- commit / apply ---------------------------------------------------
    def _advance_commit(self) -> None:
        if not self.is_leader:
            return
        changed = False
        while self.commit_index < self.last_index:
            candidate = self.commit_index + 1
            # Only entries from the current term are committed by counting;
            # older entries follow indirectly (Raft Figure 8 rule).
            if self.term_at(candidate) != self.current_term:
                # Look ahead: a later current-term entry can carry it.
                nxt = candidate
                while nxt < self.last_index and self.term_at(nxt) != self.current_term:
                    nxt += 1
                if nxt > self.last_index or self.term_at(nxt) != self.current_term:
                    break
                candidate = nxt
            replicated = 1 + sum(1 for p in self.peers if self.match_index.get(p, 0) >= candidate)
            if replicated >= self.majority:
                self.commit_index = candidate
                changed = True
            else:
                break
        if changed:
            self._apply_committed()
            self._resolve_waiters()

    def _apply_committed(self) -> None:
        newly = self.log[self.last_applied : self.commit_index]
        for entry in newly:
            self.applied.append(entry)
            if self.apply_callback is not None:
                result = self.apply_callback(entry)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
        self.last_applied = max(self.last_applied, self.commit_index)

    def _resolve_waiters(self) -> None:
        for index in list(self._commit_waiters):
            if index <= self.commit_index:
                future = self._commit_waiters.pop(index)
                if not future.done():
                    future.set_result(self.log[index - 1])

    def _fail_waiters(self, error: Exception) -> None:
        for future in self._commit_waiters.values():
            if not future.done():
                future.set_exception(error)
        self._commit_waiters.clear()

    # -- client API -------------------------------------------------------
    async def append(self, command: Any, timeout: float = 10.0) -> LogEntry:
        """Append *command* on the leader and wait until it is committed."""
        if not self._alive:
            raise NotLeader(None)
        if self.role != "leader":
            raise NotLeader(self.leader_id)
        index = self.last_index + 1
        entry = LogEntry(index=index, term=self.current_term, command=command)
        self.log.append(entry)
        future: asyncio.Future[LogEntry] = asyncio.get_running_loop().create_future()
        self._commit_waiters[index] = future
        self._broadcast_append()
        if self.cluster_size == 1:
            self.commit_index = index
            self._apply_committed()
            self._resolve_waiters()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._commit_waiters.pop(index, None)
            raise TimeoutError(f"raft entry {index} not committed within {timeout}s") from exc

    def status(self) -> dict[str, Any]:
        return {
            "node": self.name,
            "role": self.role,
            "term": self.current_term,
            "leader": self.leader_id,
            "alive": self._alive,
            "log_length": self.last_index,
            "commit_index": self.commit_index,
            "last_applied": self.last_applied,
            "voted_for": self.voted_for,
            "elections_started": self.elections_started,
        }


class RaftCluster:
    """Build and control an in-process Raft cluster."""

    def __init__(
        self,
        network: SimulatedNetwork,
        n: int = 3,
        *,
        heartbeat_s: float = 0.10,
        election_min_s: float = 0.30,
        election_max_s: float = 0.60,
        seed: int = 1,
        apply_callback: ApplyCallback | None = None,
        prefix: str = "raft",
    ) -> None:
        if n < 1:
            raise ValueError("raft cluster needs at least one node")
        self.network = network
        self.names = [f"{prefix}-{i}" for i in range(n)]
        self.n = n
        self.nodes = [
            RaftNode(
                name,
                [x for x in self.names if x != name],
                network,
                heartbeat_s=heartbeat_s,
                election_min_s=election_min_s,
                election_max_s=election_max_s,
                seed=seed + i * 997,
                apply_callback=apply_callback,
            )
            for i, name in enumerate(self.names)
        ]
        self.by_name = {node.name: node for node in self.nodes}

    def start(self) -> None:
        for node in self.nodes:
            node.start()

    async def stop(self) -> None:
        for node in self.nodes:
            await node.stop()

    @property
    def leader(self) -> RaftNode | None:
        leaders = [n for n in self.nodes if n.is_leader]
        if not leaders:
            return None
        # Highest term wins if a stale leader has not stepped down yet.
        return max(leaders, key=lambda n: n.current_term)

    async def wait_for_leader(self, timeout: float = 5.0) -> RaftNode:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            leader = self.leader
            if leader is not None:
                # Give one heartbeat interval for followers to learn the leader.
                await asyncio.sleep(0.02)
                return leader
            await asyncio.sleep(0.01)
        raise TimeoutError("no Raft leader elected")

    def crash(self, name: str) -> None:
        self.by_name[name].crash()

    def recover(self, name: str) -> None:
        self.by_name[name].recover()

    def status(self) -> list[dict[str, Any]]:
        return [node.status() for node in self.nodes]
