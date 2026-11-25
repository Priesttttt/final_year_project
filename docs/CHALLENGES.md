# Real challenges encountered

This file records problems that actually occurred while building and running
the prototype, and what was done about them. It is evidence for the viva, not a
generic risk list.

## 1. Python 3.14 broke the original dependency pins

**Symptom.** Installing the initially chosen `pydantic==2.10.4` failed while
building `pydantic-core`. The Rust/PyO3 build reported a maximum supported
Python version of 3.13, while the delivery machine runs Python 3.14.10.

**Fix.** Moved to versions that ship Python 3.14 wheels and pinned them:
FastAPI 0.165.0, uvicorn 0.54.0, pydantic 2.14.4, cryptography 50.0.2,
pytest 9.2.0, httpx 0.28.1, matplotlib 3.11.2, ruff 0.17.6. `make install`
now succeeds from a clean venv.

**Lesson.** "Pinned" is not the same as "portable"; wheel availability for the
target interpreter matters.

## 2. RFC 6962 consistency verification has a subtle special case

**Symptom.** A naive consistency verifier rejected valid proofs for some
`old_size`/`new_size` pairs. The RFC 9162 algorithm inserts the old root into
the path **only when the old tree size is an exact power of two**, and treats
the empty tree as a special case. Getting this wrong makes tests fail in a way
that looks like a proof-generation bug.

**Fix.** Implemented §2.1.4.2 exactly, with an explicit, documented extension
for `old_size = 0` (the empty tree is a prefix of every tree). Added the RFC
9162 seven-leaf vectors and randomized consistency tests.

**Lesson.** Merkle proof code is easy to make "work for my examples" while
being wrong at sizes a test did not cover.

## 3. The last leaf of an unbalanced tree has a shorter proof

**Symptom.** E5 initially measured the inclusion proof for the last leaf of a
10^6-leaf tree and reported **12 hashes / 384 bytes**, not the paper's
`ceil(log2 m) = 20` / 640 B. It looked like a bug. It is not: RFC 6962 does not
pad the tree to a power of two, so a leaf on the right edge is shallower.
Index 0 needs exactly 20 hashes.

**Fix.** The benchmark now reports the **worst case (index 0)** and the
**shallowest leaf** separately. README and `docs/RESULTS.md` explain that the
paper's bound is a worst case, not a constant. Both proofs are verified.

**Lesson.** A headline number can be technically true only for the worst case;
hiding the variation would be misleading.

## 4. Raft message counts were polluted by heartbeats and elections

**Symptom.** Counting `network.sent("raft")` around one append gave more than
`2(n−1)` because heartbeats, election requests and delayed responses were
included. The measurement depended on when the counter was reset.

**Fix.** For E2/tests, start the cluster, wait for a stable leader, suppress
heartbeats (`heartbeat_s=30`), reset counters immediately before the append,
and count only that window. The test asserts exactly `2(n−1)`.

**Lesson.** Message-count claims require stating the measurement window and
what was excluded.

## 5. PBFT message counts differ under Byzantine faults

**Symptom.** With one silent backup at n=4, the count is 18 per request, not
24. The normal-case formula assumes every replica participates.

**Fix.** Report normal-case and faulty-case counts separately. The test asserts
the normal-case `2n(n−1)` and only asserts that the Byzantine case is lower and
still commits.

**Lesson.** Do not quote a single formula without its fault assumption.

## 6. Anchoring while holding the AuthLog lock serialises appends

**Symptom.** With `b=1` and a 20 ms simulated ledger latency, appends took
roughly 20 ms each even though the log itself is fast. `_anchor_locked()` holds
the `asyncio.Lock` while awaiting `ledger.append_root()`.

**Fix for the prototype.** Accepted and documented: it preserves a simple
ordering invariant and makes audit-delay measurements meaningful. Benchmarks
use b=1/10/100 to show the cost. A production log would move anchoring off the
append critical path with a durable queue.

**Lesson.** The batch size trade-off is not only ledger cost; lock scope and
latency also matter.

## 7. Node-name collisions across three clusters

**Symptom.** Early wiring registered CRDT, Raft and PBFT nodes with names like
`node-0`, so one network `crash("node-0")` hit several protocols and message
counters mixed.

**Fix.** Unique prefixes: `crdt-i`, `raft-i`, `pbft-i`; one shared
`SimulatedNetwork`; fault injection checks which cluster owns a node.

**Lesson.** In a single process, names are the only failure-domain boundary.

## 8. Browser and Python canonical JSON must match byte for byte

**Symptom.** The in-browser verifier initially disagreed with the server on
some records. The causes were key ordering and formatting, not SHA-256.

**Fix.** Canonical JSON is sorted keys with compact separators and
`ensure_ascii=False` with literal UTF-8, matching JavaScript; the workload uses ASCII strings and integer amounts, and
the browser sorts object keys recursively before `JSON.stringify`. The
dashboard also compares its regenerated canonical string with the server's
string and shows a failure if they differ.

**Lesson.** "The JSON is the same" is not enough; the exact bytes are signed and
hashed.

## 9. Demo keys are held by the server

**Symptom / design tension.** The dashboard needs to sign submissions, but a
real agency would hold its private key locally. Shipping keys in JavaScript
would be worse.

**Decision.** Deterministic synthetic keys live on the server; `/demo/submit`
signs. `POST /records` still requires a signature and membership. The docs
state that this does not demonstrate separation of duties.

**Lesson.** A convenient demo can quietly weaken a security claim; label it.

## 10. PBFT has no view change, so a Byzantine primary is fatal

**Symptom.** The implementation can tolerate silent/equivocating backups, but
there is no path to replace a faulty primary. Toggling the primary would make
the demo fail in a confusing way.

**Decision.** Do not implement view change in this prototype. The UI only
toggles a backup, and README/ARCHITECTURE/CHALLENGES state the gap. Tests cover
the f=1 backup boundary, including the expected timeout with two silenced
backups.

**Lesson.** "PBFT" in a paper often means the full protocol; the normal case is
not the whole story.

## 11. Raft state is not durable

**Symptom.** The authenticated log survives a restart, but Raft's term, vote
and log are in memory. A real process restart would lose consensus state, even
though the records already committed to `AuthLog` survive.

**Decision.** Scope the prototype to in-process failover (crash/recover flags),
document the gap, and avoid claiming restart durability for consensus.

**Lesson.** Durable log ≠ durable consensus.

## 12. SQLite and asyncio

**Symptom.** SQLite connections are not thread-safe by default, and the app
uses a single event loop with objects created in different contexts.

**Fix.** `check_same_thread=False`, one connection per component, all access
from the event loop, WAL mode, and short transactions. This is fine for the
prototype; it is not a concurrency design for a multi-worker server.

**Lesson.** The single-process assumption is load-bearing; `--workers 4` would
break these components.

## 13. Tests became flaky until simulated timers were tuned

**Symptom.** Raft election/failover tests occasionally timed out or missed a
leader when election timeouts were close to heartbeat intervals, or when the
test waited too briefly after a crash.

**Fix.** Fixed seeds, heartbeat 30 ms, election 120–220 ms, explicit
`wait_for_leader`/`wait_for(predicate)` helpers with generous deadlines, and
counter resets only after a stable leader. Total suite still runs in ~5 s.

**Lesson.** Distributed-systems tests need condition-based waits, not fixed
sleeps, even in simulation.

## 14. The EVM backend was not exercised in this environment

**Symptom.** Running a real EVM path requires Anvil/Hardhat, a deployed
contract, a funded key and web3.py. Those were not available/needed for the
delivery.

**Decision.** Implement `EvmLedger` and `RootAnchor.sol`, keep the default
simulated backend, and state plainly in `docs/EVM.md` and `docs/RESULTS.md`
that no EVM transaction was measured. The ABI is kept in sync with the
contract.

**Lesson.** An optional integration that is not run must be labelled
"unverified", not "implemented and tested".

## 15. `make bench -- --quick` argument passing

**Symptom.** GNU make treats `--` and `--quick` after the target as extra
goals, so a naive recipe either errored ("No rule to make target '--quick'") or
passed the wrong arguments.

**Fix.** The Makefile filters `bench`/`--` out of `MAKECMDGOALS`, has no-op
targets for `--quick`/`--full`, and passes the rest to `run_bench.py`.
`make bench -- --quick` and `make bench` both work.

**Lesson.** The documented command must be tested, not just the Python script.

## 16. Time-boxing the benchmark without weakening it

**Symptom.** A naive full run with 10^6 leaves, many Raft trials and large
batches could exceed the viva budget.

**Fix.** Quick mode reduces repetitions and record counts but **keeps** the
10^6-leaf Merkle case and the exact message-count experiments. It runs in
~13 s here. Full mode increases load and still finishes well under 10 minutes.
Both regenerate the same tables and charts.

**Lesson.** Optimise the harness, not the claim.

## 17. PBFT order comparison sampled before every backup finished

**Symptom.** In one full E4 run, `orders_agree` was `False` for n=7 even though
every request committed. The primary returns as soon as it has `2f+1` commits,
while the last COMMIT messages may still be in flight to some backups. The
benchmark read `ordered_requests()` immediately, so one honest node could
legitimately have a shorter committed list at that instant.

**Fix.** E4 now waits until every honest replica has committed all requests
before comparing orders; the PBFT tests use the same condition-based wait. Five
consecutive full E4 runs then agreed for all n=4/n=7 honest/silent/equivocating
scenarios.

**Lesson.** A protocol returning to the client is not the same as every replica
having observed the result. Benchmarks must state and wait for the observation
point, especially when comparing replicas.
