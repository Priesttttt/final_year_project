from __future__ import annotations

import base64
import time

from fastapi.testclient import TestClient

from dimf.api.app import create_app
from dimf.common.crypto import KeyPair, b64e, canonical_bytes
from dimf.common.models import Submission


def _signed(registry, agency: str, cls: str, rid: str, payload: dict | None = None, document: bytes | None = None):
    submission = Submission(
        record_id=rid,
        agency=agency,
        data_class=cls,  # type: ignore[arg-type]
        timestamp=int(time.time() * 1000),
        payload=payload or {"amount_paise": 1000, "synthetic": True},
        document_b64=base64.b64encode(document).decode() if document else None,
    )
    return registry.sign(submission)


def _wait_for(fn, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met before timeout")


def test_api_end_to_end_and_tamper(fast_settings):
    app = create_app(fast_settings)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert "SYNTHETIC" in client.get("/").text
        assert client.get("/health").json()["status"] == "ok"

        agencies = client.get("/demo/agencies").json()
        assert agencies["policy"]["agency-1"] == ["A", "B", "C"]
        assert agencies["policy"]["agency-2"] == ["A", "C"]

        state = client.app.state.dimf
        registry = state.identities

        # --- authentication -------------------------------------------------
        unsigned = {
            "record_id": "unsigned-1",
            "agency": "agency-1",
            "data_class": "A",
            "timestamp": 1,
            "payload": {},
        }
        assert client.post("/records", json=unsigned).status_code == 422
        bad = dict(unsigned)
        bad["record_id"] = "bad-1"
        bad["signature"] = "AAAA"
        assert client.post("/records", json=bad).status_code == 401

        stranger = Submission(
            record_id="stranger-1", agency="agency-x", data_class="A", timestamp=1, payload={}
        )
        outsider_key = KeyPair.from_seed("not-a-member")
        signature = outsider_key.sign(canonical_bytes(stranger.signed_payload()))
        payload = stranger.model_dump()
        payload["signature"] = b64e(signature)
        response = client.post("/records", json=payload)
        assert response.status_code == 401
        assert "member" in response.json()["detail"]

        # --- policy ---------------------------------------------------------
        signed_policy = registry.sign(
            Submission(record_id="policy-1", agency="agency-2", data_class="B", timestamp=1, payload={})
        )
        response = client.post("/records", json=signed_policy.model_dump())
        assert response.status_code == 403
        assert "may not write class B" in response.json()["detail"]

        # --- submit A, B, C (C with an off-chain document) -------------------
        a = registry.sign(Submission(record_id="rec-a", agency="agency-2", data_class="A", timestamp=2, payload={"amount_paise": 1}))
        b = registry.sign(Submission(record_id="rec-b", agency="agency-1", data_class="B", timestamp=3, payload={"amount_paise": 2}))
        document = b"SYNTHETIC API TEST DOCUMENT"
        c = registry.sign(
            Submission(
                record_id="rec-c",
                agency="agency-3",
                data_class="C",
                timestamp=4,
                payload={"amount_paise": 3},
                document_b64=base64.b64encode(document).decode(),
            )
        )
        for signed, protocol in [(a, "crdt"), (b, "raft"), (c, "pbft")]:
            response = client.post("/records", json=signed.model_dump())
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["protocol"] == protocol
            assert body["record_id"] == signed.record_id

        # Exactly one anchor after three records (b=3).
        anchors = client.get("/ledger/anchors").json()
        assert anchors["stats"]["tree_size"] == 3
        assert anchors["stats"]["anchors"] == 1
        assert anchors["ledger_transactions"] == 1
        assert len(anchors["anchors"]) == 1

        # Off-chain document: stored by hash, only hash+size in the record.
        proof_c = client.get("/log/proof/rec-c").json()
        assert proof_c["record"]["document"]["size"] == len(document)
        assert state.blobs.exists(proof_c["record"]["document"]["sha256"])
        assert state.blobs.get(proof_c["record"]["document"]["sha256"]) == document

        # --- proof and standalone verification ------------------------------
        proof = client.get("/log/proof/rec-b").json()
        assert proof["valid_at_generation"] is True
        assert proof["index"] == 1 and proof["size"] == 3
        verify_payload = {
            "record": proof["record"],
            "index": proof["index"],
            "size": proof["size"],
            "proof": proof["proof"],
            "root": proof["root"],
        }
        assert client.post("/verify", json=verify_payload).json()["valid"] is True
        tampered_view = dict(verify_payload)
        tampered_record = dict(proof["record"])
        tampered_record["payload"] = {"amount_paise": 999999}
        tampered_view["record"] = tampered_record
        assert client.post("/verify", json=tampered_view).json()["valid"] is False

        consistency = client.get("/log/consistency", params={"old": 1, "new": 3}).json()
        assert consistency["valid"] is True

        # --- Raft failover while submitting --------------------------------
        status = client.get("/cluster/status").json()
        leader = status["B"]["leader"]
        assert leader
        crash = client.post("/cluster/fault", json={"action": "crash", "node": leader, "data_class": "B"})
        assert crash.status_code == 200
        new_leader = _wait_for(
            lambda: client.get("/cluster/status").json()["B"]["leader"] != leader
            and client.get("/cluster/status").json()["B"]["leader"],
            timeout=6,
        )
        assert new_leader
        b2 = registry.sign(
            Submission(record_id="rec-b-after-failover", agency="agency-1", data_class="B", timestamp=5, payload={})
        )
        response = client.post("/records", json=b2.model_dump())
        assert response.status_code == 200, response.text

        # --- PBFT with one Byzantine backup ---------------------------------
        byz = client.post("/cluster/fault", json={"action": "byzantine", "enabled": True}).json()
        assert byz["byzantine"] is True
        c2 = registry.sign(
            Submission(record_id="rec-c-byz", agency="agency-3", data_class="C", timestamp=6, payload={})
        )
        response = client.post("/records", json=c2.model_dump())
        assert response.status_code == 200, response.text
        byz_off = client.post("/cluster/fault", json={"action": "byzantine", "enabled": False}).json()
        assert byz_off["byzantine"] is False

        # --- tamper the stored record; auditor must fail --------------------
        tamper = client.post("/demo/tamper/rec-a").json()
        assert tamper["inclusion_valid_after_tamper"] is False
        assert tamper["root_matches"] is False
        proof_after = client.get("/log/proof/rec-a").json()
        assert proof_after["valid_at_generation"] is False
        assert client.post(
            "/verify",
            json={
                "record": proof_after["record"],
                "index": proof_after["index"],
                "size": proof_after["size"],
                "proof": proof_after["proof"],
                "root": proof_after["root"],
            },
        ).json()["valid"] is False

        # --- metrics --------------------------------------------------------
        metrics = client.get("/metrics").json()
        assert metrics["counters"]["records_accepted"] >= 5
        assert metrics["counters"]["policy_rejections"] >= 1
        assert metrics["counters"]["signature_rejections"] >= 1
        assert metrics["ledger"]["transaction_count"] >= 1
        assert metrics["messages_per_entry"]["C"]["count"] >= 1

        assert client.get("/demo/records").json()["records"]
