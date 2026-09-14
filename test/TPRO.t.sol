// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {TPRO} from "../contracts/TPRO.sol";
import {IERC20Errors} from "@openzeppelin/contracts/interfaces/draft-IERC6093.sol";

contract TPROTest is Test {
    uint256 internal constant GENESIS = 1_122_500_000e18;
    address internal recipient = makeAddr("distributor");
    TPRO internal token;

    function setUp() public {
        token = new TPRO(recipient, GENESIS);
    }

    function test_metadata() public view {
        assertEq(token.name(), "TPRO");
        assertEq(token.symbol(), "TPRO");
        assertEq(token.decimals(), 18);
    }

    function test_genesisMintedOnceToRecipient() public view {
        assertEq(token.totalSupply(), GENESIS);
        assertEq(token.balanceOf(recipient), GENESIS);
        assertEq(token.balanceOf(address(this)), 0); // the deployer never holds a token
    }

    function test_deployToZeroRecipientReverts() public {
        vm.expectRevert(abi.encodeWithSelector(IERC20Errors.ERC20InvalidReceiver.selector, address(0)));
        new TPRO(address(0), GENESIS);
    }

    function test_burnReducesSupply() public {
        vm.prank(recipient);
        token.burn(1_000e18);
        assertEq(token.totalSupply(), GENESIS - 1_000e18);
        assertEq(token.balanceOf(recipient), GENESIS - 1_000e18);
    }

    function test_burnFromRespectsAllowance() public {
        address spender = makeAddr("spender");
        vm.prank(recipient);
        token.approve(spender, 500e18);
        vm.prank(spender);
        token.burnFrom(recipient, 500e18);
        assertEq(token.totalSupply(), GENESIS - 500e18);

        vm.prank(spender);
        vm.expectRevert(); // allowance exhausted
        token.burnFrom(recipient, 1);
    }

    function test_permit() public {
        (address holder, uint256 holderPk) = makeAddrAndKey("holder");
        address spender = makeAddr("spender");
        vm.prank(recipient);
        token.transfer(holder, 100e18);

        uint256 deadline = block.timestamp + 1 hours;
        bytes32 structHash = keccak256(
            abi.encode(
                keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)"),
                holder,
                spender,
                100e18,
                token.nonces(holder),
                deadline
            )
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", token.DOMAIN_SEPARATOR(), structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(holderPk, digest);

        token.permit(holder, spender, 100e18, deadline, v, r, s);
        assertEq(token.allowance(holder, spender), 100e18);

        vm.prank(spender);
        token.transferFrom(holder, spender, 100e18);
        assertEq(token.balanceOf(spender), 100e18);
    }

    function testFuzz_transferKeepsSupply(uint256 amount) public {
        amount = bound(amount, 0, GENESIS);
        address to = makeAddr("to");
        vm.prank(recipient);
        token.transfer(to, amount);
        assertEq(token.balanceOf(to), amount);
        assertEq(token.balanceOf(recipient), GENESIS - amount);
        assertEq(token.totalSupply(), GENESIS);
    }
}
