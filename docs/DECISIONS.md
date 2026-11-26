# Design decisions and compromises

This is an Architecture Decision Record (ADR) log for the prototype. The goal
is to make the paper demonstrable in a viva without overclaiming.

## Summary table

| # | Decision | Main reason | Cost / limitation |
|---|---|---|---|
| 1 | One Python process, asyncio nodes | deterministic, fast tests and demo | no real sockets, failure domains, OS effects |
| 2 | Simulated network with explicit counters | exact message accounting and fault injection | in-process latency is not WAN latency |
| 3 | `SimulatedLedger` is the default | works offline, zero external services | local hash chain, not a public blockchain |
| 4 | Optional `EvmLedger` behind env flag | shows a path to a real ledger | unverified here; optional deps |
| 5 | Sign and store a tree head on every append | consistency proofs at arbitrary sizes | more Ed25519 work per record |
| 6 | Anchor every `b` records, not every record | the paper's cost/latency trade-off | audit delay for records in a batch |
| 7 | Merkle tree from stored leaf hashes | DB text tampering must not silently alter the tree | tree rebuild is O(n) on startup |
| 8 | RFC 6962 tree without power-of-two padding | exact spec and proof shape | proof length varies by leaf position |
| 9 | CRDT class A, Raft class B, PBFT class C | makes "match mechanism to data class" concrete | three implementations to explain/test |
| 10 | PBFT without view change | scope/time; honest limitation | Byzantine primary is not tolerated |
| 11 | Raft in memory, no snapshots | scope/time | no durable consensus state or log compaction |
| 12 | Ed25519 via `cryptography` | standard, fast, available | key management is demo-grade |
| 13 | Server-held demo agency keys | dashboard can sign without a wallet | not a real separation of duties |
| 14 | Policy as a simple agency→classes table | demonstrable, explainable | not ABAC/RBAC |
| 15 | Synthetic ASCII/integer workload | Python/JS canonical JSON stays identical | not representative of real schemas |
| 16 | Vanilla JS dashboard, no CDN/npm | offline viva, single file | no framework/build tooling |
| 17 | Pinned modern dependency versions | Python 3.14 compatibility | older Python 3.11 still works but pins are newer |
| 18 | Benchmarks use real runs, quick + full modes | credibility and reproducibility | numbers are machine-specific |
| 19 | Honest generated `docs/RESULTS.md` | prevents hand-edited numbers | docs change when benchmark reruns |
| 20 | Demo tamper endpoint | makes detection visible | deliberately dangerous, must stay demo-only |

---

## ADR-1: single-process asyncio instead of containers

**Context.** A four-layer system naturally suggests many services. The viva
machine may not have Docker, and slow multi-container tests hide correctness
bugs behind timing.

**Decision.** Every node is an asyncio object in one process. `SimulatedNetwork`
schedules delivery and models faults. `docker-compose.yml` exists for convenience
but the prototype does not depend on it.

**Consequences.** Tests run in seconds and failures are reproducible. The model
cannot claim real network, scheduling, partial-failure or resource-isolation
behaviour. Docs say so wherever numbers are reported.

## ADR-2: explicit message counters in the simulated network

**Context.** The paper makes message-cost claims (e.g. PBFT `2n(n-1)`, Raft
`2(n-1)`). We need to measure, not assume.

**Decision.** Count every `send`/`dispatch` attempt per protocol, with separate
`sent`, `delivered`, `dropped`. Benchmarks reset counters at a precise boundary
and suppress heartbeats for Raft measurement.

**Consequences.** Counts are exact for the modelled protocol and tests assert
them. They are counts, not bytes; serialisation/TLS/retries are absent.

## ADR-3: `SimulatedLedger` by default

**Context.** The project must run with no Anvil, no funded account and no
internet.

**Decision.** Default ledger is an append-only SQLite table with a SHA-256 hash
chain and optional latency. `make_ledger()` selects EVM only when
`DIMF_LEDGER_BACKEND=evm`.

**Consequences.** Anchoring, audit delay and tamper checks are fully runnable.
The ledger is local and operated by the same process as the log, so it does not
provide independent trust; an auditor must still compare heads. EVM is the
optional stronger path.

## ADR-4: optional EVM backend, not verified in this delivery

**Context.** A public chain is the paper's long-term anchor, but adds toolchain
and funding dependencies.

**Decision.** Implement `EvmLedger` and `contracts/RootAnchor.sol`, gate them
behind `DIMF_LEDGER_BACKEND=evm`, and state clearly whether they were executed.

**Consequences.** The project works offline. `docs/EVM.md` gives exact steps,
but the delivered results were produced with the simulated ledger. Do not claim
EVM was benchmarked.

## ADR-5: sign every tree head, not only anchored heads

**Context.** Consistency proofs need the old signed root at an arbitrary size.
Signing only at anchors would limit proofs to batch boundaries.

**Decision.** Store and sign an Ed25519 tree head on every append. Ed25519 is
cheap enough at prototype scale.

**Consequences.** `GET /log/consistency?old=&new=` works for any sizes. The
append path pays one signature plus one SQLite head row per record. A
production log would batch signatures or use a more efficient STH scheme.

## ADR-6: anchor every `b` records

**Context.** Anchoring every record is simple but expensive; anchoring rarely
increases audit delay.

**Decision.** Anchor exactly when `tree_size % b == 0`, with `flush()` for the
tail. The default is b=100; the demo uses b=10.

**Consequences.** Ledger transactions = `ceil(n/b)`. E6 shows the trade-off:
b=1 → 400 tx and ~8.4 ms audit p50; b=100 → 4 tx and ~154 ms audit p50 on the
synthetic run. The dashboard shows anchored vs pending.

## ADR-7: rebuild the tree from stored leaf hashes

**Context.** If the tree were rebuilt from current record text on every request,
editing SQLite would silently move the root and hide tampering.

**Decision.** Store each record's leaf hash at append time and rebuild the tree
from those hashes. Verification recomputes the leaf from current text.

**Consequences.** A tampered record fails inclusion while the tree and signed
head stay unchanged; the root recomputed from DB text differs from the signed
root. Startup is O(n) for the tree rebuild, acceptable for the prototype.

## ADR-8: no Merkle power-of-two padding

**Context.** Many textbook explanations pad to a power of two, giving a constant
`log2 n` proof. RFC 6962 does not.

**Decision.** Follow RFC 6962 exactly: split at the largest power of two strictly
less than n, no padding. Tests use the RFC 9162 seven-leaf vectors.

**Consequences.** Proofs are correct and match the RFC, but the path can be
shallower (e.g. 12 hashes for the last of 10^6 leaves vs 20 for index 0). The
benchmark reports both, and the README explains the paper's `ceil(log2 m)` is a
worst-case bound, not a promise for every leaf.

## ADR-9: three protocols, not a configurable consensus framework

**Context.** The paper's claim is that different data classes need different
mechanisms.

**Decision.** Implement one clear CRDT, one Raft, one PBFT, and route by the
signed `data_class`.

**Consequences.** Easy to explain and test, and the cost differences are
visible. It is not a general framework: adding a class or protocol requires
code changes, and A/B/C do not share a cross-class transaction.

## ADR-10: PBFT without view change

**Context.** Full PBFT includes view change, checkpointing and a primary
replacement protocol. Implementing it correctly is substantial.

**Decision.** Implement the normal case, tolerate up to f faulty **backups**,
and disable/toggle only backups in the UI. Document the gap everywhere.

**Consequences.** The demo is honest: "n=4 tolerates one Byzantine backup; a
Byzantine primary is out of scope." Tests assert the f=1 boundary. A viva
question about Byzantine primaries has a direct answer: not handled.

## ADR-11: Raft state is in memory

**Context.** Durable Raft state and log compaction are large pieces of work.

**Decision.** Implement election, replication, commit, crash/recover and
catch-up in memory. The authenticated log is the durable part.

**Consequences.** Failover and catch-up are demonstrable, but a real process
restart loses consensus state. Docs call this out; it is not hidden behind a
"production ready" claim.

## ADR-12: Ed25519 via `cryptography`

**Context.** Signatures must be standard and fast.

**Decision.** Use `cryptography`'s Ed25519, deterministic key derivation from a
seed for synthetic agencies and the log operator, Base64 signatures in JSON.

**Consequences.** Standard verification and small keys. Key storage is a demo
compromise (see ADR-13); no HSM or key rotation.

## ADR-13: server-held agency keys in the demo

**Context.** A browser-only demo cannot use real agency private keys without a
wallet or local key generation.

**Decision.** Derive deterministic keys for three synthetic agencies and let the
demo server sign via `/demo/submit`. `POST /records` still requires a real
signature and verifies membership.

**Consequences.** The demo is one click. It does **not** prove separation of
duties or non-repudiation against a malicious server. The docs say the server
holds the keys and that a real deployment would sign client-side.

## ADR-14: simple policy table

**Context.** Policy enforcement must be visible and explainable.

**Decision.** `agency-1: A,B,C; agency-2: A,C; agency-3: A,C`, checked after
signature verification, with violations logged to SQLite.

**Consequences.** A clear 403 demo (agency-2 writing class B). It is not a
general policy language, and the policy file is not itself in the signed log.

## ADR-15: synthetic, canonical-JSON-friendly data

**Context.** Python and browser JavaScript must hash identical bytes.

**Decision.** Use ASCII strings and integer amounts in paise; canonical JSON is
`sorted keys` with compact separators and literal UTF-8 (`ensure_ascii=False`). Timestamps are
integer milliseconds. Every record and document is labelled synthetic.

**Consequences.** Browser and server canonicalisation match exactly, which the
dashboard cross-checks. The workload is deliberately boring and not
representative of real Unicode-heavy government data.

## ADR-16: vanilla JS, no CDN

**Context.** The demo may run without internet, and supply-chain risk in a viva
is unnecessary.

**Decision.** One `index.html` with inline CSS/JS and Web Crypto. No npm, no
build step, no external fonts.

**Consequences.** Full offline operation and a visible verifier implementation.
The single file now carries an eight-section app shell, inline SVG icons and
dependency-free Canvas charts; all UI logic still lives in that one file and
there is no build step.

## ADR-17: modern pinned dependencies

**Context.** The delivered machine has Python 3.14.10. The originally chosen
pydantic 2.10.4 could not build `pydantic-core` because its PyO3 supported only
up to Python 3.13.

**Decision.** Pin versions with Python 3.14 wheels (see `requirements.txt`) and
state the minimum as Python 3.11+.

**Consequences.** `make install` works on the delivered environment. The pins
are newer than a typical tutorial stack; `docs/CHALLENGES.md` records the
failure and the fix.

## ADR-18: quick and full benchmark modes from real runs

**Context.** Viva time is limited; the paper's numbers need to be reproducible.

**Decision.** E1–E6 always run real code; `--quick` reduces repetitions/sizes
but keeps the 10^6-leaf Merkle case. Full mode increases load. Both regenerate
`docs/RESULTS.md` with environment and honest commentary.

**Consequences.** Quick completes in ~13 s here, full well under 10 min. Numbers
are machine-specific and vary slightly; the generated file is authoritative.

## ADR-19: generated results documentation

**Context.** Hand-copied numbers drift from code.

**Decision.** `scripts/run_bench.py` writes CSVs, PNGs, `summary.json` and
`docs/RESULTS.md` in one run.

**Consequences.** The docs always match the committed scripts. Reviewers can
rerun and compare, and the environment block prevents pretending the numbers
came from a cluster.

## ADR-20: tamper endpoint is deliberately destructive

**Context.** Tamper detection is more convincing when the audience sees it.

**Decision.** `POST /demo/tamper/{record_id}` edits the stored `canonical` text
only, gated by `DIMF_ALLOW_TAMPER`. The CLI `scripts/tamper_demo.py` does the
same offline.

**Consequences.** The demo is memorable and verifiable. The endpoint could
destroy demo data; it is explicitly not a production API and the docs say so.
