#!/usr/bin/env python3
"""Start the DIMF prototype demo with SYNTHETIC seed data.

Usage::

    python scripts/run_demo.py [--host 127.0.0.1] [--port 8000] [--reset]

The dashboard is served at http://127.0.0.1:8000/ and needs no internet.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("DIMF_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DIMF_PORT", "8000")))
    parser.add_argument("--data-dir", default=os.getenv("DIMF_DATA_DIR", "data/demo"))
    parser.add_argument("--seed-records", type=int, default=24)
    parser.add_argument("--anchor-batch", type=int, default=int(os.getenv("DIMF_ANCHOR_BATCH", "10")))
    parser.add_argument("--reset", action="store_true", help="delete the demo data directory first")
    parser.add_argument("--no-seed", action="store_true", help="do not seed synthetic records on an empty DB")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if args.reset and data_dir.exists():
        shutil.rmtree(data_dir)
        print(f"removed demo data directory {data_dir}")

    from dimf.common.config import load_settings

    settings = load_settings(
        data_dir=data_dir,
        anchor_batch=args.anchor_batch,
        seed_on_start=not args.no_seed,
        demo_seed_records=args.seed_records,
    )
    settings.ensure_dirs()

    from dimf.api.app import create_app

    app = create_app(settings)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 78)
    print(" DIMF prototype — SYNTHETIC DATA, NOT PRODUCTION")
    print(f" Dashboard:      {url}")
    print(f" OpenAPI docs:   {url}docs")
    print(f" Data directory: {data_dir.resolve()}")
    print(f" Anchor batch:   b={args.anchor_batch} (demo value; framework default is 100)")
    print(" Agencies:       agency-1 (A,B,C), agency-2 (A,C), agency-3 (A,C)")
    print(" The app seeds synthetic records on an empty database and starts the")
    print(" simulated Raft/PBFT/CRDT clusters plus the signed Merkle log.")
    print("=" * 78)
    sys.stdout.flush()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level=os.getenv("DIMF_UVICORN_LOG", "info"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
