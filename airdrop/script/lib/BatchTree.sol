// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

/// @notice Reference implementation of the batch tree used by CommittedBatchAirdrop, for scripts
///         and tests. `tools/common.py` implements the same construction in Python; the two must
///         always agree, and `script/VerifyRoot.s.sol` checks that they do.
///
/// Construction:
///   - leaf(i) = keccak256(keccak256(abi.encode(i, recipients_i, amounts_i)))
///   - level 0 is the list of leaves in batch order;
///   - each next level pairs neighbours (0,1), (2,3), ... and hashes each pair smaller-first;
///   - an unpaired last node is carried up unchanged;
///   - the root is the single node of the top level.
///   - the proof for leaf i is its sibling at every level where it has one.
library BatchTree {
    function leaf(uint256 batchIndex, address[] memory recipients, uint256[] memory amounts)
        internal
        pure
        returns (bytes32)
    {
        return keccak256(bytes.concat(keccak256(abi.encode(batchIndex, recipients, amounts))));
    }

    function hashPair(bytes32 a, bytes32 b) internal pure returns (bytes32) {
        return a < b ? keccak256(abi.encodePacked(a, b)) : keccak256(abi.encodePacked(b, a));
    }

    /// @notice Every level of the tree, leaves first, root last.
    function levels(bytes32[] memory leaves) internal pure returns (bytes32[][] memory out) {
        require(leaves.length > 0, "no leaves");
        uint256 depth = 1;
        for (uint256 n = leaves.length; n > 1; n = (n + 1) / 2) {
            depth++;
        }
        out = new bytes32[][](depth);
        out[0] = leaves;
        for (uint256 d = 1; d < depth; d++) {
            bytes32[] memory prev = out[d - 1];
            bytes32[] memory next = new bytes32[]((prev.length + 1) / 2);
            for (uint256 i = 0; i < next.length; i++) {
                uint256 l = 2 * i;
                next[i] = l + 1 < prev.length ? hashPair(prev[l], prev[l + 1]) : prev[l];
            }
            out[d] = next;
        }
    }

    function root(bytes32[] memory leaves) internal pure returns (bytes32) {
        bytes32[][] memory lv = levels(leaves);
        return lv[lv.length - 1][0];
    }

    function proof(bytes32[][] memory lv, uint256 index) internal pure returns (bytes32[] memory out) {
        bytes32[] memory tmp = new bytes32[](lv.length);
        uint256 n;
        uint256 idx = index;
        for (uint256 d = 0; d + 1 < lv.length; d++) {
            uint256 sib = idx ^ 1;
            if (sib < lv[d].length) tmp[n++] = lv[d][sib];
            idx >>= 1;
        }
        out = new bytes32[](n);
        for (uint256 i = 0; i < n; i++) {
            out[i] = tmp[i];
        }
    }

    function verify(bytes32[] memory proof_, bytes32 root_, bytes32 leaf_) internal pure returns (bool) {
        bytes32 h = leaf_;
        for (uint256 i = 0; i < proof_.length; i++) {
            h = hashPair(h, proof_[i]);
        }
        return h == root_;
    }
}
