// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Round 4 / Reviewer 1 - known-answer test against the REAL tool output.
// STATUS: PASSES against the current code. The fixture
// test/fixtures/real-table-kat.json is written by scripts/snapshot/make_kat.py
// from a published table directory (today: the 2026-09-04 dry-run table
// tmp/snapshot-25901028-93189774/table/): index.json root + totalWei and 24 rows
// with the proofs exactly as published in table/proofs/<xx>.json (8 largest
// rows, 8 smallest, 8 seeded-random, plus the LP row). It proves
// that the PUBLISHED artefacts (root, proof shards, amounts) and the contract's
// leaf encoding agree, with genesis = the table total (not the sample sum).
// The existing KAT (merkle-kat.json) uses 8 synthetic rows and never touches
// the real table. Regenerate this fixture from the FINAL table on the snapshot day before
// the broadcast (finding R1-8).
import {Test, console2} from "forge-std/Test.sol";
import {TPRO} from "../../contracts/TPRO.sol";
import {TPROMerkleDistributor} from "../../contracts/TPROMerkleDistributor.sol";

contract R1_RealTableKATTest is Test {
    string internal constant PATH = "test/fixtures/real-table-kat.json";

    struct Row {
        address account;
        uint256 amount;
        bytes32[] proof;
    }

    Row[] internal rows;
    bytes32 internal root;
    uint256 internal genesis;
    TPRO internal token;
    TPROMerkleDistributor internal dist;

    function setUp() public {
        string memory json = vm.readFile(PATH);
        root = vm.parseJsonBytes32(json, ".root");
        genesis = vm.parseUint(vm.parseJsonString(json, ".totalWei"));
        uint256 n;
        while (vm.keyExistsJson(json, string.concat(".rows[", vm.toString(n), "].address"))) {
            ++n;
        }
        require(n >= 20, "fixture too small");
        for (uint256 i; i < n; ++i) {
            string memory base = string.concat(".rows[", vm.toString(i), "]");
            rows.push(
                Row(
                    vm.parseJsonAddress(json, string.concat(base, ".address")),
                    vm.parseUint(vm.parseJsonString(json, string.concat(base, ".amount"))),
                    vm.parseJsonBytes32Array(json, string.concat(base, ".proof"))
                )
            );
        }
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        token = new TPRO(predicted, genesis);
        dist = new TPROMerkleDistributor(address(token), root, genesis, block.timestamp + 1096 days);
        assertEq(address(dist), predicted);
    }

    /// @dev The KAT fixture and the index fixture the deploy script reads must describe the SAME table
    ///      (both are written by scripts/snapshot/make_kat.py from one table directory).
    function test_realTableRootAndGenesisMatchTheIndexFixture() public view {
        string memory idx = vm.readFile("test/fixtures/real-table-index.json");
        assertEq(dist.merkleRoot(), vm.parseJsonBytes32(idx, ".merkleRoot"));
        assertEq(dist.genesis(), vm.parseUint(vm.parseJsonString(idx, ".totalWei")));
        assertEq(token.totalSupply(), dist.genesis());
        assertEq(rows.length, 24);
    }

    function test_everyPublishedProofClaimsItsRow() public {
        uint256 sum;
        for (uint256 i; i < rows.length; ++i) {
            vm.prank(rows[i].account);
            dist.claim(rows[i].account, rows[i].amount, rows[i].proof);
            assertEq(token.balanceOf(rows[i].account), rows[i].amount);
            assertTrue(dist.isClaimed(rows[i].account));
            sum += rows[i].amount;
        }
        assertEq(dist.totalClaimed(), sum);
        assertEq(token.balanceOf(address(dist)), genesis - sum);
        console2.log("real-table rows verified on-chain:", rows.length);
    }

    function test_publishedProofRejectsAnyOtherAmountOrRow() public {
        for (uint256 i; i < rows.length; ++i) {
            vm.startPrank(rows[i].account);
            vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
            dist.claim(rows[i].account, rows[i].amount + 1, rows[i].proof);
            if (rows[i].amount > 0) {
                vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
                dist.claim(rows[i].account, rows[i].amount - 1, rows[i].proof);
            }
            // the neighbour's published proof never opens my row
            uint256 j = (i + 1) % rows.length;
            vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
            dist.claim(rows[i].account, rows[i].amount, rows[j].proof);
            vm.stopPrank();
        }
    }

    function test_nobodyElseCanClaimARealRow() public {
        address thief = makeAddr("thief");
        for (uint256 i; i < rows.length; ++i) {
            vm.prank(thief);
            vm.expectRevert(TPROMerkleDistributor.NotYourRow.selector);
            dist.claim(rows[i].account, rows[i].amount, rows[i].proof);
        }
    }
}
