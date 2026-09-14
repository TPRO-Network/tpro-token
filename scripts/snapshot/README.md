# Snapshot tool - the v3 table, reproducible by anyone

Everything here is READ-ONLY. No keys, no transactions. The scripts:

| Script | Purpose |
|---|---|
| `snapshot.py` | THE table: every holder of the original TPRO at a pinned Ethereum block (+ the Polygon block at the same timestamp), one row per address, exclusions per the published rules, merkle root + proof shards, sha256 of the CSV and of SHA256SUMS. `--final` = the mode of the real table (every guard on). |
| `census.py` | The staking (T1-T6) + vesting (three contracts, vesting-1..3) census it builds on: per-user amounts read from the contracts, address set from the contracts' own events, `sum == balanceOf` per contract. Runs alone too. |
| `merkle.py` | OpenZeppelin StandardMerkleTree (`["address","uint256"]`) in Python - recompute the root from the CSV, write proof shards, verify every proof. Bit-identical with `@openzeppelin/merkle-tree` (checked on the full 1,350-row dry-run table and on a 1001-row random table: same root, same proofs). |
| `compare_legacy.py` | Cross-check of the staking census against the 2023 pipeline of the original dev team (`legacy/tokenomia-contracts/bin/script/js-source`, private repo only). Requires all six reference directories T1..T6 and a non-empty comparison - a missing reference set is a FAIL, never "ALL MATCH" (external review, S09). |
| `selftest.py` | OFFLINE checks of the guard logic (no network, no keys), 169 checks: pinned census verdicts, the MEXC set, designation validation + call-tracer binding, block pinning, the strict second-source comparison, the StakingV2 fingerprint, the eligibility decision on indirect beneficiaries, accepted unsolicited transfers, the verdict plumbing, the exact Uniswap V4 math, the bridge decomposition, cache headers. Run after every edit. |
| `carveouts.py` | Prints the three Treasury carve-out amounts (community tranche, exceptions reserve, lock) from a `summary.json` - nothing is typed by hand on deploy day. |
| `make_kat.py` | Regenerates the Foundry known-answer fixtures (`test/fixtures/real-table-*.json`) from a published table directory (strict-OK tables only), so `forge test` proves the published proofs against the contracts. |
| `monitor_pins.py` | Daily watch of the 15 targets whose balances the final run asserts exactly (6 + 3 + bucket + tunnel on Ethereum, 4 stakings on Polygon): one transfer-index query per chain since the last run, every transfer into / out of a target classified from its receipt (normal / STRAY / RESERVE / BUCKET). Empty "flagged" = fine; exit 1 otherwise (external review, S07). |
| `requirements.txt` / `requirements.lock` | The direct dependencies (pinned) and the full resolved set (`pip freeze`, Python 3.12.3) - install the lock for a reproduction (external review, S10). |

Method, contract facts, the final-run rule and RPC lessons: `docs/SNAPSHOT_NOTES.md` (private repo).
Throttling: every RPC call goes through a patient provider (HTTP 429 / 5xx / connection
errors retried with a 2..20 s backoff, ~2 min in total) - run ONE tool at a time per key.
Formats: `docs/v3-interface.md` section 2. Independent reviews of the tool: `docs/AUDIT_2026-09-01.md` sections 10 and 11 (public repo: `AUDIT.md`).

## Reproduce the published table

Inputs you need: the announced instant and the two pinned block numbers (+ their
hashes), an Ethereum + a Polygon ARCHIVE RPC (a free Alchemy or dRPC key; public
endpoints rate-limit and some return empty results silently), a free Etherscan API
key (`ETHERSCAN_API_KEY`; the Etherscan API v2 serves Ethereum AND Polygon logs/ABIs
with one key). The four MEXC custody addresses and the four Polygon staking contracts
are PINNED in `snapshot.py` (published constants). Python 3.12 (3.12.3 used for the
published runs) with the pinned set in `requirements.lock`:

```bash
cd tpro-token
python3.12 -m venv .venv && .venv/bin/pip install -r scripts/snapshot/requirements.lock
export ETHERSCAN_API_KEY=...            # or put it in .env (repo root; the ONLY file the tool reads secrets from)
export ETH_ARCHIVE_RPC=https://...      # keyed archive URLs stay in the ENVIRONMENT (or .env): the tool reads
export POLYGON_ARCHIVE_RPC=https://...  # them as defaults, so the key never lands in shell history or `ps`,
                                        # and only scheme://host is written into the published summary.json
# the published rule, exactly as we ran it (the blocks are DERIVED from the instant over finalized heads):
.venv/bin/python scripts/snapshot/snapshot.py --at-utc <the announced instant> --workers 4
# or pin the announced blocks explicitly (the instant is still given so the pin rule is re-verified):
.venv/bin/python scripts/snapshot/snapshot.py --at-utc <instant> --eth-block <N> --polygon-block <M> --workers 4
# -> tmp/snapshot-N-M/snapshot-N-M.csv, summary.json, contract-wallets.csv,
#    table/{index.json, snapshot-N-M.csv, summary.json, contract-wallets.json, proofs/<xx>.json x256, SHA256SUMS}
sha256sum tmp/snapshot-N-M/snapshot-N-M.csv                     # must equal the published CSV sha256
sha256sum tmp/snapshot-N-M/table/SHA256SUMS                     # must equal the published SHA256SUMS sha256 ONLY for the
                                                                # published run itself; a reproduction matches the root, the
                                                                # CSV sha256 and the tool's "content sha256" line instead
                                                                # (index.json/summary.json carry the run's provenance)
.venv/bin/python scripts/snapshot/merkle.py tmp/snapshot-N-M/snapshot-N-M.csv --verify   # must print the published root
```
Alternatively recompute the root from the published CSV alone with the OZ
library: `StandardMerkleTree.of(rows.map(r => [r.address, r.amount_wei]), ["address","uint256"]).root`.
The merkle root is the canonical commitment; the CSV sha256 binds the exact
file of this tool (its `sources` column is tool-specific).

Pin rule (published as implemented): ETH block = the FIRST block with
`timestamp >= instant`; Polygon block = the LAST block with `timestamp <= the
ETH block's timestamp`; both searched over FINALIZED heads. With `--at-utc` and
explicit blocks the tool re-verifies the rule (`T(N-1) < ts <= T(N)`;
`T(M) <= T(N) < T(M+1)`) and records `pinVerified` in index.json. The instant
is parsed as UTC regardless of the machine's time zone.

## `--final` (the real table, runbook B1)

`--final --at-utc <instant>` enforces everything below and refuses `--eth-only`,
`--no-strict`, `--no-second-source`, `--workers > 4` and RPC URLs on argv:
- keyed archive hosts only (`eth-mainnet.g.alchemy.com`, `polygon-mainnet.g.alchemy.com`,
  `lb.drpc.live`) from the environment / `.env`, and a SECOND RPC provider per chain
  (`ETH_ARCHIVE_RPC_2` / `POLYGON_ARCHIVE_RPC_2`) that must agree on `totalSupply` and
  sampled balances;
- both pinned blocks at or below the `finalized` head at run time; an EMPTY `raw/` cache;
- the independent transfer index (Alchemy `alchemy_getAssetTransfers`) identical to
  the Etherscan replay per transaction (multisets of from/to/value); any disputed
  transaction is settled by the RPC receipt - a Transfer log missing from the replay
  is an ERROR, a re-numbered or duplicated index record is not;
- every census difference equal to its PINNED reserve and zero read errors;
- Morpheus bucket balance == 0 at the block; every designation verified on-chain AND
  bound by the second provider's call tracer (`debug_traceTransaction`, callTracer:
  exactly one successful CALL from the contract to the designation address with the Base
  address in THAT call's input - external review, S03);
- every INDIRECT beneficiary (staker, vesting beneficiary, Polygon staker, bridge burner)
  classified on the chain of its source before it is credited: a contract without a
  designation or an address of the excluded set is a strict ERROR (S02);
- a clean working tree at the frozen commit and a `PUBLIC_REF` mapping of that commit to the
  public repository (`scripts/export-public.sh` writes it): the index's `tool` field must
  point at code anyone can read.

### The verdict is in the files (S01)

`index.json` carries `strictOk` and `errors`. A `--final` run with errors is MOVED to
`<out>-FAILED/` (never a deployable-looking bundle), the deploy script refuses an index
without `mode: final` / `strictOk: true` on mainnet, and `verify_deploy.mjs` refuses to
publish such a table when `LIVE`.

### Accepted unsolicited transfers (`--accept-explained`, S07)

The final run asserts EXACT balances of 15 targets (the 13 pinned census contracts, the
bridge tunnel, the Morpheus bucket). One wei sent by anyone into one of them before the block
would otherwise STOP the table, and history cannot be repaired afterwards. The safety valve:

```
--accept-explained <label>:<wei>:<tx>[,<tx>...]        # repeatable; labels = staking-T1..T6,
                                                        # vesting-1..3, polygon-staking-<0x..>,
                                                        # bridge-tunnel, morpheus-bucket
```
Accepted ONLY when the surplus above the pinned reserve equals, to the wei, the sum of the
named TPRO `Transfer` events INTO the target, every named transaction is mined at or before
the block with status 1, is not a direct call to the target and carries no event emitted by
the target itself (a Deposited / addBeneficiaries / bridge flow is a normal flow, never
"unsolicited"), and every named transaction also appears among the unsolicited inflows of the
explorer index (the `census.py --explain` machinery, `explain_inflows`). The amount is then
excluded under the label `unsolicited transfer into <label> (burned)`, recorded in
`summary.json.accepted_explained` and printed. A DEFICIT is never accepted. For the bucket the
flag is used ONLY with the project lead's explicit written go on the day; for the other targets
the operator and the second person decide with the transaction list in front of them (runbook
B1). `monitor_pins.py` runs daily until the snapshot so that nothing here is a surprise.

## What the tool does (and asserts)

1. Ethereum wallets: replays EVERY `Transfer` of the old token since its
   creation (Etherscan logs, block-cursor pagination; the client fails CLOSED on
   any error answer), reads `balanceOf` for every address ever seen at the block
   (Multicall3), asserts `sum == totalSupply` and replay == chain for every address,
   then cross-checks the event set against the independent index.
2. Staking T1-T6: `userInfo(pid, user)` credited to the USER; the per-contract
   difference `balanceOf - sum(userInfo)` MUST equal the pinned reward reserve
   (222,222.00 TPRO across T3-T6 minus 2 wei; one deployer transfer from 2022,
   burned by decision) - anything else is an ERROR, never a label (or an
   explicitly accepted, chain-verified unsolicited surplus - see above).
3. Vestings: `currentBalance(beneficiary)` credited to the beneficiary
   (vesting-2's pinned funding surplus 0.0301 TPRO excluded, labeled).
   Every staker and beneficiary passes the SAME eligibility decision as a direct
   holder, on the chain of the source, BEFORE it is credited: EOA or EIP-7702
   designator = credited; a contract with a verified designation = re-addressed
   to its Base address; a contract without one, or an address of the excluded set
   (0x0, 0xdead, the bucket, MEXC custody, the tokens, the tunnel, the
   PoolManager) = STRICT ERROR, the amount excluded and labeled (S02).
4. Polygon: same replay for the child token; the four pinned StakingV2
   contracts are censused the same way; any OTHER contract holder whose runtime
   bytecode carries the StakingV2 fingerprint (the `Deposited` + `Withdrawn`
   event topics and the `userInfo` selector) or whose verified ABI exposes that
   interface = ERROR to classify, and an explorer lookup that fails for any
   reason other than "not verified" is an ERROR too - no silent empty ABI (H06;
   the same check runs on Ethereum's contract holders); rows credited natively
   (same address on Base). Bridge burners are classified on Polygon (where they
   burned). The Ethereum root tunnel is NOT a row; `tunnel - childSupply` is
   decomposed into the unclaimed exits of identified Polygon burners
   (`summary.json` `bridge`) and CREDITED to those burners by default (policy of
   2026-09-07; rows labeled `bridge-unclaimed-exit`; `--exclude-unclaimed-exits`
   reproduces the pre-policy exclusion for dry-run comparisons and is refused in
   `--final`); a tunnel deposit without a child mint (holder in transit) is an
   ERROR in `--final`.
5. Exclusions: 0x0 / 0xdead, the Morpheus bucket (must be 0), the four MEXC
   custody addresses (their sum = `reserve_exceptions_wei`), the bridge surplus,
   contracts without a designation (`contract-wallets.json`, with the verified
   contract name and a classification label: contract wallet / router / child
   token itself / unclassified), the third-party residual of the Uniswap V4
   PoolManager. The project's two V4 positions are credited EXACTLY (principal +
   fees owed, integer math) as one labeled row `uniswap-v4-LP` of the Liquidity
   wallet; the pool's active liquidity must equal the project's.
6. Code check: `eth_getCode` on every row address on the chain where the
   balance sits; EIP-7702 delegation designators (`0xef0100` + 20 bytes) are
   EOAs; other code -> contract-wallet report. A contract that proved its owner
   through the designation path is re-addressed via
   `scripts/snapshot/contract_owners.json`
   (`{"eth:0xContract": {"base_address": "0xEIP55", "tx": "0x..", "verified_by": "..", "date": ".."}}`,
   strict EIP-55, chain-scoped; the tool verifies the designation transaction
   on-chain) and listed in the summary.
7. Identity: `rows + exclusions == eth totalSupply` - a self-consistency check of
   the assembly; correctness rests on the assertions above. The run exits 1
   unless everything passed (`--no-strict` only for exploration, never for a
   published table).

Raw logs and code lookups are cached under `tmp/snapshot-*/raw/` with chain /
block / completion headers (a cache from another block or an incomplete fetch
is refused); `--final` requires an empty cache.

## Census alone
```bash
.venv/bin/python scripts/snapshot/census.py --block <N> --rpc <archive rpc> [--explain] [--only staking|vesting]
```

## Daily monitor of the pinned targets (until the snapshot)
```bash
.venv/bin/python scripts/snapshot/monitor_pins.py            # state under tmp/monitor-pins-state.json; exit 1 = something flagged
```
