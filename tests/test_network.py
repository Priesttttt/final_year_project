from __future__ import annotations

import asyncio
from typing import Any

from dimf.l2_sync.network import SimulatedNetwork


class EchoNode:
    def __init__(self, name: str, network: SimulatedNetwork) -> None:
        self.name = name
        self.received: list[dict[str, Any]] = []
        network.register(self)

    async def receive(self, message: dict[str, Any]) -> None:
        self.received.append(message)


def test_messages_are_delivered_and_counted():
    async def scenario() -> None:
        network = SimulatedNetwork(latency_ms=1, jitter_ms=0, seed=1)
        _a, b = EchoNode("a", network), EchoNode("b", network)
        ok = await network.send("test", "a", "b", {"x": 1})
        assert ok and b.received == [{"x": 1}]
        counts = network.counters()
        assert counts["test"]["sent"] == 1
        assert counts["test"]["delivered"] == 1
        assert network.sent("test") == 1

    asyncio.run(scenario())


def test_partition_blocks_and_heals():
    async def scenario() -> None:
        network = SimulatedNetwork(latency_ms=0, jitter_ms=0, seed=1)
        _a, b, c = EchoNode("a", network), EchoNode("b", network), EchoNode("c", network)
        network.partition([{"a"}, {"b", "c"}])
        assert not await network.send("test", "a", "b", {"n": 1})
        assert await network.send("test", "b", "c", {"n": 2})
        assert len(b.received) == 0 and len(c.received) == 1
        network.heal()
        assert await network.send("test", "a", "b", {"n": 3})
        assert len(b.received) == 1
        assert network.counters()["test"]["dropped"] == 1

    asyncio.run(scenario())


def test_crash_and_drop_rate():
    async def scenario() -> None:
        network = SimulatedNetwork(latency_ms=0, jitter_ms=0, drop_rate=1.0, seed=1)
        _a, b = EchoNode("a", network), EchoNode("b", network)
        assert not await network.send("test", "a", "b", {"x": 1})
        network.drop_rate = 0.0
        network.crash("b")
        assert not await network.send("test", "a", "b", {"x": 1})
        network.recover("b")
        assert await network.send("test", "a", "b", {"x": 1})
        assert len(b.received) == 1
        assert network.counters()["test"]["dropped"] == 2

    asyncio.run(scenario())
