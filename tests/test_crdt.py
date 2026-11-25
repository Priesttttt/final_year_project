from __future__ import annotations

import asyncio

from dimf.l2_sync.crdt import CRDTCluster, GCounter, LWWRegister
from dimf.l2_sync.network import SimulatedNetwork


def test_gcounter_merge_is_commutative_and_idempotent():
    a = GCounter()
    a.increment("r1", 3)
    a.increment("r2", 1)
    b = GCounter()
    b.increment("r1", 2)
    b.increment("r3", 7)
    a.merge(b)
    b.merge(a)
    assert a.value == b.value == 11
    before = a.to_dict()
    a.merge(b)
    assert a.to_dict() == before


def test_lww_register_tie_break_and_merge():
    a = LWWRegister()
    a.set("old", timestamp=10, replica="r1")
    b = LWWRegister()
    b.set("new", timestamp=10, replica="r2")
    a.merge(b)
    assert a.value == "new"
    b.merge(a)
    assert b.value == "new"
    a.set("later", timestamp=11, replica="r1")
    b.merge(a)
    assert b.value == "later"


def test_crdt_converges_after_partition_heals():
    async def scenario() -> None:
        network = SimulatedNetwork(latency_ms=0, jitter_ms=0, seed=1)
        cluster = CRDTCluster(network, n=3, gossip_interval_s=10)
        r0, r1, r2 = cluster.replicas
        network.partition([{"crdt-0"}, {"crdt-1", "crdt-2"}])
        r0.increment("hits")
        r0.increment("hits")
        r1.increment("hits")
        r2.increment("hits")
        r2.set_register("status", "B", timestamp=5)
        r0.set_register("status", "A", timestamp=4)
        await cluster.gossip_all()
        assert r0.counter_value("hits") == 2
        assert r1.counter_value("hits") == 2
        network.heal()
        await cluster.gossip_all()
        await cluster.gossip_all()
        assert {r.counter_value("hits") for r in cluster.replicas} == {4}
        assert {r.register_value("status") for r in cluster.replicas} == {"B"}
        assert cluster.converged("hits")

    asyncio.run(scenario())


def test_writes_send_zero_coordination_messages():
    async def scenario() -> None:
        network = SimulatedNetwork(latency_ms=0, jitter_ms=0, seed=1)
        cluster = CRDTCluster(network, n=3, gossip_interval_s=10)
        before = network.sent("crdt")
        for i in range(50):
            cluster.replicas[i % 3].increment("writes")
        assert network.sent("crdt") == before == 0
        await cluster.gossip_all()
        assert network.sent("crdt") == 6  # 3 replicas x 2 peers, gossip only

    asyncio.run(scenario())
