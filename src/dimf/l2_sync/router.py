"""L2 routing: map each data class to its cheapest adequate mechanism."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from dimf.common.metrics import MetricsCollector
from dimf.common.models import Record
from dimf.l2_sync.crdt import CRDTCluster
from dimf.l2_sync.network import SimulatedNetwork
from dimf.l2_sync.pbft import PBFTCluster, PBFTNotPrimary
from dimf.l2_sync.raft import NotLeader, RaftCluster
from dimf.l3_verify.authlog import AuthLog


@dataclass
class RouteResult:
    record_id: str
    data_class: str
    index: int
    tree_size: int
    latency_ms: float
    anchored: bool
    protocol: str
    messages_for_entry: int


class SyncRouter:
    """Owns the three clusters and sends committed records to L3."""

    def __init__(
        self,
        network: SimulatedNetwork,
        crdt: CRDTCluster,
        raft: RaftCluster,
        pbft: PBFTCluster,
        authlog: AuthLog,
        metrics: MetricsCollector,
    ) -> None:
        self.network = network
        self.crdt = crdt
        self.raft = raft
        self.pbft = pbft
        self.authlog = authlog
        self.metrics = metrics
        self._crdt_rr = 0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.crdt.start()
        self.raft.start()
        self._started = True

    async def stop(self) -> None:
        await self.crdt.stop()
        await self.raft.stop()
        self._started = False

    async def route(self, record: Record) -> RouteResult:
        started = time.perf_counter()
        before = self.network.sent()
        if record.data_class == "A":
            protocol = await self._route_a(record)
        elif record.data_class == "B":
            protocol = await self._route_b(record)
        else:
            protocol = await self._route_c(record)
        append_result = await self.authlog.append(record.canonical_dict())
        latency_ms = (time.perf_counter() - started) * 1000.0
        messages = self.network.sent() - before
        self.metrics.incr("records_accepted")
        self.metrics.incr(f"records_class_{record.data_class}")
        self.metrics.record_latency("overall", latency_ms)
        self.metrics.record_latency(f"class_{record.data_class}", latency_ms)
        self.metrics.record_entry_messages(record.data_class, messages)
        return RouteResult(
            record_id=record.record_id,
            data_class=record.data_class,
            index=append_result.index,
            tree_size=append_result.tree_size,
            latency_ms=latency_ms,
            anchored=append_result.anchored,
            protocol=protocol,
            messages_for_entry=messages,
        )

    async def _route_a(self, record: Record) -> str:
        # No coordination: one replica accepts the write, gossip propagates
        # state later.  Records are still appended to the verifiable log so an
        # auditor can check them; class A does not claim a total order.
        alive = [r for r in self.crdt.replicas if self.network.is_alive(r.name)]
        if not alive:
            raise RuntimeError("no CRDT replica is alive")
        replica = alive[self._crdt_rr % len(alive)]
        self._crdt_rr += 1
        replica.increment("records_a", 1)
        replica.set_register("last_record_a", record.record_id, record.timestamp)
        return "crdt"

    async def _route_b(self, record: Record) -> str:
        deadline = time.monotonic() + 10.0
        while True:
            leader = self.raft.leader
            if leader is not None:
                try:
                    await leader.append(record.canonical_dict())
                    return "raft"
                except NotLeader:
                    pass
                except TimeoutError:
                    # A leader crash mid-append; retry at the new leader.
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError("no Raft leader available")
            await asyncio.sleep(0.02)

    async def _route_c(self, record: Record) -> str:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                await self.pbft.submit(record.canonical_dict())
                return "pbft"
            except PBFTNotPrimary:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.02)

    # -- status and faults ------------------------------------------------
    def status(self) -> dict[str, Any]:
        crdt_replicas = []
        for replica in self.crdt.replicas:
            crdt_replicas.append(
                {
                    "node": replica.name,
                    "alive": self.network.is_alive(replica.name),
                    "gossip_rounds": replica.gossip_rounds,
                    "records_a": replica.counter_value("records_a"),
                    "last_record_a": replica.register_value("last_record_a"),
                }
            )
        return {
            "network": {
                "latency_ms": self.network.latency_ms,
                "jitter_ms": self.network.jitter_ms,
                "drop_rate": self.network.drop_rate,
                "partitions": [sorted(g) for g in self.network.partitions],
                "messages": self.network.counters(),
            },
            "A": {"protocol": "crdt", "replicas": crdt_replicas},
            "B": {
                "protocol": "raft",
                "n": self.raft.n,
                "quorum": self.raft.n // 2 + 1,
                "leader": (self.raft.leader.name if self.raft.leader else None),
                "nodes": self.raft.status(),
            },
            "C": {
                "protocol": "pbft",
                "n": self.pbft.n,
                "f": self.pbft.f,
                "primary": self.pbft.primary.name,
                "nodes": self.pbft.status(),
            },
        }

    async def inject_fault(
        self,
        action: str,
        data_class: str | None = None,
        node: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if action == "crash":
            if not node:
                raise ValueError("node is required for crash")
            self.network.crash(node)
            if node in self.raft.by_name:
                self.raft.by_name[node].crash()
            if node in self.pbft.by_name:
                self.pbft.by_name[node].crash()
            return {"action": action, "node": node, "status": "crashed"}

        if action == "recover":
            if not node:
                raise ValueError("node is required for recover")
            self.network.recover(node)
            if node in self.raft.by_name:
                self.raft.by_name[node].recover()
            if node in self.pbft.by_name:
                self.pbft.by_name[node].recover()
            return {"action": action, "node": node, "status": "recovered"}

        if action == "byzantine":
            target = node
            if target is None:
                candidates = [n.name for n in self.pbft.nodes if not n.is_primary]
                if not candidates:
                    raise ValueError("no PBFT backup available")
                target = candidates[0]
            if target not in self.pbft.by_name:
                raise ValueError(f"{target} is not a PBFT node")
            self.pbft.set_byzantine(target, "silent" if enabled else "off")
            return {"action": action, "node": target, "byzantine": enabled}

        if action == "partition":
            groups = self._partition_groups(data_class)
            self.network.partition(groups)
            return {"action": action, "groups": [sorted(g) for g in groups]}

        if action == "heal":
            self.network.heal()
            return {"action": action, "status": "healed"}

        raise ValueError(f"unknown fault action {action!r}")

    def _partition_groups(self, data_class: str | None) -> list[set[str]]:
        groups: list[set[str]] = []
        classes = [data_class] if data_class else ["A", "B", "C"]
        for cls in classes:
            if cls == "A":
                names = [r.name for r in self.crdt.replicas]
                if len(names) >= 2:
                    groups.append({names[0]})
                    groups.append(set(names[1:]))
            elif cls == "B":
                names = [n.name for n in self.raft.nodes]
                leader = self.raft.leader
                leader_name = leader.name if leader else names[0]
                groups.append({leader_name})
                groups.append(set(names) - {leader_name})
            elif cls == "C":
                names = [n.name for n in self.pbft.nodes]
                primary = self.pbft.primary.name
                groups.append({primary})
                groups.append(set(names) - {primary})
        return [g for g in groups if g]
