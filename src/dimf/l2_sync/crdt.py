"""Class A synchronisation: conflict-free replicated data types.

Two small, well-known CRDTs are implemented:

* ``GCounter`` -- grow-only counter, merge by per-replica maximum;
* ``LWWRegister`` -- last-writer-wins register ordered by (timestamp, replica).

Writes are local and send **zero** coordination messages.  Replicas converge
through periodic full-state gossip, which is counted separately by the
network simulator.  This is enough to demonstrate eventual consistency and
partition healing; it is not a production CRDT library.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from dimf.l2_sync.network import SimulatedNetwork


@dataclass
class GCounter:
    """Grow-only counter; merge is component-wise max."""

    counts: dict[str, int] = field(default_factory=dict)

    def increment(self, replica: str, delta: int = 1) -> None:
        if delta < 0:
            raise ValueError("GCounter only grows")
        self.counts[replica] = self.counts.get(replica, 0) + delta

    @property
    def value(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: "GCounter") -> None:
        for replica, value in other.counts.items():
            self.counts[replica] = max(self.counts.get(replica, 0), value)

    def to_dict(self) -> dict[str, int]:
        return dict(self.counts)

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> "GCounter":
        return cls(counts={str(k): int(v) for k, v in data.items()})


@dataclass
class LWWRegister:
    """Last-writer-wins register with a deterministic tie-break."""

    value: Any = None
    timestamp: int = 0
    replica: str = ""

    def set(self, value: Any, timestamp: int, replica: str) -> None:
        candidate = (timestamp, replica)
        if candidate >= (self.timestamp, self.replica):
            self.value = value
            self.timestamp = timestamp
            self.replica = replica

    def merge(self, other: "LWWRegister") -> None:
        if (other.timestamp, other.replica) > (self.timestamp, self.replica):
            self.value = other.value
            self.timestamp = other.timestamp
            self.replica = other.replica

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "timestamp": self.timestamp, "replica": self.replica}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LWWRegister":
        return cls(value=data.get("value"), timestamp=int(data.get("timestamp", 0)), replica=data.get("replica", ""))


class CRDTReplica:
    """One replica holding a set of named counters and registers."""

    protocol = "crdt"

    def __init__(
        self,
        name: str,
        network: SimulatedNetwork | None = None,
        peers: list[str] | None = None,
        gossip_interval_s: float = 0.25,
    ) -> None:
        self.name = name
        self.network = network
        self.peers = peers or []
        self.gossip_interval_s = gossip_interval_s
        self.counters: dict[str, GCounter] = {}
        self.registers: dict[str, LWWRegister] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.gossip_rounds = 0
        if network is not None:
            network.register(self)

    # -- local writes (no coordination) ----------------------------------
    def increment(self, key: str, delta: int = 1, replica: str | None = None) -> int:
        counter = self.counters.setdefault(key, GCounter())
        counter.increment(replica or self.name, delta)
        return counter.value

    def counter_value(self, key: str) -> int:
        return self.counters.get(key, GCounter()).value

    def set_register(self, key: str, value: Any, timestamp: int | None = None) -> None:
        ts = int(time.time() * 1000) if timestamp is None else timestamp
        register = self.registers.setdefault(key, LWWRegister())
        register.set(value, ts, self.name)

    def register_value(self, key: str) -> Any:
        return self.registers.get(key, LWWRegister()).value

    # -- state transfer ---------------------------------------------------
    def state_dict(self) -> dict[str, Any]:
        return {
            "counters": {k: v.to_dict() for k, v in self.counters.items()},
            "registers": {k: v.to_dict() for k, v in self.registers.items()},
        }

    def merge_state(self, state: dict[str, Any]) -> None:
        for key, data in state.get("counters", {}).items():
            self.counters.setdefault(key, GCounter()).merge(GCounter.from_dict(data))
        for key, data in state.get("registers", {}).items():
            self.registers.setdefault(key, LWWRegister()).merge(LWWRegister.from_dict(data))

    async def receive(self, message: dict[str, Any]) -> None:
        if message.get("type") == "gossip":
            self.merge_state(message["state"])

    async def gossip(self) -> int:
        """Push full state to every peer; returns attempted messages."""
        if self.network is None:
            return 0
        state = self.state_dict()
        attempts = 0
        tasks = []
        for peer in self.peers:
            attempts += 1
            tasks.append(
                self.network.send(
                    self.protocol,
                    self.name,
                    peer,
                    {"type": "gossip", "from": self.name, "state": state},
                )
            )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.gossip_rounds += 1
        return attempts

    async def _gossip_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.gossip_interval_s)
            if self._running:
                await self.gossip()

    def start(self) -> None:
        if self.network is None or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._gossip_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


class CRDTCluster:
    """Convenience wrapper around N replicas and their gossip links."""

    def __init__(
        self,
        network: SimulatedNetwork,
        n: int = 3,
        gossip_interval_s: float = 0.25,
        prefix: str = "crdt",
    ) -> None:
        self.network = network
        self.names = [f"{prefix}-{i}" for i in range(n)]
        self.replicas = [
            CRDTReplica(name, network, [x for x in self.names if x != name], gossip_interval_s)
            for name in self.names
        ]
        self.by_name = {r.name: r for r in self.replicas}

    def start(self) -> None:
        for replica in self.replicas:
            replica.start()

    async def stop(self) -> None:
        for replica in self.replicas:
            await replica.stop()

    async def gossip_all(self) -> None:
        await asyncio.gather(*(r.gossip() for r in self.replicas))

    def converged(self, key: str) -> bool:
        values = {r.counter_value(key) for r in self.replicas}
        return len(values) <= 1
