from __future__ import annotations

import asyncio
import time

import pytest

from dimf.l2_sync.network import SimulatedNetwork
from dimf.l2_sync.raft import NotLeader, RaftCluster


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("condition not met in time")


def make_cluster(n: int = 3, heartbeat: float = 0.03, latency: float = 0.3) -> tuple[SimulatedNetwork, RaftCluster]:
    network = SimulatedNetwork(latency_ms=latency, jitter_ms=0.1, seed=11)
    cluster = RaftCluster(
        network,
        n=n,
        heartbeat_s=heartbeat,
        election_min_s=0.08,
        election_max_s=0.16,
        seed=99,
    )
    return network, cluster


def test_leader_election_and_replication():
    async def scenario() -> None:
        network, cluster = make_cluster()
        cluster.start()
        try:
            leader = await cluster.wait_for_leader(5)
            assert leader.is_leader
            entry = await leader.append({"record_id": "r1"})
            assert entry.index == 1 and entry.term >= 1
            await wait_for(lambda: all(node.commit_index >= 1 for node in cluster.nodes))
            for node in cluster.nodes:
                assert node.log[0].command == {"record_id": "r1"}
            # Followers redirect rather than pretending to be leaders.
            follower = next(node for node in cluster.nodes if not node.is_leader)
            with pytest.raises(NotLeader):
                await follower.append({"record_id": "nope"})
        finally:
            await cluster.stop()
            await network.close()

    asyncio.run(scenario())


def test_leader_crash_failover_and_follower_catch_up():
    async def scenario() -> None:
        network, cluster = make_cluster(n=5)
        cluster.start()
        try:
            old = await cluster.wait_for_leader(5)
            for i in range(3):
                await old.append({"n": i})
            await wait_for(lambda: all(node.commit_index >= 3 for node in cluster.nodes))

            old_name = old.name
            cluster.crash(old_name)
            assert not cluster.by_name[old_name].alive
            new = await cluster.wait_for_leader(6)
            assert new.name != old_name
            assert new.current_term > old.current_term
            entry = await new.append({"n": "after-failover"})
            assert entry.index == 4
            await wait_for(lambda: sum(1 for node in cluster.nodes if node.commit_index >= 4) >= 3)

            # Recover the old leader; it must become a follower and catch up.
            cluster.recover(old_name)
            recovered = cluster.by_name[old_name]
            await wait_for(lambda: recovered.commit_index >= 4, timeout=6)
            assert recovered.role == "follower"
            assert recovered.log[3].command == {"n": "after-failover"}
        finally:
            await cluster.stop()
            await network.close()

    asyncio.run(scenario())


def test_logs_agree_on_committed_prefix():
    async def scenario() -> None:
        network, cluster = make_cluster(n=5)
        cluster.start()
        try:
            leader = await cluster.wait_for_leader(5)
            for i in range(8):
                await leader.append({"i": i})
            await wait_for(lambda: all(node.commit_index >= 8 for node in cluster.nodes), timeout=6)
            committed = [
                [entry.command for entry in node.log[: node.commit_index]] for node in cluster.nodes
            ]
            assert all(items == committed[0] for items in committed)
        finally:
            await cluster.stop()
            await network.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("n,expected", [(3, 4), (5, 8), (7, 12)])
def test_message_count_per_entry_matches_2n_minus_2(n: int, expected: int):
    async def scenario() -> None:
        network = SimulatedNetwork(latency_ms=0.2, jitter_ms=0, seed=5)
        cluster = RaftCluster(
            network,
            n=n,
            heartbeat_s=30.0,  # suppress heartbeats during the measured append
            election_min_s=0.08,
            election_max_s=0.16,
            seed=13,
        )
        cluster.start()
        try:
            leader = await cluster.wait_for_leader(6)
            await asyncio.sleep(0.05)
            network.counters(reset=True)
            await leader.append({"measure": 1})
            assert network.sent("raft") == expected
        finally:
            await cluster.stop()
            await network.close()

    asyncio.run(scenario())
