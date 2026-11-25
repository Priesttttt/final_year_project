"""Central configuration for the DIMF prototype.

Every knob can be overridden with an environment variable using the
``DIMF_`` prefix, for example ``DIMF_ANCHOR_BATCH=10``.  Defaults are
chosen so that ``make demo`` works out of the box and tests are fast.

This is a single-process prototype.  See docs/DECISIONS.md for why.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None and raw != "" else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None and raw != "" else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw is not None and raw != "" else default


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    data_dir: Path = field(default_factory=lambda: Path(_env_str("DIMF_DATA_DIR", "data")))
    db_path: Path | None = None
    blob_dir: Path | None = None
    ledger_db_path: Path | None = None

    # L2 cluster sizes.  Class C must satisfy n = 3f + 1.
    crdt_nodes: int = field(default_factory=lambda: _env_int("DIMF_CRDT_NODES", 3))
    raft_nodes: int = field(default_factory=lambda: _env_int("DIMF_RAFT_NODES", 3))
    pbft_nodes: int = field(default_factory=lambda: _env_int("DIMF_PBFT_NODES", 4))

    # Simulated network.
    network_latency_ms: float = field(default_factory=lambda: _env_float("DIMF_NET_LATENCY_MS", 1.0))
    network_jitter_ms: float = field(default_factory=lambda: _env_float("DIMF_NET_JITTER_MS", 0.5))
    network_drop_rate: float = field(default_factory=lambda: _env_float("DIMF_NET_DROP_RATE", 0.0))
    seed: int = field(default_factory=lambda: _env_int("DIMF_SEED", 20261125))

    # Raft timing (seconds).
    raft_heartbeat_s: float = field(default_factory=lambda: _env_float("DIMF_RAFT_HEARTBEAT_S", 0.10))
    raft_election_min_s: float = field(default_factory=lambda: _env_float("DIMF_RAFT_ELECTION_MIN_S", 0.30))
    raft_election_max_s: float = field(default_factory=lambda: _env_float("DIMF_RAFT_ELECTION_MAX_S", 0.60))

    # CRDT gossip.
    crdt_gossip_s: float = field(default_factory=lambda: _env_float("DIMF_CRDT_GOSSIP_S", 0.25))

    # L3.
    anchor_batch: int = field(default_factory=lambda: _env_int("DIMF_ANCHOR_BATCH", 100))
    ledger_backend: str = field(default_factory=lambda: _env_str("DIMF_LEDGER_BACKEND", "simulated"))
    ledger_latency_ms: float = field(default_factory=lambda: _env_float("DIMF_LEDGER_LATENCY_MS", 0.0))
    evm_rpc_url: str = field(default_factory=lambda: _env_str("DIMF_EVM_RPC_URL", "http://127.0.0.1:8545"))
    evm_contract: str = field(default_factory=lambda: _env_str("DIMF_EVM_CONTRACT", ""))
    evm_private_key: str = field(default_factory=lambda: _env_str("DIMF_EVM_PRIVATE_KEY", ""))

    # Demo / safety switches.
    seed_on_start: bool = field(default_factory=lambda: _env_bool("DIMF_SEED_ON_START", False))
    allow_tamper_endpoint: bool = field(default_factory=lambda: _env_bool("DIMF_ALLOW_TAMPER", True))
    demo_seed_records: int = field(default_factory=lambda: _env_int("DIMF_DEMO_SEED_RECORDS", 24))

    # Policy: agency -> allowed data classes.  Comma-separated env override is
    # deliberately simple; custom policies can be passed to PolicyEngine.
    policy_spec: str = field(
        default_factory=lambda: _env_str(
            "DIMF_POLICY",
            "agency-1:A,B,C;agency-2:A,C;agency-3:A,C",
        )
    )
    log_key_seed: str = field(default_factory=lambda: _env_str("DIMF_LOG_KEY_SEED", "dimf-log-operator-seed"))
    identity_master_seed: str = field(
        default_factory=lambda: _env_str("DIMF_IDENTITY_MASTER_SEED", "dimf-synthetic-agencies")
    )

    def __post_init__(self) -> None:
        base = Path(self.data_dir)
        object.__setattr__(self, "data_dir", base)
        if self.db_path is None:
            object.__setattr__(self, "db_path", base / "dimf.sqlite3")
        if self.blob_dir is None:
            object.__setattr__(self, "blob_dir", base / "blobs")
        if self.ledger_db_path is None:
            object.__setattr__(self, "ledger_db_path", base / "ledger.sqlite3")
        if self.pbft_nodes < 4:
            raise ValueError("PBFT requires at least 4 nodes (n = 3f + 1)")
        if self.pbft_nodes % 3 != 1:
            raise ValueError("PBFT node count must be 3f + 1 (e.g. 4, 7, 10, 13)")

    def ensure_dirs(self) -> None:
        """Create the data directories (idempotent)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        assert self.blob_dir is not None
        self.blob_dir.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    """Build settings, applying explicit keyword overrides last.

    Derived paths (database, blobs, ledger) are recomputed from the final
    ``data_dir`` unless the caller explicitly supplies them.
    """
    base = Settings()
    values: dict[str, object] = {
        f: getattr(base, f)
        for f in Settings.__dataclass_fields__
        if f not in {"db_path", "blob_dir", "ledger_db_path"}
    }
    values["db_path"] = None
    values["blob_dir"] = None
    values["ledger_db_path"] = None
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]
