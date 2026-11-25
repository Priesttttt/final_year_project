#!/usr/bin/env python3
"""Generate reproducible SYNTHETIC records as JSON Lines.

The first line is a metadata/header object.  All following lines are records
with beneficiary_id, agency, scheme, amount_paise, timestamp, data_class and
an optional small document.  These are NOT real government records.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from dimf.eval.workload import SYNTHETIC_WARNING, mixed_workload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="results/synthetic_records.jsonl")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261125)
    args = parser.parse_args()

    records = mixed_workload(args.count, seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "synthetic": True,
                    "warning": SYNTHETIC_WARNING,
                    "count": len(records),
                    "seed": args.seed,
                    "schema": [
                        "record_id",
                        "agency",
                        "data_class",
                        "timestamp",
                        "payload.beneficiary_id",
                        "payload.scheme",
                        "payload.amount_paise",
                        "payload.district",
                    ],
                }
            )
            + "\n"
        )
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    total_bytes = sum(len(base64.b64decode(r["document_b64"])) for r in records if r.get("document_b64"))
    print(f"wrote {len(records)} SYNTHETIC records to {out}")
    print(f"records with documents: {sum(1 for r in records if r.get('document_b64'))}; document bytes: {total_bytes}")
    print("NOT real government data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
