from __future__ import annotations

import asyncio
import math
import sqlite3

import pytest

from dimf.common.metrics import MetricsCollector
from dimf.common.models import Record
from dimf.l3_verify.anchor import SimulatedLedger
from dimf.l3_verify.authlog import AuthLog, DuplicateRecord
from dimf.l3_verify.verifier import verify_record, verify_tree_head_signature


def rec(i: int, cls: str = "A") -> dict:
    return Record(
        record_id=f"r-{i}",
        agency="agency-1",
        data_class=cls,  # type: ignore[arg-type]
        timestamp=1000 + i,
        payload={"i": i, "amount_paise": i * 10},
    ).canonical_dict()


def test_ledger_is_hash_chained(tmp_path):
    async def scenario() -> None:
        ledger = SimulatedLedger(tmp_path / "l.sqlite3", latency_ms=0)
        await ledger.append_root(1, "a" * 64, 111)
        r2 = await ledger.append_root(2, "b" * 64, 222)
        assert ledger.transaction_count() == 2
        entries = ledger.entries()
        assert entries[0]["tx_id"] == int(r2.tx_id)
        assert entries[0]["prev_hash"] == _entry_hash(entries[1])
        assert entries[0]["entry_hash"] == _entry_hash(entries[0])
        ledger.close()

    asyncio.run(scenario())


def _entry_hash(row: dict) -> str:
    import hashlib

    return hashlib.sha256(
        f"{row['prev_hash']}|{row['sequence']}|{row['root']}|{row['timestamp_ms']}".encode()
    ).hexdigest()


@pytest.mark.parametrize("n,b", [(1, 1), (10, 3), (9, 3), (100, 100), (7, 1000)])
def test_anchor_cadence_is_ceil_n_over_b(tmp_path, n: int, b: int):
    async def scenario() -> None:
        metrics = MetricsCollector()
        ledger = SimulatedLedger(tmp_path / "l.sqlite3", latency_ms=0)
        log = AuthLog(tmp_path / "log.sqlite3", ledger, anchor_batch=b, metrics=metrics)
        for i in range(n):
            await log.append(rec(i))
        expected_mid = n // b
        assert ledger.transaction_count() == expected_mid
        await log.flush()
        assert ledger.transaction_count() == math.ceil(n / b)
        assert log.anchor_count() == math.ceil(n / b)
        assert log.pending_count() == 0
        snapshot = metrics.snapshot()
        assert int(snapshot.audit["count"]) == n
        log.close()

    asyncio.run(scenario())


def test_head_proof_consistency_and_restart(tmp_path):
    async def scenario() -> None:
        ledger = SimulatedLedger(tmp_path / "l.sqlite3", latency_ms=0)
        log = AuthLog(tmp_path / "log.sqlite3", ledger, anchor_batch=3)
        for i in range(7):
            await log.append(rec(i))
        head = log.head()
        assert head["tree_size"] == 7
        assert verify_tree_head_signature(head)

        proof = log.proof("r-3")
        assert proof["index"] == 3 and proof["size"] == 7
        assert len(proof["proof"]) == 3
        result = verify_record(proof["record"], 3, 7, proof["proof"], proof["root"])
        assert result["valid"]

        consistency = log.consistency(2, 7)
        from dimf.l3_verify.merkle import verify_consistency

        assert verify_consistency(
            2, 7, bytes.fromhex(consistency["old_root"]), bytes.fromhex(consistency["new_root"]),
            [bytes.fromhex(x) for x in consistency["proof"]],
        )

        with pytest.raises(DuplicateRecord):
            await log.append(rec(3))

        # Tampering with stored content does not touch the tree.
        tampered = log.tamper("r-3")
        assert tampered["recomputed_leaf_hash"] != tampered["stored_leaf_hash"]
        assert not log.verify_record("r-3")
        proof_after = log.proof("r-3")
        assert not verify_record(proof_after["record"], 3, 7, proof_after["proof"], proof_after["root"])["valid"]
        rebuilt, signed = log.reproduce_root_from_stored_records()
        assert rebuilt != signed
        log.close()

        # Re-open: the tree and signed heads survive; tampering is still visible.
        ledger2 = SimulatedLedger(tmp_path / "l.sqlite3", latency_ms=0)
        log2 = AuthLog(tmp_path / "log.sqlite3", ledger2, anchor_batch=3)
        assert log2.tree.size == 7
        assert log2.head()["root"] == head["root"]
        assert not log2.verify_record("r-3")
        log2.close()

    asyncio.run(scenario())


def test_audit_delay_uses_ledger_latency(tmp_path):
    async def scenario() -> None:
        metrics = MetricsCollector()
        ledger = SimulatedLedger(tmp_path / "l.sqlite3", latency_ms=20)
        log = AuthLog(tmp_path / "log.sqlite3", ledger, anchor_batch=1, metrics=metrics)
        for i in range(3):
            await log.append(rec(i))
        snapshot = metrics.snapshot()
        assert snapshot.audit["p50"] >= 15.0
        assert ledger.transaction_count() == 3
        log.close()

    asyncio.run(scenario())


def test_anchored_rows_persist(tmp_path):
    async def scenario() -> None:
        ledger = SimulatedLedger(tmp_path / "l.sqlite3")
        log = AuthLog(tmp_path / "log.sqlite3", ledger, anchor_batch=2)
        for i in range(4):
            await log.append(rec(i))
        anchors = log.anchors()
        assert len(anchors) == 2
        assert anchors[0]["tree_size"] == 4
        assert anchors[1]["tree_size"] == 2
        conn = sqlite3.connect(str(tmp_path / "log.sqlite3"))
        count = conn.execute("SELECT COUNT(*) FROM records WHERE anchored_at IS NOT NULL").fetchone()[0]
        assert count == 4
        conn.close()
        log.close()

    asyncio.run(scenario())
