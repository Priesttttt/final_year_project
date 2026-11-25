"""Class C synchronisation: a compact PBFT-style Byzantine ordering protocol.

Normal case (no view change, primary known):

1. primary assigns a sequence number and sends PRE-PREPARE to n-1 backups;
2. each backup sends PREPARE to every other replica: (n-1)^2 messages;
3. every replica sends COMMIT to every other replica: n(n-1) messages.

Total protocol messages per entry: (n-1) + (n-1)^2 + n(n-1) = 2n(n-1),
which is 84 for n=7 -- matching the analytical formula used in the paper.

Deliberately omitted (documented): view change, checkpoints, garbage
collection and a separate client that collects f+1 replies.  The submitting
primary acts as the client and commits after 2f+1 COMMIT messages.  This is
enough to demonstrate ordering with one silent/equivocating Byzantine backup;
a Byzantine primary would require view change and is out of scope.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from dimf.common.crypto import canonical_bytes
from dimf.l2_sync.network import SimulatedNetwork


def request_digest(request: Any) -> str:
    return hashlib.sha256(canonical_bytes(request)).hexdigest()


class PBFTNotPrimary(Exception):
    pass


@dataclass
class PBFTNode:
    name: str
    peers: list[str]
    network: SimulatedNetwork
    f: int
    view: int = 0
    primary_name: str = ""
    _alive: bool = True
    byzantine_mode: str | None = None  # None | "silent" | "equivocate"
    next_sequence: int = 0
    prepared: dict[tuple[int, int, str], set[str]] = field(default_factory=dict)
    commits: dict[tuple[int, int, str], set[str]] = field(default_factory=dict)
    requests: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    prepared_sent: set[tuple[int, int, str]] = field(default_factory=set)
    commit_sent: set[tuple[int, int, str]] = field(default_factory=set)
    committed: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    _waiters: dict[tuple[int, int], asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    _started_at: float = field(default_factory=time.time)

    protocol = "pbft"

    def __post_init__(self) -> None:
        self.network.register(self)

    @property
    def alive(self) -> bool:
        return self._alive and self.name not in self.network.crashed

    @property
    def cluster_size(self) -> int:
        return len(self.peers) + 1

    @property
    def is_primary(self) -> bool:
        return self.name == self.primary_name

    @property
    def is_byzantine(self) -> bool:
        return self.byzantine_mode is not None or self.name in self.network.byzantine

    def crash(self) -> None:
        self._alive = False
        self.network.crash(self.name)
        for future in self._waiters.values():
            if not future.done():
                future.set_exception(PBFTNotPrimary("node crashed"))
        self._waiters.clear()

    def recover(self) -> None:
        self._alive = True
        self.network.recover(self.name)

    # -- message helpers --------------------------------------------------
    def _send(self, dst: str, message: dict[str, Any]) -> None:
        if not self.alive or self.is_byzantine:
            return
        self.network.dispatch(self.protocol, self.name, dst, message)

    def _broadcast(self, message: dict[str, Any], exclude_self: bool = True) -> None:
        for peer in self.peers:
            if exclude_self and peer == self.name:
                continue
            self._send(peer, message)

    # -- client entry point (primary only) --------------------------------
    async def submit(self, request: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        if not self.alive:
            raise PBFTNotPrimary("primary is not alive")
        if not self.is_primary:
            raise PBFTNotPrimary(f"{self.name} is not the primary ({self.primary_name})")
        if self.is_byzantine:
            raise PBFTNotPrimary("primary is configured Byzantine; view change is out of scope")
        seq = self.next_sequence
        self.next_sequence += 1
        digest = request_digest(request)
        message = {
            "type": "pre_prepare",
            "view": self.view,
            "seq": seq,
            "digest": digest,
            "request": request,
            "primary": self.name,
        }
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[(self.view, seq)] = future
        # The primary processes its own PRE-PREPARE locally (no network send),
        # then sends it to the n-1 backups.
        await self._handle_pre_prepare(message)
        for peer in self.peers:
            self._send(peer, message)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._waiters.pop((self.view, seq), None)
            raise TimeoutError(f"PBFT sequence {seq} did not commit") from exc

    # -- receive ----------------------------------------------------------
    async def receive(self, message: dict[str, Any]) -> None:
        if not self.alive:
            return
        if self.is_byzantine and self.byzantine_mode == "silent":
            return
        msg_type = message.get("type")
        if msg_type == "pre_prepare":
            await self._handle_pre_prepare(message)
        elif msg_type == "prepare":
            await self._handle_prepare(message)
        elif msg_type == "commit":
            await self._handle_commit(message)

    async def _handle_pre_prepare(self, msg: dict[str, Any]) -> None:
        if int(msg["view"]) != self.view or msg.get("primary") != self.primary_name:
            return
        seq = int(msg["seq"])
        digest = str(msg["digest"])
        if request_digest(msg["request"]) != digest:
            return
        key = (self.view, seq)
        if key in self.requests and request_digest(self.requests[key]) != digest:
            return  # conflicting pre-prepare; no view change in this prototype
        self.requests[key] = msg["request"]

        prepare = {
            "type": "prepare",
            "view": self.view,
            "seq": seq,
            "digest": digest,
            "replica": self.name,
        }
        if self.is_byzantine and self.byzantine_mode == "equivocate":
            # Send a prepare for a different digest to everyone.  Honest nodes
            # will never reach a certificate for it.
            bad = dict(prepare)
            bad["digest"] = hashlib.sha256(b"byzantine-equivocation").hexdigest()
            self._broadcast(bad)
            return
        # A backup prepares; the primary does not send PREPARE (it sent
        # PRE-PREPARE).  Record the replica's own prepare locally.
        if not self.is_primary:
            pkey = (self.view, seq, digest)
            self.prepared.setdefault(pkey, set()).add(self.name)
            self._broadcast(prepare)
            await self._maybe_send_commit(pkey)
        # The primary waits for 2f PREPAREs from backups in _handle_prepare.

    async def _handle_prepare(self, msg: dict[str, Any]) -> None:
        if int(msg["view"]) != self.view:
            return
        seq = int(msg["seq"])
        digest = str(msg["digest"])
        key = (self.view, seq)
        # Only accept prepares matching a known pre-prepare.
        if key not in self.requests or request_digest(self.requests[key]) != digest:
            return
        pkey = (self.view, seq, digest)
        self.prepared.setdefault(pkey, set()).add(str(msg["replica"]))
        await self._maybe_send_commit(pkey)

    async def _maybe_send_commit(self, pkey: tuple[int, int, str]) -> None:
        view, seq, digest = pkey
        # A prepared certificate needs 2f matching prepares from backups
        # (the pre-prepare contributes the remaining replica).
        back_up_prepares = self.prepared.get(pkey, set()) - {self.primary_name}
        if len(back_up_prepares) < 2 * self.f:
            return
        if pkey in self.commit_sent:
            return
        self.commit_sent.add(pkey)
        commit = {"type": "commit", "view": view, "seq": seq, "digest": digest, "replica": self.name}
        self.commits.setdefault(pkey, set()).add(self.name)
        self._broadcast(commit)
        await self._maybe_commit(pkey)

    async def _handle_commit(self, msg: dict[str, Any]) -> None:
        if int(msg["view"]) != self.view:
            return
        seq = int(msg["seq"])
        digest = str(msg["digest"])
        key = (self.view, seq)
        if key not in self.requests or request_digest(self.requests[key]) != digest:
            return
        pkey = (self.view, seq, digest)
        self.commits.setdefault(pkey, set()).add(str(msg["replica"]))
        await self._maybe_commit(pkey)

    async def _maybe_commit(self, pkey: tuple[int, int, str]) -> None:
        view, seq, digest = pkey
        if len(self.commits.get(pkey, set())) < 2 * self.f + 1:
            return
        key = (view, seq)
        if key in self.committed:
            return
        # Committed order is the primary's sequence order.
        self.committed[key] = self.requests[key]
        future = self._waiters.pop(key, None)
        if future is not None and not future.done():
            future.set_result(self.requests[key])

    # -- inspection -------------------------------------------------------
    def ordered_requests(self) -> list[tuple[int, dict[str, Any]]]:
        return [(seq, req) for (view, seq), req in sorted(self.committed.items()) if view == self.view]

    def status(self) -> dict[str, Any]:
        return {
            "node": self.name,
            "primary": self.primary_name,
            "view": self.view,
            "alive": self.alive,
            "byzantine": self.is_byzantine,
            "byzantine_mode": self.byzantine_mode,
            "committed": len(self.committed),
            "next_sequence": self.next_sequence,
        }


class PBFTCluster:
    """Convenience wrapper for an n = 3f+1 PBFT cluster."""

    def __init__(self, network: SimulatedNetwork, n: int = 4, prefix: str = "pbft") -> None:
        if n < 4 or (n - 1) % 3 != 0:
            raise ValueError("PBFT requires n = 3f + 1 (4, 7, 10, 13, ...)")
        self.network = network
        self.n = n
        self.f = (n - 1) // 3
        self.names = [f"{prefix}-{i}" for i in range(n)]
        self.view = 0
        primary_name = self.names[self.view % n]
        self.nodes = [
            PBFTNode(
                name,
                [x for x in self.names if x != name],
                network,
                f=self.f,
                view=self.view,
                primary_name=primary_name,
            )
            for name in self.names
        ]
        self.by_name = {node.name: node for node in self.nodes}

    @property
    def primary(self) -> PBFTNode:
        return self.by_name[self.names[self.view % self.n]]

    async def submit(self, request: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        return await self.primary.submit(request, timeout=timeout)

    def set_byzantine(self, name: str, mode: str = "silent") -> None:
        node = self.by_name[name]
        node.byzantine_mode = None if mode == "off" else mode
        self.network.set_byzantine(name, node.byzantine_mode is not None)

    def crash(self, name: str) -> None:
        self.by_name[name].crash()

    def recover(self, name: str) -> None:
        self.by_name[name].recover()

    def status(self) -> list[dict[str, Any]]:
        return [node.status() for node in self.nodes]

    def honest_nodes(self) -> list[PBFTNode]:
        return [n for n in self.nodes if not n.is_byzantine and n.alive]
