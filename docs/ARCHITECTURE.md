# Architecture

This document describes what the prototype actually does, layer by layer. It
matches the code in `src/dimf/`. Where the paper describes a larger system, the
difference is called out explicitly.

## Goals and non-goals

**Goals**

- Make the four-layer argument concrete and runnable on one laptop.
- Match the RFCs exactly where correctness is claimed (RFC 6962 Merkle tree,
  RFC 9162 proof verification shapes, Ed25519 signatures).
- Expose the trade-offs: CRDT coordination vs Raft crash tolerance vs PBFT
  Byzantine tolerance; anchoring frequency vs audit delay.
- Produce repeatable measurements from a synthetic workload.

**Non-goals**

- Production security, availability, multi-tenancy or compliance.
- Real sockets, TLS, WAN behaviour or separate failure domains.
- Real government data, eligibility rules or payment integration.
- A complete PBFT (view change is absent) or a complete Raft (no persistent
  state or snapshots).

## Process model

One Python process runs one asyncio event loop. Every "node" is a Python
object. `SimulatedNetwork` delivers messages by scheduling `receive()`
coroutines with configurable latency and jitter, and it models drops,
partitions, crashes and Byzantine flags. This gives deterministic, fast tests
and an interactive demo without containers. The price is that "crash" is a
boolean; the OS, TCP stack and real disk are not exercised.

```
FastAPI request
   └── AppState (composition root)
         ├── IdentityRegistry (SQLite)
         ├── PolicyEngine (SQLite)
         ├── BlobStore (filesystem)
         ├── AuthLog (SQLite + Merkle tree + Ed25519 key)
         ├── Ledger (SimulatedLedger or EvmLedger)
         ├── SimulatedNetwork
         ├── CRDTCluster, RaftCluster, PBFTCluster
         ├── SyncRouter
         └── PlacementService
```

`AppState` is constructed once in the FastAPI lifespan and stopped on shutdown.
The data directory is configurable; SQLite files are created idempotently, and
the authenticated log reloads its tree from stored leaf hashes on restart.

## L1 — Placement

`PlacementService.prepare()` turns an authenticated `Submission` into the
canonical `Record` that L3 will hash. If a document is attached:

1. `BlobStore.put(bytes)` computes SHA-256 and writes the bytes atomically
   (temp file + `os.replace`) under `blobs/<first2>/<remaining62>`.
2. The record receives only `document: {sha256, size}`.
3. The raw bytes never enter the Merkle log, so the log stays small and the
   document can be fetched separately and re-hashed by an auditor.

`BlobStore.get()` re-hashes on read and raises if the content no longer matches
its address. The prototype does not delete or replicate blobs; that is out of
scope.

The class decision (`A`/`B`/`C`) is carried in the signed record and is not
re-derived from the payload. A production system would enforce a schema per
class; here the policy table is the only eligibility rule.

## L2 — Synchronisation

### Simulated network

`SimulatedNetwork` provides:

- `send()`/`dispatch()` with latency + jitter, drop probability, and per-protocol
  counters for `sent`, `delivered`, `dropped`;
- partitions as a list of disjoint groups (messages across groups are dropped);
- `crash(name)` / `recover(name)` (crashed nodes neither send nor receive);
- `set_byzantine(name, enabled)` for PBFT-mode faults.

Every send attempt is counted, including attempts that are later dropped. This
is intentional: message-count claims are about replica-to-replica work, not
successful deliveries.

### Class A — CRDT

`GCounter` (per-replica counts, merge = element-wise max) and `LWWRegister`
(timestamp, replica-id tie-break). `CRDTCluster` keeps three replicas and a
gossip loop. A local write needs **no coordination messages**; gossip exchange
happens later and converges because both merge functions are commutative,
associative and idempotent. A partition heals by gossip without a leader.

The log still receives class-A records in arrival order. That order is a local
observation, not a total order; an auditor should treat class A as
"eventually consistent state" rather than a ledger of conflict-free events.
`GET /cluster/status` exposes per-replica counters so convergence is visible.

### Class B — Raft

Implemented in `raft.py`: terms, randomized election timeouts, RequestVote,
AppendEntries, majority commit, heartbeat, `NotLeader` redirection, crash and
recover, and follower catch-up. The append path counts exactly `2(n-1)`
replica-to-replica messages when heartbeats are suppressed: one request and
one response per follower.

Deliberate omissions: no persistent term/vote/log on disk, no snapshots or log
compaction, no pre-vote, no membership changes. A process restart loses
consensus state (though the authenticated log is durable). These are recorded
in `docs/CHALLENGES.md`.

### Class C — PBFT

Implemented in `pbft.py` for `n = 3f + 1` (default n=4, f=1). The primary takes
the client request, sends PRE-PREPARE, replicas broadcast PREPARE, then COMMIT
after `2f` matching prepares, and commit after `2f+1` matching commits. The
normal-case replica-to-replica count is:

```
(n-1) PRE-PREPARE + (n-1)^2 PREPARE + n(n-1) COMMIT = 2n(n-1)
```

At n=4 this is 24; at n=7 it is 84. The primary acts as the client (the HTTP
layer is the client), and requests are serialized by the primary.

Fault modes for the demo: `silent` (a backup sends nothing) and `equivocate`
(it sends conflicting messages). Honest replicas still commit and agree on
order for one faulty backup. Two silenced backups at n=4 leave only two honest
replicas, below the `2f+1 = 3` commit threshold, and the request times out —
this is the expected f=1 boundary, and it is tested.

**No view change.** A Byzantine primary is not tolerated, and there is no way
to replace a primary that stops. The UI only toggles a backup so the demo does
not overclaim. This is a major gap versus a production PBFT.

### Routing

`SyncRouter.route()` dispatches on `record.data_class`, then appends the record
to `AuthLog`. Per-class latency and messages-per-entry are recorded in the L4
metrics plane. Raft writes retry at the current leader for up to 10 s; PBFT
retries `PBFTNotPrimary` similarly (normally the primary is fixed). Fault
injection is centralized in `SyncRouter.inject_fault()` so the API and tests
use the same paths.

## L3 — Verifiability

### Merkle tree

`merkle.py` implements RFC 6962 exactly:

- empty root `SHA256("")`;
- leaf `SHA256(0x00 || data)`;
- interior `SHA256(0x01 || left || right)`;
- split at the largest power of two **strictly less than** `n`;
- no padding of the tree to a power of two.

The class stores leaf hashes and a compact list of perfect-subtree peaks, so an
append updates the root in O(log n) merges. Proofs are generated recursively
from the stored leaves and verified by standalone functions that need only
`(leaf_hash, index, size, proof, root)`.

Inclusion verification follows RFC 9162 §2.1.3.2. Consistency verification
follows §2.1.4.2, with an explicit documented extension for `old_size = 0`
(the empty tree is a prefix of every tree) and the subtle rule that the old
root is prepended only when `old_size` is a power of two. The RFC 9162
seven-leaf vectors are tests.

Proof length is **not constant**: a leaf near the right edge of an unbalanced
tree can be shallower. At 10^6 leaves, index 0 needs 20 hashes (640 B) and the
last leaf needs 12 (384 B). Benchmarks report both rather than pretending
`ceil(log2 m)` always holds.

### Authenticated log

`AuthLog` persists:

- `records(seq, record_id, canonical, leaf_hash, created_at, anchored_at, …)`;
- `tree_heads(tree_size, root, timestamp_ms, signature, public_key)`;
- `anchors(anchor_id, tree_size, root, timestamp_ms, signature, ledger_tx_id,
  ledger_backend, ledger_latency_ms, created_at)`.

Every append:

1. takes an `asyncio.Lock`;
2. rejects a duplicate `record_id`;
3. appends the canonical JSON bytes' leaf hash to the in-memory tree;
4. signs the new head `{root, timestamp_ms, tree_size}` with Ed25519 and stores
   it **for every size** (this enables consistency proofs at arbitrary sizes);
5. inserts the record and head in one SQLite transaction;
6. if `tree_size % anchor_batch == 0`, writes the current root to the ledger and
   marks the batch's records anchored, recording per-record audit delay.

`flush()` anchors any remaining records at the end of a run. Therefore ledger
transactions = `ceil(n / b)`, and the exact cadence is unit-tested.

On startup the tree is rebuilt from stored `leaf_hash` values, not from current
record text. If someone edits a record's `canonical` column, the tree does not
change; the record's recomputed leaf hash no longer matches its inclusion proof,
and the root recomputed from the database differs from the signed head. That is
the tamper-detection mechanism demonstrated by the UI.

### Ledger backends

`Ledger` has `append_root`, `transaction_count`, `entries`.

- `SimulatedLedger` (default): append-only SQLite table with a SHA-256 hash chain
  (`entry_hash = SHA256(prev_hash | sequence | root | timestamp)`), optional
  per-transaction latency. No external software.
- `EvmLedger` (optional): web3.py + `contracts/RootAnchor.sol`. Requires
  `pip install -r requirements-evm.txt`, an RPC endpoint, a deployed contract
  and a funded key. Not exercised by the default tests; see `docs/EVM.md`.

Only roots go to the ledger. Records stay in the log; documents stay in the
blob store. A verifier needs a signed head plus a proof, and can compare the
root against the ledger entry.

### Verifier

`verifier.py` contains pure functions used by the API, scripts and tests, plus
a CLI (`python -m dimf.l3_verify.verifier ...`) that reads a JSON proof and
reports VALID/INVALID. The browser implements the same RFC 9162 inclusion
algorithm with `crypto.subtle.digest`, regenerates the canonical JSON from the
record object, hashes the leaf and checks the root. The server's verdict is
displayed as a cross-check, not trusted as the only result.

## L4 — Identity, policy and evaluation

### Identity

`IdentityRegistry` stores `agency_id, name, public_key, active` in SQLite and
derives deterministic Ed25519 keys for three **synthetic** agencies from a
master seed. Signature coverage is the canonical JSON of the submission with
the `signature` field excluded (including `record_id`, `agency`, `data_class`,
`timestamp`, `payload`, `document_b64`). The server verifies the signature and
membership before any policy or storage work.

The demo server holds the private keys so the dashboard can sign. In a real
deployment each agency would sign locally and the registry would know only
public keys. This separation is documented as a compromise, not a feature.

### Policy

`PolicyEngine` parses a simple spec such as
`agency-1:A,B,C;agency-2:A,C;agency-3:A,C` and checks
`(agency, data_class)` before routing. Violations are rejected with HTTP 403
and written to `policy_violations` with a reason. The metrics snapshot exposes
both rejection counters and recent violations. This is a demonstrable policy
plane, not a general ABAC/RBAC engine.

### Evaluation plane

`MetricsCollector` records:

- counters (accepted records, class counts, auth/policy rejections);
- per-scope latency samples (overall + per class, nearest-rank p50/p95/p99);
- messages per committed entry from the simulated network;
- proof size and verification time;
- audit delay from record insert to anchoring;
- ledger transaction count/backend, plus arbitrary gauges.

`GET /metrics` returns the snapshot with `synthetic: true` and the current log
and policy state. E1–E6 in `src/dimf/eval/bench.py` use the same building blocks
directly for reproducible, fast measurements.

## Threat model (what the prototype does and does not defend)

**Addressed**

- A modified stored record is detected: inclusion fails and the recomputed root
  differs from the signed head.
- A forged submission without a valid agency signature is rejected.
- An authenticated agency writing a class outside its policy is rejected and
  logged.
- A modified proof/root/leaf is rejected by RFC 9162 verification.
- One Byzantine PBFT backup (silent or equivocating) at n=4/f=1 does not prevent
  commit or cause honest replicas to diverge.
- A Raft leader crash does not lose committed entries (within the process) and
  a recovered follower catches up.

**Not addressed**

- A compromised log operator key: the operator can sign a different head. The
  ledger anchor limits this only if auditors compare against it.
- A Byzantine PBFT primary (no view change).
- Split-brain/long partitions beyond the modelled Raft behaviour; there is no
  cross-cluster consistency between A, B and C.
- Denial of service, rate limiting, resource exhaustion, malicious payloads.
- Confidentiality: records and documents are not encrypted at rest or in
  transit (no TLS).
- Sybil identities in the registry provisioning process.
- EVM contract/account compromise; the EVM path is optional and unverified.

## Failure and recovery semantics

- CRDT: partitions delay convergence; healing + gossip converges counters and
  last-writer-wins registers.
- Raft: a leader crash triggers an election after the randomized timeout;
  committed prefixes agree across nodes; a restarted node catches up from the
  leader. In-memory state is lost on a real process restart.
- PBFT: up to `f` Byzantine backups tolerated for safety/liveness in the normal
  case; no primary replacement.
- AuthLog: durable across restart; the tree is rebuilt from leaf hashes; signed
  heads and anchors persist. An interrupted head write is repaired on startup
  by signing the current tree if its size has no stored head.
- Ledger: `SimulatedLedger` is append-only and hash-chained; the EVM backend
  waits for a receipt with status 1 or raises.

## Configuration and operations

`Settings` is frozen and env-driven (`DIMF_*`). `load_settings()` recomputes
derived paths when `data_dir` is overridden, which matters for tests and
benchmarks. PBFT size is validated (`n % 3 == 1`, n ≥ 4) at startup.

The demo writes to `data/demo` by default. `make clean` removes caches and
`results/tmp` but deliberately keeps generated CSVs/PNGs and user data.
