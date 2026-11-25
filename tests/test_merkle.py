"""RFC 6962 Merkle tree tests, including the RFC 9162 worked example."""

from __future__ import annotations

import hashlib
import random

import pytest

from dimf.l3_verify.merkle import (
    EMPTY_ROOT,
    MerkleTree,
    hash_leaf,
    mth,
    mth_from_leaf_hashes,
    verify_consistency,
    verify_inclusion,
)


def leaf(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def tree_of(n: int) -> MerkleTree:
    tree = MerkleTree()
    for i in range(n):
        tree.append_data(f"leaf-{i}".encode())
    return tree


def test_empty_and_single_leaf_hashes():
    assert EMPTY_ROOT == hashlib.sha256(b"").digest()
    assert hash_leaf(b"abc") == hashlib.sha256(b"\x00abc").digest()
    tree = MerkleTree()
    assert tree.root() == EMPTY_ROOT
    tree.append_data(b"abc")
    assert tree.root() == leaf(b"abc")


def test_two_and_three_leaf_shapes():
    tree = tree_of(2)
    assert tree.root() == node(leaf(b"leaf-0"), leaf(b"leaf-1"))
    tree = tree_of(3)
    expected = node(node(leaf(b"leaf-0"), leaf(b"leaf-1")), leaf(b"leaf-2"))
    assert tree.root() == expected
    # k is the largest power of two *strictly* smaller than n.
    assert mth([b"leaf-0", b"leaf-1", b"leaf-2"]) == expected


def test_rfc9162_seven_leaf_inclusion_vectors():
    data = [f"d{i}".encode() for i in range(7)]
    tree = MerkleTree()
    for item in data:
        tree.append_data(item)
    a, b, c, d, e, f = (leaf(x) for x in data[:6])
    g = node(a, b)
    h = node(c, d)
    i = node(e, f)
    j = leaf(data[6])
    k = node(g, h)
    l = node(i, j)  # noqa: E741 - matches RFC diagram labels
    root = node(k, l)
    assert tree.root() == root

    expected_paths = {
        0: [b, h, l],
        3: [c, g, l],
        4: [f, j, k],
        6: [i, k],
    }
    for index, expected in expected_paths.items():
        assert tree.inclusion_proof(index) == expected
        assert verify_inclusion(tree.leaves[index], index, 7, expected, root)


def test_rfc9162_seven_leaf_consistency_vectors():
    data = [f"d{i}".encode() for i in range(7)]
    tree = MerkleTree()
    for item in data:
        tree.append_data(item)
    a, b, c, d, e, f = (leaf(x) for x in data[:6])
    g = node(a, b)
    h = node(c, d)
    i = node(e, f)
    j = leaf(data[6])
    k = node(g, h)  # noqa: F841 - kept for clarity of the diagram
    l = node(i, j)  # noqa: E741
    assert tree.consistency_proof(3, 7) == [c, d, g, l]
    assert tree.consistency_proof(4, 7) == [l]
    assert tree.consistency_proof(6, 7) == [i, j, k]


@pytest.mark.parametrize("size", [1, 2, 3, 7, 8, 9, 1000])
def test_edge_sizes_inclusion_and_consistency(size: int):
    tree = tree_of(size)
    root = tree.root()
    assert root == mth([f"leaf-{i}".encode() for i in range(size)])
    for index in range(size):
        proof = tree.inclusion_proof(index)
        assert verify_inclusion(tree.leaves[index], index, size, proof, root), (size, index)
        corrupted = bytearray(tree.leaves[index])
        corrupted[0] ^= 0x01
        assert not verify_inclusion(bytes(corrupted), index, size, proof, root)
    # All prefix roots must match the reference definition.
    for old in range(size + 1):
        assert tree.root_at(old) == mth([f"leaf-{i}".encode() for i in range(old)])
    # Sample consistency pairs (all pairs at size 1000 would be slow for no gain).
    pairs = [(m, size) for m in range(1, size + 1)] if size <= 9 else [
        (0, size), (1, size), (size // 3, size), (size // 2, size), (size - 1, size)
    ]
    for old, new in pairs:
        proof = tree.consistency_proof(old, new)
        assert verify_consistency(old, new, tree.root_at(old), tree.root_at(new), proof), (old, new)


def test_randomized_properties():
    rng = random.Random(12345)
    for _ in range(120):
        size = rng.randint(1, 90)
        tree = tree_of(size)
        index = rng.randrange(size)
        proof = tree.inclusion_proof(index)
        assert verify_inclusion(tree.leaves[index], index, size, proof, tree.root())
        old = rng.randint(0, size)
        cproof = tree.consistency_proof(old, size)
        assert verify_consistency(old, size, tree.root_at(old), tree.root(), cproof)
        # Wrong roots, indices and proofs must fail.
        wrong_root = bytearray(tree.root())
        wrong_root[-1] ^= 1
        assert not verify_inclusion(tree.leaves[index], index, size, proof, bytes(wrong_root))
        if len(proof) > 1:
            assert not verify_inclusion(tree.leaves[index], index, size, list(reversed(proof)), tree.root())
        if old < size:
            assert not verify_consistency(old, size, bytes(wrong_root), tree.root(), cproof)


def test_incremental_root_matches_reference_for_random_appends():
    rng = random.Random(7)
    tree = MerkleTree()
    data: list[bytes] = []
    for _ in range(200):
        item = rng.randbytes(rng.randint(1, 40))
        data.append(item)
        tree.append_data(item)
        assert tree.root() == mth(data)
    assert tree.root() == mth_from_leaf_hashes(tree.leaves)


def test_invalid_inputs():
    tree = tree_of(5)
    with pytest.raises(ValueError):
        tree.inclusion_proof(5)
    with pytest.raises(ValueError):
        tree.consistency_proof(6, 5)
    with pytest.raises(ValueError):
        tree.root_at(6)
    assert not verify_inclusion(tree.leaves[0], 0, 0, [], tree.root())
    assert not verify_consistency(3, 2, tree.root_at(3), tree.root_at(2), [])
    # Empty tree consistency is our documented extension.
    assert verify_consistency(0, 5, EMPTY_ROOT, tree.root(), [])
