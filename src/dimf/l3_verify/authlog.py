"""The signed, append-only authenticated log (L3).

The log stores records in SQLite, keeps an RFC 6962 Merkle tree over their
canonical JSON, signs every tree head with Ed25519, and anchors the current
signed root to the configured ledger every ``b`` records.  Only the root is
written to the ledger; the records themselves stay off-chain.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dimf.common.crypto import KeyPair, b64e, canonical_bytes, canonical_json, sha256_hex
from dimf.common.metrics import MetricsCollector
from dimf.l3_verify.anchor import Ledger
from dimf.l3_verify.merkle import EMPTY_ROOT, MerkleTree, verify_inclusion


class DuplicateRecord(Exception):
    pass


class RecordNotFound(Exception):
    pass


@dataclass(frozen=True)
class AppendResult:
    record_id: str
    index: int
    tree_size: int
    root: str
    anchored: bool


class AuthLog:
    """Append-only authenticated log with signed tree heads and anchoring."""

    def __init__(
        self,
        db_path: str | Path,
        ledger: Ledger,
        *,
        anchor_batch: int = 100,
        key_seed: str = "dimf-log-operator-seed",
        metrics: MetricsCollector | None = None,
    ) -> None:
        if anchor_batch < 1:
            raise ValueError("anchor_batch must be >= 1")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger
        self.anchor_batch = anchor_batch
        self.metrics = metrics
        self.keypair = KeyPair.from_seed(key_seed)
        self.public_key = self.keypair.public_key_hex()
        self._lock = asyncio.Lock()

        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                seq INTEGER PRIMARY KEY,
                record_id TEXT NOT NULL UNIQUE,
                canonical TEXT NOT NULL,
                leaf_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                anchored_at REAL,
                anchor_id INTEGER,
                anchor_latency_ms REAL
            );
            CREATE TABLE IF NOT EXISTS tree_heads (
                tree_size INTEGER PRIMARY KEY,
                root TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                signature TEXT NOT NULL,
                public_key TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS anchors (
                anchor_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tree_size INTEGER NOT NULL,
                root TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                signature TEXT NOT NULL,
                ledger_tx_id TEXT NOT NULL,
                ledger_backend TEXT NOT NULL,
                ledger_latency_ms REAL NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()
        self.tree = MerkleTree()
        rows = self._conn.execute("SELECT leaf_hash FROM records ORDER BY seq").fetchall()
        for row in rows:
            self.tree.append_hash(bytes.fromhex(row["leaf_hash"]))
        row = self._conn.execute("SELECT MAX(tree_size) AS n FROM anchors").fetchone()
        self._last_anchor_size = int(row["n"] or 0)
        if self.tree.size and self.tree.size not in [
            r["tree_size"] for r in self._conn.execute("SELECT tree_size FROM tree_heads").fetchall()
        ]:
            # Recover a missing head after an interrupted write (rare, but a
            # log must never silently continue with an unsigned head).
            self._sign_and_store_head_locked()
        if self.metrics:
            self.metrics.set_ledger(
                backend=self.ledger.backend_name,
                transaction_count=self.ledger.transaction_count(),
                anchor_batch=self.anchor_batch,
            )

    # -- signed tree head -------------------------------------------------
    def _sth_payload(self, tree_size: int, root_hex: str, timestamp_ms: int) -> dict[str, Any]:
        return {"root": root_hex, "timestamp_ms": timestamp_ms, "tree_size": tree_size}

    def _sign_head(self, tree_size: int, root_hex: str, timestamp_ms: int) -> dict[str, Any]:
        payload = self._sth_payload(tree_size, root_hex, timestamp_ms)
        signature = self.keypair.sign(canonical_bytes(payload))
        return {
            **payload,
            "signature": b64e(signature),
            "public_key": self.public_key,
            "log_id": self.keypair.key_id(),
        }

    def _sign_and_store_head_locked(self) -> dict[str, Any]:
        size = self.tree.size
        root_hex = self.tree.root().hex()
        ts = int(time.time() * 1000)
        head = self._sign_head(size, root_hex, ts)
        self._conn.execute(
            "INSERT OR REPLACE INTO tree_heads (tree_size, root, timestamp_ms, signature, public_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (size, root_hex, ts, head["signature"], self.public_key),
        )
        self._conn.commit()
        return head

    def head(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM tree_heads ORDER BY tree_size DESC LIMIT 1"
        ).fetchone()
        if row is None:
            size, root_hex = 0, EMPTY_ROOT.hex()
            head = self._sign_head(size, root_hex, int(time.time() * 1000))
            return head
        return {
            "tree_size": int(row["tree_size"]),
            "root": row["root"],
            "timestamp_ms": int(row["timestamp_ms"]),
            "signature": row["signature"],
            "public_key": row["public_key"],
            "log_id": self.keypair.key_id(),
        }

    def head_at(self, size: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM tree_heads WHERE tree_size = ?", (size,)).fetchone()
        if row is None:
            return None
        return {
            "tree_size": int(row["tree_size"]),
            "root": row["root"],
            "timestamp_ms": int(row["timestamp_ms"]),
            "signature": row["signature"],
            "public_key": row["public_key"],
            "log_id": self.keypair.key_id(),
        }

    # -- append -----------------------------------------------------------
    async def append(self, record: dict[str, Any]) -> AppendResult:
        record_id = str(record["record_id"])
        canonical = canonical_json(record)
        data = canonical.encode("utf-8")
        async with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if exists:
                raise DuplicateRecord(record_id)
            leaf = self.tree.append_data(data)
            size = self.tree.size
            index = size - 1
            root = self.tree.root()
            root_hex = root.hex()
            ts = int(time.time() * 1000)
            signature = b64e(self.keypair.sign(canonical_bytes(self._sth_payload(size, root_hex, ts))))
            with self._conn:
                self._conn.execute(
                    "INSERT INTO records (seq, record_id, canonical, leaf_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (index, record_id, canonical, leaf.hex(), time.time()),
                )
                self._conn.execute(
                    "INSERT INTO tree_heads (tree_size, root, timestamp_ms, signature, public_key) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (size, root_hex, ts, signature, self.public_key),
                )
            if self.metrics:
                self.metrics.set_ledger(
                    backend=self.ledger.backend_name,
                    transaction_count=self.ledger.transaction_count(),
                    anchor_batch=self.anchor_batch,
                    log_size=size,
                )
            anchored = False
            if size % self.anchor_batch == 0:
                await self._anchor_locked(size, root, ts, signature)
                anchored = True
            return AppendResult(
                record_id=record_id,
                index=index,
                tree_size=size,
                root=root_hex,
                anchored=anchored,
            )

    async def _anchor_locked(self, tree_size: int, root: bytes, ts: int, signature: str) -> dict[str, Any] | None:
        if tree_size <= self._last_anchor_size:
            return None
        receipt = await self.ledger.append_root(tree_size, root.hex(), ts)
        now = time.time()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO anchors (tree_size, root, timestamp_ms, signature, ledger_tx_id, "
                "ledger_backend, ledger_latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tree_size,
                    root.hex(),
                    ts,
                    signature,
                    receipt.tx_id,
                    receipt.backend,
                    receipt.latency_ms,
                    now,
                ),
            )
            anchor_id = int(cursor.lastrowid)
            pending = self._conn.execute(
                "SELECT seq, created_at FROM records WHERE seq >= ? AND seq < ? AND anchored_at IS NULL",
                (self._last_anchor_size, tree_size),
            ).fetchall()
            for row in pending:
                delay_ms = (now - float(row["created_at"])) * 1000.0
                self._conn.execute(
                    "UPDATE records SET anchored_at = ?, anchor_id = ?, anchor_latency_ms = ? WHERE seq = ?",
                    (now, anchor_id, receipt.latency_ms, int(row["seq"])),
                )
                if self.metrics:
                    self.metrics.record_audit_delay(delay_ms)
        self._last_anchor_size = tree_size
        if self.metrics:
            self.metrics.set_ledger(
                backend=self.ledger.backend_name,
                transaction_count=self.ledger.transaction_count(),
                anchors=self.anchor_count(),
                anchor_batch=self.anchor_batch,
                last_anchor_latency_ms=receipt.latency_ms,
                log_size=self.tree.size,
            )
        return {
            "anchor_id": anchor_id,
            "tree_size": tree_size,
            "root": root.hex(),
            "ledger_tx_id": receipt.tx_id,
            "latency_ms": receipt.latency_ms,
        }

    async def flush(self) -> dict[str, Any] | None:
        """Anchor any pending records (used at the end of a run)."""
        async with self._lock:
            if self.tree.size <= self._last_anchor_size or self.tree.size == 0:
                return None
            size = self.tree.size
            root = self.tree.root()
            ts = int(time.time() * 1000)
            signature = b64e(self.keypair.sign(canonical_bytes(self._sth_payload(size, root.hex(), ts))))
            return await self._anchor_locked(size, root, ts, signature)

    # -- proofs -----------------------------------------------------------
    def _record_row(self, record_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM records WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFound(record_id)
        return row

    def proof(self, record_id: str) -> dict[str, Any]:
        row = self._record_row(record_id)
        index = int(row["seq"])
        size = self.tree.size
        proof = self.tree.inclusion_proof(index, size)
        head = self.head()
        try:
            record_obj = json.loads(row["canonical"])
        except json.JSONDecodeError:
            record_obj = None
        return {
            "record_id": record_id,
            "record": record_obj,
            "canonical": row["canonical"],
            "index": index,
            "size": size,
            "proof": [node.hex() for node in proof],
            "root": head["root"],
            "tree_head": head,
            "stored_leaf_hash": row["leaf_hash"],
            "anchored": row["anchored_at"] is not None,
        }

    def verify_record(self, record_id: str) -> bool:
        """Server-side check using the same standalone verifier logic."""
        row = self._record_row(record_id)
        data = row["canonical"].encode("utf-8")
        leaf = sha256_hex(b"\x00" + data)
        head = self.head()
        proof = self.tree.inclusion_proof(int(row["seq"]), self.tree.size)
        return verify_inclusion(
            bytes.fromhex(leaf),
            int(row["seq"]),
            self.tree.size,
            proof,
            bytes.fromhex(head["root"]),
        )

    def consistency(self, old_size: int, new_size: int) -> dict[str, Any]:
        if not (0 <= old_size <= new_size <= self.tree.size):
            raise ValueError("require 0 <= old <= new <= current tree size")
        proof = self.tree.consistency_proof(old_size, new_size)
        old_head = self.head_at(old_size)
        new_head = self.head_at(new_size)
        old_root = old_head["root"] if old_head else (EMPTY_ROOT.hex() if old_size == 0 else self.tree.root_at(old_size).hex())
        new_root = new_head["root"] if new_head else self.tree.root_at(new_size).hex()
        return {
            "old_size": old_size,
            "new_size": new_size,
            "old_root": old_root,
            "new_root": new_root,
            "proof": [node.hex() for node in proof],
        }

    # -- inspection / demo ------------------------------------------------
    def anchors(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM anchors ORDER BY anchor_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def anchor_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM anchors").fetchone()
        return int(row["n"])

    def pending_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM records WHERE anchored_at IS NULL"
        ).fetchone()
        return int(row["n"])

    def stats(self) -> dict[str, Any]:
        head = self.head()
        return {
            "tree_size": self.tree.size,
            "root": head["root"],
            "head": head,
            "anchors": self.anchor_count(),
            "pending": self.pending_count(),
            "anchor_batch": self.anchor_batch,
            "ledger_transactions": self.ledger.transaction_count(),
            "ledger_backend": self.ledger.backend_name,
            "last_anchor_size": self._last_anchor_size,
        }

    def records(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM records ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                obj = json.loads(row["canonical"])
            except json.JSONDecodeError:
                obj = {"record_id": row["record_id"], "corrupt": True}
            out.append(
                {
                    "seq": int(row["seq"]),
                    "record_id": row["record_id"],
                    "agency": obj.get("agency"),
                    "data_class": obj.get("data_class"),
                    "timestamp": obj.get("timestamp"),
                    "payload": obj.get("payload"),
                    "document": obj.get("document"),
                    "anchored": row["anchored_at"] is not None,
                    "created_at": row["created_at"],
                    "anchored_at": row["anchored_at"],
                }
            )
        return out

    def get_canonical(self, record_id: str) -> str:
        return str(self._record_row(record_id)["canonical"])

    def tamper(self, record_id: str) -> dict[str, Any]:
        """DEMO ONLY: corrupt stored record content, leaving the tree intact."""
        row = self._record_row(record_id)
        original = str(row["canonical"])
        obj = json.loads(original)
        payload = dict(obj.get("payload") or {})
        payload["_tampered"] = True
        payload["note"] = "TAMPERED BY DEMO BUTTON"
        obj["payload"] = payload
        tampered = canonical_json(obj)
        with self._conn:
            self._conn.execute(
                "UPDATE records SET canonical = ? WHERE record_id = ?", (tampered, record_id)
            )
        return {
            "record_id": record_id,
            "original": original,
            "tampered": tampered,
            "stored_leaf_hash": row["leaf_hash"],
            "recomputed_leaf_hash": sha256_hex(b"\x00" + tampered.encode("utf-8")),
        }

    def reproduce_root_from_stored_records(self) -> tuple[str, str]:
        """Recompute a root from current DB content and compare to the head.

        Returns ``(recomputed_root, signed_root)``.  They differ after tamper.
        """
        tree = MerkleTree()
        rows = self._conn.execute("SELECT canonical FROM records ORDER BY seq").fetchall()
        for row in rows:
            tree.append_data(row["canonical"].encode("utf-8"))
        return tree.root().hex(), self.head()["root"]

    def close(self) -> None:
        self._conn.close()
        close = getattr(self.ledger, "close", None)
        if callable(close):
            close()
