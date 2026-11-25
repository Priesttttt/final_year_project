"""A simulated asynchronous network with faults and exact message counters.

All nodes live in one Python process.  ``send`` counts every attempt, then
applies crash/partition/drop rules, waits for the configured latency and
jitter, and finally calls ``node.receive(message)``.

This is a deliberate prototype choice: it lets the viva demonstrate message
complexity and faults without Docker clusters, and it is NOT a real network
or a distributed deployment.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from typing import Any, Protocol


class NetworkNode(Protocol):
    name: str

    async def receive(self, message: dict[str, Any]) -> None:  # pragma: no cover - protocol
        ...


class SimulatedNetwork:
    def __init__(
        self,
        latency_ms: float = 1.0,
        jitter_ms: float = 0.5,
        drop_rate: float = 0.0,
        seed: int = 1,
    ) -> None:
        self.latency_ms = max(0.0, latency_ms)
        self.jitter_ms = max(0.0, jitter_ms)
        self.drop_rate = min(max(drop_rate, 0.0), 1.0)
        self._rng = random.Random(seed)
        self.nodes: dict[str, NetworkNode] = {}
        self.crashed: set[str] = set()
        self.byzantine: set[str] = set()
        # Each group in a partition can only talk to nodes in the same group.
        self.partitions: list[set[str]] = []
        self._counts: Counter[tuple[str, str]] = Counter()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self.started_at = time.time()

    # -- topology ---------------------------------------------------------
    def register(self, node: NetworkNode) -> None:
        if node.name in self.nodes:
            raise ValueError(f"node {node.name!r} already registered")
        self.nodes[node.name] = node

    def unregister(self, name: str) -> None:
        self.nodes.pop(name, None)

    # -- faults -----------------------------------------------------------
    def crash(self, name: str) -> None:
        self.crashed.add(name)

    def recover(self, name: str) -> None:
        self.crashed.discard(name)

    def is_alive(self, name: str) -> bool:
        return name in self.nodes and name not in self.crashed

    def set_byzantine(self, name: str, enabled: bool = True) -> None:
        if enabled:
            self.byzantine.add(name)
        else:
            self.byzantine.discard(name)

    def partition(self, groups: list[set[str]]) -> None:
        """Install a partition; nodes in different groups cannot communicate."""
        self.partitions = [set(g) for g in groups if g]

    def heal(self) -> None:
        self.partitions = []

    def _same_partition(self, a: str, b: str) -> bool:
        if not self.partitions:
            return True
        group_a = next((g for g in self.partitions if a in g), None)
        group_b = next((g for g in self.partitions if b in g), None)
        if group_a is None or group_b is None:
            return True
        return group_a is group_b

    # -- sending ----------------------------------------------------------
    def _count(self, protocol: str, event: str) -> None:
        self._counts[(protocol, event)] += 1

    async def send(
        self,
        protocol: str,
        src: str,
        dst: str,
        message: dict[str, Any],
        *,
        count: bool = True,
    ) -> bool:
        """Deliver one message, returning False when it was dropped."""
        if count:
            self._count(protocol, "sent")
        if self._closed or not self.is_alive(src) or not self.is_alive(dst):
            self._count(protocol, "dropped")
            return False
        if not self._same_partition(src, dst):
            self._count(protocol, "dropped")
            return False
        if self.drop_rate and self._rng.random() < self.drop_rate:
            self._count(protocol, "dropped")
            return False

        delay = self.latency_ms
        if self.jitter_ms:
            delay += self._rng.uniform(0.0, self.jitter_ms)
        if delay > 0:
            await asyncio.sleep(delay / 1000.0)

        if not self.is_alive(dst):
            self._count(protocol, "dropped")
            return False
        node = self.nodes[dst]
        try:
            await node.receive(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive; surfaced in logs
            self._count(protocol, "error")
            raise
        self._count(protocol, "delivered")
        return True

    def dispatch(self, protocol: str, src: str, dst: str, message: dict[str, Any]) -> None:
        """Schedule a send without awaiting it (normal protocol use)."""
        if self._closed:
            return
        task = asyncio.create_task(self.send(protocol, src, dst, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- counters ---------------------------------------------------------
    def counters(self, reset: bool = False) -> dict[str, dict[str, int]]:
        """Per-protocol counts keyed by event (sent/delivered/dropped/error)."""
        result: dict[str, dict[str, int]] = {}
        for (protocol, event), value in self._counts.items():
            result.setdefault(protocol, {})[event] = value
        if reset:
            self._counts.clear()
        return result

    def sent(self, protocol: str | None = None) -> int:
        if protocol is None:
            return sum(v for (_, event), v in self._counts.items() if event == "sent")
        return self._counts.get((protocol, "sent"), 0)

    def total_messages(self) -> int:
        return self.sent()

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
