"""L4 policy: which agency may write which data class."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from dimf.common.models import ALL_DATA_CLASSES, DataClass


def parse_policy(spec: str) -> dict[str, set[str]]:
    """Parse ``agency-1:A,B;agency-2:A,C`` into a mapping."""
    policy: dict[str, set[str]] = {}
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        agency, _, classes = part.partition(":")
        allowed = {c.strip() for c in classes.split(",") if c.strip()}
        unknown = allowed - set(ALL_DATA_CLASSES)
        if unknown:
            raise ValueError(f"unknown data classes in policy: {sorted(unknown)}")
        policy[agency.strip()] = allowed
    return policy


class PolicyEngine:
    def __init__(self, db_path: str | Path, spec: str) -> None:
        self.policy = parse_policy(spec)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                agency TEXT NOT NULL,
                data_class TEXT NOT NULL,
                record_id TEXT,
                reason TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def allowed(self, agency: str, data_class: DataClass) -> bool:
        return data_class in self.policy.get(agency, set())

    def check(self, agency: str, data_class: DataClass, record_id: str = "") -> tuple[bool, str]:
        if agency not in self.policy:
            reason = f"agency {agency!r} has no policy entry"
        elif not self.allowed(agency, data_class):
            allowed = ",".join(sorted(self.policy[agency]))
            reason = f"agency {agency!r} may not write class {data_class} (allowed: {allowed or 'none'})"
        else:
            return True, "ok"
        self._log_violation(agency, data_class, record_id, reason)
        return False, reason

    def _log_violation(self, agency: str, data_class: str, record_id: str, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO policy_violations (created_at, agency, data_class, record_id, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), agency, data_class, record_id, reason),
        )
        self._conn.commit()

    def violations(self, limit: int = 50) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM policy_violations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def as_dict(self) -> dict[str, list[str]]:
        return {agency: sorted(classes) for agency, classes in sorted(self.policy.items())}
