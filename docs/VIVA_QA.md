# Viva questions and answers (20)

Answers are written for this prototype, not for a hypothetical production
system. Numbers are from the quick benchmark described in `docs/RESULTS.md` and
vary by run.

---

### 1. What problem does DIMF solve, in one sentence?

It argues that a single synchronisation mechanism and a single trust model are a
bad fit for heterogeneous government data, so it places data by class, matches
each class to an appropriate consensus mechanism, and makes everything that was
recorded independently verifiable through a signed Merkle log with ledger
anchors.

### 2. Why three synchronisation mechanisms instead of using PBFT for everything?

Cost and requirements differ. Class A monitoring data tolerates eventual
consistency and should not pay coordination; a CRDT write is local. Class B is
single-authority data where crash-fault consensus (Raft) is sufficient and much
cheaper than PBFT: measured **4** messages per entry at n=3 versus **24** at
n=4 for PBFT. Class C (shared funds) needs Byzantine tolerance, so it pays the
PBFT cost. The prototype makes this trade-off measurable rather than asserted.

### 3. What exactly does the Merkle proof prove?

That a specific canonical record is the leaf at a specific index in a tree of a
specific size whose root equals the root in a signed tree head. It does **not**
prove the record is true, legal or unique; it proves inclusion in the log the
operator signed. A signed head proves the operator attested to that root at a
time; a ledger anchor limits later equivocation if auditors compare.

### 4. Why RFC 6962 rather than a simple hash chain or a binary Merkle tree you designed?

RFC 6962 is a precise, widely reviewed specification with defined inclusion and
consistency proof shapes. Using it means we can test against published vectors
(the RFC 9162 seven-leaf example), and an external auditor can implement the
verifier without reading our server. A hash chain is simpler but does not give
logarithmic inclusion proofs; an arbitrary tree design risks subtle
incompatibilities.

### 5. At 10^6 records, how big is a proof and why?

At 10^6 leaves the **worst case** is **20 hashes = 640 bytes** (index 0), because
`ceil(log2 10^6) = 20`. The measured last leaf is shallower: **12 hashes = 384
bytes**, because RFC 6962 does not pad the tree to a power of two. The benchmark
reports both. Verification is cheap: measured about **9.5 ms** in Python in
this run, and the browser version does the same 20 SHA-256 operations.

### 6. What happens if someone edits a record in the database?

The Merkle tree is rebuilt from leaf hashes stored at append time, not from
current text. Editing the `canonical` column leaves the tree and signed head
unchanged, but the record's recomputed leaf no longer matches its audit path, so
inclusion verification fails. Recomputing a root from the edited database gives
a different root from the signed head. In the benchmark **10/10** edited records
were detected, and a clean record still verified. This is tamper **evidence**,
not prevention: an attacker with the signing key could sign a new head.

### 7. Why anchor every *b* records instead of every record?

Ledger transactions cost money/latency, while audit delay grows with the batch.
The prototype exposes the trade-off: with a simulated 2 ms ledger and 120
records, b=1 produced 400 transactions and ~**8.4 ms** audit p50; b=10 produced
40 transactions and ~**22.3 ms**; b=100 produced 4 transactions and ~**153.7 ms**.
The default is b=100; the demo uses b=10 so anchors are visible. The number of
transactions is exactly `ceil(n/b)`, and a `flush()` anchors the tail.

### 8. What does the ledger actually add if the log operator signs every head?

The log operator alone could sign a different history to different auditors
(equivocation). An independent ledger record of the root at a known size gives
auditors a common reference: if two signed heads claim the same size with
different roots, at least one contradicts the anchored root. In this prototype
the default ledger is a local SQLite hash chain, so it does **not** provide
independent trust unless it is operated separately; the optional EVM backend is
the stronger path, but it was not run in this environment.

### 9. How do you know the browser verifier is not just trusting the server?

The browser fetches only the record, index, size, audit path and root. It
regenerates canonical JSON from the record object, hashes the leaf with
`crypto.subtle.digest("SHA-256", 0x00 || canonical)`, and walks the RFC 9162
inclusion algorithm itself, including the `fn == sn` case. It compares the
computed root with the supplied/signed root and with the anchor table. The
server's verdict is displayed as a cross-check, not used for the browser's
VALID/INVALID decision.

### 10. Is the CRDT path really "zero coordination"? What about the log?

The write itself is local: a replica increments its counter/register with no
messages. In the benchmark, 100 class-A writes produced **0** coordination
messages; one later gossip round at n=3 produced 6 messages. But class-A records
are still appended to the authenticated log, which is shared state and can
serialise on the log lock. So "zero coordination" refers to the data
synchronisation mechanism, not to the audit log. The docs say this explicitly.

### 11. What does Raft guarantee here, and what does it not?

Within the process: at most one leader per term, committed entries are a prefix
of every future leader's log, a leader crash triggers a new election, and a
recovered follower catches up. Measured failover p50 ≈ **178 ms** and catch-up
p50 ≈ **32 ms** at n=3 with simulated timers. It does **not** persist term/vote/
log to disk, implement snapshots or compaction, or model real network
partitions beyond the simulator. A process restart loses consensus state.

### 12. PBFT with n=4 tolerates one faulty replica. What if the primary is faulty?

This prototype does **not** handle a Byzantine primary, because there is no view
change. It tolerates one silent or equivocating **backup** at n=4/f=1, and the
UI only toggles a backup. With two silenced backups at n=4, only two honest
replicas remain, below the `2f+1 = 3` commit threshold, and the request times
out; that boundary is tested. Full PBFT with view change is future work and is
listed as a gap in README and `docs/CHALLENGES.md`.

### 13. How is a submission authenticated, and what is signed?

Each agency has an Ed25519 key pair. The signature covers canonical JSON of the
submission with the `signature` field excluded: `record_id`, `agency`,
`data_class`, `timestamp`, `payload`, and optional `document_b64`. The server
verifies the signature against the registered public key and checks that the
agency is an active member before policy or storage. Signed tree heads use the
log operator's Ed25519 key over `{root, timestamp_ms, tree_size}`.

### 14. Where are the private keys in the demo, and is that secure?

The demo server derives deterministic keys for three **synthetic** agencies and
signs via `/demo/submit`, because the browser cannot hold real agency keys in a
convincing way. This is explicitly a demo compromise: it does not demonstrate
separation of duties or non-repudiation against a malicious server.
`POST /records` still requires a real signature, so the verification path is
real. A production deployment would sign client-side and the registry would hold
only public keys.

### 15. How is policy enforced, and can a client bypass it?

Policy is enforced server-side in `AppState.submit_signed()` after signature
verification: `PolicyEngine.check(agency, data_class)` returns allow/deny,
denials become HTTP 403 and are logged to `policy_violations` with a reason.
The dashboard cannot bypass it; the `/demo/submit` helper goes through the same
path. It is a simple agency→classes table, not a general ABAC engine, and the
policy itself is not inside the signed log (a limitation).

### 16. Why is the workload synthetic, and does that undermine the results?

There is no lawful or safe way to put real beneficiary data in a student
prototype. Synthetic data makes the system runnable and reproducible, and every
record/document/agency is labelled. It does limit external validity: the
schema, payload sizes and class mix are chosen by us. The results are valid for
**this program on this machine**; they are not evidence about a real ministry's
traffic. The docs separate measured, simulated and out-of-scope claims.

### 17. What did you actually measure, and what is only simulated?

Measured from real execution: Ed25519 sign/verify, SHA-256/Merkle build and proof
verification, SQLite persistence, Raft/PBFT/CRDT message counts, failover time
in the simulator, anchoring cadence, audit delay, tamper detection, API
behaviour, test suite. Simulated: the network (latency/jitter/drops/partitions),
crashes as flags, the default ledger (local hash chain with modelled latency),
and the synthetic workload. Not run: EVM backend, multi-machine deployment,
TLS, real WAN.

### 18. Why does the system need consistency proofs, not just inclusion proofs?

Inclusion proves "record R is in the log at size N". Consistency proves "the log
at size N is a prefix of the log at size M" (or that they are inconsistent). An
auditor who saw an earlier signed head can check that the operator has not
rewritten history as the log grew. The prototype signs a head at every append
and implements RFC 6962 consistency proofs for arbitrary old/new sizes, exposed
at `/log/consistency` and tested against RFC 9162 vectors plus randomized pairs.

### 19. What is the biggest weakness of the prototype?

The trust and failure model is collapsed into one process: all clusters and the
default ledger are asyncio objects in one address space, with a simulated
network. That makes the demo reproducible but means we cannot claim real
fault isolation, real network behaviour or independent ledger trust. The second
biggest is PBFT without view change, so a Byzantine primary is fatal. Both are
stated prominently rather than hidden.

### 20. How would you take this toward a real deployment?

Split the log/ledger/consensus into independent services with durable Raft
state, snapshots and PBFT view change; use real TLS and authenticated channels;
put agency signing keys in client hardware/HSMs; operate the ledger (or a
permissioned chain) independently of the log operator; add rate limits,
monitoring, backup/restore and schema validation per data class; and run
multi-machine fault-injection and load tests. The Merkle/Ed25519 record format
and verifier can stay, which is the reusable core.

---

## Rapid-fire extras

**Why integer paise and integer timestamps?** To keep Python and JavaScript
canonical JSON byte-identical; floating point formatting differs across
languages and would break hashing.

**Why sign a head on every append instead of only at anchors?** So consistency
proofs work at arbitrary sizes. Ed25519 is cheap at prototype scale.

**What happens on restart?** `AuthLog` reloads leaf hashes, signed heads and
anchors from SQLite and rebuilds the tree. Raft/PBFT state is in memory and is
lost; that is documented.

**Why is the dashboard dependency-free?** It must run offline in a viva, and the
verifier logic should be auditable in one file. No CDN or npm supply chain.

**What is the difference between `sent` and `delivered`?** `sent` counts every
send attempt, including messages later dropped by partition/drop rate;
`delivered` counts successful handoffs. Message-cost claims use `sent` and state
the window.
