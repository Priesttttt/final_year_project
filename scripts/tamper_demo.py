#!/usr/bin/env python3
"""Tamper-detection demonstration (CLI).

Modifies stored record content in SQLite *without* changing the Merkle tree,
then shows that:

1. every modified record fails inclusion verification against the signed root;
2. the root recomputed from the modified database no longer equals the signed
   tree head.

This is a demo tool and deliberately corrupts the selected database.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dimf.common.crypto import canonical_json, sha256_hex  # noqa: E402
from dimf.l3_verify.merkle import MerkleTree, verify_inclusion  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/demo/dimf.sqlite3")
    parser.add_argument("--record-id", default=None)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261125)
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path} (run `make demo` first)", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT seq, record_id, canonical, leaf_hash FROM records ORDER BY seq").fetchall()
    if not rows:
        print("no records in database", file=sys.stderr)
        return 2
    head = conn.execute("SELECT root FROM tree_heads ORDER BY tree_size DESC LIMIT 1").fetchone()
    if head is None:
        print("no signed tree head", file=sys.stderr)
        return 2
    signed_root = str(head["root"])

    tree = MerkleTree()
    for row in rows:
        tree.append_hash(bytes.fromhex(row["leaf_hash"]))
    assert tree.size == len(rows)

    if args.record_id:
        targets = [r for r in rows if r["record_id"] == args.record_id]
        if not targets:
            print(f"record not found: {args.record_id}", file=sys.stderr)
            return 2
    else:
        rng = random.Random(args.seed)
        targets = rng.sample(rows, k=min(args.count, len(rows)))

    # First show that untouched records verify.
    baseline_failures = 0
    for row in rows:
        canonical = str(row["canonical"])
        leaf = bytes.fromhex(sha256_hex(b"\x00" + canonical.encode("utf-8")))
        proof = tree.inclusion_proof(int(row["seq"]), tree.size)
        if not verify_inclusion(leaf, int(row["seq"]), tree.size, proof, bytes.fromhex(signed_root)):
            baseline_failures += 1
    total = len(rows)
    print(f"SYNTHETIC demo database: {db_path}")
    print(f"records: {total}; signed tree size: {tree.size}; baseline inclusion failures: {baseline_failures}")

    # Tamper: change payload only, leave leaf_hash/tree untouched.
    for row in targets:
        obj = json.loads(str(row["canonical"]))
        payload = dict(obj.get("payload") or {})
        payload["_tampered"] = True
        payload["note"] = "TAMPERED BY scripts/tamper_demo.py"
        obj["payload"] = payload
        new_canonical = canonical_json(obj)
        conn.execute(
            "UPDATE records SET canonical = ? WHERE record_id = ?",
            (new_canonical, row["record_id"]),
        )
    conn.commit()

    failed = 0
    for row in targets:
        current = conn.execute(
            "SELECT seq, canonical FROM records WHERE record_id = ?", (row["record_id"],)
        ).fetchone()
        canonical = str(current["canonical"])
        leaf = bytes.fromhex(sha256_hex(b"\x00" + canonical.encode("utf-8")))
        proof = tree.inclusion_proof(int(current["seq"]), tree.size)
        ok = verify_inclusion(leaf, int(current["seq"]), tree.size, proof, bytes.fromhex(signed_root))
        if not ok:
            failed += 1
        print(f"  tampered {row['record_id']:>20}  inclusion_valid={ok}  -> {'DETECTED' if not ok else 'NOT DETECTED'}")

    # Rebuild root from current database content.
    rebuilt = MerkleTree()
    for row in conn.execute("SELECT canonical FROM records ORDER BY seq"):
        rebuilt.append_data(str(row["canonical"]).encode("utf-8"))
    rebuilt_root = rebuilt.root().hex()
    print(f"recomputed root from DB: {rebuilt_root}")
    print(f"signed root:             {signed_root}")
    print(f"root match:              {rebuilt_root == signed_root}")

    conn.close()
    rate = (failed / len(targets) * 100.0) if targets else 0.0
    print(f"tamper detection rate: {failed}/{len(targets)} = {rate:.1f}%")
    success = failed == len(targets) and rebuilt_root != signed_root and baseline_failures == 0
    print("RESULT:", "all tampering detected" if success else "SOME TAMPERING NOT DETECTED")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
