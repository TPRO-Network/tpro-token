// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Round 4 / Reviewer 1 - evidence for finding R1-1 (wiring atomicity of the
// two-transaction CREATE deployment in script/DeployV3.s.sol).
//
// STATUS: all tests PASS against the current code. They do not show a bug in
// the contracts; they pin down EVM facts the deploy procedure relies on:
//  (1) CREATE (nonce prediction): a mined-but-reverted distributor creation
//      consumes the predicted address forever - the genesis minted to it in
//      tx1 becomes unreachable (no code, no key), and a retry can never land
//      there again (its constructor rejects the wiring: GenesisMismatch).
//  (2) CREATE2: a reverted creation does NOT consume the address, so the same
//      deployment can be retried at the predicted address later, and a stray
//      nonce-consuming transaction between tx1 and tx2 is harmless.
//  (3) The same holds when the CREATE2 goes through the canonical factory
//      0x4e59b44847b379578588920cA78FbF26c0B4956C, which is what a forge
//      script broadcast uses for `new X{salt: s}(...)`.
import {Test} from "forge-std/Test.sol";
import {TPRO} from "../../contracts/TPRO.sol";
import {TPROMerkleDistributor} from "../../contracts/TPROMerkleDistributor.sol";

contract Dummy {}

contract R1_WiringAtomicityTest is Test {
    bytes32 internal constant ROOT = bytes32(uint256(1));
    // the 2026-09-04 dry-run table total (tmp/snapshot-25901028-93189774/table/index.json)
    uint256 internal constant GENESIS = 1_121_662_711_442119974133225147;
    bytes32 internal constant SALT = keccak256("TPRO v3 distributor");
    // Arachnid deterministic-deployment-proxy runtime code (the CREATE2_FACTORY forge uses)
    bytes internal constant FACTORY_CODE =
        hex"7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe03601600081602082378035828234f58015156039578182fd5b8082525050506014600cf3";

    // ------------------------------------------------------------ (1) CREATE

    function test_create_minedButRevertedSecondTxBurnsThePredictedAddress() public {
        uint64 n = vm.getNonce(address(this));
        address predicted = vm.computeCreateAddress(address(this), n + 1);

        TPRO token = new TPRO(predicted, GENESIS); // tx1 mined fine
        assertEq(vm.getNonce(address(this)), n + 1);

        // tx2 mined but reverted (BadSunsetTime stands in for ANY constructor revert or OOG)
        try new TPROMerkleDistributor(address(token), ROOT, GENESIS, block.timestamp) returns (TPROMerkleDistributor) {
            fail();
        } catch {}

        // the failed creation consumed the nonce ...
        assertEq(vm.getNonce(address(this)), n + 2, "failed CREATE still consumes the nonce");
        // ... the predicted address has no code and holds the whole genesis
        assertEq(predicted.code.length, 0);
        assertEq(token.balanceOf(predicted), GENESIS);
        // a retry lands one address further and its own wiring proof rejects it
        vm.expectRevert(TPROMerkleDistributor.GenesisMismatch.selector);
        new TPROMerkleDistributor(address(token), ROOT, GENESIS, block.timestamp + 1096 days);
        // nothing can ever move the tokens out of `predicted`
        assertEq(token.totalSupply(), GENESIS);
    }

    function test_create_strayTxBetweenTheTwoTxsBurnsThePredictedAddress() public {
        uint64 n = vm.getNonce(address(this));
        address predicted = vm.computeCreateAddress(address(this), n + 1);
        TPRO token = new TPRO(predicted, GENESIS); // tx1
        Dummy stray = new Dummy(); // ANY other tx from the deployer (a sweep, a re-run, a second terminal)
        assertEq(address(stray), predicted, "the stray tx took the predicted address");
        assertEq(token.balanceOf(address(stray)), GENESIS); // genesis sits in a contract that cannot move it
        vm.expectRevert(TPROMerkleDistributor.GenesisMismatch.selector);
        new TPROMerkleDistributor(address(token), ROOT, GENESIS, block.timestamp + 1096 days);
    }

    // ----------------------------------------------------------- (2) CREATE2

    function test_create2_failedCreationDoesNotConsumeTheAddress_retryAndStrayTxAreHarmless() public {
        uint256 sunset = block.timestamp + 1096 days;
        uint64 n = vm.getNonce(address(this));
        address tokenPredicted = vm.computeCreateAddress(address(this), n);
        bytes memory initCode = abi.encodePacked(
            type(TPROMerkleDistributor).creationCode, abi.encode(tokenPredicted, ROOT, GENESIS, sunset)
        );
        address distPredicted = vm.computeCreate2Address(SALT, keccak256(initCode), address(this));

        TPRO token = new TPRO(distPredicted, GENESIS); // tx1 (CREATE, nonce n)
        assertEq(address(token), tokenPredicted);

        // tx2 fails transiently (simulated: the wiring read reverts this once)
        vm.mockCallRevert(address(token), abi.encodeCall(token.balanceOf, (distPredicted)), "transient");
        try new TPROMerkleDistributor{salt: SALT}(address(token), ROOT, GENESIS, sunset) returns (TPROMerkleDistributor) {
            fail();
        } catch {}
        vm.clearMockedCalls();
        assertEq(distPredicted.code.length, 0, "address not consumed by the failed creation");

        // a stray nonce-consuming tx between tx1 and the retry changes nothing for CREATE2
        new Dummy();

        TPROMerkleDistributor dist =
            new TPROMerkleDistributor{salt: SALT}(address(token), ROOT, GENESIS, sunset);
        assertEq(address(dist), distPredicted, "retry landed at the predicted address");
        assertEq(token.balanceOf(address(dist)), GENESIS);
        assertEq(address(dist.token()), address(token));
    }

    // ------------------------------------------- (3) CREATE2 via the factory

    function test_create2Factory_revertedInitCodeLeavesAddressFree_thenSucceeds() public {
        address factory = CREATE2_FACTORY;
        if (factory.code.length == 0) vm.etch(factory, FACTORY_CODE);

        uint256 sunset = block.timestamp + 1096 days;
        uint64 n = vm.getNonce(address(this));
        address tokenPredicted = vm.computeCreateAddress(address(this), n);
        bytes memory initCode = abi.encodePacked(
            type(TPROMerkleDistributor).creationCode, abi.encode(tokenPredicted, ROOT, GENESIS, sunset)
        );
        // 2-arg form = the default factory, exactly what a script would compute
        address distPredicted = vm.computeCreate2Address(SALT, keccak256(initCode));
        assertEq(distPredicted, vm.computeCreate2Address(SALT, keccak256(initCode), factory));

        // BEFORE tx1: the init code reverts (no token code yet) -> the factory call reverts, nothing consumed
        (bool ok,) = factory.call(abi.encodePacked(SALT, initCode));
        assertFalse(ok);
        assertEq(distPredicted.code.length, 0);

        TPRO token = new TPRO(distPredicted, GENESIS); // tx1
        assertEq(address(token), tokenPredicted);

        // ANYONE can now run tx2 (a third party paying the gas gets the identical contract)
        vm.prank(makeAddr("anyone"));
        (ok,) = factory.call(abi.encodePacked(SALT, initCode));
        assertTrue(ok);
        assertEq(distPredicted.code.length > 0, true);
        TPROMerkleDistributor dist = TPROMerkleDistributor(distPredicted);
        assertEq(token.balanceOf(address(dist)), GENESIS);
        assertEq(dist.merkleRoot(), ROOT);
        assertEq(dist.sunsetTime(), sunset);
        assertEq(address(dist.token()), address(token));

        // a second run of the same init code cannot redeploy (address occupied) - harmless revert
        (ok,) = factory.call(abi.encodePacked(SALT, initCode));
        assertFalse(ok);
    }
}
