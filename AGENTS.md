# AGENTS.md — working rules for this repository

This file is for any future contributor, human or AI. Read it before changing
code, and keep it accurate.

## What this repository is

A runnable B.Tech viva prototype of the four-layer DIMF framework:

- L1: `BlobStore` + `PlacementService`
- L2: `SimulatedNetwork` + CRDT / Raft / PBFT + `SyncRouter`
- L3: RFC 6962 `MerkleTree` + `AuthLog` + ledger backends + `verifier`
- L4: `IdentityRegistry` + `PolicyEngine` + metrics + FastAPI dashboard

It is **synthetic-data, single-process, simulated-network** software. Never
describe it as a production system, a real government deployment, or a
benchmark of real infrastructure.

## Golden rules

1. **Never invent a measurement.** Every number in `docs/RESULTS.md` or the
   README must come from `python scripts/run_bench.py` (or a test assertion)
   executed on the machine that produced the file. If you cannot run it, say
   so.
2. **Keep the synthetic labelling.** Records, documents, agencies and demo
   responses must remain visibly synthetic (`SYNTH-*`, `synthetic: true`,
   warning notes). Do not add anything that looks like real beneficiary data.
3. **Do not weaken the crypto or Merkle logic to make a test pass.** If a
   proof test fails, fix the implementation or the test against RFC 6962/9162,
   not the verifier's acceptance condition.
4. **Do not hide limitations.** If a feature is simulated, missing or
   unverified, update `README.md`, `docs/RESULTS.md` and
   `docs/CHALLENGES.md` honestly.
5. **Tests must stay deterministic and fast.** Fixed seeds, short simulated
   timers, no external network, no sleeps longer than a few hundred ms unless
   a test specifically measures failover.

## Commands

```bash
make install          # .venv + pinned deps + editable install
make test             # pytest, target < 90 s (currently ~5 s)
make demo             # http://127.0.0.1:8000/ with synthetic seed data
make bench -- --quick # regenerate results/ and docs/RESULTS.md (< 2 min)
make bench            # full benchmark (< 10 min)
make lint             # ruff
make clean            # remove caches/temp artefacts; keeps CSVs and PNGs
```

Direct equivalents use `PYTHONPATH=src .venv/bin/python ...` if the editable
install is not active.

## Architecture map

```
api/app.py        FastAPI routes, demo endpoints, error mapping
api/state.py      composition root; owns every long-lived object
api/static/       dashboard, no CDN, browser Merkle verifier
l1_placement/     document hashing/off-chain storage, record preparation
l2_sync/          network simulation, CRDT, Raft, PBFT, class routing
l3_verify/        Merkle tree, authenticated log, anchoring, standalone verify
l4_identity/      agency registry, policy engine
common/           config, crypto, models, metrics, logging
eval/             workload generator, benchmarks E1–E6
```

Data flow for one submission:

`SignedSubmission` → identity verification → policy check → `BlobStore`
(optional document) → `Record` → `SyncRouter` (A/B/C) → `AuthLog.append`
(Merkle + signed head) → anchor every `b` records → response.

## Invariants to preserve

- Canonical JSON used for hashing and signatures is
  `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`
  over JSON-mode data. Python and the browser must produce identical bytes.
- RFC 6962 hashing is `SHA256(0x00 || leaf)` and
  `SHA256(0x01 || left || right)`. The tree is **not** padded to a power of
  two; proof length varies by leaf position.
- The Merkle tree is built from stored leaf hashes, so editing a record's
  `canonical` text in SQLite does not silently change the tree — it makes the
  record fail verification, which is the point of tamper detection.
- A tree head is signed and stored for **every** append, not only at anchors.
  This allows consistency proofs between arbitrary sizes.
- Anchoring happens exactly when `tree_size % anchor_batch == 0`, plus an
  explicit `flush()` at the end of a run. Number of ledger transactions is
  `ceil(n / b)`.
- PBFT requires `n = 3f + 1`; the API default is n=4 (f=1) and the UI toggles
  a **backup**, never the primary, because view change is not implemented.
- Raft `append()` raises `NotLeader(leader_id)` on followers; the router must
  retry at the current leader.
- The simulated network counts every send attempt (`sent`), delivery and drop.
  Message-count assertions depend on this; do not reset counters mid-test
  without a reason.

## Notes on tests

- `tests/test_merkle.py` includes the RFC 9162 seven-leaf vectors and
  randomized properties. Keep the naive `mth` reference in the tests.
- `tests/test_raft.py` uses suppressed heartbeats and a counter reset to assert
  exactly `2(n-1)` messages for one append. Election traffic is excluded on
  purpose.
- `tests/test_pbft.py` asserts `2n(n-1)` normal-case messages and checks that
  one silent/equivocating backup does not block commits. Two silenced backups
  at n=4 must time out; that is the f=1 boundary.
- `tests/test_api.py` is the end-to-end contract. It uses `TestClient` and a
  temporary data directory (`tests/conftest.py`), and exercises Raft failover
  and the tamper path.
- If you add a new API or state object, update `tests/test_api.py` and the
  endpoint table in `README.md`.

## Benchmarking rules

- Add new experiments to `src/dimf/eval/bench.py` and a matching CSV/PNG.
- Quick mode must stay under 2 minutes; full mode under 10.
- Keep the 10^6-leaf Merkle case in **both** modes, because the paper's proof
  bound is a headline claim.
- Record actual environment (Python/platform/CPU) in `docs/RESULTS.md`.
- Distinguish measured, simulated and out-of-scope in the generated text.
- If a number looks too good, check whether the measurement excluded setup
  cost, heartbeats, gossip or the ledger. State what was excluded.

## Style

- Python 3.11+ syntax, `from __future__ import annotations`, type hints on
  public functions.
- `ruff check src tests scripts` must pass (`line-length = 120`).
- No new runtime dependency without updating `requirements.txt`,
  `pyproject.toml` and the Dockerfile.
- The dashboard is deliberately dependency-free vanilla JS. Do not add a CDN,
  npm package or build step.
- Prefer small, readable modules over clever abstractions; this is viva code
  that a student must be able to explain line by line.

## Definition of done for a change

1. `make lint` passes.
2. `make test` passes and stays fast/deterministic.
3. Demo still works (`make demo`, open `/`, submit A/B/C, verify, tamper).
4. `make bench -- --quick` runs and regenerates `docs/RESULTS.md`.
5. README/docs updated if behaviour, endpoints, dependencies or limitations
   changed.
6. No real personal/government data and no fabricated measurements.
