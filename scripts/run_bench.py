#!/usr/bin/env python3
"""Run benchmarks E1-E6 and regenerate docs/RESULTS.md.

Usage::

    python scripts/run_bench.py --quick    # under 2 minutes
    python scripts/run_bench.py            # full run
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dimf.eval.bench import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
