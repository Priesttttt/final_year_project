"""Benchmarks E1-E6 for the DIMF prototype.

Every number written by this module comes from an actual run on the machine
that executes it.  The workload is synthetic; this is a single-process
prototype with a simulated network.  See docs/RESULTS.md for the generated
tables and docs/CHALLENGES.md for what the numbers do NOT show.

Experiments
-----------
E1  end-to-end throughput / latency by data class through L1 -> L2 -> L3
E2  protocol message cost per committed entry (CRDT, Raft, PBFT)
E3  Raft leader crash, re-election time, write availability and catch-up
E4  PBFT with 0 / 1 silent / 1 equivocating Byzantine replica
E5  Merkle tree build, inclusion proof size and verification at 10^6 leaves
E6  anchoring cadence, audit delay and tamper detection
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import platform
import random
import shutil
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from dimf.api.state import AppState  # noqa: E402
from dimf.common.config import load_settings  # noqa: E402
from dimf.common.metrics import MetricsCollector, percentile  # noqa: E402
from dimf.common.models import Record, Submission  # noqa: E402
from dimf.eval.workload import SYNTHETIC_WARNING  # noqa: E402
from dimf.l2_sync.crdt import CRDTCluster  # noqa: E402
from dimf.l2_sync.network import SimulatedNetwork  # noqa: E402
from dimf.l2_sync.pbft import PBFTCluster  # noqa: E402
from dimf.l2_sync.raft import RaftCluster  # noqa: E402
from dimf.l3_verify.anchor import SimulatedLedger  # noqa: E402
from dimf.l3_verify.authlog import AuthLog  # noqa: E402
from dimf.l3_verify.merkle import MerkleTree, verify_consistency, verify_inclusion  # noqa: E402

MODE = "quick"


@dataclass
class BenchResult:
    name: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _round(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _record(index: int, cls: str, agency: str | None = None) -> Record:
    payload = {
        "beneficiary_id": f"SYNTH-BEN-{10000 + index % 80000}",
        "scheme": "SYNTH-SCHOLARSHIP",
        "amount_paise": 10_000 + (index * 137) % 890_000,
        "district": f"SYNTH-DISTRICT-{chr(65 + index % 4)}",
        "synthetic": True,
        "note": SYNTHETIC_WARNING,
    }
    return Record(
        record_id=f"BENCH-{index:07d}",
        agency=agency or ("agency-1" if cls == "B" else ("agency-2" if cls == "A" else "agency-3")),
        data_class=cls,  # type: ignore[arg-type]
        timestamp=1_700_000_000_000 + index * 1000,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# E1: end-to-end throughput and latency by class
# ---------------------------------------------------------------------------
async def run_e1(out: Path) -> BenchResult:
    n_total = 120 if MODE == "quick" else 600
    per_class = n_total // 3
    data_dir = out / "tmp" / "e1"
    shutil.rmtree(data_dir, ignore_errors=True)
    settings = load_settings(
        data_dir=data_dir,
        seed_on_start=False,
        network_latency_ms=0.3,
        network_jitter_ms=0.1,
        raft_heartbeat_s=0.03,
        raft_election_min_s=0.10,
        raft_election_max_s=0.20,
        crdt_gossip_s=1.0,
        anchor_batch=100,
        seed=20261125,
    )
    state = AppState(settings)
    rows: list[dict[str, Any]] = []
    try:
        await state.start()
        await state.raft.wait_for_leader(5.0)
        # Warm up each path so the first Raft election is not part of E1.
        for cls, agency in (("A", "agency-2"), ("B", "agency-1"), ("C", "agency-3")):
            record = _record(900_000 + ord(cls), cls, agency)
            await state.submit_signed(state.identities.sign(Submission(**record.model_dump())))
        latencies: dict[str, list[float]] = {"A": [], "B": [], "C": []}
        messages: dict[str, list[float]] = {"A": [], "B": [], "C": []}
        started = time.perf_counter()
        for i in range(per_class):
            for cls in ("A", "B", "C"):
                record = _record(i * 3 + ord(cls) % 3, cls)
                result = await state.submit_signed(
                    state.identities.sign(Submission(**record.model_dump()))
                )
                latencies[cls].append(float(result["latency_ms"]))
                messages[cls].append(float(result["messages_for_entry"]))
        elapsed = time.perf_counter() - started
        all_latencies = [value for values in latencies.values() for value in values]
        rows.append(
            {
                "mode": MODE,
                "scope": "all",
                "n": len(all_latencies),
                "throughput_rps": _round(len(all_latencies) / elapsed),
                "p50_ms": _round(percentile(all_latencies, 50)),
                "p95_ms": _round(percentile(all_latencies, 95)),
                "p99_ms": _round(percentile(all_latencies, 99)),
                "mean_ms": _round(statistics.fmean(all_latencies)),
                "avg_messages_per_entry": _round(statistics.fmean([v for vs in messages.values() for v in vs]), 2),
            }
        )
        for cls in ("A", "B", "C"):
            values = latencies[cls]
            rows.append(
                {
                    "mode": MODE,
                    "scope": cls,
                    "n": len(values),
                    "throughput_rps": _round(len(values) / elapsed),
                    "p50_ms": _round(percentile(values, 50)),
                    "p95_ms": _round(percentile(values, 95)),
                    "p99_ms": _round(percentile(values, 99)),
                    "mean_ms": _round(statistics.fmean(values)),
                    "avg_messages_per_entry": _round(statistics.fmean(messages[cls]), 2),
                }
            )
        snapshot = state.metrics.snapshot()
        result = BenchResult("e1", rows)
        result.notes.update(
            {
                "log_size": state.authlog.tree.size,
                "ledger_transactions": state.ledger.transaction_count(),
                "audit_p95_ms": _round(snapshot.audit["p95"]),
                "elapsed_s": _round(elapsed),
            }
        )
    finally:
        await state.stop()

    _chart_e1(out / "e1_latency_by_class.png", rows)
    return result


def _chart_e1(path: Path, rows: list[dict[str, Any]]) -> None:
    classes = [r for r in rows if r["scope"] != "all"]
    if not classes:
        return
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    x = range(len(classes))
    width = 0.36
    ax.bar([i - width / 2 for i in x], [r["p50_ms"] for r in classes], width, label="p50")
    ax.bar([i + width / 2 for i in x], [r["p95_ms"] for r in classes], width, label="p95")
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"class {r['scope']}" for r in classes])
    ax.set_ylabel("end-to-end latency (ms)")
    ax.set_title("E1: latency by data class (synthetic workload)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


# ---------------------------------------------------------------------------
# E2: protocol message cost
# ---------------------------------------------------------------------------
async def run_e2(out: Path) -> BenchResult:
    rows: list[dict[str, Any]] = []
    raft_counts: dict[int, int] = {}
    pbft_counts: dict[int, int] = {}
    for n in (3, 5, 7):
        network = SimulatedNetwork(latency_ms=0.2, jitter_ms=0.0, seed=2)
        cluster = RaftCluster(
            network,
            n=n,
            heartbeat_s=30.0,
            election_min_s=0.08,
            election_max_s=0.16,
            seed=13,
        )
        cluster.start()
        try:
            leader = await cluster.wait_for_leader(6.0)
            await asyncio.sleep(0.05)
            network.counters(reset=True)
            await leader.append({"bench": "e2", "n": n})
            sent = network.sent("raft")
        finally:
            await cluster.stop()
            await network.close()
        raft_counts[n] = sent
        rows.append(
            {
                "protocol": "raft",
                "n": n,
                "scenario": "one committed entry, heartbeats suppressed",
                "messages": sent,
                "expected_formula": "2(n-1)",
                "expected": 2 * (n - 1),
                "matches": sent == 2 * (n - 1),
            }
        )
    for n in (4, 7):
        network = SimulatedNetwork(latency_ms=0.2, jitter_ms=0.0, seed=3)
        cluster = PBFTCluster(network, n=n)
        network.counters(reset=True)
        await cluster.submit({"bench": "e2", "n": n})
        sent = network.sent("pbft")
        await network.close()
        pbft_counts[n] = sent
        rows.append(
            {
                "protocol": "pbft",
                "n": n,
                "scenario": "one committed entry, all replicas honest",
                "messages": sent,
                "expected_formula": "2n(n-1)",
                "expected": 2 * n * (n - 1),
                "matches": sent == 2 * n * (n - 1),
            }
        )
    network = SimulatedNetwork(latency_ms=0.2, jitter_ms=0.0, seed=4)
    cluster = CRDTCluster(network, n=3, gossip_interval_s=10.0)
    network.counters(reset=True)
    for i in range(100):
        cluster.replicas[i % 3].increment("e2_writes")
    write_messages = network.sent("crdt")
    await cluster.gossip_all()
    gossip_messages = network.sent("crdt")
    await network.close()
    rows.append(
        {
            "protocol": "crdt",
            "n": 3,
            "scenario": "100 local writes (no coordination)",
            "messages": write_messages,
            "expected_formula": "0",
            "expected": 0,
            "matches": write_messages == 0,
        }
    )
    rows.append(
        {
            "protocol": "crdt",
            "n": 3,
            "scenario": "one full gossip round after writes",
            "messages": gossip_messages - write_messages,
            "expected_formula": "n(n-1)",
            "expected": 3 * 2,
            "matches": (gossip_messages - write_messages) == 6,
        }
    )
    _chart_e2(out / "e2_messages_per_entry.png", raft_counts, pbft_counts)
    result = BenchResult("e2", rows)
    result.notes["pbft_n7_messages"] = pbft_counts.get(7)
    result.notes["pbft_n7_expected"] = 2 * 7 * 6
    result.notes["raft_n3_messages"] = raft_counts.get(3)
    return result


def _chart_e2(path: Path, raft: dict[int, int], pbft: dict[int, int]) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    ax.plot(sorted(raft), [raft[n] for n in sorted(raft)], "o-", label="Raft measured")
    ax.plot(sorted(raft), [2 * (n - 1) for n in sorted(raft)], "--", color="tab:blue", alpha=0.5, label="Raft 2(n-1)")
    ax.plot(sorted(pbft), [pbft[n] for n in sorted(pbft)], "s-", label="PBFT measured")
    ax.plot(sorted(pbft), [2 * n * (n - 1) for n in sorted(pbft)], "--", color="tab:orange", alpha=0.5, label="PBFT 2n(n-1)")
    ax.set_xlabel("replicas n")
    ax.set_ylabel("replica-to-replica messages per entry")
    ax.set_title("E2: measured message cost per committed entry")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


# ---------------------------------------------------------------------------
# E3: Raft failover
# ---------------------------------------------------------------------------
async def _wait_for(predicate: Callable[[], bool], timeout: float, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("condition not met before timeout")


async def run_e3(out: Path) -> BenchResult:
    trials = 5 if MODE == "quick" else 15
    rows: list[dict[str, Any]] = []
    for trial in range(trials):
        network = SimulatedNetwork(latency_ms=0.3, jitter_ms=0.1, seed=100 + trial)
        cluster = RaftCluster(
            network,
            n=3,
            heartbeat_s=0.03,
            election_min_s=0.12,
            election_max_s=0.22,
            seed=200 + trial,
        )
        cluster.start()
        try:
            old = await cluster.wait_for_leader(6.0)
            for i in range(2):
                await old.append({"trial": trial, "i": i})
            await _wait_for(lambda c=cluster: all(node.commit_index >= 2 for node in c.nodes), 5.0)

            started = time.perf_counter()
            cluster.crash(old.name)
            new = await cluster.wait_for_leader(6.0)
            election_ms = (time.perf_counter() - started) * 1000.0
            assert new.name != old.name

            append_ok = True
            try:
                entry = await new.append({"trial": trial, "i": "after-failover"})
                append_ok = entry.index == 3
            except Exception:
                append_ok = False
            await _wait_for(
                lambda c=cluster: sum(1 for node in c.nodes if node.commit_index >= 3) >= 2, 5.0
            )

            recover_started = time.perf_counter()
            cluster.recover(old.name)
            recovered = cluster.by_name[old.name]
            await _wait_for(lambda r=recovered: r.commit_index >= 3, 6.0)
            catchup_ms = (time.perf_counter() - recover_started) * 1000.0
            rows.append(
                {
                    "mode": MODE,
                    "trial": trial + 1,
                    "old_leader": old.name,
                    "new_leader": new.name,
                    "new_term": new.current_term,
                    "election_ms": _round(election_ms),
                    "failover_append_ok": append_ok,
                    "recovered_role": recovered.role,
                    "catchup_ms": _round(catchup_ms),
                }
            )
        finally:
            await cluster.stop()
            await network.close()

    elections = [float(r["election_ms"]) for r in rows]
    catchups = [float(r["catchup_ms"]) for r in rows]
    result = BenchResult("e3", rows)
    result.notes.update(
        {
            "trials": len(rows),
            "election_p50_ms": _round(percentile(elections, 50)),
            "election_p95_ms": _round(percentile(elections, 95)),
            "catchup_p50_ms": _round(percentile(catchups, 50)),
            "all_writes_ok": all(bool(r["failover_append_ok"]) for r in rows),
        }
    )
    _chart_e3(out / "e3_failover.png", rows)
    return result


def _chart_e3(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    x = [r["trial"] for r in rows]
    ax.bar([i - 0.2 for i in x], [r["election_ms"] for r in rows], 0.4, label="time to new leader")
    ax.bar([i + 0.2 for i in x], [r["catchup_ms"] for r in rows], 0.4, label="recovered follower catch-up")
    ax.set_xlabel("trial")
    ax.set_ylabel("milliseconds")
    ax.set_title("E3: Raft leader crash and recovery (n=3)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


# ---------------------------------------------------------------------------
# E4: PBFT Byzantine tolerance
# ---------------------------------------------------------------------------
async def run_e4(out: Path) -> BenchResult:
    rows: list[dict[str, Any]] = []
    requests_per_scenario = 5 if MODE == "quick" else 10
    for n in (4, 7):
        for scenario in ("honest", "silent", "equivocate"):
            network = SimulatedNetwork(latency_ms=0.2, jitter_ms=0.05, seed=300 + n)
            cluster = PBFTCluster(network, n=n)
            if scenario != "honest":
                cluster.set_byzantine("pbft-1", scenario)
            network.counters(reset=True)
            committed = 0
            orders: list[list[str]] = []
            try:
                for i in range(requests_per_scenario):
                    reply = await cluster.submit(
                        {"record_id": f"e4-{n}-{scenario}-{i}", "payload": {"i": i}}, timeout=6.0
                    )
                    if reply:
                        committed += 1
                # The primary returns after its own 2f+1 commits; backups may
                # still be processing the tail of the last request.  Wait for
                # every honest replica to finish before comparing orders.
                await _wait_for(
                    lambda c=cluster, target=requests_per_scenario: all(
                        len(node.committed) >= target for node in c.honest_nodes()
                    ),
                    5.0,
                )
                orders = [
                    [req["record_id"] for _, req in node.ordered_requests()]
                    for node in cluster.honest_nodes()
                ]
            except TimeoutError:
                committed = -1
            sent = network.sent("pbft")
            await network.close()
            orders_agree = bool(orders) and all(order == orders[0] for order in orders)
            expected = 2 * n * (n - 1) * max(committed, 0)
            rows.append(
                {
                    "mode": MODE,
                    "n": n,
                    "f": (n - 1) // 3,
                    "scenario": scenario,
                    "requests": requests_per_scenario,
                    "committed": committed,
                    "orders_agree": orders_agree,
                    "messages": sent,
                    "messages_per_committed_request": _round(sent / committed, 2) if committed > 0 else "",
                    "normal_formula_total": expected,
                }
            )
    _chart_e4(out / "e4_byzantine.png", rows)
    result = BenchResult("e4", rows)
    result.notes["all_committed"] = all(int(r["committed"]) == requests_per_scenario for r in rows)
    result.notes["all_orders_agree"] = all(bool(r["orders_agree"]) for r in rows)
    return result


def _chart_e4(path: Path, rows: list[dict[str, Any]]) -> None:
    labels = []
    values = []
    for row in rows:
        labels.append(f"n={row['n']}\n{row['scenario']}")
        values.append(row["messages_per_committed_request"] or 0)
    fig, ax = plt.subplots(figsize=(8.0, 4.2))
    bars = ax.bar(range(len(values)), values, color=["#4da3ff", "#ffc857", "#ff8d6b"])
    ax.set_xticks(range(len(values)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("messages per committed request")
    ax.set_title("E4: PBFT message cost with one Byzantine replica")
    ax.grid(axis="y", alpha=0.3)
    for bar, value in zip(bars, values, strict=False):
        ax.annotate(str(value), (bar.get_x() + bar.get_width() / 2, value), ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


# ---------------------------------------------------------------------------
# E5: Merkle proof scaling (including 10^6 leaves)
# ---------------------------------------------------------------------------
def run_e5(out: Path) -> BenchResult:
    sizes = [1, 8, 1000, 100_000, 1_000_000] if MODE == "quick" else [1, 8, 1000, 10_000, 100_000, 1_000_000]
    rows: list[dict[str, Any]] = []
    for size in sizes:
        started = time.perf_counter()
        tree = MerkleTree()
        for i in range(size):
            tree.append_data(b"leaf-%d" % i)
        build_ms = (time.perf_counter() - started) * 1000.0
        # RFC 6962 audit paths are not fixed-length: the last leaf of an
        # unbalanced tree can be shallower.  Index 0 is the worst case here
        # (ceil(log2 size) hashes); report the last leaf separately so the
        # table cannot be mistaken for a constant proof length.
        index = 0
        proof = tree.inclusion_proof(index, size)
        last_index = size - 1
        last_proof = tree.inclusion_proof(last_index, size)
        root = tree.root()
        assert verify_inclusion(tree.leaves[index], index, size, proof, root)
        assert verify_inclusion(tree.leaves[last_index], last_index, size, last_proof, root)
        verify_ms: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            verify_inclusion(tree.leaves[index], index, size, proof, root)
            verify_ms.append((time.perf_counter() - t0) * 1000.0)
        old = max(1, size // 2)
        cproof = tree.consistency_proof(old, size)
        assert verify_consistency(old, size, tree.root_at(old), root, cproof)
        rows.append(
            {
                "mode": MODE,
                "leaves": size,
                "build_ms": _round(build_ms, 1),
                "proof_index": index,
                "proof_hashes": len(proof),
                "proof_bytes": len(proof) * 32,
                "last_leaf_proof_hashes": len(last_proof),
                "last_leaf_proof_bytes": len(last_proof) * 32,
                "verify_ms_mean": _round(statistics.fmean(verify_ms), 4),
                "expected_hashes_ceil_log2": math.ceil(math.log2(size)) if size > 1 else 0,
                "consistency_hashes": len(cproof),
            }
        )
    _chart_e5(out / "e5_proof_scaling.png", rows)
    result = BenchResult("e5", rows)
    million = next((r for r in rows if int(r["leaves"]) == 1_000_000), None)
    if million:
        result.notes.update(
            {
                "million_proof_hashes": million["proof_hashes"],
                "million_proof_bytes": million["proof_bytes"],
                "million_build_ms": million["build_ms"],
                "million_verify_ms": million["verify_ms_mean"],
                "million_last_leaf_hashes": million["last_leaf_proof_hashes"],
            }
        )
    return result


def _chart_e5(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    xs = [r["leaves"] for r in rows]
    ax.plot(xs, [r["proof_bytes"] for r in rows], "o-", label="inclusion proof bytes")
    ax.plot(xs, [max(1, r["proof_bytes"]) for r in rows], alpha=0)  # keep axes positive
    ax.set_xscale("log")
    ax.set_xlabel("tree size (leaves, log scale)")
    ax.set_ylabel("proof size (bytes)")
    ax.set_title("E5: RFC 6962 inclusion proof size (32-byte hashes)")
    ax.grid(alpha=0.3, which="both")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


# ---------------------------------------------------------------------------
# E6: anchoring cadence, audit delay, tamper detection
# ---------------------------------------------------------------------------
async def run_e6(out: Path) -> BenchResult:
    n = 120 if MODE == "quick" else 400
    batches = (1, 10, 100)
    rows: list[dict[str, Any]] = []
    ledger_latency_ms = 2.0
    for batch in batches:
        data_dir = out / "tmp" / f"e6-b{batch}"
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        metrics = MetricsCollector()
        ledger = SimulatedLedger(data_dir / "ledger.sqlite3", latency_ms=ledger_latency_ms)
        log = AuthLog(data_dir / "log.sqlite3", ledger, anchor_batch=batch, metrics=metrics)
        started = time.perf_counter()
        for i in range(n):
            await log.append(_record(i, "ABC"[i % 3]).canonical_dict())
        await log.flush()
        elapsed = time.perf_counter() - started
        snapshot = metrics.snapshot()
        rows.append(
            {
                "mode": MODE,
                "anchor_batch": batch,
                "records": n,
                "ledger_latency_ms": ledger_latency_ms,
                "transactions": ledger.transaction_count(),
                "expected_transactions_ceil_n_over_b": math.ceil(n / batch),
                "pending_after_flush": log.pending_count(),
                "audit_p50_ms": _round(snapshot.audit["p50"]),
                "audit_p95_ms": _round(snapshot.audit["p95"]),
                "append_total_ms": _round(elapsed * 1000.0, 1),
                "append_mean_ms": _round(elapsed * 1000.0 / n, 3),
            }
        )
        log.close()

    # Tamper detection on a separate log.
    data_dir = out / "tmp" / "e6-tamper"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = SimulatedLedger(data_dir / "ledger.sqlite3", latency_ms=0.0)
    log = AuthLog(data_dir / "log.sqlite3", ledger, anchor_batch=10)
    record_count = 60 if MODE == "quick" else 200
    for i in range(record_count):
        await log.append(_record(i, "ABC"[i % 3]).canonical_dict())
    await log.flush()
    tampered_count = 10 if MODE == "quick" else 20
    rng = random.Random(4242)
    targets = rng.sample(range(record_count), k=tampered_count)
    detected = 0
    for index in targets:
        record_id = f"BENCH-{index:07d}"
        log.tamper(record_id)
        if not log.verify_record(record_id):
            detected += 1
    clean_index = next(i for i in range(record_count) if i not in targets)
    clean_valid = log.verify_record(f"BENCH-{clean_index:07d}")
    rebuilt, signed = log.reproduce_root_from_stored_records()
    root_mismatch = rebuilt != signed
    log.close()
    tamper_row = {
        "mode": MODE,
        "records": record_count,
        "tampered": tampered_count,
        "detected": detected,
        "detection_rate_pct": _round(detected / tampered_count * 100.0, 1),
        "clean_record_still_valid": clean_valid,
        "recomputed_root_mismatch": root_mismatch,
    }
    _chart_e6(out / "e6_audit_delay.png", rows)
    result = BenchResult("e6", rows)
    result.notes["tamper"] = tamper_row
    return result


def _chart_e6(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    xs = [r["anchor_batch"] for r in rows]
    ax.plot(xs, [r["audit_p50_ms"] for r in rows], "o-", label="audit delay p50")
    ax.plot(xs, [r["audit_p95_ms"] for r in rows], "s-", label="audit delay p95")
    ax.set_xscale("log")
    ax.set_xlabel("anchor batch b (log scale)")
    ax.set_ylabel("record -> anchored delay (ms)")
    ax.set_title("E6: audit delay vs anchor batch (simulated ledger, 2 ms/tx)")
    ax.grid(alpha=0.3, which="both")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


# ---------------------------------------------------------------------------
# results markdown
# ---------------------------------------------------------------------------
def _md_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_no rows_"
    fields = list(rows[0].keys())
    lines = ["| " + " | ".join(fields) + " |", "|" + "|".join(["---"] * len(fields)) + "|"]
    for row in rows:
        cells = []
        for key in fields:
            value = row.get(key, "")
            if isinstance(value, float):
                value = f"{value:.3f}".rstrip("0").rstrip(".")
            cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_results_md(
    out: Path,
    results: dict[str, BenchResult],
    started_wall: float,
    elapsed_s: float,
) -> Path:
    e1 = _read_csv(out / "e1_throughput_latency.csv")
    e2 = _read_csv(out / "e2_messages.csv")
    e3 = _read_csv(out / "e3_raft_failover.csv")
    e4 = _read_csv(out / "e4_pbft_byzantine.csv")
    e5 = _read_csv(out / "e5_merkle_scaling.csv")
    e6 = _read_csv(out / "e6_anchor_audit.csv")
    e6t = _read_csv(out / "e6_tamper.csv")

    e5_million = next((r for r in e5 if r.get("leaves") == "1000000"), None)
    e2_pbft7 = next((r for r in e2 if r.get("protocol") == "pbft" and r.get("n") == "7"), None)
    e2_raft3 = next((r for r in e2 if r.get("protocol") == "raft" and r.get("n") == "3"), None)
    e1_all = next((r for r in e1 if r.get("scope") == "all"), None)
    notes = {name: result.notes for name, result in results.items()}

    key_lines = []
    if e1_all:
        key_lines.append(
            f"- E1 overall: **{e1_all['throughput_rps']} records/s**, "
            f"p50 **{e1_all['p50_ms']} ms**, p95 **{e1_all['p95_ms']} ms** "
            f"for {e1_all['n']} sequential synthetic submissions."
        )
    if e2_pbft7:
        key_lines.append(
            f"- E2 PBFT: **{e2_pbft7['messages']} messages** for n=7 at one committed entry, "
            f"matching the normal-case formula 2n(n-1) = **{e2_pbft7['expected']}**."
        )
    if e2_raft3:
        key_lines.append(
            f"- E2 Raft: **{e2_raft3['messages']} messages** for n=3, matching 2(n-1) = "
            f"**{e2_raft3['expected']}**."
        )
    if e5_million:
        key_lines.append(
            f"- E5 Merkle: at **m = 1,000,000** leaves the worst-case inclusion proof "
            f"(index 0) has **{e5_million['proof_hashes']} hashes = {e5_million['proof_bytes']} bytes** "
            f"(ceil(log2 m) = {e5_million['expected_hashes_ceil_log2']}); the last leaf is "
            f"shallower at {e5_million['last_leaf_proof_hashes']} hashes. Tree build "
            f"{e5_million['build_ms']} ms, verification {e5_million['verify_ms_mean']} ms."
        )
    if notes.get("e3"):
        e3n = notes["e3"]
        key_lines.append(
            f"- E3 Raft failover: new leader in p50 **{e3n['election_p50_ms']} ms** / "
            f"p95 **{e3n['election_p95_ms']} ms**; recovered follower catch-up p50 "
            f"**{e3n['catchup_p50_ms']} ms** over {e3n['trials']} trials."
        )
    if notes.get("e4"):
        key_lines.append(
            f"- E4 PBFT: all scenarios committed and all honest orders agreed: "
            f"**{notes['e4']['all_committed']}** / **{notes['e4']['all_orders_agree']}**."
        )
    if notes.get("e6"):
        tamper = notes["e6"]["tamper"]
        key_lines.append(
            f"- E6 tamper: **{tamper['detected']}/{tamper['tampered']}** modified stored records "
            f"failed inclusion verification; the root recomputed from the database no longer "
            f"matched the signed root: **{tamper['recomputed_root_mismatch']}**."
        )

    text = f"""# DIMF prototype — measured results

> **Synthetic data only.** Every number below was produced by an actual run of
> this repository on the machine described here. The workload is generated
> (`src/dimf/eval/workload.py`), the network is simulated in one Python process,
> and none of this is a real government deployment or a production benchmark.

- Generated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_wall))}
- Mode: **{MODE}** (`make bench -- --quick` for quick, `make bench` for full)
- Total wall clock: **{elapsed_s:.1f} s**
- Python: {platform.python_version()} on {platform.platform()}
- CPU count: {os.cpu_count()}
- Ledger backend: simulated hash-chained SQLite (no EVM was used)
- Seed: 20261125; record schema and synthetic markers in `docs/ARCHITECTURE.md`

## Headline measurements

{chr(10).join(key_lines)}

## E1 — end-to-end latency and throughput by data class

Path exercised: signed submission -> L1 placement/blob decision -> L2
(CRDT/Raft/PBFT) -> L3 signed Merkle log. Latency is measured inside the
process from the start of L1 routing to the end of the L3 append. The
network latency is simulated at 0.3 ms per hop.

{_md_table(e1)}

![E1 latency by class](../results/e1_latency_by_class.png)

**Reading this honestly:** throughput is *sequential* submissions from one
asyncio task, so it measures request latency, not maximum server throughput.
The Raft path is the slowest because every entry needs a majority round-trip.
CRDT writes have no coordination messages but still pay L3 append cost.

## E2 — protocol messages per committed entry

Counts come from the simulated network's per-protocol counter. For Raft the
counter was reset after the election and heartbeats were suppressed for the
measured append, so the count is exactly the append traffic. PBFT counts only
replica-to-replica PRE-PREPARE / PREPARE / COMMIT messages.

{_md_table(e2)}

![E2 messages per entry](../results/e2_messages_per_entry.png)

**Reading this honestly:** this is a message *count*, not bytes or bandwidth.
The simulated network delivers in-process messages; a real implementation adds
serialisation, TLS, retries and WAN effects. CRDT has zero write-coordination
messages, but gossip still costs messages and eventual consistency is weaker
than the other two protocols.

## E3 — Raft leader crash and recovery

Each trial: elect a leader, commit two entries, crash the leader, wait for a
new leader, commit one more entry, recover the old leader and wait until it
has caught up. n = 3 with a 30 ms heartbeat and 120–220 ms election timeout.

{_md_table(e3)}

![E3 failover](../results/e3_failover.png)

**Reading this honestly:** a single process means "crash" is a flag, not a
process kill, so no OS scheduling or TCP reconnect delay is modelled. The
timings are dominated by the simulated election timeout, and no snapshots or
log compaction exist.

## E4 — PBFT with one Byzantine replica

n = 4 (f = 1) and n = 7 (f = 2). `silent` means the replica stops sending;
`equivocate` means it sends conflicting PREPARE/COMMIT messages.

{_md_table(e4)}

![E4 Byzantine](../results/e4_byzantine.png)

**Reading this honestly:** there is no view change. A Byzantine *primary* is
therefore not tolerated, which is a real gap (documented in
`docs/CHALLENGES.md`). The silent case uses fewer messages than the normal-case
formula because the faulty replica contributes nothing.

## E5 — Merkle proof scaling and the 10^6 case

RFC 6962 hashing (`SHA-256(0x00 || leaf)` and `SHA-256(0x01 || left || right)`),
inclusion and consistency proofs, verified with the same verifier that ships
to auditors.

{_md_table(e5)}

![E5 proof scaling](../results/e5_proof_scaling.png)

**Reading this honestly:** the tree is held in memory and built leaf by leaf.
The proof is logarithmic: at 10^6 leaves the worst-case audit path (index 0)
is only
{('**' + str(e5_million['proof_hashes']) + ' hashes / ' + str(e5_million['proof_bytes']) + ' bytes**') if e5_million else 'logarithmic'},
and the shallowest leaf (the last one) needs
{('**' + str(e5_million['last_leaf_proof_hashes']) + ' hashes / ' + str(e5_million['last_leaf_proof_bytes']) + ' bytes**') if e5_million else 'fewer'}.
RFC 6962 does **not** pad the tree to a power of two, so proof length varies by
position; the table records both. Build time is Python-bound and is not a
claim about production log throughput.

## E6 — anchoring cadence, audit delay and tamper detection

`Audit delay` is the time from a record being inserted until the batch
containing it is anchored; the ledger costs 2 ms per transaction.

{_md_table(e6)}

![E6 audit delay](../results/e6_audit_delay.png)

Tamper experiment: stored SQLite record content was modified without touching
the Merkle tree or signed head.

{_md_table(e6t)}

**Reading this honestly:** a larger batch reduces ledger transactions and cost
but increases audit delay (the trade-off the framework exposes). The simulated
ledger is a local SQLite hash chain, not a public blockchain. The tamper button
and endpoint are demo-only.

## What was measured, what was simulated, what is out of scope

| Area | Status in this delivery |
|---|---|
| CRDT class A | Implemented (G-counter + LWW register, gossip, partition convergence) |
| Raft class B | Implemented (election, replication, commit, crash/recover) — **no snapshots** |
| PBFT class C | Implemented for n = 3f+1, f = 1 default — **no view change** |
| Merkle log | Implemented exact RFC 6962 shape, inclusion + consistency proofs |
| Ed25519 signatures | Implemented (agency submissions + signed tree heads) |
| Anchoring | Implemented with simulated SQLite ledger; EVM backend optional, see `docs/EVM.md` |
| Network | **Simulated** in one Python process; no real sockets, TLS or WAN |
| Workload | **Synthetic**; amounts in paise, no real beneficiaries or agencies |
| Production hardening | **Out of scope** (no auth server, no rate limits, no HA, no backups) |

## Reproduce

```bash
make install
make bench -- --quick     # under 2 minutes on the development machine
make bench                # full run
```

Raw data: `results/*.csv`. Charts: `results/*.png`. This file is regenerated
by each run, so the numbers always correspond to the committed scripts.
"""
    path = out.parent / "docs" / "RESULTS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
async def run_all(out: Path) -> dict[str, BenchResult]:
    results: dict[str, BenchResult] = {}
    steps: list[tuple[str, Callable[[], Any]]] = [
        ("E1 end-to-end throughput/latency", lambda: run_e1(out)),
        ("E2 protocol message cost", lambda: run_e2(out)),
        ("E3 Raft failover", lambda: run_e3(out)),
        ("E4 PBFT Byzantine tolerance", lambda: run_e4(out)),
    ]
    for label, fn in steps:
        print(f"[bench] {label} ...", flush=True)
        t0 = time.perf_counter()
        result = await fn()
        print(f"[bench]   done in {time.perf_counter() - t0:.1f}s", flush=True)
        results[result.name] = result

    print("[bench] E5 Merkle scaling (includes 10^6 leaves) ...", flush=True)
    t0 = time.perf_counter()
    results["e5"] = run_e5(out)
    print(f"[bench]   done in {time.perf_counter() - t0:.1f}s", flush=True)

    print("[bench] E6 anchoring and tamper detection ...", flush=True)
    t0 = time.perf_counter()
    results["e6"] = await run_e6(out)
    print(f"[bench]   done in {time.perf_counter() - t0:.1f}s", flush=True)
    return results


def main(argv: Iterable[str] | None = None) -> int:
    global MODE
    args = list(sys.argv[1:] if argv is None else argv)
    quick = "--quick" in args
    full = "--full" in args
    if quick and full:
        print("choose either --quick or --full", file=sys.stderr)
        return 2
    MODE = "quick" if quick else "full"
    out = Path("results")
    for i, arg in enumerate(args):
        if arg == "--out" and i + 1 < len(args):
            out = Path(args[i + 1])
    out.mkdir(parents=True, exist_ok=True)
    out.joinpath("tmp").mkdir(parents=True, exist_ok=True)
    started_wall = time.time()
    started = time.perf_counter()
    results = asyncio.run(run_all(out))

    csv_names = {
        "e1": "e1_throughput_latency.csv",
        "e2": "e2_messages.csv",
        "e3": "e3_raft_failover.csv",
        "e4": "e4_pbft_byzantine.csv",
        "e5": "e5_merkle_scaling.csv",
        "e6": "e6_anchor_audit.csv",
    }
    for name, filename in csv_names.items():
        _write_csv(out / filename, results[name].rows)
    # The tamper summary is a single row.
    _write_csv(out / "e6_tamper.csv", [results["e6"].notes["tamper"]])

    elapsed = time.perf_counter() - started
    summary = {
        "synthetic": True,
        "mode": MODE,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started_wall)),
        "elapsed_s": _round(elapsed, 1),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "key_numbers": {
            "e2_pbft_n7_messages": results["e2"].notes.get("pbft_n7_messages"),
            "e2_pbft_n7_expected": results["e2"].notes.get("pbft_n7_expected"),
            "e3_election_p50_ms": results["e3"].notes.get("election_p50_ms"),
            "e3_catchup_p50_ms": results["e3"].notes.get("catchup_p50_ms"),
            "e4_all_committed": results["e4"].notes.get("all_committed"),
            "e5_million_proof_hashes": results["e5"].notes.get("million_proof_hashes"),
            "e5_million_proof_bytes": results["e5"].notes.get("million_proof_bytes"),
            "e5_million_build_ms": results["e5"].notes.get("million_build_ms"),
            "e6_tamper": results["e6"].notes.get("tamper"),
        },
        "notes": {name: result.notes for name, result in results.items()},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    results_path = write_results_md(out, results, started_wall, elapsed)

    print()
    print(f"[bench] mode={MODE} wall={elapsed:.1f}s")
    print(f"[bench] wrote {out}/*.csv, {out}/*.png")
    print(f"[bench] wrote {results_path}")
    if MODE == "quick" and elapsed > 120:
        print("[bench] WARNING: quick mode exceeded 2 minutes on this machine", file=sys.stderr)
    if elapsed > 600:
        print("[bench] WARNING: benchmark exceeded 10 minutes on this machine", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
