from __future__ import annotations

from dimf.common.models import Submission
from dimf.l4_identity.identity import IdentityRegistry
from dimf.l4_identity.policy import PolicyEngine, parse_policy


def test_parse_policy():
    parsed = parse_policy("agency-1:A,B,C;agency-2:A,C")
    assert parsed == {"agency-1": {"A", "B", "C"}, "agency-2": {"A", "C"}}


def test_policy_allows_and_logs_violation(tmp_path):
    engine = PolicyEngine(tmp_path / "p.sqlite3", "agency-1:A,B;agency-2:A")
    assert engine.check("agency-1", "B", "r1")[0]
    ok, reason = engine.check("agency-2", "B", "r2")
    assert not ok and "may not write class B" in reason
    ok, reason = engine.check("agency-9", "A", "r3")
    assert not ok and "no policy entry" in reason
    violations = engine.violations()
    assert len(violations) == 2
    assert {v["record_id"] for v in violations} == {"r2", "r3"}


def test_identity_sign_and_verify(tmp_path):
    registry = IdentityRegistry(tmp_path / "i.sqlite3", "master")
    submission = Submission(
        record_id="r1", agency="agency-1", data_class="A", timestamp=1, payload={"a": 1}
    )
    signed = registry.sign(submission)
    ok, reason = registry.verify_submission(signed)
    assert ok, reason
    assert registry.is_member("agency-1")


def test_identity_rejects_wrong_signature_and_non_member(tmp_path):
    registry = IdentityRegistry(tmp_path / "i.sqlite3", "master")
    submission = Submission(
        record_id="r1", agency="agency-1", data_class="A", timestamp=1, payload={"a": 1}
    )
    signed = registry.sign(submission)
    tampered = signed.model_copy(update={"payload": {"a": 2}})
    ok, reason = registry.verify_submission(tampered)
    assert not ok and "signature" in reason

    stranger = Submission(
        record_id="r2", agency="agency-unknown", data_class="A", timestamp=1, payload={}
    )
    # The server refuses to sign for a non-member.
    try:
        registry.sign(stranger)
    except ValueError as exc:
        assert "not a member" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("non-member was signed")
