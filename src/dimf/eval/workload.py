"""Reproducible SYNTHETIC workload generation.

The schema is documented in docs/ARCHITECTURE.md.  Every record carries a
``synthetic`` marker and a note saying it is not real government data.
"""

from __future__ import annotations

import base64
import random
from typing import Any

SCHEMES = ["SYNTH-SCHOLARSHIP", "SYNTH-HOUSING", "SYNTH-HEALTH", "SYNTH-FOOD", "SYNTH-PENSION"]
DISTRICTS = ["SYNTH-DISTRICT-A", "SYNTH-DISTRICT-B", "SYNTH-DISTRICT-C", "SYNTH-DISTRICT-D"]
AGENCIES = ["agency-1", "agency-2", "agency-3"]

SYNTHETIC_WARNING = "SYNTHETIC DATA — generated for a prototype demo, not real government data"


def make_record(
    index: int,
    data_class: str,
    agency: str | None = None,
    rng: random.Random | None = None,
    with_document: bool = False,
    prefix: str = "SYN",
) -> dict[str, Any]:
    """Build one submission-shaped record with optional off-chain document."""
    rng = rng or random.Random(index)
    if agency is None:
        # Demo policy: only agency-1 may write class B.
        agency = "agency-1" if data_class == "B" else AGENCIES[index % len(AGENCIES)]
    record: dict[str, Any] = {
        "record_id": f"{prefix}-{index:07d}",
        "agency": agency,
        "data_class": data_class,
        "timestamp": 1_700_000_000_000 + index * 1_000,
        "payload": {
            "beneficiary_id": f"SYNTH-BEN-{rng.randint(10000, 99999)}",
            "scheme": rng.choice(SCHEMES),
            "amount_paise": rng.randint(10_000, 900_000),
            "district": rng.choice(DISTRICTS),
            "synthetic": True,
            "note": SYNTHETIC_WARNING,
        },
        "document_b64": None,
    }
    if with_document:
        document = f"SYNTHETIC DOCUMENT for {record['record_id']}\n".encode("utf-8")
        record["document_b64"] = base64.b64encode(document).decode("ascii")
    return record


def mixed_workload(
    n: int,
    seed: int = 20261125,
    a_share: float = 0.60,
    b_share: float = 0.20,
) -> list[dict[str, Any]]:
    """Generate N records with the benchmark's 60/20/20 class mix."""
    rng = random.Random(seed)
    records: list[dict[str, Any]] = []
    for i in range(n):
        draw = rng.random()
        cls = "A" if draw < a_share else ("B" if draw < a_share + b_share else "C")
        records.append(
            make_record(i, cls, rng=rng, prefix="BENCH", with_document=(i % 17 == 0))
        )
    return records
