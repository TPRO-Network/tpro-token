// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {TPRO} from "../contracts/TPRO.sol";
import {TPROMerkleDistributor} from "../contracts/TPROMerkleDistributor.sol";
import {TestMerkle} from "./utils/TestMerkle.sol";

/// @dev Random walk over: claims (valid rows, in any order), donations back to
///      the distributor by holders who already claimed, time warps across the
///      sunset, and sunset() calls. `fail_on_revert = true`, so every action is
///      guarded and must succeed when taken.
contract DistributorHandler is Test {
    using TestMerkle for bytes32[];

    TPRO public token;
    TPROMerkleDistributor public dist;
    TestMerkle.Row[] public rows;
    bytes32[] internal tree;
    uint256[] internal li;

    uint256 public donated; // tokens sent back to the distributor by claimers
    uint256 public ghostClaimed; // sum of successful claims
    uint256 public ghostBurned; // sum of successful sunset burns
    uint256 public lastClaimTs; // timestamp of the last successful claim
    uint256 public claimCalls;
    uint256 public sunsetCalls;
    mapping(address => uint256) public claimCount;

    constructor(
        TPRO token_,
        TPROMerkleDistributor dist_,
        TestMerkle.Row[] memory rows_,
        bytes32[] memory tree_,
        uint256[] memory li_
    ) {
        token = token_;
        dist = dist_;
        for (uint256 i; i < rows_.length; ++i) {
            rows.push(rows_[i]);
        }
        tree = tree_;
        li = li_;
    }

    function rowCount() external view returns (uint256) {
        return rows.length;
    }

    function claim(uint256 idx) external {
        idx = bound(idx, 0, rows.length - 1);
        TestMerkle.Row memory r = rows[idx];
        if (block.timestamp >= dist.sunsetTime() || dist.isClaimed(r.account)) return;
        vm.prank(r.account);
        dist.claim(r.account, r.amount, tree.proof(li[idx]));
        claimCount[r.account] += 1;
        ghostClaimed += r.amount;
        lastClaimTs = block.timestamp;
        claimCalls += 1;
    }

    function donate(uint256 idx, uint256 amount) external {
        idx = bound(idx, 0, rows.length - 1);
        address a = rows[idx].account;
        uint256 bal = token.balanceOf(a);
        if (bal == 0) return;
        amount = bound(amount, 1, bal);
        vm.prank(a);
        token.transfer(address(dist), amount);
        donated += amount;
    }

    function warp(uint256 dt) external {
        dt = bound(dt, 1, 400 days);
        vm.warp(block.timestamp + dt);
    }

    function sunset() external {
        if (block.timestamp < dist.sunsetTime()) return;
        uint256 bal = token.balanceOf(address(dist));
        dist.sunset();
        ghostBurned += bal;
        sunsetCalls += 1;
    }
}

contract TPROMerkleDistributorInvariantTest is Test {
    using TestMerkle for bytes32[];

    TPRO internal token;
    TPROMerkleDistributor internal dist;
    DistributorHandler internal handler;
    uint256 internal genesis;

    function setUp() public {
        TestMerkle.Row[] memory rows = new TestMerkle.Row[](12);
        for (uint256 i; i < rows.length; ++i) {
            rows[i] = TestMerkle.Row(
                address(uint160(uint256(keccak256(abi.encode("holder", i))))),
                bound(uint256(keccak256(abi.encode("amount", i))), 1, 300_000_000e18)
            );
            genesis += rows[i].amount;
        }
        (bytes32[] memory tree, uint256[] memory li) = TestMerkle.build(rows);
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        token = new TPRO(predicted, genesis);
        dist = new TPROMerkleDistributor(address(token), tree.root(), genesis, block.timestamp + 1096 days);
        handler = new DistributorHandler(token, dist, rows, tree, li);
        targetContract(address(handler));
    }

    /// balance + claimed + burned == genesis + donations (the public invariant, donation-adjusted)
    function invariant_accounting() public view {
        assertEq(
            token.balanceOf(address(dist)) + dist.totalClaimed() + dist.sunsetBurned(), genesis + handler.donated()
        );
    }

    function invariant_claimedNeverExceedsGenesis() public view {
        assertLe(dist.totalClaimed(), genesis);
        assertEq(dist.totalClaimed(), handler.ghostClaimed());
    }

    function invariant_supplyOnlyDropsBySunsetBurns() public view {
        assertEq(token.totalSupply(), genesis - dist.sunsetBurned());
        assertEq(dist.sunsetBurned(), handler.ghostBurned());
    }

    function invariant_noDoubleClaimsAndClaimedFlagsMatch() public view {
        uint256 n = handler.rowCount();
        for (uint256 i; i < n; ++i) {
            (address a,) = handler.rows(i);
            uint256 c = handler.claimCount(a);
            assertLe(c, 1);
            assertEq(dist.isClaimed(a), c == 1);
        }
    }

    function invariant_noClaimAtOrAfterSunset() public view {
        if (handler.claimCalls() > 0) assertLt(handler.lastClaimTs(), dist.sunsetTime());
    }

    function invariant_afterAnySunsetTheDistributorIsEmptyUntilNextDonation() public view {
        // after a sunset call, whatever the distributor holds arrived afterwards
        // (donations) - a sunset never leaves the pre-existing remainder behind.
        if (handler.sunsetCalls() > 0) {
            assertLe(token.balanceOf(address(dist)), handler.donated());
        }
    }
}
