"""Ledger abstraction for L3 anchoring.

Two interchangeable backends:

``SimulatedLedger`` (default)
    An append-only, hash-chained SQLite table.  It counts transactions and
    can sleep for a configurable per-transaction latency to model a slow
    audited ledger.  No external software is required.

``EvmLedger`` (optional, ``DIMF_LEDGER_BACKEND=evm``)
    Calls the Solidity contract in ``contracts/RootAnchor.sol`` through
    web3.py against Anvil/Hardhat.  See docs/EVM.md.  It is not exercised by
    the default test suite; the docs state clearly whether it was verified
    in the delivered environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LedgerReceipt:
    sequence: int
    root: str
    timestamp_ms: int
    tx_id: str
    latency_ms: float
    backend: str


class Ledger(ABC):
    """Minimal interface required by L3 anchoring."""

    backend_name: str = "abstract"

    @abstractmethod
    async def append_root(self, sequence: int, root_hex: str, timestamp_ms: int) -> LedgerReceipt:
        """Anchor one root; returns a receipt after the write is durable."""

    @abstractmethod
    def transaction_count(self) -> int:
        """Number of ledger transactions performed so far."""

    @abstractmethod
    def entries(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent anchored entries, most recent first."""


class SimulatedLedger(Ledger):
    """Default zero-dependency ledger: append-only hash chain in SQLite."""

    backend_name = "simulated"

    def __init__(self, db_path: str | Path, latency_ms: float = 0.0) -> None:
        self.db_path = Path(db_path)
        self.latency_ms = max(0.0, latency_ms)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger_chain (
                tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence INTEGER NOT NULL,
                root TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                prev_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    async def append_root(self, sequence: int, root_hex: str, timestamp_ms: int) -> LedgerReceipt:
        if len(root_hex) != 64:
            raise ValueError("root must be a 32-byte hex string")
        started = time.perf_counter()
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000.0)
        async with self._lock:
            row = self._conn.execute("SELECT entry_hash FROM ledger_chain ORDER BY tx_id DESC LIMIT 1").fetchone()
            prev_hash = row["entry_hash"] if row else "0" * 64
            entry_hash = hashlib.sha256(
                f"{prev_hash}|{sequence}|{root_hex}|{timestamp_ms}".encode("utf-8")
            ).hexdigest()
            cursor = self._conn.execute(
                "INSERT INTO ledger_chain (sequence, root, timestamp_ms, prev_hash, entry_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sequence, root_hex, timestamp_ms, prev_hash, entry_hash, time.time()),
            )
            self._conn.commit()
            tx_id = str(cursor.lastrowid)
        elapsed = (time.perf_counter() - started) * 1000.0
        return LedgerReceipt(
            sequence=sequence,
            root=root_hex,
            timestamp_ms=timestamp_ms,
            tx_id=tx_id,
            latency_ms=elapsed,
            backend=self.backend_name,
        )

    def transaction_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM ledger_chain").fetchone()
        return int(row["n"])

    def entries(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_chain ORDER BY tx_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def last_entry_hash(self) -> str:
        row = self._conn.execute("SELECT entry_hash FROM ledger_chain ORDER BY tx_id DESC LIMIT 1").fetchone()
        return row["entry_hash"] if row else "0" * 64

    def close(self) -> None:
        self._conn.close()


class EvmLedger(Ledger):  # pragma: no cover - optional backend
    """Optional web3.py backend.  Requires ``pip install -r requirements-evm.txt``."""

    backend_name = "evm"

    def __init__(
        self,
        rpc_url: str,
        contract_address: str,
        private_key: str,
        abi: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            from web3 import Web3
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "EvmLedger requires web3.py. Install optional deps: "
                "pip install -r requirements-evm.txt"
            ) from exc
        if not contract_address:
            raise ValueError("DIMF_EVM_CONTRACT must be set for the EVM backend")
        if not private_key:
            raise ValueError("DIMF_EVM_PRIVATE_KEY must be set for the EVM backend")
        self._web3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self._web3.is_connected():
            raise RuntimeError(f"cannot connect to EVM node at {rpc_url}")
        self._account = self._web3.eth.account.from_key(private_key)
        self._contract = self._web3.eth.contract(
            address=Web3.to_checksum_address(contract_address),
            abi=abi or _ROOT_ANCHOR_ABI,
        )
        self._tx_count = int(self._contract.functions.count().call())

    async def append_root(self, sequence: int, root_hex: str, timestamp_ms: int) -> LedgerReceipt:
        started = time.perf_counter()
        tx = self._contract.functions.anchor(
            int(sequence), bytes.fromhex(root_hex), int(timestamp_ms)
        ).build_transaction(
            {
                "from": self._account.address,
                "nonce": self._web3.eth.get_transaction_count(self._account.address),
                "gas": 200_000,
                "gasPrice": self._web3.eth.gas_price,
            }
        )
        signed = self._web3.eth.account.sign_transaction(tx, private_key=self._account.key)
        tx_hash = self._web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._web3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt.status != 1:
            raise RuntimeError(f"anchor transaction failed: {tx_hash.hex()}")
        self._tx_count += 1
        elapsed = (time.perf_counter() - started) * 1000.0
        return LedgerReceipt(
            sequence=sequence,
            root=root_hex,
            timestamp_ms=timestamp_ms,
            tx_id=tx_hash.hex(),
            latency_ms=elapsed,
            backend=self.backend_name,
        )

    def transaction_count(self) -> int:
        return int(self._contract.functions.count().call())

    def entries(self, limit: int = 100) -> list[dict[str, Any]]:
        total = self.transaction_count()
        out: list[dict[str, Any]] = []
        for i in range(max(0, total - limit), total):
            seq, root, ts = self._contract.functions.get(i).call()
            out.append(
                {
                    "sequence": int(seq),
                    "root": bytes(root).hex(),
                    "timestamp_ms": int(ts),
                    "tx_id": f"evm-{i}",
                    "backend": "evm",
                }
            )
        return list(reversed(out))


# Solidity ABI kept in sync with contracts/RootAnchor.sol.
_ROOT_ANCHOR_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {"internalType": "uint64", "name": "sequence", "type": "uint64"},
            {"internalType": "bytes32", "name": "root", "type": "bytes32"},
            {"internalType": "uint64", "name": "timestamp", "type": "uint64"},
        ],
        "name": "anchor",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "count",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "i", "type": "uint256"}],
        "name": "get",
        "outputs": [
            {"internalType": "uint64", "name": "sequence", "type": "uint64"},
            {"internalType": "bytes32", "name": "root", "type": "bytes32"},
            {"internalType": "uint64", "name": "timestamp", "type": "uint64"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


def make_ledger(settings: Any) -> Ledger:
    """Construct the configured ledger backend."""
    backend = str(getattr(settings, "ledger_backend", "simulated")).lower()
    if backend == "evm":
        return EvmLedger(
            rpc_url=settings.evm_rpc_url,
            contract_address=settings.evm_contract,
            private_key=settings.evm_private_key,
        )
    return SimulatedLedger(settings.ledger_db_path, latency_ms=settings.ledger_latency_ms)
