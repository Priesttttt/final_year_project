# Viva demonstration script (8–10 minutes)

This script assumes a fresh clone and the delivered environment. It is designed
to show each layer **and** at least one failure, without claiming more than the
prototype does.

## Before the viva (2 minutes, not counted)

```bash
make install
make test                  # should pass in roughly 5 s
python scripts/run_demo.py --reset   # fresh synthetic data, b=10
```

Open <http://127.0.0.1:8000/>. If the dashboard ever fails, the JSON API is the
fallback; every UI action has an endpoint (see `README.md`).

Useful terminal windows:

- `make demo` output (URL, data dir, anchor batch);
- a spare shell for `curl` and `make bench -- --quick`.

## 0. Frame the demo (30 s)

Say:

> "This is a synthetic-data, single-process prototype of the paper's four
> layers. All nodes are asyncio objects over a simulated network, so failures
> are reproducible. The dashboard is deliberately labelled synthetic. I will
> show placement, three different synchronisation mechanisms, independent
> proof verification, tamper detection, and the measured trade-offs."

Point at the red **SYNTHETIC DATA — NOT PRODUCTION** tag.

## 1. Submit class A, B, C (1.5 min)

In **Submit a synthetic record**:

1. Keep agency `agency-2`, class **A**, click **Sign & submit**.
   - Expected result: `protocol crdt`, a log index, and `messages_for_entry`
     near 0. Say: "Class A is monitoring data; writes are local and gossip
     later, so there are zero coordination messages for the write."
2. Select class **B** (the UI switches the agency to `agency-1`), submit.
   - Expected: `protocol raft`. Point at the Raft cards: one **LEADER**, two
     followers, terms and commit indexes.
3. Select class **C**, agency `agency-3` (or 2), submit.
   - Expected: `protocol pbft`, about 24 replica-to-replica messages at n=4.
     Say: "n=4, f=1; the normal-case cost is 2n(n−1) = 24."

Open **Evaluation plane** and point at:
- per-class average messages (A≈0, B≈6, C≈24);
- p50/p95 end-to-end latency;
- audit delay and ledger transactions.

## 2. Policy rejection (45 s)

Select class **B**, then manually change the agency back to `agency-2` and
submit.

Expected: a red toast / result such as
`rejected: agency 'agency-2' may not write class B (allowed: A,C)`.

Say: "The signature is valid, but the policy plane rejects the write after
authentication and logs the violation. Policy is L4; it is not hidden in the
browser."

The violation count appears in `GET /metrics` (`policy_rejections`).

## 3. Independent verification in the browser (1.5 min)

In **Auditor**:

1. Choose a seeded record such as `SYN-00003` (or the record you just added).
2. Click **Fetch proof & verify**.
3. Expected verdict: green **VALID — RECORD INCLUDED**.
4. Point out the checks:
   - the browser regenerated the canonical JSON from the record object;
   - it computed the leaf with Web Crypto SHA-256 and walked the RFC 6962 audit
     path itself;
   - the server-side check is shown only as a cross-check;
   - the root matches the anchor **if** that tree size has been anchored; with
     b=10, some sizes are still pending.

Say: "The auditor does not have to trust the server's boolean. The proof is
20 hashes at 10^6 leaves in the worst case; here it is a handful."

## 4. Tamper detection (1 min)

With the same record selected, click **Tamper this record**.

Expected:
- verdict turns red **INVALID — PROOF FAILS**;
- the checks show the browser's recomputed leaf/root failing and the root no
  longer matching the anchor;
- the record table still lists the record, but `Tamper` has changed only the
  stored text.

Say: "The Merkle tree and signed head were not touched. The stored record now
hashes to a different leaf, so the audit path no longer reaches the signed
root. That is tamper **evidence**, not tamper prevention."

Optional terminal proof:

```bash
curl -s http://127.0.0.1:8000/log/proof/SYN-00003 | python -m json.tool | head -40
curl -s http://127.0.0.1:8000/log/stats | python -m json.tool
```

(The tamper endpoint is demo-only and gated by `DIMF_ALLOW_TAMPER`.)

## 5. Raft leader crash and failover (1.5 min)

Click **Kill Raft leader**.

- The killed node turns red/crashed.
- Within roughly the election timeout (measured p50 ≈ 178 ms, but the UI polls
  every 2 s), another node becomes **LEADER** with a higher term.
- Submit a class **B** record. Expected: success via the new leader.
- Click **Recover all crashed nodes**. The old leader returns as a follower and
  catches up (measured p50 ≈ 32 ms).

Say: "Committed entries survive a leader crash in this process. The prototype
does not persist Raft state or implement snapshots, so a real process restart is
a different story."

## 6. PBFT with one Byzantine backup (1 min)

Click **Toggle PBFT Byzantine backup**.

- `pbft-1` is marked **BYZANTINE (silent)**.
- Submit a class **C** record. Expected: success.
- Point at the honest replicas' committed counts and the lower message count
  (18 instead of 24 at n=4), because the faulty replica contributes nothing.
- Toggle it off.

Say: "With n=4 and f=1, one faulty backup is tolerated. There is **no view
change**: a Byzantine primary is out of scope, and I only toggle a backup so the
demo does not overclaim."

If asked to show the boundary: silencing two backups at n=4 leaves only two
honest replicas and the request times out; this is asserted in
`tests/test_pbft.py`.

## 7. Partition (1 min, optional if time is short)

Click **Partition Raft (leader | rest)**, then submit class **B**.

- The isolated leader cannot reach a majority; the write waits and eventually
  fails (the router allows 10 s).
- Class **A** writes still succeed locally because CRDT writes need no
  coordination; the replicas' counters diverge until gossip.
- Click **Heal network**, then retry the class-B write.

Say: "The point is not that partitions are free; it is that the class matters.
CRDT stays available and eventually consistent; Raft chooses consistency and
becomes unavailable without a majority."

If the UI request is still pending, heal the network and wait for the timeout;
do not kill the browser tab.

## 8. Anchoring and metrics (1 min)

Scroll to **Authenticated log & anchoring**.

- Tree size, anchors, pending, ledger transactions.
- With b=10, ledger transactions ≈ tree_size/10; the newest batch may be
  pending.
- The anchor table shows only roots and ledger transaction ids, never records.

Open `GET /metrics` (link or curl) and show:
- `counters.records_accepted`;
- `counters.policy_rejections`, `signature_rejections`;
- `messages_per_entry` for A/B/C;
- `proof.avg_hash_count`, `proof.verify_ms`;
- `audit.p50/p95`.

Say: "This is the L4 evaluation plane from actual observations in this process,
not a separate monitoring product."

## 9. Measured results (1.5 min)

In a terminal:

```bash
make bench -- --quick
```

While it runs (~13 s), say what E1–E6 are. Open `docs/RESULTS.md` and highlight:

- **E5**: at 10^6 leaves, worst-case inclusion proof = **20 hashes / 640 B**;
  the last leaf is shallower (12/384) because RFC 6962 does not pad. This is the
  honest version of the paper's `ceil(log2 m)` claim.
- **E2**: PBFT n=7 = **84** messages (2n(n−1)); Raft n=3 = **4** (2(n−1)).
- **E3**: Raft failover p50 ≈ **178 ms**, catch-up p50 ≈ **32 ms**.
- **E4**: all Byzantine-backup scenarios committed and all honest orders agreed.
- **E6**: b=1 → 400 ledger tx / audit p50 ≈ 8.4 ms; b=100 → 4 tx / ≈ 154 ms.
  The batching trade-off is visible in the generated table.
- Tamper: **10/10** detected.

Then explicitly say: "These are real runs of this program on this laptop with a
simulated network and synthetic data. They are not a production benchmark."

## 10. Close (30 s)

> "The prototype demonstrates the paper's four layers end to end, including
> independent proof verification and tamper evidence. The main honest gaps are:
> single process and simulated network, no PBFT view change, no durable Raft
> state or snapshots, a local simulated ledger by default, and no EVM run in
> this environment. All of these are written down in README and
> docs/CHALLENGES.md."

## Timing summary

| Segment | Time |
|---|---|
| Frame + A/B/C | 2 min |
| Policy + browser proof | 2 min |
| Tamper | 1 min |
| Raft failover | 1.5 min |
| PBFT Byzantine | 1 min |
| Partition (optional) | 1 min |
| Anchoring/metrics | 1 min |
| Benchmarks + close | 1.5 min |

## Recovery moves if something goes wrong

- **Dashboard blank / API unreachable:** `Ctrl+C`, rerun
  `python scripts/run_demo.py --reset`, refresh.
- **Raft has no leader:** wait 2 s; if a partition is still active, click
  **Heal network**.
- **PBFT request hangs:** make sure only **one** backup is Byzantine (toggle
  once, check the node cards), or restart the demo.
- **Verify says pending anchor:** that is correct for a size not divisible by
  b; submit more records or choose an older record. It still verifies against
  the signed head.
- **You tampered the only record you wanted to show:** restart with `--reset`;
  the demo is synthetic and disposable.
