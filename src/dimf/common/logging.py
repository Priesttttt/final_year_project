"""Logging setup for the prototype."""

from __future__ import annotations

import logging
import os
import sys


def configure_logging(level: str | None = None) -> None:
    """Configure a compact console logger once."""
    chosen = (level or os.getenv("DIMF_LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(chosen)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(chosen)
    # Uvicorn access logs are noisy in a classroom demo; keep warnings.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
