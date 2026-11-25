from __future__ import annotations

import base64

from dimf.common.crypto import KeyPair, b64e, canonical_json, verify_signature
from dimf.common.models import SignedSubmission, Submission


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"a": [1, {"z": "x", "y": "w"}]}) == '{"a":[1,{"y":"w","z":"x"}]}'


def test_sign_and_verify_roundtrip():
    key = KeyPair.from_seed("test-seed")
    payload = {"amount_paise": 123, "record_id": "r1"}
    signature = key.sign(canonical_json(payload).encode())
    assert key.verify(signature, canonical_json(payload).encode())
    assert verify_signature(key.public_key_hex(), b64e(signature), payload)
    tampered = dict(payload)
    tampered["amount_paise"] = 999
    assert not verify_signature(key.public_key_hex(), b64e(signature), tampered)
    assert not key.verify(signature, b"different")


def test_deterministic_keys_and_signature_in_submission():
    key = KeyPair.from_seed("agency")
    assert key.public_key_hex() == KeyPair.from_seed("agency").public_key_hex()
    submission = Submission(
        record_id="rec-1",
        agency="agency-1",
        data_class="A",
        timestamp=1234,
        payload={"x": 1},
    )
    signed = SignedSubmission(
        **submission.model_dump(exclude={"signature"}),
        signature=b64e(key.sign(canonical_json(submission.signed_payload()).encode())),
    )
    assert verify_signature(key.public_key_hex(), signed.signature, signed.signed_payload())
    assert "signature" not in submission.signed_payload()


def test_signature_decoding_errors_are_rejected():
    key = KeyPair.from_seed("x")
    bad = base64.b64encode(b"short").decode()
    assert not verify_signature(key.public_key_hex(), bad, {"a": 1})
    assert not verify_signature("zz", b64e(b"x"), {"a": 1})
