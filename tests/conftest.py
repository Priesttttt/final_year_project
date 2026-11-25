from __future__ import annotations

from pathlib import Path

import pytest

from dimf.common.config import load_settings


@pytest.fixture
def fast_settings(tmp_path: Path):
    return load_settings(
        data_dir=tmp_path / "data",
        seed_on_start=False,
        network_latency_ms=0.2,
        network_jitter_ms=0.1,
        network_drop_rate=0.0,
        raft_heartbeat_s=0.03,
        raft_election_min_s=0.10,
        raft_election_max_s=0.20,
        crdt_gossip_s=0.05,
        anchor_batch=3,
        seed=1234,
    )
