// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @dev Test-side reimplementation of OpenZeppelin's `@openzeppelin/merkle-tree`
///      StandardMerkleTree (the format the snapshot tool publishes):
///      leaf = keccak256(bytes.concat(keccak256(abi.encode(address, uint256)))),
///      leaves sorted ASCENDING by hash, tree array of length 2n-1 with the k-th
///      smallest leaf at index 2n-2-k, node i = hash of the SORTED pair of its
///      children (2i+1, 2i+2), proof = the siblings on the path to the root.
///      Roots built here must equal the roots of the Python tool and the JS
///      library for the same rows (the KAT fixture test checks that).
library TestMerkle {
    struct Row {
        address account;
        uint256 amount;
    }

    function leaf(address account, uint256 amount) internal pure returns (bytes32) {
        return keccak256(bytes.concat(keccak256(abi.encode(account, amount))));
    }

    function hashPair(bytes32 a, bytes32 b) internal pure returns (bytes32) {
        return a < b ? keccak256(abi.encodePacked(a, b)) : keccak256(abi.encodePacked(b, a));
    }

    /// @return tree      the full node array (root at index 0)
    /// @return leafIndex for every row i, the index of its leaf inside `tree`
    function build(Row[] memory rows) internal pure returns (bytes32[] memory tree, uint256[] memory leafIndex) {
        uint256 n = rows.length;
        require(n > 0, "empty table");
        bytes32[] memory leaves = new bytes32[](n);
        uint256[] memory order = new uint256[](n);
        for (uint256 i; i < n; ++i) {
            leaves[i] = leaf(rows[i].account, rows[i].amount);
            order[i] = i;
        }
        // insertion sort of row indices by leaf hash, ascending
        for (uint256 i = 1; i < n; ++i) {
            uint256 j = i;
            while (j > 0 && leaves[order[j - 1]] > leaves[order[j]]) {
                (order[j - 1], order[j]) = (order[j], order[j - 1]);
                --j;
            }
        }
        tree = new bytes32[](2 * n - 1);
        leafIndex = new uint256[](n);
        for (uint256 k; k < n; ++k) {
            uint256 idx = tree.length - 1 - k;
            tree[idx] = leaves[order[k]];
            leafIndex[order[k]] = idx;
        }
        // internal nodes are indices 0 .. n-2, filled bottom-up
        for (uint256 i = n - 1; i > 0;) {
            --i;
            tree[i] = hashPair(tree[2 * i + 1], tree[2 * i + 2]);
        }
    }

    function root(bytes32[] memory tree) internal pure returns (bytes32) {
        return tree[0];
    }

    function proof(bytes32[] memory tree, uint256 idx) internal pure returns (bytes32[] memory p) {
        uint256 depth;
        for (uint256 j = idx; j > 0; j = (j - 1) / 2) {
            ++depth;
        }
        p = new bytes32[](depth);
        uint256 k;
        for (uint256 j = idx; j > 0; j = (j - 1) / 2) {
            uint256 sibling = (j % 2 == 1) ? j + 1 : j - 1;
            p[k++] = tree[sibling];
        }
    }
}
