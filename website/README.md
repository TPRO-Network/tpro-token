# Claim page - claim.tpro.network (v3: snapshot + merkle claim)

Static, self-contained claim page for the v3 model: a published snapshot table
(address, amount) with a merkle root, and an immutable distributor on Base that
pays each row exactly once to the address itself. The page looks a row up,
verifies its proof locally against the root, and sends the one claim
transaction. Two files plus a host config, no build step:

- `index.html` - the whole app: styles + inline JS. **ZERO external
  dependencies by policy** - no CDN scripts, no wallet SDKs, no fonts. A page
  where people move money must have a reviewable surface of exactly two files;
  a compromised CDN script could otherwise swap the contract address. All
  chain access is raw JSON-RPC (`fetch`), all encoding is hand-rolled with
  precomputed selectors (verify each against `cast sig` - they are commented),
  all amount math is BigInt (never floats), and keccak256 is implemented
  inline (Keccak-f[1600]) with a mandatory self-test at boot.
- `config.js` - the ONLY deploy-time surface: addresses, the RPC list, the three
  pins (root, CSV sha256, SHA256SUMS sha256), the table location, the `LIVE` flag,
  the designation address, the links.
- `vercel.json` - the host's security headers (CSP with `frame-ancestors 'none'`,
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, HSTS) and `no-cache` on
  the files that change at the T0 flip. Meta CSP in `index.html` mirrors it.
- `verify_deploy.mjs` - the gate: run it before EVERY push and after every deploy
  (section "Publish").

The table format the page reads is `docs/v3-interface.md` section 2
(`table/index.json`, `snapshot-<eth>-<poly>.csv`, `proofs/<xx>.json` shards,
`summary.json`, `contract-wallets.json`, `SHA256SUMS`). All 256 proof shards must
exist (empty ones as `{}`): the page treats a missing shard as an ERROR, never as
"not in the table" (fail-closed). `table-sample/` holds a 3-row sample in that
exact format for local testing (`make_sample.mjs` regenerates it and documents
the merkle algorithm - it is NOT a snapshot).

## Safety properties built into the page

Fail-closed: the CLAIM button arms only after ALL of these pass, and any
failure disables it (it never degrades):

1. **Keccak self-test** at boot against `cast keccak` vectors (empty string,
   `"abc"`, one full 136-byte block of zeros, 200 zero bytes, the
   `abi.encode(address(1), 1)` word pair + its double-hashed leaf) plus the
   EIP-55 reference vector. A failing hash could make a forged proof "verify",
   so a failure kills the page.
2. **Three roots must agree:** the root pinned in `config.js`, the root in
   the served `index.json`, and `merkleRoot()` read from the distributor on
   Base. A mismatch anywhere = red banner, all actions dead. The on-chain root
   is read from TWO RPC providers when two are reachable; a disagreement kills
   the page (an immutable can never legitimately differ).
3. **Token + genesis cross-check:** `token()` must equal the configured TPRO
   address and `genesis()` must equal the table's `totalWei`.
4. **Served files anchored to the pins (round 4, R3-3; state machine fixed after
   the external review of 2026-09-08, S05):** `SHA256SUMS` must hash to the pinned
   `sha256sumsSha256`; `index.json` and every shard the page reads must hash to
   their `SHA256SUMS` lines; the CSV line must equal `csvSha256`. The table load
   works on locals and publishes `tableIndex` / `pinnedRoot` / `sums` together
   with `tableVerified` only after EVERY check passed - a checksum file that is
   unreachable or withheld leaves the page UNVERIFIED (no rows/total shown, no
   lookups, CLAIM off, a line saying so) and the timer / REFRESH re-run the whole
   table load until it passes; a mismatch or a malformed file KILLS. With a pinned
   checksum file that is not loaded, every table read fails ("hash only when the
   sums exist" is gone). `tableVerified` and `chainVerified` are separate flags;
   arming and sending require both. Only after that does the page say "not in the
   published table" - a dropped shard entry on a compromised host is a KILL, not a
   confident denial. Without pins (test copies only) absence answers are labeled
   UNVERIFIED.
5. **Local proof verification** of the looked-up row (leaf =
   `keccak256(keccak256(abi.encode(address, amount)))`, sorted-pair walk)
   against the verified root. A shard that does not verify kills the page.
6. **Re-verification right before sending:** the root, `isClaimed` AND the
   latest block timestamp vs `sunsetTime()` are read again, the proof is
   re-walked, and the exact call is **simulated (`eth_call` from the account)**;
   a revert is decoded (`NotYourRow`, `ClaimWindowClosed`, `AlreadyClaimed`,
   `InvalidProof`) and stops the send.
7. **Only the address itself:** the button arms only when the connected
   account equals the looked-up address (the contract enforces
   `msg.sender == account`; the page just refuses to build a doomed tx).
8. **Reads go through the PUBLIC RPC list in config, never the wallet provider**
   (a malicious wallet cannot spoof the root, the status or the receipt); every
   endpoint's `eth_chainId` is verified before use; reads fail over; REFRESH
   rotates the first endpoint (a lying `isClaimed` from one provider is escaped
   by a refresh); the poll interval backs off to 5 minutes after the first
   success. **Deadlines and bounds (external review 2026-09-08, S06):** every
   `fetch` carries `AbortSignal.timeout` covering headers AND body (10 s per
   JSON-RPC request, 20 s per table file), bodies are size-capped (1 MiB RPC,
   8 MiB table file), retries are bounded (one pass over the endpoint list per
   read; the receipt poll 60 x 3 s). The root quorum asks the providers
   CONCURRENTLY under one 12 s deadline and takes the first two answers (3 s of
   grace for a second answer after the first) - a stalled first provider can
   neither block a healthy one nor hang the page; an endpoint that failed or
   stalled is tried LAST afterwards (never dropped: REFRESH still rotates).
9. **XSS containment:** RPC results, fetched table files and wallet return
   values are UNTRUSTED. Every remote value is validated against strict shapes
   (`VALID_HASH`/`VALID_ADDR`/`VALID_HEX`/`VALID_DEC`/`VALID_FILE`/`VALID_SHA`)
   before it can influence anything; user/wallet-supplied values render via
   `textContent` / DOM building, never markup interpolation. CSP (meta + header)
   allows only `config.js`, the inline app, same-origin table files and the
   listed RPC hosts.
10. **The wallet prompt is described before every claim (round 4, R3-5):** to
    = the checksummed distributor, value 0 ETH, Base, data prefix `0x3d13f874`,
    one transaction, never a signature or approval, never Ethereum/Polygon; the
    tx carries `value: "0x0"` explicitly. The distributor and token addresses
    are shown in STATUS, checksummed.
11. **Stacked banners:** kill > rehearsal/host > preview > sunset > note - a
    later message never overwrites an earlier warning (round 4, R3-8).
12. **EIP-55:** a mixed-case address with a wrong checksum is refused as a typo;
    every address is rendered checksummed (round 4, R3-9). Amounts show the
    exact wei next to the 2-decimal figure; dates show UTC and local time.
13. **Contract-wallet marker (amendment 21; chain-keyed since the external
    review, S08):** `contract-wallets.json` is keyed `<chain>:<0xlower>` (`eth` |
    `polygon`). An address absent from the table is looked up under BOTH keys;
    each hit renders its own marker "CONTRACT WALLET ON ETHEREUM / ON POLYGON -
    DESIGNATION NEEDED" (or "- RE-ADDRESSED") naming the chain from the KEY, with
    the designation address; an entry whose `chain` field disagrees with its key
    is malformed and never rendered (console warning). Bare-address keys are not
    looked up. Plus an informational, NON-BLOCKING `eth_getCode` check of the
    looked-up address on Base (EIP-7702 designators `0xef0100...` count as EOAs)
    whose marker appears when the read arrives - it never delays the row or the
    CLAIM button.
14. **Manual path always available (ordering fixed after the external review,
    S06):** "Claim without this page" prints the exact `claim(account, amount,
    proof)` arguments and calldata with the Basescan write-contract link. It is
    rendered right after the LOCAL proof verification and BEFORE any chain read:
    when the contract cannot be verified or `isClaimed` cannot be read, the row
    stays visible with status "unknown - ... REFRESH", the manual box stays, and
    CLAIM stays off (arming needs a KNOWN unclaimed status), so no RPC outage can
    deny a determined holder the exact call.
15. **Receipt bound to the claim (external review, H07):** after the wallet
    returns a hash, "Confirmed" is shown only for a receipt with `status 0x1`,
    `to` == the distributor and a `Claimed(account, amount)` log from the
    distributor whose indexed account is the connected address; any other
    successful receipt (a wallet returning an unrelated hash, a replaced
    transaction) reads "confirmed on chain but not as a claim - REFRESH" and the
    row is re-read from the contract.

Claim calldata: `0x3d13f874` + `abi.encode(account, amount, proof)` (head:
account word, amount word, array offset `0x60`; tail: length word + elements).
Cross-checked against `cast calldata "claim(address,uint256,bytes32[])" ...`.

## The two states of an OFFICIAL deployment (`LIVE: true`)

- **Preview** (`distributor` all zeros): the page serves the published table for
  review - lookups and local proof checks work, the STATUS panel shows
  rows/total/blocks from `index.json`, the CONNECT button is hidden (before
  launch the official page NEVER asks for a wallet - say so in comms), the
  banner reads "PREVIEW - ... claims open at launch".
- **Live** (`distributor` + `token` set): the three-way root check runs, claims
  are on. `LIVE: true` REQUIRES all three pins - the page kills itself otherwise.

`LIVE: false` = a rehearsal / testnet / local copy: sticky "TEST COPY" banner,
pins optional (absence answers marked unverified), the host warning is off.

## Publish (preview publications before launch, the final table before the claim opens, the T0 flip)

1. Copy the tool's `table/` output next to `index.html` (same origin: the page
   fetches it with relative URLs). The tool's last lines print the three pins.
2. Fill `config.js`: `table.merkleRoot`, `table.csvSha256`,
   `table.sha256sumsSha256` (copy from the tool output), `table.ethBlock` /
   `table.polygonBlock`, `designation.address` (once published), the `links`,
   `LIVE: true`. Preview: `distributor`/`token` stay zero. T0 flip: fill
   `base.token`, `base.distributor`, `base.deployBlock` and NOTHING else.
3. GATE: `node website/verify_deploy.mjs website` (add `--rpc` after T0) must
   print `ALL OK`. It checks every pin against the served table, every
   SHA256SUMS line, the 256 shards, the `contract-wallets.json` key format
   (`<eth|polygon>:<0xlower>`, `chain` field == key prefix), the table's own
   validation verdict (with `LIVE: true` the index must carry `strictOk: true`
   and `errors: 0`; with a distributor set it must also be `mode: "final"` -
   the same gate the deploy script enforces on mainnet), the CSP hosts vs the
   RPC list, and (with `--rpc`) `merkleRoot()`, `genesis()`, `token()`,
   `sunsetTime()` on Base; every network request in the gate has a 10 s
   deadline. A second person runs it independently and reads the pins back
   aloud.
4. Push to the deploy repo (recommendation: a DEDICATED repo + Vercel project
   for claim.tpro.network with this `vercel.json`, separate from nft.tpro.network;
   who holds push rights = who can change `config.js` - two people, hardware
   2FA). rsync `website/` (without `table-sample/`, with `table/`) -> commit
   -> push -> the host deploys.
5. Post-deploy: `node website/verify_deploy.mjs website --served https://claim.tpro.network`
   compares the SERVED `index.html`, `config.js`, `table/index.json`,
   `table/SHA256SUMS` with the committed files, checks the security headers and
   the hostname. Then open the page from an external network (mobile data):
   STATUS shows the green "verified live" line, a known row looks up with
   "verifies against the root", a random address shows "not in the published
   table" with "served table verified intact", the SHA256SUMS link resolves.
6. DNS: `claim.tpro.network` must point at the host that serves this page and
   nothing else (round 4, R3-1 found it pointing at an unused ISP address);
   `dig +short claim.tpro.network` is a checklist line at every publication and at T0.

Rollback of a wrong `config.js`: push the previous commit; the page fails closed
meanwhile (the three-way check), which is the right failure.

## Local test with the sample table

```bash
cd website/table-sample && node make_sample.mjs      # regenerates the 3-row sample (optional)
cd .. && python3 -m http.server 8080                  # serve website/ (same origin for the table)
# in config.js set   table.url: "./table-sample/"   (distributor stays zero = preview mode, LIVE false)
# open http://localhost:8080/ and look up 0x70997970C51812dc3A010C7d01b50e0d17dc79C8
```
Expected: 42.00 TPRO, "preview - claims open at launch", "verifies against the
root"; `0x0000000000000000000000000000000000000001` -> "not in the served table
(unverified copy)" because the sample is not pinned. The sample root is
`0x2f249a62a4407e2d6845a55e4a66d3f475afeefbcf47029228a725992c7a944d`
(anvil test accounts 0-2, made-up amounts; leaves and root cross-checked with
`cast keccak` step by step). Change `table.url` back to `./table/` before deploying.

## Backlog (post-launch)

- Claimed-events history per address (needs an indexer or log scanning on Base).
- Localized copy (PL/KM) once the English copy is frozen with comms.
- EIP-6963 multi-wallet discovery (today: `window.ethereum`, last injector wins).
- Optional: pin the inline script with a CSP `sha256-...` source instead of
  `'unsafe-inline'` (recompute on every edit; verify_deploy.mjs can print it).
