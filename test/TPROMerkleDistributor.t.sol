// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console2} from "forge-std/Test.sol";
import {TPRO} from "../contracts/TPRO.sol";
import {TPROMerkleDistributor} from "../contracts/TPROMerkleDistributor.sol";
import {TestMerkle} from "./utils/TestMerkle.sol";

/// @dev A holder that is a CONTRACT at its address: the row is claimable by the
///      contract itself (msg.sender == account), documenting the deliberate
///      absence of any tx.origin / EOA check.
contract ContractHolder {
    function claimMyRow(TPROMerkleDistributor d, uint256 amount, bytes32[] calldata proof) external {
        d.claim(address(this), amount, proof);
    }
}

/// @dev A "token" whose balance and supply can disagree: the distributor's wiring
///      proof must require BOTH balanceOf(this) == genesis AND totalSupply() == genesis
///      (round 4, R1-6 - the whole supply, and nothing but the supply, must be here).
contract FakeSupplyToken {
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;

    function set(address who, uint256 bal, uint256 supply) external {
        balanceOf[who] = bal;
        totalSupply = supply;
    }
}

contract TPROMerkleDistributorTest is Test {
    using TestMerkle for bytes32[];

    uint256 internal constant SUNSET_IN = 1096 days; // 2026-09-22 -> 2029-09-22

    TestMerkle.Row[] internal rows;
    bytes32[] internal tree;
    uint256[] internal li;
    uint256 internal genesis;
    TPRO internal token;
    TPROMerkleDistributor internal dist;

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal carol = makeAddr("carol");
    address internal dave = makeAddr("dave");
    address internal stranger = makeAddr("stranger");

    event Claimed(address indexed account, uint256 amount);
    event Sunset(uint256 burned);

    function setUp() public {
        rows.push(TestMerkle.Row(alice, 1_000e18));
        rows.push(TestMerkle.Row(bob, 250_000_000e18));
        rows.push(TestMerkle.Row(carol, 1)); // one wei row
        rows.push(TestMerkle.Row(dave, 9_690_413_123456789012345678));
        _deployFromRows();
    }

    // ---------------------------------------------------------------- helpers

    /// @dev Mirrors script/DeployV3.s.sol: predict the distributor from the
    ///      deployer's next nonce, mint the token to it, then deploy it.
    function _deployFromRows() internal {
        (tree, li) = TestMerkle.build(rows);
        genesis = 0;
        for (uint256 i; i < rows.length; ++i) {
            genesis += rows[i].amount;
        }
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        token = new TPRO(predicted, genesis);
        dist = new TPROMerkleDistributor(address(token), tree.root(), genesis, block.timestamp + SUNSET_IN);
        assertEq(address(dist), predicted, "prediction");
    }

    function _proof(uint256 i) internal view returns (bytes32[] memory) {
        return tree.proof(li[i]);
    }

    function _claim(uint256 i) internal {
        vm.prank(rows[i].account);
        dist.claim(rows[i].account, rows[i].amount, _proof(i));
    }

    // ----------------------------------------------------------- deployment

    function test_wiring() public view {
        assertEq(address(dist.token()), address(token));
        assertEq(dist.merkleRoot(), tree.root());
        assertEq(dist.genesis(), genesis);
        assertEq(dist.sunsetTime(), block.timestamp + SUNSET_IN);
        assertEq(dist.totalClaimed(), 0);
        assertEq(dist.sunsetBurned(), 0);
        assertEq(token.balanceOf(address(dist)), genesis);
        assertEq(token.totalSupply(), genesis);
        assertEq(token.balanceOf(address(this)), 0);
    }

    function test_constructorRejectsZeroRoot() public {
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        TPRO t = new TPRO(predicted, genesis);
        vm.expectRevert(TPROMerkleDistributor.ZeroRoot.selector);
        new TPROMerkleDistributor(address(t), bytes32(0), genesis, block.timestamp + 1);
    }

    function test_constructorRejectsSunsetNotInFuture() public {
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        TPRO t = new TPRO(predicted, genesis);
        vm.expectRevert(TPROMerkleDistributor.BadSunsetTime.selector);
        new TPROMerkleDistributor(address(t), tree.root(), genesis, block.timestamp);
        vm.expectRevert(TPROMerkleDistributor.BadSunsetTime.selector);
        new TPROMerkleDistributor(address(t), tree.root(), genesis, block.timestamp - 1);
    }

    function test_constructorRejectsGenesisMismatch() public {
        // (a) token minted to somebody else: the distributor holds nothing
        TPRO elsewhere = new TPRO(stranger, genesis);
        vm.expectRevert(TPROMerkleDistributor.GenesisMismatch.selector);
        new TPROMerkleDistributor(address(elsewhere), tree.root(), genesis, block.timestamp + 1);

        // (b) right recipient, wrong genesis figure
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        TPRO t = new TPRO(predicted, genesis);
        vm.expectRevert(TPROMerkleDistributor.GenesisMismatch.selector);
        new TPROMerkleDistributor(address(t), tree.root(), genesis - 1, block.timestamp + 1);

        // (c) zero genesis never passes, even with a matching zero balance
        TPRO empty = new TPRO(stranger, genesis);
        vm.expectRevert(TPROMerkleDistributor.GenesisMismatch.selector);
        new TPROMerkleDistributor(address(empty), tree.root(), 0, block.timestamp + 1);
    }

    function test_constructorRejectsSupplyThatExceedsTheBalance() public {
        // balance == genesis but the token has MORE supply elsewhere: not "the whole supply here"
        // (a reverted CREATE still consumes the creator's nonce, so re-predict before each attempt)
        FakeSupplyToken fake = new FakeSupplyToken();
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)));
        fake.set(predicted, genesis, genesis + 1);
        vm.expectRevert(TPROMerkleDistributor.GenesisMismatch.selector);
        new TPROMerkleDistributor(address(fake), tree.root(), genesis, block.timestamp + 1);
        // supply BELOW the balance is just as wrong (an inconsistent token)
        predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)));
        fake.set(predicted, genesis, genesis - 1);
        vm.expectRevert(TPROMerkleDistributor.GenesisMismatch.selector);
        new TPROMerkleDistributor(address(fake), tree.root(), genesis, block.timestamp + 1);
        // and the consistent case passes with the fake too (the check is about the numbers)
        predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)));
        fake.set(predicted, genesis, genesis);
        TPROMerkleDistributor d = new TPROMerkleDistributor(address(fake), tree.root(), genesis, block.timestamp + 1);
        assertEq(address(d), predicted);
    }

    // ---------------------------------------------------------------- claims

    function test_claimHappyPath() public {
        vm.expectEmit(true, false, false, true, address(dist));
        emit Claimed(bob, rows[1].amount);
        _claim(1);
        assertEq(token.balanceOf(bob), rows[1].amount);
        assertEq(token.balanceOf(address(dist)), genesis - rows[1].amount);
        assertEq(dist.totalClaimed(), rows[1].amount);
        assertTrue(dist.isClaimed(bob));
        assertFalse(dist.isClaimed(alice));
        assertEq(token.totalSupply(), genesis); // a claim never changes supply
    }

    function test_everyRowClaimsExactlyOnce() public {
        for (uint256 i; i < rows.length; ++i) {
            _claim(i);
            assertEq(token.balanceOf(rows[i].account), rows[i].amount);
        }
        assertEq(dist.totalClaimed(), genesis);
        assertEq(token.balanceOf(address(dist)), 0);
        for (uint256 i; i < rows.length; ++i) {
            vm.prank(rows[i].account);
            vm.expectRevert(TPROMerkleDistributor.AlreadyClaimed.selector);
            dist.claim(rows[i].account, rows[i].amount, _proof(i));
        }
    }

    function test_claimRevertsWhenSenderIsNotTheRow() public {
        bytes32[] memory p = _proof(0);
        vm.prank(stranger);
        vm.expectRevert(TPROMerkleDistributor.NotYourRow.selector);
        dist.claim(alice, rows[0].amount, p);
        // ...including another legitimate row holder
        vm.prank(bob);
        vm.expectRevert(TPROMerkleDistributor.NotYourRow.selector);
        dist.claim(alice, rows[0].amount, p);
    }

    function test_claimRevertsOnWrongAmount() public {
        bytes32[] memory p = _proof(0);
        vm.startPrank(alice);
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(alice, rows[0].amount + 1, p);
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(alice, rows[0].amount - 1, p); // partial claims do not exist
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(alice, 0, p);
        vm.stopPrank();
        assertFalse(dist.isClaimed(alice));
    }

    function test_claimRevertsOnTamperedOrForeignProof() public {
        bytes32[] memory p = _proof(0);
        p[0] = bytes32(uint256(p[0]) ^ 1);
        vm.prank(alice);
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(alice, rows[0].amount, p);

        // bob's proof for alice's row
        vm.prank(alice);
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(alice, rows[0].amount, _proof(1));

        // empty proof against a multi-row tree
        bytes32[] memory none;
        vm.prank(alice);
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(alice, rows[0].amount, none);

        // a proof that is one element too long
        bytes32[] memory longer = new bytes32[](p.length + 1);
        for (uint256 i; i < p.length; ++i) {
            longer[i] = _proof(0)[i];
        }
        vm.prank(alice);
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(alice, rows[0].amount, longer);
    }

    function test_claimRevertsForAddressNotInTable() public {
        vm.prank(stranger);
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(stranger, 1, _proof(0));
    }

    function test_claimClosesExactlyAtSunsetTime() public {
        vm.warp(dist.sunsetTime() - 1);
        _claim(0); // last second still open
        vm.warp(dist.sunsetTime());
        vm.prank(bob);
        vm.expectRevert(TPROMerkleDistributor.ClaimWindowClosed.selector);
        dist.claim(bob, rows[1].amount, _proof(1));
        vm.warp(dist.sunsetTime() + 365 days);
        vm.prank(bob);
        vm.expectRevert(TPROMerkleDistributor.ClaimWindowClosed.selector);
        dist.claim(bob, rows[1].amount, _proof(1));
    }

    function test_contractAtItsOwnAddressCanClaim() public {
        ContractHolder holder = new ContractHolder();
        rows.push(TestMerkle.Row(address(holder), 42e18));
        _deployFromRows();
        uint256 i = rows.length - 1;
        holder.claimMyRow(dist, rows[i].amount, _proof(i));
        assertEq(token.balanceOf(address(holder)), 42e18);
        // ...but nobody else can claim FOR it
        vm.prank(stranger);
        vm.expectRevert(TPROMerkleDistributor.NotYourRow.selector);
        dist.claim(address(holder), rows[i].amount, _proof(i));
    }

    function test_singleRowTableHasEmptyProof() public {
        delete rows;
        rows.push(TestMerkle.Row(alice, 7e18));
        _deployFromRows();
        bytes32[] memory p = _proof(0);
        assertEq(p.length, 0);
        assertEq(dist.merkleRoot(), TestMerkle.leaf(alice, 7e18));
        _claim(0);
        assertEq(token.balanceOf(alice), 7e18);
        assertEq(token.balanceOf(address(dist)), 0);
    }

    function test_zeroAmountRowIsHarmless() public {
        // The tool never emits zero rows; if one existed it would be claimable
        // once and move nothing.
        rows.push(TestMerkle.Row(stranger, 0));
        _deployFromRows();
        uint256 i = rows.length - 1;
        _claim(i);
        assertTrue(dist.isClaimed(stranger));
        assertEq(token.balanceOf(stranger), 0);
        assertEq(dist.totalClaimed(), 0);
    }

    // ---------------------------------------------------------------- sunset

    function test_sunsetTooEarly() public {
        vm.expectRevert(TPROMerkleDistributor.SunsetTooEarly.selector);
        dist.sunset();
        vm.warp(dist.sunsetTime() - 1);
        vm.expectRevert(TPROMerkleDistributor.SunsetTooEarly.selector);
        dist.sunset();
    }

    function test_sunsetBurnsExactlyTheRemainder() public {
        _claim(0);
        _claim(2);
        uint256 remainder = genesis - rows[0].amount - rows[2].amount;
        vm.warp(dist.sunsetTime());
        vm.expectEmit(false, false, false, true, address(dist));
        emit Sunset(remainder);
        vm.prank(stranger); // anyone
        dist.sunset();
        assertEq(token.balanceOf(address(dist)), 0);
        assertEq(dist.sunsetBurned(), remainder);
        assertEq(token.totalSupply(), genesis - remainder);
        assertEq(token.totalSupply(), dist.totalClaimed());
        // the public invariant
        assertEq(token.balanceOf(address(dist)) + dist.totalClaimed() + dist.sunsetBurned(), genesis);
    }

    function test_sunsetIsRepeatableAndBurnsLaterDonations() public {
        _claim(1);
        vm.warp(dist.sunsetTime() + 1);
        dist.sunset();
        uint256 burnedFirst = dist.sunsetBurned();
        // second call with nothing to burn: a no-op event, no revert
        vm.expectEmit(false, false, false, true, address(dist));
        emit Sunset(0);
        dist.sunset();
        assertEq(dist.sunsetBurned(), burnedFirst);
        // bob sends some of his claimed tokens back: burnable at the next call
        vm.prank(bob);
        token.transfer(address(dist), 5e18);
        dist.sunset();
        assertEq(dist.sunsetBurned(), burnedFirst + 5e18);
        assertEq(token.balanceOf(address(dist)), 0);
        assertEq(token.totalSupply(), genesis - burnedFirst - 5e18);
    }

    function test_donationBeforeSunsetStaysUntilSunset() public {
        _claim(1);
        vm.prank(bob);
        token.transfer(address(dist), 1e18);
        // the invariant holds with the donation on the left side
        assertEq(token.balanceOf(address(dist)) + dist.totalClaimed() + dist.sunsetBurned(), genesis + 1e18);
        // no function can move it before sunset (there is none) - the claim path is unaffected
        _claim(0);
        vm.warp(dist.sunsetTime());
        dist.sunset();
        assertEq(dist.sunsetBurned(), genesis - rows[0].amount - rows[1].amount + 1e18);
    }

    // ------------------------------------------------------------------ fuzz

    function testFuzz_randomTableClaimsExactlyOnce(uint8 nRaw, uint256 seed) public {
        uint256 n = bound(uint256(nRaw), 2, 64);
        delete rows;
        for (uint256 i; i < n; ++i) {
            address a = address(uint160(uint256(keccak256(abi.encode(seed, i, "acct")))));
            uint256 amt = bound(uint256(keccak256(abi.encode(seed, i, "amt"))), 1, 1e27);
            rows.push(TestMerkle.Row(a, amt));
        }
        _deployFromRows();
        uint256 claimed;
        for (uint256 i; i < n; ++i) {
            _claim(i);
            claimed += rows[i].amount;
            assertEq(token.balanceOf(rows[i].account), rows[i].amount);
            assertEq(dist.totalClaimed(), claimed);
            vm.prank(rows[i].account);
            vm.expectRevert(TPROMerkleDistributor.AlreadyClaimed.selector);
            dist.claim(rows[i].account, rows[i].amount, _proof(i));
        }
        assertEq(dist.totalClaimed(), genesis);
        assertEq(token.balanceOf(address(dist)), 0);
    }

    function testFuzz_amountMustMatchTheRow(uint256 wrong) public {
        vm.assume(wrong != rows[1].amount);
        vm.prank(bob);
        vm.expectRevert(TPROMerkleDistributor.InvalidProof.selector);
        dist.claim(bob, wrong, _proof(1));
    }

    function testFuzz_claimNeverAfterSunset(uint256 dt) public {
        dt = bound(dt, 0, 100 * 365 days);
        vm.warp(dist.sunsetTime() + dt);
        vm.prank(alice);
        vm.expectRevert(TPROMerkleDistributor.ClaimWindowClosed.selector);
        dist.claim(alice, rows[0].amount, _proof(0));
    }

    // ----------------------------------------------------------------- KAT

    /// @dev Known-answer fixture produced by scripts/snapshot/merkle.py and
    ///      cross-checked against the OZ JS library. The fixture is REQUIRED: a
    ///      missing file FAILS this test (external review 2026-09-08, H03 - a skip
    ///      used to pass silently and the cross-builder proof disappeared with it).
    function test_knownAnswerFixture() public {
        string memory path = "test/fixtures/merkle-kat.json";
        require(vm.exists(path), "KAT fixture test/fixtures/merkle-kat.json is missing - regenerate it with scripts/snapshot/merkle.py");
        string memory json = vm.readFile(path);
        bytes32 fixtureRoot = vm.parseJsonBytes32(json, ".root");
        uint256 n;
        while (vm.keyExistsJson(json, string.concat(".rows[", vm.toString(n), "].address"))) {
            ++n;
        }
        require(n > 0, "fixture has no rows");
        delete rows;
        bytes32[][] memory proofs = new bytes32[][](n);
        for (uint256 i; i < n; ++i) {
            string memory base = string.concat(".rows[", vm.toString(i), "]");
            address a = vm.parseJsonAddress(json, string.concat(base, ".address"));
            // amounts are decimal STRINGS in the fixture (JSON numbers cannot carry wei safely)
            uint256 amt = vm.parseUint(vm.parseJsonString(json, string.concat(base, ".amount")));
            proofs[i] = vm.parseJsonBytes32Array(json, string.concat(base, ".proof"));
            rows.push(TestMerkle.Row(a, amt));
        }
        // (1) the Solidity builder reproduces the Python/JS root for the same rows
        (bytes32[] memory t,) = TestMerkle.build(rows);
        assertEq(t.root(), fixtureRoot, "root mismatch between builders");
        // (2) every published proof verifies on-chain and a full claim round-trip works
        _deployFromRows();
        assertEq(dist.merkleRoot(), fixtureRoot);
        for (uint256 i; i < n; ++i) {
            vm.prank(rows[i].account);
            dist.claim(rows[i].account, rows[i].amount, proofs[i]);
            assertEq(token.balanceOf(rows[i].account), rows[i].amount);
        }
        assertEq(dist.totalClaimed(), genesis);
        console2.log("KAT fixture verified, rows:", n);
    }
}
