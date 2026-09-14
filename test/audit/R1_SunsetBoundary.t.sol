// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Round 4 / Reviewer 1 - the claim/sunset time gates are exact complements.
// STATUS: PASSES against the current code (regression guard for the `>=` / `<`
// pair in TPROMerkleDistributor.claim / sunset). Fuzzed densely around the
// boundary and over the whole practical timestamp range; every failure is
// checked to be the TIME error and nothing else.
import {Test} from "forge-std/Test.sol";
import {TPRO} from "../../contracts/TPRO.sol";
import {TPROMerkleDistributor} from "../../contracts/TPROMerkleDistributor.sol";
import {TestMerkle} from "../utils/TestMerkle.sol";

contract R1_SunsetBoundaryTest is Test {
    using TestMerkle for bytes32[];

    TestMerkle.Row[] internal rows;
    bytes32[] internal tree;
    uint256[] internal li;
    TPRO internal token;
    TPROMerkleDistributor internal dist;
    uint256 internal genesis;
    uint256 internal S; // sunsetTime

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    function setUp() public {
        rows.push(TestMerkle.Row(alice, 5e18));
        rows.push(TestMerkle.Row(bob, 7e18));
        (tree, li) = TestMerkle.build(rows);
        genesis = 12e18;
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        token = new TPRO(predicted, genesis);
        S = block.timestamp + 1096 days;
        dist = new TPROMerkleDistributor(address(token), tree.root(), genesis, S);
        assertEq(address(dist), predicted);
    }

    function _tryClaim(uint256 i) internal returns (bool ok) {
        vm.prank(rows[i].account);
        bytes memory ret;
        (ok, ret) = address(dist).call(abi.encodeCall(dist.claim, (rows[i].account, rows[i].amount, tree.proof(li[i]))));
        if (!ok) assertEq(bytes4(ret), TPROMerkleDistributor.ClaimWindowClosed.selector, "claim failed for a non-time reason");
    }

    function _trySunset() internal returns (bool ok) {
        bytes memory ret;
        (ok, ret) = address(dist).call(abi.encodeCall(dist.sunset, ()));
        if (!ok) assertEq(bytes4(ret), TPROMerkleDistributor.SunsetTooEarly.selector, "sunset failed for a non-time reason");
    }

    /// dense around the boundary: +-30 days
    function testFuzz_gatesAreExactComplementsNearSunset(uint256 t) public {
        t = bound(t, S - 30 days, S + 30 days);
        vm.warp(t);
        bool claimOpen = _tryClaim(0);
        bool sunsetOpen = _trySunset();
        assertEq(claimOpen, t < S, "claim gate");
        assertEq(sunsetOpen, t >= S, "sunset gate");
        assertTrue(claimOpen != sunsetOpen, "exactly one gate open at any second");
    }

    /// the whole practical range (uint64 covers ~584 billion years)
    function testFuzz_gatesAreExactComplementsEverywhere(uint64 t64) public {
        uint256 t = bound(uint256(t64), block.timestamp, type(uint64).max);
        vm.warp(t);
        bool claimOpen = _tryClaim(0);
        bool sunsetOpen = _trySunset();
        assertEq(claimOpen, t < S);
        assertEq(sunsetOpen, t >= S);
    }

    /// same block as sunset, both orderings: a claim at t == S fails with the TIME
    /// error whether or not sunset() already ran (not with an insufficient balance).
    function test_sameBlockOrderingAtSunsetTime() public {
        vm.warp(S);
        vm.prank(alice);
        vm.expectRevert(TPROMerkleDistributor.ClaimWindowClosed.selector);
        dist.claim(alice, rows[0].amount, tree.proof(li[0]));

        dist.sunset(); // burns everything
        assertEq(token.balanceOf(address(dist)), 0);

        vm.prank(alice);
        vm.expectRevert(TPROMerkleDistributor.ClaimWindowClosed.selector);
        dist.claim(alice, rows[0].amount, tree.proof(li[0]));
    }

    /// the last second: claims succeed, sunset cannot pre-empt them in the same block
    function test_lastSecondBeforeSunset() public {
        vm.warp(S - 1);
        vm.expectRevert(TPROMerkleDistributor.SunsetTooEarly.selector);
        dist.sunset();
        assertTrue(_tryClaim(0));
        assertTrue(_tryClaim(1));
        assertEq(dist.totalClaimed(), genesis);
        vm.warp(S);
        dist.sunset(); // nothing left, no revert
        assertEq(dist.sunsetBurned(), 0);
    }
}
