"""L4 identities and membership (Ed25519, synthetic agencies)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from dimf.common.crypto import KeyPair, b64e, canonical_bytes, verify_signature
from dimf.common.models import SignedSubmission, Submission

DEFAULT_AGENCIES: dict[str, str] = {
    "agency-1": "Synthetic Agency One",
    "agency-2": "Synthetic Agency Two",
    "agency-3": "Synthetic Agency Three",
}


class IdentityRegistry:
    """Membership list plus server-held demo keys for the synthetic agencies.

    In a real deployment each agency would hold its own private key and the
    registry would only know public keys.  Here the demo server holds
    deterministic keys so the dashboard can sign submissions; this is stated
    explicitly in the README and docs/DECISIONS.md.
    """

    def __init__(self, db_path: str | Path, master_seed: str = "dimf-synthetic-agencies") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.master_seed = master_seed
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agencies (
                agency_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                public_key TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self._conn.commit()
        self._keys: dict[str, KeyPair] = {}
        for agency_id, name in DEFAULT_AGENCIES.items():
            self.register(agency_id, name)

    def _derive(self, agency_id: str) -> KeyPair:
        if agency_id not in self._keys:
            self._keys[agency_id] = KeyPair.from_seed(f"{self.master_seed}:{agency_id}")
        return self._keys[agency_id]

    def register(self, agency_id: str, name: str) -> None:
        keypair = self._derive(agency_id)
        self._conn.execute(
            "INSERT OR IGNORE INTO agencies (agency_id, name, public_key, active) VALUES (?, ?, ?, 1)",
            (agency_id, name, keypair.public_key_hex()),
        )
        self._conn.commit()

    def is_member(self, agency_id: str) -> bool:
        row = self._conn.execute(
            "SELECT active FROM agencies WHERE agency_id = ?", (agency_id,)
        ).fetchone()
        return bool(row and row["active"])

    def public_key(self, agency_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT public_key FROM agencies WHERE agency_id = ?", (agency_id,)
        ).fetchone()
        return str(row["public_key"]) if row else None

    def sign(self, submission: Submission) -> SignedSubmission:
        if not self.is_member(submission.agency):
            raise ValueError(f"agency {submission.agency!r} is not a member")
        signature = self._derive(submission.agency).sign(canonical_bytes(submission.signed_payload()))
        data = submission.model_dump()
        data["signature"] = b64e(signature)
        return SignedSubmission(**data)

    def verify_submission(self, submission: SignedSubmission) -> tuple[bool, str]:
        public_key = self.public_key(submission.agency)
        if public_key is None:
            return False, "unknown agency (not a member)"
        if not self.is_member(submission.agency):
            return False, "agency is inactive"
        if not submission.signature:
            return False, "missing signature"
        if not verify_signature(public_key, submission.signature, submission.signed_payload()):
            return False, "signature does not verify"
        return True, "ok"

    def members(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT agency_id, name, public_key, active FROM agencies ORDER BY agency_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def to_json(self) -> str:
        return json.dumps(self.members(), indent=2, sort_keys=True)
