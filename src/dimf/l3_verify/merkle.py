"""RFC 6962 / Certificate Transparency Merkle tree.

Hash rules
----------
* empty tree: ``SHA256("")``
* leaf:       ``SHA256(0x00 || data)``
* interior:   ``SHA256(0x01 || left || right)``
* split at the largest power of two strictly smaller than ``n``.

The class keeps an append-only list of leaf *hashes* and a compact list of
perfect-subtree peaks.  The current root is therefore O(log n) to update
after an append, while proofs are generated from the stored leaf hashes.
This is a readable prototype implementation, not an optimised log server.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Sequence


def _hash_leaf_bytes(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _hash_node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


EMPTY_ROOT: bytes = hashlib.sha256(b"").digest()


def largest_power_of_two_less_than(n: int) -> int:
    """Return the largest power of two strictly less than *n* (n >= 2)."""
    if n < 2:
        raise ValueError("n must be >= 2")
    # 1 << (bit_length - 1) is <= n; if n is a power of two, halve it.
    k = 1 << (n.bit_length() - 1)
    return k if k < n else k >> 1


def mth(data_entries: Sequence[bytes]) -> bytes:
    """Naive reference Merkle Tree Hash over raw leaf data (testing aid)."""
    n = len(data_entries)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return _hash_leaf_bytes(data_entries[0])
    k = largest_power_of_two_less_than(n)
    return _hash_node(mth(data_entries[:k]), mth(data_entries[k:]))


def mth_from_leaf_hashes(leaf_hashes: Sequence[bytes]) -> bytes:
    """Reference MTH over already-hashed leaves (no 0x00 prefix applied)."""
    n = len(leaf_hashes)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return leaf_hashes[0]
    k = largest_power_of_two_less_than(n)
    return _hash_node(
        mth_from_leaf_hashes(leaf_hashes[:k]),
        mth_from_leaf_hashes(leaf_hashes[k:]),
    )


class MerkleTree:
    """An append-only RFC 6962 tree."""

    def __init__(self, leaf_hashes: Iterable[bytes] = ()) -> None:
        self._leaves: list[bytes] = []
        # Peaks are (subtree_size, hash), ordered left to right.
        self._peaks: list[tuple[int, bytes]] = []
        for leaf_hash in leaf_hashes:
            self.append_hash(leaf_hash)

    # -- construction -----------------------------------------------------
    def append_data(self, data: bytes) -> bytes:
        """Hash *data* as an RFC 6962 leaf and append it; returns leaf hash."""
        leaf = _hash_leaf_bytes(data)
        self.append_hash(leaf)
        return leaf

    def append_hash(self, leaf_hash: bytes) -> None:
        """Append an already-computed 32-byte leaf hash."""
        if len(leaf_hash) != 32:
            raise ValueError("leaf hash must be 32 bytes")
        self._leaves.append(leaf_hash)
        peaks = self._peaks
        peaks.append((1, leaf_hash))
        # Merge adjacent equal-size perfect subtrees (binary counter).
        while len(peaks) >= 2 and peaks[-1][0] == peaks[-2][0]:
            size = peaks[-1][0]
            right = peaks.pop()[1]
            left = peaks.pop()[1]
            peaks.append((size * 2, _hash_node(left, right)))

    @property
    def size(self) -> int:
        return len(self._leaves)

    @property
    def leaves(self) -> list[bytes]:
        """Copy of the leaf hashes (safe for tests; O(n))."""
        return list(self._leaves)

    # -- roots ------------------------------------------------------------
    def root(self) -> bytes:
        """Current Merkle Tree Hash; O(number of peaks) after an append."""
        if not self._peaks:
            return EMPTY_ROOT
        acc = self._peaks[-1][1]
        # RFC 6962 root folds the peaks right-to-left.
        for _, peak_hash in reversed(self._peaks[:-1]):
            acc = _hash_node(peak_hash, acc)
        return acc

    def root_at(self, size: int) -> bytes:
        """Root of the prefix of *size* leaves (used for consistency roots)."""
        if size < 0 or size > self.size:
            raise ValueError(f"size {size} outside [0, {self.size}]")
        if size == self.size:
            return self.root()
        return self._mth(0, size)

    def root_hex(self) -> str:
        return self.root().hex()

    # -- range hashing ----------------------------------------------------
    def _mth(self, lo: int, hi: int) -> bytes:
        """MTH of leaves[lo:hi] using the RFC split rule."""
        n = hi - lo
        if n == 0:
            return EMPTY_ROOT
        if n == 1:
            return self._leaves[lo]
        k = largest_power_of_two_less_than(n)
        return _hash_node(self._mth(lo, lo + k), self._mth(lo + k, hi))

    def _subtree_hash(self, lo: int, size: int) -> bytes:
        return self._mth(lo, lo + size)

    # -- inclusion proofs -------------------------------------------------
    def inclusion_proof(self, index: int, size: int | None = None) -> list[bytes]:
        """RFC 6962 audit path for *index* in the tree of *size* leaves."""
        size = self.size if size is None else size
        if size < 1 or size > self.size:
            raise ValueError("invalid tree size")
        if index < 0 or index >= size:
            raise ValueError("leaf index out of range")
        return self._path(index, 0, size)

    def _path(self, m: int, lo: int, n: int) -> list[bytes]:
        if n == 1:
            return []
        k = largest_power_of_two_less_than(n)
        if m < k:
            return self._path(m, lo, k) + [self._mth(lo + k, lo + n)]
        return self._path(m - k, lo + k, n - k) + [self._mth(lo, lo + k)]

    # -- consistency proofs ----------------------------------------------
    def consistency_proof(self, old_size: int, new_size: int | None = None) -> list[bytes]:
        """RFC 6962 PROOF(old_size, D[new_size]) (empty when sizes match)."""
        new_size = self.size if new_size is None else new_size
        if not (0 <= old_size <= new_size <= self.size):
            raise ValueError("need 0 <= old_size <= new_size <= tree size")
        if old_size == new_size:
            return []
        if old_size == 0:
            return []
        return self._subproof(old_size, 0, new_size, True)

    def _subproof(self, m: int, lo: int, n: int, complete: bool) -> list[bytes]:
        if m == n:
            return [] if complete else [self._mth(lo, lo + n)]
        k = largest_power_of_two_less_than(n)
        if m <= k:
            return self._subproof(m, lo, k, complete) + [self._mth(lo + k, lo + n)]
        return self._subproof(m - k, lo + k, n - k, False) + [self._mth(lo, lo + k)]


# ---------------------------------------------------------------------------
# Verification functions are deliberately standalone: an auditor needs only
# a root, a proof and the record bytes -- not this tree object or the server.
# ---------------------------------------------------------------------------


def verify_inclusion(
    leaf_hash: bytes,
    index: int,
    size: int,
    proof: Sequence[bytes],
    root: bytes,
) -> bool:
    """Verify an RFC 6962 inclusion proof (RFC 9162 Section 2.1.3.2)."""
    if size <= 0 or index < 0 or index >= size:
        return False
    if len(leaf_hash) != 32 or len(root) != 32:
        return False
    if any(len(node) != 32 for node in proof):
        return False
    fn, sn = index, size - 1
    r = leaf_hash
    for p in proof:
        if sn == 0:
            return False
        if (fn & 1) == 1 or fn == sn:
            r = _hash_node(p, r)
            if (fn & 1) == 0:
                while fn != 0 and (fn & 1) == 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = _hash_node(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root


def verify_consistency(
    old_size: int,
    new_size: int,
    old_root: bytes,
    new_root: bytes,
    proof: Sequence[bytes],
) -> bool:
    """Verify an RFC 6962 consistency proof (RFC 9162 Section 2.1.4.2)."""
    if old_size < 0 or new_size < 0 or old_size > new_size:
        return False
    if len(old_root) != 32 or len(new_root) != 32:
        return False
    if any(len(node) != 32 for node in proof):
        return False
    if old_size == new_size:
        return len(proof) == 0 and old_root == new_root
    if old_size == 0:
        # The empty tree is a prefix of every tree; RFC 9162 only defines
        # 0 < first < second, so this is our documented extension.
        return len(proof) == 0 and old_root == EMPTY_ROOT
    if len(proof) == 0:
        return False

    path = list(proof)
    if old_size & (old_size - 1) == 0:  # exact power of two
        path.insert(0, old_root)

    fn = old_size - 1
    sn = new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1

    fr = path[0]
    sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if (fn & 1) == 1 or fn == sn:
            fr = _hash_node(c, fr)
            sr = _hash_node(c, sr)
            if (fn & 1) == 0:
                while fn != 0 and (fn & 1) == 0:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = _hash_node(sr, c)
        fn >>= 1
        sn >>= 1

    return fr == old_root and sr == new_root and sn == 0


def hash_leaf(data: bytes) -> bytes:
    """Public helper: RFC 6962 leaf hash of raw data."""
    return _hash_leaf_bytes(data)
