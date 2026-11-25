from __future__ import annotations

import asyncio
import time

import pytest

from dimf.l2_sync.network import SimulatedNetwork
from dimf.l2_sync.pbft import PBFTCluster


def make_cluster(n: int = 4, latency: float = 0.2) -> tuple[SimulatedNetwork, PBFTCluster]:
    network = SimulatedNetwork(latency_ms=latency, jitter_ms=0.1, seed=3)
    return network, PBFTCluster(network, n=n)


async def wait_all_committed(cluster: PBFTCluster, count: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(len(node.committed) >= count for node in cluster.honest_nodes()):
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"honest replicas did not commit {count} requests in time")


def test_cluster_rejects_invalid_size():
    network = SimulatedNetwork()
    with pytest.raises(ValueError):
        PBFTCluster(network, n=5)


def test_commits_and_orders_with_all_honest_nodes():
    async def scenario() -> None:
        network, cluster = make_cluster()
        requests = [{"record_id": f"r{i}", "payload": {"i": i}} for i in range(4)]
        replies = await asyncio.gather(*(cluster.submit(r) for r in requests))
        assert [r["record_id"] for r in replies] == ["r0", "r1", "r2", "r3"]
        await wait_all_committed(cluster, len(requests))
        orders = [[req["record_id"] for _, req in node.ordered_requests()] for node in cluster.nodes]
        assert all(order == ["r0", "r1", "r2", "r3"] for order in orders)
        await network.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("n,expected", [(4, 24), (7, 84), (10, 180)])
def test_normal_case_message_count(n: int, expected: int):
    async def scenario() -> None:
        network, cluster = make_cluster(n=n)
        network.counters(reset=True)
        await cluster.submit({"record_id": "counted"})
        assert network.sent("pbft") == expected == 2 * n * (n - 1)
        await network.close()

    asyncio.run(scenario())


def test_one_silent_byzantine_backup_does_not_block_commit():
    async def scenario() -> None:
        network, cluster = make_cluster(n=4)
        cluster.set_byzantine("pbft-1", "silent")
        replies = await asyncio.gather(
            *(cluster.submit({"record_id": f"b{i}"}) for i in range(3))
        )
        assert [r["record_id"] for r in replies] == ["b0", "b1", "b2"]
        await wait_all_committed(cluster, 3)
        honest = cluster.honest_nodes()
        assert len(honest) == 3
        orders = [[req["record_id"] for _, req in node.ordered_requests()] for node in honest]
        assert all(order == ["b0", "b1", "b2"] for order in orders)
        # The Byzantine node missed the commits, as expected.
        assert cluster.by_name["pbft-1"].committed == {}
        # Fewer messages than the fully honest normal case.
        assert network.sent("pbft") < 3 * 24
        await network.close()

    asyncio.run(scenario())


def test_equivocating_backup_does_not_break_ordering():
    async def scenario() -> None:
        network, cluster = make_cluster(n=4)
        cluster.set_byzantine("pbft-1", "equivocate")
        reply = await cluster.submit({"record_id": "eq"})
        assert reply["record_id"] == "eq"
        await wait_all_committed(cluster, 1)
        orders = [[req["record_id"] for _, req in node.ordered_requests()] for node in cluster.honest_nodes()]
        assert all(order == ["eq"] for order in orders)
        await network.close()

    asyncio.run(scenario())


def test_two_byzantine_nodes_with_four_replicas_cannot_be_tolerated():
    """f=1 for n=4; silencing two nodes leaves only two honest replicas."""

    async def scenario() -> None:
        network, cluster = make_cluster(n=4)
        cluster.set_byzantine("pbft-1", "silent")
        cluster.set_byzantine("pbft-2", "silent")
        with pytest.raises(TimeoutError):
            await cluster.submit({"record_id": "should-timeout"}, timeout=0.5)
        await network.close()

    asyncio.run(scenario())
