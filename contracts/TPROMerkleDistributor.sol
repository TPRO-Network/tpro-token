// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {TPRO} from "./TPRO.sol";

/// @title TPROMerkleDistributor
/// @notice The one-time claim contract of the TPRO migration to Base.
///
///         How it works, in plain words:
///         1. Balances of the original TPRO token were recorded at a published
///            Ethereum block (plus the Polygon block at the same moment):
///            wallets, stakers (their deposits) and vesting beneficiaries
///            (their unclaimed amounts). The table "address, amount" and the
///            tool that produced it are public; anyone can recompute it and
///            its merkle root from public archive nodes.
///         2. The merkle root of that table is fixed in this contract at
///            deployment. The whole genesis supply of the new TPRO token was
///            minted straight into this contract.
///         3. Every address in the table claims its FULL row once, in one
///            transaction, by calling `claim` from that very address with the
///            proof published next to the table. No fee, no penalty, no
///            deadline inside the window, nothing to sign on Ethereum.
///         4. The window is open until `sunsetTime` (36 months after launch).
///            After that anyone may call `sunset()`, which burns whatever was
///            never claimed. The supply of the new token only ever goes down.
///
///         What this contract deliberately does NOT have: an owner, an admin,
///         a pause switch, an upgrade path, a rescue/sweep function, a way to
///         change the root or the sunset time, a fee, or any key that matters
///         after deployment. It cannot be stopped and it cannot be redirected.
///
/// @dev    Leaf format = OpenZeppelin StandardMerkleTree with the encoding
///         `["address","uint256"]`: `keccak256(bytes.concat(keccak256(abi.encode(
///         account, amount))))`. The double hash keeps a leaf from ever colliding
///         with an internal node (second-preimage hardening). One row per
///         address in the table (enforced by the tool), so `claimed[account]`
///         is the complete claimed-set - no index, no bitmap.
///
///         `msg.sender == account` is the whole access control. There is NO
///         `tx.origin` / EOA requirement on purpose: an EIP-7702-delegated EOA
///         claims like any EOA, and a contract that exists at the same address
///         on Base claims its own row (contract wallets without a Base
///         counterpart are handled upstream, in the table, per the published
///         designation procedure).
contract TPROMerkleDistributor {
    using SafeERC20 for TPRO;

    /// @notice The new TPRO token on Base (minted in full to this contract).
    TPRO public immutable token;
    /// @notice Merkle root of the published snapshot table (address, amount).
    bytes32 public immutable merkleRoot;
    /// @notice The table total = the token's genesis supply, in wei.
    uint256 public immutable genesis;
    /// @notice First second at which claims are closed and `sunset()` opens.
    uint256 public immutable sunsetTime;

    /// @notice Sum of all rows claimed so far, in wei.
    uint256 public totalClaimed;
    /// @notice Sum of everything burned by `sunset()` so far, in wei.
    uint256 public sunsetBurned;

    mapping(address account => bool) private _claimed;

    /// @notice A row was claimed.
    event Claimed(address indexed account, uint256 amount);
    /// @notice The unclaimed remainder (and any donation) was burned.
    event Sunset(uint256 burned);

    /// @dev `claim` was called from an address other than the row's `account`.
    error NotYourRow();
    /// @dev `claim` was called at or after `sunsetTime`.
    error ClaimWindowClosed();
    /// @dev The row for `account` has already been claimed.
    error AlreadyClaimed();
    /// @dev (account, amount) with this proof is not a row of the table.
    error InvalidProof();
    /// @dev `sunset` was called before `sunsetTime`.
    error SunsetTooEarly();
    /// @dev The constructor was given an empty merkle root.
    error ZeroRoot();
    /// @dev The constructor was given a sunset time that is not in the future.
    error BadSunsetTime();
    /// @dev The constructor found a balance or a total supply different from
    ///      `genesis` (or a zero genesis): the token was not minted, in full and
    ///      only, to this address as intended.
    error GenesisMismatch();

    /// @param token_      The TPRO token, already deployed and already minted
    ///                    in full to THIS (predicted) address.
    /// @param merkleRoot_ Root of the published table.
    /// @param genesis_    The table total in wei; must equal BOTH this contract's
    ///                    token balance AND the token's total supply (the wiring
    ///                    proof: the whole supply, and nothing but the supply, is
    ///                    here).
    /// @param sunsetTime_ Unix time when the claim window closes.
    constructor(address token_, bytes32 merkleRoot_, uint256 genesis_, uint256 sunsetTime_) {
        if (merkleRoot_ == bytes32(0)) revert ZeroRoot();
        if (sunsetTime_ <= block.timestamp) revert BadSunsetTime();
        if (
            genesis_ == 0 || TPRO(token_).balanceOf(address(this)) != genesis_
                || TPRO(token_).totalSupply() != genesis_
        ) revert GenesisMismatch();
        token = TPRO(token_);
        merkleRoot = merkleRoot_;
        genesis = genesis_;
        sunsetTime = sunsetTime_;
    }

    /// @notice Claim the full row of `account`. Must be called BY `account`.
    /// @param account The address exactly as it appears in the table.
    /// @param amount  The row's amount in wei, exactly as published.
    /// @param proof   The merkle proof published for this row.
    function claim(address account, uint256 amount, bytes32[] calldata proof) external {
        if (msg.sender != account) revert NotYourRow();
        if (block.timestamp >= sunsetTime) revert ClaimWindowClosed();
        if (_claimed[account]) revert AlreadyClaimed();
        bytes32 leaf = keccak256(bytes.concat(keccak256(abi.encode(account, amount))));
        if (!MerkleProof.verifyCalldata(proof, merkleRoot, leaf)) revert InvalidProof();

        _claimed[account] = true;
        totalClaimed += amount;
        emit Claimed(account, amount);
        token.safeTransfer(account, amount);
    }

    /// @notice After `sunsetTime`: burn everything still held here. Anyone may
    ///         call it, as often as they like (a token sent here later can be
    ///         burned too). Claims are already closed by time, not by this call.
    function sunset() external {
        if (block.timestamp < sunsetTime) revert SunsetTooEarly();
        uint256 remainder = token.balanceOf(address(this));
        sunsetBurned += remainder;
        emit Sunset(remainder);
        token.burn(remainder);
    }

    /// @notice Whether `account` has already claimed its row.
    function isClaimed(address account) external view returns (bool) {
        return _claimed[account];
    }
}
