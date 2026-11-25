"""Standalone public verifier.

This module deliberately imports only the Merkle primitives and canonical
JSON helper.  An auditor needs a record, its index and tree size, an audit
proof and a root -- no access to the log database or the log server's code.

CLI::

    python -m dimf.l3_verify.verifier < request.json

where ``request.json`` has the shape of ``VerifyRequest``.  Exit code 0
means VALID, 1 means INVALID, 2 means malformed input.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from dimf.common.crypto import canonical_json, verify_signature
from dimf.l3_verify.merkle import EMPTY_ROOT, hash_leaf, verify_consistency, verify_inclusion


def verify_record(
    record: dict[str, Any],
    index: int,
    size: int,
    proof: list[str],
    root: str,
) -> dict[str, Any]:
    """Recompute the leaf from the record and verify its inclusion."""
    try:
        canonical = canonical_json(record)
    except (TypeError, ValueError) as exc:
        return {"valid": False, "reason": f"record is not canonicalisable: {exc}", "leaf_hash": ""}
    leaf = hash_leaf(canonical.encode("utf-8"))
    try:
        proof_bytes = [bytes.fromhex(node) for node in proof]
        root_bytes = bytes.fromhex(root)
    except ValueError as exc:
        return {
            "valid": False,
            "reason": f"malformed proof/root hex: {exc}",
            "leaf_hash": leaf.hex(),
        }
    valid = verify_inclusion(leaf, index, size, proof_bytes, root_bytes)
    return {
        "valid": valid,
        "reason": "record included in the signed root" if valid else "inclusion proof does not match the record",
        "leaf_hash": leaf.hex(),
        "computed_root": root if valid else None,
        "canonical": canonical,
    }


def verify_consistency_proof(
    old_size: int,
    new_size: int,
    old_root: str,
    new_root: str,
    proof: list[str],
) -> bool:
    try:
        proof_bytes = [bytes.fromhex(node) for node in proof]
        return verify_consistency(
            old_size,
            new_size,
            bytes.fromhex(old_root),
            bytes.fromhex(new_root),
            proof_bytes,
        )
    except ValueError:
        return False


def verify_tree_head_signature(head: dict[str, Any]) -> bool:
    """Check the log operator's Ed25519 signature on a signed tree head."""
    payload = {
        "root": head["root"],
        "timestamp_ms": int(head["timestamp_ms"]),
        "tree_size": int(head["tree_size"]),
    }
    return verify_signature(head["public_key"], head["signature"], payload)


def verify_anchored_root(root: str, anchor_root: str) -> bool:
    """An anchored ledger value is simply compared with the claimed root."""
    return root.lower() == anchor_root.lower()


def verify_empty_root(root: str) -> bool:
    return root.lower() == EMPTY_ROOT.hex()


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if argv and argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    try:
        if argv:
            with open(argv[0], "r", encoding="utf-8") as handle:
                request = json.load(handle)
        else:
            request = json.load(sys.stdin)
        result = verify_record(
            request["record"],
            int(request["index"]),
            int(request["size"]),
            list(request["proof"]),
            str(request["root"]),
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
