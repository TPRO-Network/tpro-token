// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";

/// @title TPRO
/// @notice TPRO on Base. Fixed supply, minted exactly once at deployment to a
///         single recipient: the TPROMerkleDistributor. That contract lets every
///         holder of the original TPRO token (Ethereum and Polygon) claim, 1:1
///         and on Base, the balance recorded for their address in the published
///         snapshot table. The genesis supply IS the table total, so every new
///         token is backed by exactly one recorded old token.
///
///         This token deliberately has NO owner, NO mint function, NO pause,
///         NO fees, NO transfer hooks, NO allowlist/blocklist and NO proxy.
///         The supply can only ever go DOWN, via `burn`/`burnFrom`
///         (ERC20Burnable): by any holder at will, and by the distributor when
///         the claim window closes and the unclaimed remainder is burned.
/// @dev    Standard OpenZeppelin v5 building blocks only:
///         ERC20 + ERC20Burnable + ERC20Permit (EIP-2612 gasless approvals).
///         There is intentionally no custom logic beyond the constructor mint.
contract TPRO is ERC20, ERC20Burnable, ERC20Permit {
    /// @param recipient     Receives the entire genesis supply: the distributor
    ///                      contract. Its address is predicted from the
    ///                      deployer's next nonce, and the distributor's own
    ///                      constructor verifies that it really holds the
    ///                      supply. The deployer never holds a single token.
    ///                      (A zero recipient reverts inside OpenZeppelin's
    ///                      `_mint` with `ERC20InvalidReceiver`.)
    /// @param genesisSupply Exact supply in wei = the snapshot table total.
    constructor(address recipient, uint256 genesisSupply) ERC20("TPRO", "TPRO") ERC20Permit("TPRO") {
        _mint(recipient, genesisSupply);
    }
}
