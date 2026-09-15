# Security reviews of the v3 migration code

This file is the public record of the reviews of the code in this repository: what was
looked at, by which method, what was found and what happened to every finding. The full
internal review document (which also covers a retired earlier design and the operating
procedures) is held by the project and its partners; nothing that changed the code or a
published rule is missing here.

## Scope

- `contracts/TPRO.sol`, `contracts/TPROMerkleDistributor.sol` and their deployment (`script/DeployV3.s.sol`);
- the snapshot tool (`scripts/snapshot/`) that produces the published table and the merkle root;
- the claim page and its publication gate (`website/`);
- the procedures around them (publication, snapshot day, launch, recovery), reviewed but not published here.

Out of scope: the original 2022 Ethereum/Polygon contracts (live until the migration and
read by the tool, not modified), the third-party lockers used after launch, the liquidity
pool economics, and the communications plan.

## Rounds

**1. Builder's adversarial pass.** A written threat model per surface (contracts, tool,
page), controls named against each threat, unit / fuzz / invariant tests, Slither, Aderyn,
Mythril and ERC20 conformance on the contracts, and a full dry run of the tool on both
chains reconciled to the wei.

**2. Independent review, four blind reviewers (one per surface: contracts and deployment,
snapshot tool, claim page, procedures).** Each reviewer wrote findings without access to the
builder's pass, then had to state what the builder had missed. Techniques: 24 deliberate
mutants of the contract code (all 21 behaviour-changing ones were caught by the tests; the
other 3 changed storage layout, not behaviour), symbolic verification of 4 properties,
failure injection against the tool (corrupt API answers, a missing staker, a truncated
listing), an independent recomputation of the merkle tree with the OpenZeppelin JavaScript
library for every row of the dry-run table, 16 claim-page scenarios in a real headless
browser with a stubbed wallet and node, on-chain reads of the external contracts, and the
execution of every command of the procedures. Result: 62 findings (0 critical, 7 high, 27
medium, 16 low, 12 informational), none in the contracts themselves; every high and medium
finding was fixed or turned into a written rule. The most important:
- the two-transaction deployment was not atomic (a stray transaction at the deployer's next nonce would have stranded the whole supply): the distributor is now deployed through CREATE2, so a failed second transaction is retried at the same address;
- the tool described census differences instead of stopping on them (a simulation that removed three holders from the stakings and a vesting still ended "OK"): every difference between a contract's balance and the sum of its per-user reads must now equal a pinned constant, anything else stops the run; the explorer client fails closed; a second, independent event index and a second node provider are compared;
- the liquidity row was too large by the third-party share of the Uniswap V4 pool manager: the project's two positions are now computed exactly (integer math) and the rest is excluded and labeled;
- the page asserted "not in the table" without an integrity anchor: every served table file is now checked against a pinned checksum manifest before absence is asserted; the page also describes the one legitimate wallet prompt, confirms the on-chain root with two providers, simulates the claim before sending, and prints the manual path.

**3. Dress rehearsal on a public testnet.** Six token / distributor deployments, every
recovery lane (a dropped second transaction, a stray transaction, a reverted second
transaction, a third party sending the same init code), a real claim through the page in a
headless browser, the sunset burn, and the tool in its final mode at a finalized head. Eleven
findings, all in the procedures, the tests or the tool's tolerance to rate limits; none in
the contracts.

**4. External read-only review (a fourth party, source review without execution).** Ten
findings on the active code (S01-S10), eight hardening observations (H01-H08), ten on the
legacy 2022 contracts (L01-L10) and five on a retired, never-deployed design (R01-R05).
Dispositions below.

## Dispositions of the external review

| ID | Finding (short) | Disposition |
|---|---|---|
| S01 | A failed final run of the tool still wrote a complete-looking bundle; no consumer required validation success | FIXED: `index.json` carries `strictOk` and `errors`; a failed final run is moved to a `-FAILED` directory; the deploy script requires `mode: final` and `strictOk: true` on mainnet (a missing key is a revert; a failed table is refused on every chain); the page gate requires them for an official deployment; tests in both suites |
| S02 | Indirect beneficiaries (stakers, vesting beneficiaries, bridge burners) were credited without the eligibility checks direct holders get | FIXED: one chain-aware eligibility decision before every indirect credit, on the chain of the source (code lookup, EIP-7702 designators count as EOAs, the excluded-address set, the verified designation map); a contract without a designation or an excluded address is a strict error; counted in the summary |
| S03 | The designation check matched the Base address anywhere in the outer calldata and any internal call, without binding the two or requiring success | FIXED: the second provider's call tracer must show exactly one successful CALL from the contract to the designation address with the Base address in that call's input, with no reverted ancestor; the explorer's internal-transaction list stays as the cross-check; no tracing provider = failure |
| S04 | A malformed record of the second transfer index ended the comparison early as a "difference" with nothing to settle, so the strict verdict stayed OK | FIXED: every index record is validated (a malformed one is a strict error), every page of the listing must have the documented shape, every difference must map to a disputed transaction settled by a receipt |
| S05 | The page could recover into "verified" without the checksum manifest after a transient failure | FIXED: separate table and chain verification flags, both required to look up, arm and send; the table state is published only after every check; a pinned manifest that is not loaded fails every table read; the timer and the refresh re-run the whole table load |
| S06 | No request deadlines; a stalled provider blocked the failover; the manual call appeared only after a successful status read | FIXED: deadlines and size caps on every request, bounded retries, a concurrent root quorum under one deadline, the manual call rendered right after the local proof check, a failed status read shown as "unknown" with the claim button off |
| S07 | The final run asserts exact balances of pinned targets; one wei sent by anyone before the block would stop the table with no repair | FIXED as a safety valve, the rule unchanged: an explicit acceptance of named, chain-verified unsolicited transfers (exact sum, mined before the block, no event of the target, cross-checked with the explorer's inflow index; a deficit is never accepted), excluded under a public label; plus a daily monitor of the targets until the snapshot; the bucket keeps its zero rule and needs a written go |
| S08 | Contract-wallet records were keyed by address only, so the same address on both chains collapsed into one record | FIXED: records keyed `<chain>:<address>` in the tool, the page (one marker per chain) and the gate; documented in the interface specification |
| S09 | The legacy cross-check helper printed a match with no reference data | FIXED: all six reference directories and a non-empty comparison are required; the count is printed; anything else fails |
| S10 | Python dependencies were not pinned | FIXED: `requirements.txt` (pinned) and `requirements.lock` (the full resolved set), the interpreter version in the README, a dedicated environment |
| H01 | The page's content security policy allows inline scripts | ACCEPTED for now: the page loads no external code at all and validates every remote value; hash-pinning the inline script is on the backlog |
| H02 | A fallback deployment script narrowed to `uint64` before validating the input | FIXED (the script is a fallback, not deployed): range checks before the casts, plus magnitude bounds on the schedule |
| H03 | A known-answer test skipped silently when its fixture was missing; broad analyzer exclusions | FIXED: a missing fixture fails the test. The analyzer exclusions are documented one by one with the reason each is safe for this code |
| H04 | Legacy administrator / relayer trust | Not applicable to this code (the legacy owner powers concern the old contracts; the relayer design was retired) |
| H05 | The legacy census tooling is not a historical oracle | Already the rule: the legacy pipeline is a soft cross-check only; the tool's own block-pinned census is the source |
| H06 | Unknown-staking detection swallowed explorer errors; the second provider checked totals, not every beneficiary | FIXED: a runtime-bytecode fingerprint (the two StakingV2 event topics and the `userInfo` selector) flags any non-pinned staking-shaped contract on both chains; an explorer failure other than "not verified" is an error. The per-beneficiary independence is covered by the second transfer index plus the receipt tie-breaker and the second provider's sampled balances |
| H07 | The page reported "claimed" on any successful receipt | FIXED: success only for a receipt to the distributor carrying a `Claimed` event for the account |
| H08 | Keep the immutable-allocation safeguards; confirm the reserve policy | Kept; the post-launch correction policy (a published reserve, a public list) is ratified |
| L01-L10 | The 2022 contracts: embedded test signing material in a legacy configuration file; staking lock, reward and restake defects; a vesting penalty formula; an anti-bot cooldown; TLS verification disabled in old tooling | Not this code. The legacy tree is not published. The embedded material was checked privately: never used on any network (every derived address has nonce 0 and no balance on Ethereum and Polygon). The staking and vesting defects do not change the per-user principal the snapshot reads (`userInfo.amount`, `currentBalance`); rewards were never paid; the anti-bot restriction has been off since 2024. Their only possible effect on the snapshot is a contract balance change, which the tool's exact pins and the daily monitor catch |
| R01-R05 | The retired escrow / relayer design | Never deployed, not built on; removed from this repository |

## Verification after the fix round

86 Foundry tests in the full suite (unit, fuzz, invariant, known-answer fixtures, deploy-script guards; the deploy-script guards join this repository at launch together with the deploy script, 48 tests run here until then; Foundry 1.8 reports the six invariant predicates as one campaign, so its summary line reads 81 and 43), 100%
line, statement, branch and function coverage of `contracts/`, Slither and Aderyn without
findings (exclusions documented), 169 offline checks of the tool's guard logic
(`scripts/snapshot/selftest.py`), the page gate green on the sample table, seven browser
scenarios of the fixed page in a real headless browser (the official preview shape, a
withheld checksum manifest, a stalled provider, every provider down, a status-read outage
with a valid proof, an unrelated successful receipt, a real claim), and a full preview run of
the tool on both chains at a finalized head with every guard on.

## What the reader should still keep in mind

- The table's correctness rests on published constants (the pinned census reserves, the
  custody set, the staking contract addresses) and on public archive data read through two
  providers and two independent event indexes. Anyone can re-run the tool and compare.
- The root is immutable once deployed. A row found wrong after launch is paid from a
  published reserve under a published rule, with a public list; it cannot be rewritten.
- The reviews were bounded by their scope and time. They do not certify the deployment;
  the on-chain contracts, the published table and this repository are what to verify.
