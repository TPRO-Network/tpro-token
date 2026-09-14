# v3 interface contract - snapshot + merkle claim (2026-09-04, revised 2026-09-06 after audit round 4, 2026-09-09 after the external review)

Single source of truth shared by the snapshot tool (`scripts/snapshot/`), the
contracts (`contracts/`), the claim page (`website/`) and the runbook. Canon
for the decisions: the project decision record "v3 BUILD GO" + amendments and
`the project decision record`. INTERNAL until publication.
Round-4 changes are marked [R4]; the external-review fix round of 2026-09-09 (review of
2026-09-08, items S01-S10 / H01-H08, AUDIT section 11) is marked [E].

## 1. Contracts on Base

### TPRO.sol (OZ v5.7.0 `ERC20 + ERC20Burnable + ERC20Permit`, nothing else)
```
constructor(address recipient, uint256 genesisSupply)   // mints the whole genesis to `recipient`
```
`recipient` = the distributor's PREDICTED address (see deployment). No owner,
no mint, no pause, no fees, no hooks, no proxy. Supply only goes down (burn).
OpenZeppelin is pinned at tag v5.7.0 (commit cab19933) as a git submodule +
`foundry.lock` [R4]; verification uses the frozen commit only.

### TPROMerkleDistributor.sol (immutable, keyless)
```
constructor(address token, bytes32 merkleRoot, uint256 genesis, uint256 sunsetTime)
  - reverts GenesisMismatch() unless genesis != 0 AND IERC20(token).balanceOf(address(this)) == genesis
    AND IERC20(token).totalSupply() == genesis            (wiring proof: the whole supply, and only it, is here) [R4]
  - reverts ZeroRoot() if merkleRoot == 0
  - reverts BadSunsetTime() unless sunsetTime > block.timestamp

function claim(address account, uint256 amount, bytes32[] calldata proof) external
  - NotYourRow()        unless msg.sender == account                 (decision 3)
  - ClaimWindowClosed() unless block.timestamp < sunsetTime
  - AlreadyClaimed()    if claimed[account]
  - InvalidProof()      unless MerkleProof.verifyCalldata(proof, merkleRoot, leaf)
      leaf = keccak256(bytes.concat(keccak256(abi.encode(account, amount))))   // OZ StandardMerkleTree ["address","uint256"]
  - effects: claimed[account] = true; totalClaimed += amount; token.transfer(account, amount); emit Claimed(account, amount)
  - the WHOLE row in one transaction, no penalty, no partial claims

function sunset() external
  - SunsetTooEarly() unless block.timestamp >= sunsetTime
  - burns token.balanceOf(address(this)) via ERC20Burnable.burn; sunsetBurned += burned; emit Sunset(burned)
  - callable by ANYONE, repeatable (a later donation can be burned too); WE call it (laptop reminder #30)

views: token() address, merkleRoot() bytes32, genesis() uint256, sunsetTime() uint256,
       totalClaimed() uint256, sunsetBurned() uint256, isClaimed(address) bool
events: Claimed(address indexed account, uint256 amount); Sunset(uint256 burned)
errors: NotYourRow 0x6746ca73, ClaimWindowClosed 0xf0f25a33, AlreadyClaimed 0x646cf558, InvalidProof 0x09bde339,
        SunsetTooEarly 0x7c653910, ZeroRoot, BadSunsetTime, GenesisMismatch
```
No owner, no pause, no upgrade, no rescue function, no index/bitmap: one row per
address (the tool asserts address uniqueness), so `mapping(address => bool)` IS
the claimed set. Public invariant: `token.balanceOf(distributor) + totalClaimed
+ sunsetBurned == genesis` (plus any voluntary donations sent to the distributor,
which sunset() burns).

Selectors (`cast sig`): claim 0x3d13f874, isClaimed 0x8cc08025, merkleRoot
0x2eb4a7ab, token 0xfc0c546a, genesis 0xa7f0b3de, sunsetTime 0x6c63c400,
totalClaimed 0xd54ad2a1, sunsetBurned 0x99f973dc, sunset 0x026164ad.
Topics: Claimed 0xd8138f8a3f377c5259ca548e70e4c2de94f129f5a11036a15b69513cba2b426a,
Sunset 0x96b553404329e9bc1fdd7b0e3aab145251b573bbd780238cfe0b82f24b29c5b8.

### Deployment sequence (ONE forge script, ephemeral deployer, at T0) [R4: CREATE2]
1. tx1, deployer nonce n (CREATE) -> `new TPRO(predictedDistributor, genesis)`.
   THIS transaction is T0 (the token's birth) - aim it at the T0 instant of the runbook.
2. tx2 (CREATE2 through the canonical deterministic-deployment factory
   `0x4e59b44847b379578588920cA78FbF26c0B4956C`, salt = `keccak256("TPRO v3 distributor")`)
   -> `TPROMerkleDistributor(token, root, genesis, sunsetTime)`. Its address =
   `keccak256(0xff ++ factory ++ salt ++ keccak256(creationCode ++ abi.encode(token, root, genesis, sunsetTime)))[12:]`
   and depends on NOTHING the deployer does after tx1: a dropped tx2 is re-sent
   (`forge script --resume`), a mined-but-reverted tx2 is retried at the SAME
   address (`EXISTING_TOKEN=<token>` lane, tx2 only, from any funded key), a
   stray transaction from the deployer between tx1 and tx2 is harmless, and a
   third party sending the identical init code deploys the identical contract
   (harmless: the constructor proves the wiring by its own balance). With plain
   CREATE the whole genesis would have been stranded forever the moment anything
   but the distributor consumed nonce n+1 (round 4, R1-1).
3. The script refuses: a root or genesis that differs from `table/index.json`
   (`TABLE_INDEX`), a preview-mode index on chainid 8453 (`.mode` must be
   `final` and the key must EXIST - a missing key is a revert, not a pass [E]),
   an index whose `.strictOk` is not `true` (on chainid 8453 the key is REQUIRED;
   on every chain `strictOk: false` or `errors != 0` reverts - a table that failed
   the tool's own validation never deploys [E, S01]), a `SUNSET_TIME` that is not
   the pinned mainnet constant (published at launch) on chainid 8453, a fresh-lane window
   outside [36 months, 36 months + 30 days] (the retry lane is valid until
   sunset - tx1 fixed the window), and `REHEARSAL=true` on chainid 8453 (round 4,
   R1-2 / R1-3 + confirmation pass). It prints the predicted addresses, the salt
   and the init code hash before broadcasting.
Scanner optics: the token creator is the ephemeral EOA holding 0 tokens
(creator_percent 0); the distributor's creator is the public factory (as for
thousands of contracts); the supply sits in a keyless contract. The deployer
key is discarded after L2 finality of tx2 - it never had a power to keep.

### TPROTreasuryLock.sol - NOT DEPLOYED (fallback only) [R4]
the project lead chose UNCX Token Vesting on Base (2026-09-05, amendment 23). The wrapper
(OZ `VestingWallet`, start = T0 + 12 months, duration 24 months, ETH refused)
stays in the repo as a fallback and needs a written GO before any use.
UNCX Token Vesting `0xa82685520c463a752d5319e6616e4e5fd0215e33` as read on
Base 2026-09-05 (round 4, R4-5): `lock(address token, (address owner, uint256
amount, uint256 startEmission, uint256 endEmission, address condition)[])`,
NON-payable; fee = `FEES().tokenFee / 10000` of the locked amount (35 = 0.35%
today, owner-editable - read on the day), no ETH fee (the "free locking fee" is
denominated in USDC and effectively disabled); emission = 0 before
`startEmission`, then linear to `endEmission` = exactly "12-month cliff, 24
months linear" when startEmission = T0 + 12 calendar months and endEmission =
T0 + 36 calendar months (the sunset instant). The lock id = `NONCE()` before
the call; `getLock(id)`, `getWithdrawableTokens(id)` are the proof reads.

### UniV3LPTimelock.sol - NOT DEPLOYED (fallback only) [R4]
Primary = UNCX Uniswap V3 locker on Base `0x231278edd38b00b07fbd52120cef685b9baebcc1`
(`UNCX_LiquidityLocker_UniV3`, `lock((address nftPositionManager, uint256 nft_id,
address dustRecipient, address owner, address additionalCollector, address
collectAddress, uint256 unlockDate, uint16 countryCode, string feeName, bytes[] r))`
payable; DEFAULT fee as read 2026-09-05: flat 0.1 ETH EXACT (`msg.value ==
flatFee`) + 0.5% of the liquidity + 2% of collected fees; owner-editable -
`getFee("DEFAULT")` and `nftPositionManagerIsAllowed(NPM)` are read on the day;
lock id = `getLocksLength()` before the call; `getLock(id)` = the proof read;
`increaseLiquidity(lockId, params)` exists for the reservoir policy). Fallback =
our minimal contract: holds ONE NonfungiblePositionManager NFT, `collect()`
forwards fees to the beneficiary any time, `withdraw()` only after `unlockTime`.

## 2. Table publication format (the tool writes it, the page reads it)

Directory `table/` (served next to the claim page, also mirrored to the repo
that hosts the site + linked from the announcement):
```
table/index.json                 {"version":1,"leafEncoding":["address","uint256"],
                                  "ethBlock":N,"ethTimestamp":T,"ethBlockHash":"0x..",            [R4]
                                  "polygonBlock":M,"polygonTimestamp":T2,"polygonBlockHash":"0x..", [R4]
                                  "rows":count,"totalWei":"<decimal string>","merkleRoot":"0x..",
                                  "csv":"snapshot-N-M.csv","csvSha256":"<hex>","shards":256,
                                  "contractWallets":"contract-wallets.json",                        [R4]
                                  "reserveExceptionsWei":"<decimal string>",                        [R4]
                                  "mode":"final|preview","pinVerified":bool,                         [R4]
                                  "strictOk":bool,"errors":<count>,  (the tool's OWN verdict; a --final run with errors is
                                                             moved to <out>-FAILED/ and its index says strictOk:false - the
                                                             deploy script and verify_deploy.mjs refuse it)            [E, S01]
                                  "contentSha256":"<hex>",  (sha256 of the SHA256SUMS lines minus index.json/summary.json,
                                                             sorted - deterministic per table, the B1 re-run gate)  [R5]
                                  "generatedAt":"ISO-8601Z",
                                  "tool":"https://github.com/TPRO-Network/tpro-token@<public commit sha>"}
                                  (the PUBLIC repository commit that carries the exact tool, from PUBLIC_REF written by
                                   scripts/export-public.sh; --final REFUSES to run without the mapping; a dry run without
                                   it carries the private form "scripts/snapshot/snapshot.py@<sha>")               [E, item 13]
table/snapshot-N-M.csv           address,amount_wei   (checksummed address; exactly the merkle leaves. Since 2026-09-14 the
                                 per-row source labels (eth-wallet / eth-wallet-7702, staking-T1..T6, vesting-1..3,
                                 polygon-wallet / polygon-wallet-7702, polygon-staking-<0xabcd>, uniswap-v4-LP,
                                 contract-wallet:<eth addr>, bridge-unclaimed-exit [A27]) live only in the operator copy
                                 next to the run, never in the served table; the page offers no download link)
(operator-private) summary.json  written next to the run, NOT into table/, never served (2026-09-14): totals per source, every exclusion with amount + reason, genesis, the exact LP
                                 decomposition (project credit vs third-party residual), bridge unclaimed exits,
                                 census verdicts + pinned reserves, MEXC custody + reserve_exceptions_wei, carve-outs,
                                 designations, contract-wallet report, price record, reconciliation verdicts, errors,
                                 `accepted_explained` (chain-verified unsolicited transfers accepted with
                                 --accept-explained, each with its txs) and `indirect_beneficiaries` (how many
                                 stakers / beneficiaries / burners were classified EOA, EIP-7702, re-addressed,
                                 refused - on the chain of their source)                                        [E, S07/S02]
table/contract-wallets.json      {"<chain>:<0xlower>": {"chain","balance_wei","balance","status","label","contract_name",
                                 "code_prefix", "also":[...optional further records of the same contract...]}}
                                 chain = eth | polygon; the SAME address is two records when it is a contract on
                                 both chains (an address-only key let Polygon overwrite Ethereum) [E, S08]
                                 = every contract holder (designation path) + every contract that was an
                                 INDIRECT beneficiary (a staker / vesting beneficiary / bridge burner with code:
                                 re-addressed through a verified designation, or refused = a strict error) [E, S02];
                                 the page looks an absent address up under BOTH keys and shows one marker per hit [R4, E]
table/proofs/<xx>.json           xx = first two hex chars of the LOWERCASE address (256 shards):
                                 {"0xabc...": {"amount":"<decimal wei>","proof":["0x..",...]}, ...}
table/SHA256SUMS                 sha256 of every file above; the page pins sha256(SHA256SUMS) and csvSha256 [R4]
```
Merkle algorithm = OpenZeppelin `@openzeppelin/merkle-tree` StandardMerkleTree,
reproduced in `scripts/snapshot/merkle.py` and cross-checked against the JS
library: leaf = keccak256(keccak256(abi.encode(address, uint256))); leaves
sorted ascending by hash; tree array of size 2n-1, leaf i at index 2n-2-i,
node i = keccak256(sorted concat of children 2i+1, 2i+2); proof = siblings on
the path to the root. Anyone can recompute the root with
`StandardMerkleTree.of(rows, ["address","uint256"]).root`. The merkle root is
the canonical commitment; the CSV sha256 binds the exact file of the reference
tool (its `sources` column is tool-specific) [R4].

Published together with the table (announcement + page): the two block numbers
AND hashes, the instant and the pin rule as implemented (ETH = first block with
timestamp >= instant; Polygon = last block with timestamp <= the ETH block's
timestamp), the four MEXC custody addresses, the designation address, the
exact reproduction command with `--final --at-utc <instant>`, the tool's PUBLIC
repository + tag, sha256 of the CSV, of SHA256SUMS and of the page's
`index.html` / `config.js` [R4].

## 3. Claim page contract (website/)
- Reads go to the PUBLIC RPC LIST from `config.js` (failover; the contract root
  must be confirmed by two providers when two are reachable; REFRESH rotates
  the first provider), never the wallet provider [R4]. Every fetch (RPC and
  table files) carries a deadline covering headers and body, retries are
  bounded, and the root quorum queries the providers concurrently under one
  deadline - a stalled provider can neither block a healthy one nor hang the
  page [E, S06].
- Two flags, both required for a LIVE lookup, for arming and for sending:
  `tableVerified` (the served table passed EVERY integrity check; the table
  state is published only then) and `chainVerified` (the on-chain reads
  agree). A pinned SHA256SUMS that is not loaded fails every table read; the
  refresh timer and REFRESH re-run the whole table load until it passes [E, S05].
- Fail-closed: the page acts only after (a) `merkleRoot()` on-chain == config
  root == index.json root, (b) `token()` == config token, (c) `genesis()` ==
  index.json totalWei, (d) the inline keccak self-test passes, (e) SHA256SUMS
  hashes to the pinned `sha256sumsSha256` and index.json + every served shard
  hash to their SHA256SUMS lines [R4], (f) the local proof verification of the
  looked-up row against the root passes, (g) right before sending: root and
  `isClaimed` re-read, block timestamp < `sunsetTime`, and an `eth_call`
  pre-flight of the exact calldata as the account (reverts decoded) [R4].
- "Not in the table" is asserted only for a served shard proven byte-identical
  to the published one; a listed contract holder gets the marker "CONTRACT
  WALLET ON ETHEREUM / ON POLYGON - DESIGNATION NEEDED" (one per chain key) with
  the designation address [R4, E].
- The manual "Claim without this page" box is rendered right after the LOCAL
  proof verification, before any chain read: when `isClaimed` cannot be read
  the row stays visible with status "unknown" and the manual path stays
  available (the documented RPC-outage fallback) [E, S06].
- After a claim transaction the page reports success ONLY when the receipt's
  `to` is the distributor and it carries a `Claimed(account, amount)` log for
  the connected account; any other successful receipt reads "confirmed on chain
  but not as a claim - REFRESH" [E, H07].
- `LIVE: true` = official deployment: requires the three pins (root, csvSha256,
  sha256sumsSha256); `distributor` unset = the OFFICIAL PREVIEW (table lookup
  only, CONNECT hidden); `LIVE: false` = test copy (sticky banner) [R4].
- Claim tx: `claim(account, amount, proof)` from the connected wallet on Base
  (chain switch prompted), `value: 0x0`, preceded by the on-page description of
  the legitimate wallet prompt (to = distributor, 0 ETH, Base, data prefix
  0x3d13f874, one transaction, never a signature/approval) [R4]; then
  `wallet_watchAsset` for TPRO.
- Contract-wallet notice (decision 6) + "not in the table?" FAQ + sunset date
  from `sunsetTime()` (UTC + local) + "Claim without this page" (exact call).
- Gate before every push and after every deploy: `node website/verify_deploy.mjs`
  (pins vs table, SHA256SUMS, shards, CSP hosts, live reads, served files,
  headers) [R4]; with `LIVE: true` it also requires `index.json` `strictOk ==
  true` and `errors == 0`, and `mode == "final"` once a distributor is set, and
  it validates the `<chain>:<address>` keys of `contract-wallets.json` [E].
