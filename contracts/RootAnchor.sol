// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title RootAnchor
/// @notice Minimal append-only anchor for DIMF signed Merkle tree roots.
/// @dev This contract is intentionally small: it stores a root, the tree size
///      (called `sequence` in the ABI) and a timestamp.  It does not verify the
///      Ed25519 tree-head signature; that is done off-chain by auditors.  The
///      contract only makes the root history public and append-only.
contract RootAnchor {
    struct Anchor {
        uint64 sequence;   // tree size at the time of anchoring
        bytes32 root;      // RFC 6962 Merkle root
        uint64 timestamp;  // milliseconds since Unix epoch (log's clock)
    }

    address public immutable owner;
    Anchor[] private _anchors;

    event Anchored(
        uint256 indexed index,
        uint64 indexed sequence,
        bytes32 root,
        uint64 timestamp
    );

    error NotOwner();
    error ZeroRoot();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    /// @notice Append one anchored root.
    /// @param sequence Tree size that produced `root` (not required to be +1).
    /// @param root RFC 6962 Merkle root.
    /// @param timestamp Log timestamp in milliseconds.
    function anchor(uint64 sequence, bytes32 root, uint64 timestamp) external onlyOwner {
        if (root == bytes32(0)) revert ZeroRoot();
        uint256 index = _anchors.length;
        _anchors.push(Anchor({sequence: sequence, root: root, timestamp: timestamp}));
        emit Anchored(index, sequence, root, timestamp);
    }

    /// @notice Number of anchored roots.
    function count() external view returns (uint256) {
        return _anchors.length;
    }

    /// @notice Read anchor `i` (0-based, oldest first).
    function get(uint256 i) external view returns (uint64 sequence, bytes32 root, uint64 timestamp) {
        Anchor storage a = _anchors[i];
        return (a.sequence, a.root, a.timestamp);
    }
}
