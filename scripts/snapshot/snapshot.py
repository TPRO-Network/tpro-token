#!/usr/bin/env python3
"""snapshot.py - the v3 snapshot table: who holds how much old TPRO at a pinned Ethereum block
(+ the Polygon block at the same timestamp), reduced to ONE row per address, plus the merkle root
the Base claim contract is deployed with. READ-ONLY, reproducible by anyone from public archive RPCs.

Method (the v3 build task, steps 1a-1g; decisions doc = the project decision record):
  1a  Ethereum wallets: replay EVERY Transfer of the old token from its creation block (Etherscan API v2 logs,
      block-cursor pagination) -> candidate set; then balanceOf(addr) at the block for EVERY address ever seen
      (Multicall3, archive RPC); assert sum == totalSupply and replay == chain for every address. A SECOND,
      independent event index (Alchemy asset transfers) must list exactly the same events (round 4, R2-14).
  1b  Staking T1-T6: per-user userInfo (census.py) credited to the USER; balanceOf - sum(userInfo) MUST equal the
      PINNED reward reserve of that contract (222,222 TPRO in total, burned - the project lead 2026-09-03); anything else
      is an ERROR, never a label (round 4, R2-1).
  1c  The three vesting contracts (vesting-1..3, creation order): currentBalance per beneficiary credited to the
      beneficiary; the funding surplus of vesting-2 (0.0301 TPRO) is pinned the same way.
  1d  Polygon: child-token holders at the Polygon block (same replay method) + the FOUR pinned Polygon staking
      contracts (userInfo, credited natively; a fifth staking-like contract is an ERROR). The root tunnel is
      NOT a row; root-tunnel minus child-supply = the unclaimed L1 exits, decomposed to their burners.
  1e  Exclusions: 0x0/0xdead, the Morpheus bucket (MUST be 0 at the block in --final mode), the four MEXC custody
      addresses (pinned; their sum = the exceptions reserve), bridge surplus, contracts without a designation
      (contract-wallet report), the third-party remainder of the Uniswap V4 PoolManager. The project's two V4
      positions are credited EXACTLY (principal + fees owed, integer math) as ONE labeled row "uniswap-v4-LP"
      of the Liquidity wallet (the project lead 2026-09-03); nothing that is not ours is credited to us (round 4, R2-3).
  1f  Code check on every row address (chain where the balance sits): EIP-7702 delegations count as EOAs;
      other code -> contract-wallet report (excluded unless re-addressed via a VERIFIED designation in
      contract_owners.json). INDIRECT beneficiaries (stakers, vesting beneficiaries, Polygon stakers, bridge
      burners) get the same chain-aware decision BEFORE they are credited: a contract without a designation or
      an excluded address is a STRICT ERROR, never a silent row (external review 2026-09-08, S02).
  1g  Output: table/snapshot-<eth>-<poly>.csv (address,amount_wei = the merkle leaves; the operator copy next to the
      run adds the source labels and summary.json, both never served), merkle root in the
      OpenZeppelin StandardMerkleTree ["address","uint256"] format + proof shards, contract-wallets.json,
      SHA256SUMS, sha256 of the CSV and of SHA256SUMS (both pinned in the claim page's config).

Usage (from the repo root; Python 3.12 + scripts/snapshot/requirements.lock in .venv - see scripts/snapshot/README.md):
  .venv/bin/python scripts/snapshot/snapshot.py [--at-utc <YYYY-MM-DDTHH:MM:SSZ> | --eth-block N [--polygon-block M]]
        [--final] [--eth-only] [--workers 8] [--out DIR] [--no-strict] [--no-second-source]
        [--designation-address 0x..] [--exclude-unclaimed-exits] [--accept-explained LABEL:WEI:TX[,TX...]]...
  --accept-explained (external review 2026-09-08, S07): a pinned target (staking-T1..T6, vesting-*, polygon-staking-*,
  bridge-tunnel, morpheus-bucket) whose balance sits ABOVE its pin by exactly the named unsolicited transfers is
  accepted: the tool verifies every named tx on-chain (mined <= the block, status 1, a TPRO Transfer INTO the target,
  no event emitted by the target, not a direct call to it; the transfers sum to WEI exactly) and against the explorer's
  inflow index, then excludes the amount under the label "unsolicited transfer into <label> (burned)". A deficit is
  never accepted. For the bucket the flag needs the project lead's written GO on the day (runbook B1).
  Unclaimed bridge exits (Polygon burns never exited on Ethereum) are CREDITED to their burners by DEFAULT
  (policy, the project lead 2026-09-07, amendment 27; rows labeled bridge-unclaimed-exit). --exclude-unclaimed-exits is
  the pre-decision dry-run shape (decision 4 exclusion) and is REFUSED in --final.
  --at-utc pins both blocks from an instant: ETH = FIRST block with timestamp >= instant, Polygon = LAST block
  with timestamp <= that ETH block's timestamp. The search runs over FINALIZED blocks only.
  --final = the mode of the real table (runbook B1): refuses --eth-only / --no-strict / --no-second-source /
  --workers > 4 / RPC URLs on argv / public endpoints / a non-empty raw/ cache; requires --at-utc, the second
  RPC provider for both chains, finalized blocks, Morpheus bucket == 0, verified designations.
Secrets: ETHERSCAN_API_KEY, ETH_ARCHIVE_RPC, POLYGON_ARCHIVE_RPC (+ ETH_ARCHIVE_RPC_2, POLYGON_ARCHIVE_RPC_2 - the
second provider, which must also serve debug_traceTransaction for designations) from the environment or the repo's
.env. Never printed: endpoints are published as scheme://host only, every error path is scrubbed. MEXC_ETH_ADDRESS_1..4
in the environment must match the pinned constants (they are public addresses; the constants are the source of truth).
Exit 1 (strict) if anything fails to reconcile; the numbers are findings, never knobs. index.json records the verdict
(`strictOk`, `errors`); a failed --final run is moved to <out>-FAILED/ so it never looks like a deployable bundle (S01).
"""
import argparse, calendar, csv, hashlib, json, os, re, subprocess, sys, time
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, getcontext
from eth_abi import encode as abi_encode
from eth_utils import keccak, is_checksum_address, to_checksum_address
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.providers.rpc import HTTPProvider
import requests


class PatientHTTPProvider(HTTPProvider):
    """HTTPProvider that survives a keyed provider's throughput throttling (dress rehearsal 2026-09-07: three
    back-to-back runs hit Alchemy's HTTP 429 and the tool died with FATAL - web3's built-in retry is 5 attempts
    x 0.125 s, far shorter than a throttling window). Every method (incl. alchemy_getAssetTransfers) is retried
    on HTTP 429 / 5xx, connection errors and timeouts with a linear backoff (2, 4, ... 20 s; ~2 min in total);
    a persistent failure is raised with scheme://host only (never the keyed URL)."""
    ATTEMPTS = 10

    def __init__(self, endpoint_uri, request_kwargs=None, **kw):
        # our loop below is the ONLY retry (web3's built-in 5 x 0.125 s would multiply the requests, not the patience)
        super().__init__(endpoint_uri, request_kwargs=request_kwargs, exception_retry_configuration=None, **kw)

    def make_request(self, method, params):
        last = None
        for i in range(self.ATTEMPTS):
            try:
                return super().make_request(method, params)
            except requests.exceptions.HTTPError as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"RPC {rpc_label(str(self.endpoint_uri))}: HTTP {code} on {method}") from None
                last = f"HTTP {code}"
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last = type(e).__name__
            if i < self.ATTEMPTS - 1:
                print(f"    [rpc] {rpc_label(str(self.endpoint_uri))}: {last} on {method} - retry {i + 1}/{self.ATTEMPTS - 1} in {2.0 * (i + 1):.0f}s", flush=True)
                time.sleep(2.0 * (i + 1))
        raise RuntimeError(f"RPC {rpc_label(str(self.endpoint_uri))}: {last} on {method} after {self.ATTEMPTS} attempts (throttled or down)")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from census import (Etherscan, load_key, census_staking, census_vesting, explain_inflows, STAKING, VESTING, TPRO, ERC20,
                    hexstr, TRANSFER_TOPIC, REPO, scrub, fatal_guard, register_secret_url)
from merkle import StandardMerkleTree, write_shards, verify

getcontext().prec = 60
CHILD = "0xd24157aa1097486dc9d7cf094a7e15026e566b5d"
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dEaD"
BUCKET = "0xAE6C59600a860D9e87E4c7E0B8511996d252Fd25"          # Morpheus burn bucket - MUST read 0 at the snapshot block
ROOT_TUNNEL = "0xfC8bfD0Fd762EC4acaEffe4b6F8987D5259D3137"     # FxCustomERC20RootTunnel (backs the Polygon child)
POOL_MANAGER = "0x000000000004444c5dc75cB358380D2e3dE08A90"    # Uniswap V4 PoolManager (Ethereum)
POSITION_MANAGER = "0xbD216513d74C8cf14cf4747E6AaA6420FF64ee9e"
STATE_VIEW = "0x7fFE42C4a5DEeA5b0feC41C94C136Cf115597227"
V4_POSITIONS = [21106, 45911]                                   # active + out-of-range, both owned by LIQUIDITY
LIQUIDITY = "0x786fDf0d8570c1637FcEcdC1B06405DFE715492B"
TREASURY = "0x69e77e8146f43bb591211c0283f16549a36fefb6"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CHAINLINK_ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"
# MEXC custody (public on-chain addresses, published with the table; decision 4 + amendment 15). The env
# values MEXC_ETH_ADDRESS_1..4, when present, must match this set - the constants are the source of truth.
MEXC_CUSTODY = [
    "0x75e89d5979E4f6Fba9F97c104c2F0AFB3F1dcB88",
    "0xe7566A01c0Af00B90794b1DafAf7eEef23DE8678",
    "0x3CC936b795A188F0e246cBB2D74C5Bd190aeCF18",
    "0x9642b23Ed1E01Df1092B92641051881a322F5D4E",
]
# The four Polygon StakingV2 instances (partner spec, appendix A). Any OTHER contract exposing the StakingV2
# interface among the child-token holders is an ERROR to classify, never an automatic census.
POLYGON_STAKING = {
    "polygon-staking-0x34b9": "0x34b98A28Ef7981550EdA3374494b24Ae0EC51eDB",
    "polygon-staking-0x4957": "0x4957347d7ae843d8f871c007C084de60f93BE70d",
    "polygon-staking-0x9687": "0x9687a64C8bC8Fe94F1b3a5347D11b5B52fA6dA95",
    "polygon-staking-0xb412": "0xb4129cA80544029bb4Def43Ff5B9BA9433569F07",
}
# Pinned census differences, in wei (balanceOf(contract) - sum of the per-user reads). Ethereum stakings:
# the unused TPRO reward reserve the deployer sent in September 2022 (block 15567531, tx 0xbe4815e2...0378),
# 222,222 TPRO minus 2 wei in total = BURNED (decision 4). vesting-2: the deployer's funding surplus of 0.0301
# TPRO (contract arithmetic). Everything else must reconcile to the wei. ANY other difference = ERROR.
RESERVE_WEI = {
    "staking-T1": 0,
    "staking-T2": 0,
    "staking-T3": 1188128487800390787844,
    "staking-T4": 3430590759355410892611,
    "staking-T5": 12781164685292378214657,
    "staking-T6": 204822116067551820104886,
    "vesting-1": 0,
    "vesting-2": 30100000000000000,
    "vesting-3": 0,
    "polygon-staking-0x34b9": 0,
    "polygon-staking-0x4957": 0,
    "polygon-staking-0x9687": 0,
    "polygon-staking-0xb412": 0,
}
COMMUNITY_TRANCHE_WEI = 25_000_000 * 10**18                    # amendment 15 (the project lead 2026-09-04)
ALLOWED_ARCHIVE_HOSTS = {"eth-mainnet.g.alchemy.com", "polygon-mainnet.g.alchemy.com", "lb.drpc.live"}
MULTICALL_ABI = [{"inputs": [{"components": [{"name": "target", "type": "address"}, {"name": "allowFailure", "type": "bool"},
                  {"name": "callData", "type": "bytes"}], "name": "calls", "type": "tuple[]"}], "name": "aggregate3",
                  "outputs": [{"components": [{"name": "success", "type": "bool"}, {"name": "returnData", "type": "bytes"}],
                  "name": "returnData", "type": "tuple[]"}], "stateMutability": "payable", "type": "function"}]
ERC20_FULL = ERC20 + [{"inputs": [], "name": "totalSupply", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}]
POSM_ABI = [{"inputs": [{"type": "uint256"}], "name": "getPoolAndPositionInfo", "outputs": [
                {"components": [{"name": "currency0", "type": "address"}, {"name": "currency1", "type": "address"},
                                {"name": "fee", "type": "uint24"}, {"name": "tickSpacing", "type": "int24"}, {"name": "hooks", "type": "address"}],
                 "type": "tuple"}, {"type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [{"type": "uint256"}], "name": "getPositionLiquidity", "outputs": [{"type": "uint128"}], "stateMutability": "view", "type": "function"},
            {"inputs": [{"type": "uint256"}], "name": "ownerOf", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"}]
STATE_VIEW_ABI = [
    {"inputs": [{"type": "bytes32"}], "name": "getSlot0", "outputs": [{"type": "uint160"}, {"type": "int24"}, {"type": "uint24"}, {"type": "uint24"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getLiquidity", "outputs": [{"type": "uint128"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}, {"type": "int24"}, {"type": "int24"}], "name": "getFeeGrowthInside",
     "outputs": [{"type": "uint256"}, {"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}, {"type": "address"}, {"type": "int24"}, {"type": "int24"}, {"type": "bytes32"}], "name": "getPositionInfo",
     "outputs": [{"type": "uint128"}, {"type": "uint256"}, {"type": "uint256"}], "stateMutability": "view", "type": "function"}]
CHAINLINK_ABI = [{"inputs": [], "name": "latestRoundData", "outputs": [{"type": "uint80"}, {"type": "int256"}, {"type": "uint256"}, {"type": "uint256"}, {"type": "uint80"}],
                  "stateMutability": "view", "type": "function"}]
V4_INITIALIZE_TOPIC = "0x" + keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
SEL_BALANCE_OF = bytes.fromhex("70a08231")
E18 = Decimal(10) ** 18
Q96, Q128, U256 = 1 << 96, 1 << 128, 1 << 256
RX_TX = re.compile(r"^0x[0-9a-f]{64}$", re.I)
RX_ADDR = re.compile(r"^0x[0-9a-f]{40}$", re.I)
RX_HEXQ = re.compile(r"^0x[0-9a-f]+$", re.I)
# StakingV2 runtime-bytecode fingerprint (external review 2026-09-08, H06): the two event topics and the userInfo
# selector are PUSHed as constants, so any contract that logs Deposited/Withdrawn and answers userInfo carries all
# three in its code - no explorer needed. Checked 2026-09-09 on the cached codes: exactly the four pinned Polygon
# stakings (and five of the six Ethereum ones; T1 is an older build) match, no other holder does.
STAKING_V2_TOPICS = (keccak(text="Deposited(address,uint256,address,uint256)").hex(),
                     keccak(text="Withdrawn(address,uint256,address,uint256)").hex())
USERINFO_SELECTOR = keccak(text="userInfo(uint256,address)").hex()[:8]
PUBLIC_REPO = "https://github.com/TPRO-Network/tpro-token"


def tpro(wei):
    return f"{Decimal(wei) / E18:,.2f}"


def rpc_label(url):
    """Endpoint label safe to publish: scheme + host only (keyed archive URLs carry the key in the path/query)."""
    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname}"


def cs(a):
    return Web3.to_checksum_address(a)


def load_env_value(name):
    v = os.environ.get(name)
    if v:
        return v
    p = os.path.join(REPO, ".env")
    if os.path.exists(p):
        for line in open(p):
            s = line.strip()
            if s.startswith("export "):
                s = s[len("export "):].lstrip()
            if s.startswith(name + "="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def jload(path):
    with open(path) as f:
        return json.load(f)


def jdump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, default=str)


def sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def parse_utc(s):
    """'YYYY-MM-DDTHH:MM:SSZ' -> unix seconds, independent of the machine's time zone and DST (round 4, R2-5)."""
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


# ---------------------------------------------------------------- block pinning
def find_block(w3, ts, mode, hi):
    """mode 'first_at_or_after': first block with timestamp >= ts; 'last_at_or_before': last block with
    timestamp <= ts. `hi` = the highest block the search may return (the FINALIZED head). The head must have
    passed the instant, otherwise the true answer is not yet visible -> SystemExit "wait" (round 4, R2-10)."""
    cache = {}
    def T(n):
        if n not in cache:
            cache[n] = w3.eth.get_block(n)["timestamp"]
        return cache[n]
    lo = 1
    if mode == "first_at_or_after":
        if T(hi) < ts:
            raise SystemExit(f"block {hi} (finalized head) is before the requested instant - wait and retry")
        while lo < hi:
            mid = (lo + hi) // 2
            if T(mid) >= ts:
                hi = mid
            else:
                lo = mid + 1
        return lo
    if T(lo) > ts:
        raise SystemExit("chain starts after the requested instant")
    if T(hi) <= ts:
        raise SystemExit(f"block {hi} (finalized head) has not passed the instant yet - wait and retry")
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if T(mid) <= ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def verify_pin(w3e, w3p, eth_block, poly_block, ts):
    """The published rule, checked on explicit blocks: ETH = first block with T >= ts (so T(N-1) < ts <= T(N));
    Polygon = last block with T <= T(N) (so T(M) <= T(N) < T(M+1)). Returns the list of violations."""
    errs = []
    tN = w3e.eth.get_block(eth_block)["timestamp"]
    tN1 = w3e.eth.get_block(eth_block - 1)["timestamp"]
    if not (tN1 < ts <= tN):
        errs.append(f"ETH block {eth_block} violates the pin rule: T(N-1) {tN1} < ts {ts} <= T(N) {tN} is false")
    if poly_block is not None and w3p is not None:
        tM = w3p.eth.get_block(poly_block)["timestamp"]
        tM1 = w3p.eth.get_block(poly_block + 1)["timestamp"]
        if not (tM <= tN < tM1):
            errs.append(f"Polygon block {poly_block} violates the pin rule: T(M) {tM} <= T(N) {tN} < T(M+1) {tM1} is false")
    return errs


def finalized_number(w3, label, errors, final):
    """The finalized head - required in EVERY mode (a preview table is published too). No fallback to a
    recent head: an endpoint that cannot serve the 'finalized' tag is not fit for a table."""
    try:
        return w3.eth.get_block("finalized")["number"]
    except Exception as e:
        raise SystemExit(f"{label}: the RPC does not serve the 'finalized' tag ({scrub(e)}) - use an archive endpoint that does")


# ---------------------------------------------------------------- transfers replay + on-chain verification
def fetch_transfers(es, chain, token, from_block, to_block, cache_path):
    if os.path.exists(cache_path):
        d = jload(cache_path)
        ok = (d.get("complete") is True and d.get("chain") == chain and d.get("token", "").lower() == token.lower()
              and d.get("from_block") == from_block and d.get("to_block") == to_block)
        if not ok:
            raise SystemExit(f"transfer cache {cache_path} is incomplete or for another chain/token/range - delete it (round 4, R2-11)")
        print(f"    transfers: {len(d['logs'])} from cache {cache_path}", flush=True)
        return d["logs"]
    t0 = time.time()
    raw = es.logs_by_topics(token, from_block, to_block, topic0=TRANSFER_TOPIC)
    logs = []
    for lg in raw:
        if len(lg.get("topics") or []) != 3:
            continue        # not a standard 3-topic Transfer (should not happen on these tokens; counted below)
        li = lg.get("logIndex") or "0x0"
        logs.append({"from": cs("0x" + hexstr(lg["topics"][1])[-40:]), "to": cs("0x" + hexstr(lg["topics"][2])[-40:]),
                     "value": str(int(lg["data"], 16)) if lg.get("data") not in (None, "0x") else "0",
                     "block": int(lg["blockNumber"], 16), "tx": lg["transactionHash"], "logIndex": int(li, 16) if li != "0x" else 0})
    skipped = len(raw) - len(logs)
    # written only after a CLEAN completion (an exception above never reaches this line)
    jdump({"chain": chain, "token": token, "from_block": from_block, "to_block": to_block, "complete": True,
           "logs": logs, "skipped_non_standard": skipped}, cache_path)
    print(f"    transfers: {len(logs)} fetched ({skipped} non-standard skipped) in {time.time()-t0:.0f}s -> {cache_path}", flush=True)
    return logs


def replay(logs):
    bal, minted, burned = {}, 0, 0
    for lg in logs:
        v = int(lg["value"])
        if lg["from"] == ZERO:
            minted += v
        else:
            bal[lg["from"]] = bal.get(lg["from"], 0) - v
        if lg["to"] == ZERO:
            burned += v
        else:
            bal[lg["to"]] = bal.get(lg["to"], 0) + v
    return bal, minted, burned


def multicall_balances(w3, token, addrs, block, batch=600, workers=4):
    mc = w3.eth.contract(address=cs(MULTICALL3), abi=MULTICALL_ABI)
    token = cs(token)
    out = {}
    chunks = [addrs[i:i + batch] for i in range(0, len(addrs), batch)]

    def one(chunk):
        calls = [(token, False, SEL_BALANCE_OF + abi_encode(["address"], [a])) for a in chunk]
        for attempt in range(4):
            try:
                res = mc.functions.aggregate3(calls).call(block_identifier=block)
                return {a: int.from_bytes(r[1], "big") for a, r in zip(chunk, res)}
            except Exception as e:
                err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"multicall failed: {scrub(err)}")

    with ThreadPoolExecutor(workers) as ex:
        for d in ex.map(one, chunks):
            out.update(d)
    return out


def get_codes(w3, chain, addrs, block, cache_path, workers=8):
    cache = {}
    if os.path.exists(cache_path):
        d = jload(cache_path)
        if not (isinstance(d, dict) and d.get("chain") == chain and d.get("block") == block and isinstance(d.get("codes"), dict)):
            raise SystemExit(f"code cache {cache_path} is for another chain/block or malformed - delete it (round 4, R2-11)")
        cache = d["codes"]
    todo = [a for a in addrs if a not in cache]

    def one(a):
        for attempt in range(4):
            try:
                return a, w3.eth.get_code(a, block_identifier=block).hex()
            except Exception as e:
                err = e
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"getCode failed for {a}: {scrub(err)}")

    if todo:
        t0 = time.time()
        with ThreadPoolExecutor(workers) as ex:
            for a, code in ex.map(one, todo):
                cache[a] = code if code.startswith("0x") else "0x" + code
        jdump({"chain": chain, "block": block, "codes": cache}, cache_path)
        print(f"    getCode: {len(todo)} fetched in {time.time()-t0:.0f}s ({len(addrs)-len(todo)} cached)", flush=True)
    return {a: cache[a] for a in addrs}


_FACTS = {}


def contract_facts(es, addr):
    """Explorer facts about a contract in ONE strict lookup (getsourcecode carries the name AND the ABI). Three
    outcomes, never a silent default (external review 2026-09-08, H06): a verified contract -> {"name", "abi"};
    the documented "not verified" answer -> {"name": "(unverified)", "abi": None}; anything else (network failure,
    rate limit, a rejected key, an unexpected shape) -> RAISED, so the caller records an error."""
    key = (es.chainid, addr.lower())
    if key in _FACTS:
        return _FACTS[key]
    try:
        res = es.get(module="contract", action="getsourcecode", address=addr).get("result")
    except Exception as e:
        raise RuntimeError(f"explorer lookup failed for {addr}: {scrub(e)}") from None
    if not isinstance(res, list) or not res or not isinstance(res[0], dict):
        raise RuntimeError(f"explorer getsourcecode({addr}) returned an unexpected shape: {scrub(str(res)[:80])}")
    r = res[0]
    abi_text = str(r.get("ABI") or "")
    if abi_text.startswith("["):
        try:
            abi = json.loads(abi_text)
        except Exception as e:
            raise RuntimeError(f"ABI of {addr} is not valid JSON: {scrub(e)}") from None
        facts = {"name": r.get("ContractName") or "(unnamed)", "abi": abi}
    elif "not verified" in abi_text.lower():
        facts = {"name": "(unverified)", "abi": None}
    else:
        raise RuntimeError(f"explorer getsourcecode({addr}) answered neither an ABI nor 'not verified': {scrub(abi_text[:80])}")
    _FACTS[key] = facts
    return facts


def contract_name(es, addr):
    """Verified contract name from the explorer (annotation only - never a decision input)."""
    try:
        return contract_facts(es, addr)["name"]
    except Exception:
        return "(lookup failed)"


def staking_v2_shaped(code):
    """True when the runtime bytecode carries both StakingV2 event topics and the userInfo selector (H06)."""
    c = (code or "").lower()
    return all(t in c for t in STAKING_V2_TOPICS) and USERINFO_SELECTOR in c


def unknown_staking_check(es, chain, addr, code):
    """(H06) A NON-PINNED contract holder that looks like a StakingV2 is an ERROR to classify before the table:
    (1) the bytecode fingerprint (no explorer needed), (2) the verified ABI when there is one (userInfo + Deposited +
    Withdrawn). An explorer lookup that fails for any reason other than 'not verified' is an ERROR as well - the old
    `abi = []` on exception silently disabled this check (fail-open)."""
    errs = []
    if staking_v2_shaped(code):
        errs.append(f"{chain}: {addr} is a StakingV2-shaped contract (Deposited/Withdrawn topics + userInfo selector in its "
                    f"bytecode) that is not pinned - classify before the table")
    try:
        facts = contract_facts(es, addr)
    except Exception as e:
        errs.append(f"{chain}: {addr} explorer lookup failed ({scrub(e)}) - the unknown-staking check needs a definite answer")
        return errs
    abi = facts["abi"] or []
    fns = {f.get("name") for f in abi if f.get("type") == "function"}
    evs = {f.get("name") for f in abi if f.get("type") == "event"}
    if "userInfo" in fns and {"Deposited", "Withdrawn"} <= evs and not staking_v2_shaped(code):
        errs.append(f"{chain}: {addr} exposes the StakingV2 interface (verified ABI) but is not one of the pinned stakings - classify before the table")
    return errs


def classify_code(code):
    c = code.lower()
    if c in ("0x", ""):
        return "eoa"
    if c.startswith("0xef0100") and len(c) == 2 + 46:
        return "eoa-7702"            # EIP-7702 delegation designator (23 bytes) - an EOA that can claim (decision 6)
    return "contract"


WALLET_NAMES = ("safeproxy", "gnosissafe", "erc1967proxy", "beaconproxy", "proxy", "account", "wallet", "multisig")
INFRA_NAMES = ("router", "settlement", "feecollector", "aggregat", "uniswapv3pool", "uniswapv2pair", "pool", "exchange",
               "swap", "vault", "bridge", "gateway")


def classify_contract(name, code, chain, addr):
    """Published exclusion label for a contract holder (round 4, R2-12). Informational - every contract
    holder is listed in contract-wallets.json regardless of the label; the designation path is open to all."""
    n = (name or "").lower()
    if chain == "polygon" and addr.lower() == CHILD.lower():
        return "child token contract itself (tokens sent to the token address - unrecoverable)"
    if code.lower().startswith("0x363d3d37"):
        return "contract wallet (EIP-1167 minimal proxy) - designation path"
    if any(k in n for k in WALLET_NAMES):
        return "contract wallet (Safe / smart account / proxy) - designation path"
    if any(k in n for k in INFRA_NAMES):
        return "router / aggregator / fee collector / pool (not a holder) - designation path if disputed"
    return "contract - unclassified (designation path if it is a wallet)"


def chain_side(name, w3, es, token, block, out_raw, workers, strict_errors):
    """Replay + verify the holder set of `token` at `block`. Returns dict with balances (checksummed addr -> wei) + the logs."""
    print(f"[{name}] token {token} at block {block}", flush=True)
    cb = es.creation_block(cs(token), w3)
    logs = fetch_transfers(es, name, token, cb, block, os.path.join(out_raw, f"{name}-transfers-{block}.json"))
    rep, minted, burned = replay(logs)
    addrs = sorted(rep)
    tk = w3.eth.contract(address=cs(token), abi=ERC20_FULL)
    supply = tk.functions.totalSupply().call(block_identifier=block)
    t0 = time.time()
    chain = multicall_balances(w3, token, addrs, block, workers=workers)
    print(f"    balanceOf x {len(addrs)} via Multicall3 in {time.time()-t0:.0f}s", flush=True)
    mism = [(a, rep[a], chain[a]) for a in addrs if rep[a] != chain[a]]
    total = sum(chain.values())
    holders = {a: b for a, b in chain.items() if b > 0}
    ok = (total == supply) and not mism and (minted - burned == supply)
    print(f"    replay: minted {tpro(minted)} burned {tpro(burned)} -> {tpro(minted-burned)} | totalSupply {tpro(supply)} | "
          f"sum balanceOf {tpro(total)} | addresses ever seen {len(addrs)} | holders>0 {len(holders)} | "
          f"replay!=chain {len(mism)} | {'RECONCILED' if ok else 'MISMATCH'}", flush=True)
    for a, r, c in mism[:20]:
        print(f"      MISMATCH {a} replay {r} chain {c}", flush=True)
    if not ok:
        strict_errors.append(f"{name}: holder set does not reconcile (sum {total} vs supply {supply}, {len(mism)} replay mismatches)")
    return {"creation_block": cb, "transfers": len(logs), "minted_wei": str(minted), "burned_to_zero_wei": str(burned),
            "total_supply_wei": str(supply), "sum_balances_wei": str(total), "addresses_seen": len(addrs),
            "holders": len(holders), "replay_mismatches": len(mism), "reconciled": ok, "balances": holders, "logs": logs}


# ---------------------------------------------------------------- independent second sources (round 4, R2-14)
class SecondSourceError(Exception):
    """A malformed or incomplete answer of the second transfer index. A STRICT error (external review 2026-09-08,
    S04): before, one malformed record ended the comparison early as a 'difference' with an empty disputed set, so
    the rest of the index was never compared and the strict verdict stayed OK."""


def alchemy_transfers(w3, token, to_block, max_pages=100000):
    """Alchemy's own transfer index (alchemy_getAssetTransfers) - independent of Etherscan. Exact values from
    rawContract.value (hex); (tx, logIndex) from uniqueId '0x<tx>:log:<n>'. Every page must have the documented
    shape (an object with a `transfers` LIST; `pageKey` a non-empty string when present, never repeated) - anything
    else raises SecondSourceError (an incomplete pagination is a strict error, not a shorter list)."""
    out, page_key, seen_keys = [], None, set()
    for _ in range(max_pages):
        params = {"fromBlock": "0x0", "toBlock": hex(to_block), "contractAddresses": [token], "category": ["erc20"],
                  "withMetadata": False, "excludeZeroValue": False, "maxCount": "0x3e8", "order": "asc"}
        if page_key:
            params["pageKey"] = page_key
        r = w3.provider.make_request("alchemy_getAssetTransfers", [params])
        if not isinstance(r, dict) or "error" in r:
            raise SecondSourceError(f"alchemy_getAssetTransfers: {scrub(str((r or {}).get('error', r))[:120])}")
        res = r.get("result")
        if not isinstance(res, dict) or not isinstance(res.get("transfers"), list):
            raise SecondSourceError("alchemy_getAssetTransfers: a page without a `transfers` list (incomplete pagination)")
        out += res["transfers"]
        page_key = res.get("pageKey")
        if page_key is None:
            return out
        if not isinstance(page_key, str) or not page_key or page_key in seen_keys:
            raise SecondSourceError("alchemy_getAssetTransfers: malformed or repeated pageKey (incomplete pagination)")
        seen_keys.add(page_key)
    raise SecondSourceError(f"alchemy_getAssetTransfers: more than {max_pages} pages - refusing an unbounded listing")


def index_record(t):
    """One second-index record -> (tx, from, to, value), lowercase; every field validated (S04). Alchemy writes
    explicit zero addresses for mints and burns (checked live 2026-09-09 on both chains), so a missing or non-address
    `from`/`to`, a malformed uniqueId or a non-hex rawContract.value is MALFORMED -> SecondSourceError."""
    if not isinstance(t, dict):
        raise SecondSourceError(f"second source: non-object record {str(t)[:80]}")
    uid = t.get("uniqueId")
    tx, sep, idx = (uid.partition(":log:") if isinstance(uid, str) else ("", "", ""))
    if not sep or not RX_TX.match(tx) or not idx.isdigit():
        raise SecondSourceError(f"second source: malformed uniqueId {str(uid)[:80]!r}")
    frm, to = t.get("from"), t.get("to")
    if not (isinstance(frm, str) and RX_ADDR.match(frm)) or not (isinstance(to, str) and RX_ADDR.match(to)):
        raise SecondSourceError(f"second source: record {tx} has a malformed from/to ({str(frm)[:44]!r} -> {str(to)[:44]!r})")
    raw = (t.get("rawContract") or {}).get("value") if isinstance(t.get("rawContract"), dict) else None
    if not (isinstance(raw, str) and RX_HEXQ.match(raw)):
        raise SecondSourceError(f"second source: record {tx} has a missing or non-hex rawContract.value ({str(raw)[:40]!r})")
    return tx.lower(), frm.lower(), to.lower(), int(raw, 16)


def second_source_diff(logs, transfers):
    """Compare the Etherscan replay (logs) with the independent index PER TRANSACTION as multisets of
    (from, to, value) - the events that matter for completeness. The index's own logIndex numbering is
    NOT part of the key (Alchemy re-numbers / duplicates records in large blocks). Returns (differences,
    disputed_txs) with EXACTLY one disputed tx per difference; every disputed tx is settled by the RPC receipt in
    `reconcile_disputed_txs`. A malformed index record raises SecondSourceError (S04)."""
    from collections import Counter
    a, b = {}, {}
    for lg in logs:
        a.setdefault(lg["tx"].lower(), Counter())[(lg["from"].lower(), lg["to"].lower(), int(lg["value"]))] += 1
    for t in transfers:
        tx, frm, to, val = index_record(t)         # a malformed record RAISES (S04) - never a 'difference'
        b.setdefault(tx, Counter())[(frm, to, val)] += 1
    diffs, disputed = [], set()
    for tx in sorted(set(a) | set(b)):
        if a.get(tx, Counter()) != b.get(tx, Counter()):
            disputed.add(tx)
            diffs.append(f"tx {tx}: etherscan replay {sorted(a.get(tx, Counter()).items())} vs second source {sorted(b.get(tx, Counter()).items())}")
    return diffs, disputed


def reconcile_disputed_txs(w3, token, logs, disputed, max_txs=200):
    """Tie-breaker for second-source disagreements: the RPC RECEIPT of each disputed tx is the ground truth.
    The Etherscan replay must contain EXACTLY the token's Transfer logs of that receipt (from, to, value,
    logIndex). Returns (errors, notes)."""
    errors, notes = [], []
    if len(disputed) > max_txs:
        return [f"second source: {len(disputed)} disputed transactions - too many to reconcile by receipt; STOP"], notes
    by_tx = {}
    for lg in logs:
        by_tx.setdefault(lg["tx"].lower(), set()).add((lg["from"].lower(), lg["to"].lower(), int(lg["value"]), int(lg["logIndex"])))
    for tx in sorted(disputed):
        try:
            rc = w3.eth.get_transaction_receipt(tx)
        except Exception as e:
            errors.append(f"second source: receipt of disputed tx {tx} unavailable ({scrub(e)})"); continue
        truth = set()
        for l in rc["logs"]:
            if l["address"].lower() != token.lower():
                continue
            topics = [hexstr(t) for t in l["topics"]]
            if len(topics) != 3 or topics[0] != TRANSFER_TOPIC[2:]:
                continue
            data = l["data"]; data = data.hex() if not isinstance(data, str) else data
            val = int(data, 16) if data not in ("0x", "") else 0
            truth.add(("0x" + topics[1][-40:], "0x" + topics[2][-40:], val, int(l["logIndex"])))
        mine = by_tx.get(tx, set())
        if mine == truth and truth:
            notes.append(f"second source disagreed on tx {tx}: the RPC receipt confirms the Etherscan replay ({len(truth)} Transfer logs) - index artifact")
        else:
            errors.append(f"second source: tx {tx} - Etherscan replay {sorted(mine)} != receipt {sorted(truth)} (a Transfer log is missing or wrong)")
    return errors, notes


def make_w3(url, chain):
    """A patient provider for `chain` (Polygon PoS blocks carry POA extraData)."""
    w3 = Web3(PatientHTTPProvider(url, request_kwargs={"timeout": 120}))
    if chain == "polygon":
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def second_rpc_check(w3b, chain, token, block, supply, balances, sample=20):
    """Read totalSupply + a deterministic sample of balances from a SECOND RPC provider at the same block."""
    tk = w3b.eth.contract(address=cs(token), abi=ERC20_FULL)
    errs = []
    s2 = tk.functions.totalSupply().call(block_identifier=block)
    if s2 != supply:
        errs.append(f"{chain}: totalSupply differs between RPC providers ({supply} vs {s2})")
    addrs = sorted(balances)
    step = max(1, len(addrs) // sample)
    for a in addrs[::step][:sample]:
        b2 = tk.functions.balanceOf(a).call(block_identifier=block)
        if b2 != balances[a]:
            errs.append(f"{chain}: balanceOf({a}) differs between RPC providers ({balances[a]} vs {b2})")
    return errs


# ---------------------------------------------------------------- census verdicts (round 4, R2-1)
def census_verdict(c, accepted_wei=0):
    """Every census difference must equal the PINNED reserve of that contract (plus an explicitly ACCEPTED,
    chain-verified unsolicited surplus - S07, never a deficit), and every read must have succeeded. Returns the
    list of errors (empty = reconciled)."""
    errs = []
    label = c["label"]
    if label not in RESERVE_WEI:
        errs.append(f"{label}: no pinned reserve for this contract - classify before the table")
        return errs
    if accepted_wei < 0:
        errs.append(f"{label}: a negative acceptance is impossible (deficits are never accepted)")
        return errs
    diff = int(c["diff_wei"])
    expected = RESERVE_WEI[label] + accepted_wei
    if diff != expected:
        errs.append(f"{label}: balance - sum(per-user reads) = {diff} wei, pinned reserve {RESERVE_WEI[label]} wei"
                    f"{' + accepted surplus ' + str(accepted_wei) + ' wei' if accepted_wei else ''} "
                    f"({tpro(diff)} vs {tpro(expected)}) - a holder may be missing or misread; STOP and explain")
    if c.get("read_errors"):
        errs.append(f"{label}: {len(c['read_errors'])} per-user read errors - every read must succeed (retry with fewer workers)")
    return errs


# ---------------------------------------------------------------- MEXC custody (round 4, R2-8)
def mexc_check(env_values, codes, balances):
    """Pinned custody set; the env (if present) must match; each must be an EOA; reserve = their sum."""
    errs = []
    pinned = {cs(a) for a in MEXC_CUSTODY}
    if len(pinned) != 4:
        errs.append("MEXC custody constants are not four distinct addresses")
    env = {cs(v) for v in env_values if v}
    if env and env != pinned:
        errs.append(f"MEXC_ETH_ADDRESS_1..4 in the environment ({sorted(env)}) do not match the pinned custody set ({sorted(pinned)})")
    for a in sorted(pinned):
        if classify_code(codes.get(a, "0x")) != "eoa":
            errs.append(f"MEXC custody {a} has code at the block - not a custody EOA; STOP and explain")
    reserve = sum(int(balances.get(a, 0)) for a in pinned)
    return reserve, errs


# ---------------------------------------------------------------- designations (round 4, R2-9)
def load_owners(path, designation, known_excluded):
    """contract_owners.json = {"<chain>:<0xContract>": {"base_address": "0x..", "tx": "0x..", "verified_by": "..",
    "date": "YYYY-MM-DD"}}. Strict EIP-55 on both addresses (a typo is caught with probability ~1), chain-scoped
    keys, forbidden targets, and a designation address that must be known when any entry exists."""
    owners, errs = {}, []
    if not os.path.exists(path):
        return owners, errs
    raw = jload(path)
    if not isinstance(raw, dict):
        return owners, ["contract_owners.json is not an object"]
    if raw and not designation:
        errs.append("contract_owners.json has entries but no --designation-address was given")
    for key, e in raw.items():
        chain, _, addr = key.partition(":")
        if chain not in ("eth", "polygon") or not is_checksum_address(addr):
            errs.append(f"contract_owners.json key {key!r} must be '<eth|polygon>:<EIP-55 checksummed contract address>'")
            continue
        if not isinstance(e, dict):
            errs.append(f"contract_owners.json {key}: entry is not an object")
            continue
        base = e.get("base_address", "")
        if not is_checksum_address(base):
            errs.append(f"contract_owners.json {key}: base_address {base!r} is not a valid EIP-55 checksummed address")
            continue
        if base.lower() in {x.lower() for x in known_excluded} or base.lower() == addr.lower():
            errs.append(f"contract_owners.json {key}: base_address {base} is a forbidden target (excluded/known address or the contract itself)")
        tx = e.get("tx", "")
        if not (isinstance(tx, str) and len(tx) == 66 and tx.startswith("0x")):
            errs.append(f"contract_owners.json {key}: 'tx' (the designation transaction hash) is required")
        if not e.get("verified_by") or not e.get("date"):
            errs.append(f"contract_owners.json {key}: 'verified_by' and 'date' are required")
        owners[(chain, cs(addr))] = {"base_address": base, "tx": tx, "verified_by": e.get("verified_by", ""), "date": e.get("date", "")}
    return owners, errs


def trace_designation(w3_trace, tx_hash, contract, designation, base_address):
    """(S03) Bind the designation to the call that made it: `debug_traceTransaction` (callTracer) on the TRACING
    provider must show EXACTLY ONE successful CALL frame from `contract` to `designation` whose input carries the
    Base address, with no reverted ancestor. DELEGATECALL / STATICCALL / CREATE frames do not count; a frame with
    an error (a caught revert) does not count; a Base address that only appears in the OUTER calldata does not
    count - the reviewer's inert reproduction passed with recipient B in the outer input and recipient A in the
    real inner call. Returns the list of errors (empty = bound)."""
    try:
        r = w3_trace.provider.make_request("debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
    except Exception as e:
        return [f"designation tx {tx_hash}: callTracer failed ({scrub(e)})"]
    if not isinstance(r, dict) or "error" in r or not isinstance(r.get("result"), dict):
        return [f"designation tx {tx_hash}: callTracer unavailable on the tracing provider ({scrub(str((r or {}).get('error', ''))[:80])})"]
    frames = []

    def walk(f, reverted_above):
        frames.append((f, reverted_above))
        for c in f.get("calls") or []:
            walk(c, reverted_above or bool(f.get("error")))
    walk(r["result"], False)
    hits = [(f, rv) for f, rv in frames
            if str(f.get("type") or "").upper() == "CALL"
            and str(f.get("from") or "").lower() == contract.lower() and str(f.get("to") or "").lower() == designation.lower()]
    good = [f for f, rv in hits if not f.get("error") and not rv and base_address.lower()[2:] in str(f.get("input") or "").lower()]
    if not hits:
        return [f"designation tx {tx_hash}: no CALL frame from the contract to the designation address in the trace"]
    if len(good) != 1:
        return [f"designation tx {tx_hash}: {len(hits)} CALL frame(s) contract -> designation, {len(good)} successful with the Base "
                f"address in that call's input - exactly one required"]
    return []


def verify_designation(w3, es, chain, contract, entry, block, designation, w3_trace=None):
    """The designation transaction must (a) be mined at or before the snapshot block, (b) carry the Base address
    in its calldata, (c) show an internal call FROM the contract TO the designation address (Etherscan
    txlistinternal) - only the contract's owners can produce that - and (d) be BOUND by the call tracer of the
    second provider: exactly one successful CALL contract -> designation with the Base address in that call's
    input (S03). No tracing provider = error (fail-closed). Returns the list of errors."""
    errs, tx_hash = [], entry["tx"]
    try:
        tx = w3.eth.get_transaction(tx_hash)
        rc = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as e:
        return [f"designation {chain}:{contract}: tx {tx_hash} not found ({scrub(e)})"]
    if rc["status"] != 1:
        errs.append(f"designation {chain}:{contract}: tx {tx_hash} failed on-chain")
    if rc["blockNumber"] > block:
        errs.append(f"designation {chain}:{contract}: tx mined in block {rc['blockNumber']} > snapshot block {block}")
    inp = tx["input"].hex() if not isinstance(tx["input"], str) else tx["input"]
    if entry["base_address"].lower()[2:] not in inp.lower():
        errs.append(f"designation {chain}:{contract}: the Base address is not in the calldata of tx {tx_hash}")
    try:
        res = es.get(module="account", action="txlistinternal", txhash=tx_hash).get("result") or []
    except Exception as e:
        return errs + [f"designation {chain}:{contract}: internal-transaction lookup failed ({scrub(e)})"]
    hit = any((r.get("from") or "").lower() == contract.lower() and (r.get("to") or "").lower() == designation.lower() for r in res)
    top = (tx["from"] or "").lower() == contract.lower() and (tx["to"] or "").lower() == designation.lower()
    if not (hit or top):
        errs.append(f"designation {chain}:{contract}: tx {tx_hash} has no call FROM the contract TO the designation address {designation}")
    if w3_trace is None:
        errs.append(f"designation {chain}:{contract}: no tracing provider ({chain.upper()}_ARCHIVE_RPC_2) - the designation cannot be bound to its call (S03)")
    else:
        errs += [f"designation {chain}:{contract}: {e}" for e in trace_designation(w3_trace, tx_hash, contract, designation, entry["base_address"])]
    return errs


# ---------------------------------------------------------------- Uniswap V4: exact position math (round 4, R2-3)
def get_sqrt_price_at_tick(tick):
    """Uniswap TickMath.getSqrtPriceAtTick, integer-exact."""
    a = abs(tick)
    if a > 887272:
        raise ValueError("tick out of range")
    ratio = 0xfffcb933bd6fad37aa2d162d1a594001 if a & 0x1 else 0x100000000000000000000000000000000
    for bit, c in ((0x2, 0xfff97272373d413259a46990580e213a), (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc), (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0),
                   (0x10, 0xffcb9843d60f6159c9db58835c926644), (0x20, 0xff973b41fa98c081472e6896dfb254c0), (0x40, 0xff2ea16466c96a3843ec78b326b52861),
                   (0x80, 0xfe5dee046a99a2a811c461f1969c3053), (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4), (0x200, 0xf987a7253ac413176f2b074cf7815e54),
                   (0x400, 0xf3392b0822b70005940c7a398e4b70f3), (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9), (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
                   (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5), (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7), (0x8000, 0x31be135f97d08fd981231505542fcfa6),
                   (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9), (0x20000, 0x5d6af8dedb81196699c329225ee604), (0x40000, 0x2216e584f5fa1ea926041bedfe98),
                   (0x80000, 0x48a170391f7dc42444e8fa2)):
        if a & bit:
            ratio = (ratio * c) >> 128
    if tick > 0:
        ratio = (U256 - 1) // ratio
    return (ratio >> 32) + (1 if ratio % (1 << 32) else 0)


def amount0_delta(sa, sb, liq):
    if sa > sb:
        sa, sb = sb, sa
    return ((liq << 96) * (sb - sa) // sb) // sa          # rounded DOWN = what can actually be withdrawn


def amount1_delta(sa, sb, liq):
    if sa > sb:
        sa, sb = sb, sa
    return liq * (sb - sa) // Q96


def amounts_for_liquidity(sp, sa, sb, liq):
    if sp <= sa:
        return amount0_delta(sa, sb, liq), 0
    if sp < sb:
        return amount0_delta(sp, sb, liq), amount1_delta(sa, sp, liq)
    return 0, amount1_delta(sa, sb, liq)


def sign24(x):
    return x - (1 << 24) if x & (1 << 23) else x


def v4_report(w3, es, block, pm_balance, errors):
    """The project's two V4 positions at the block: EXACT principal + fees owed (integer math, rounded down),
    the pool price record for the LP opening, and the third-party remainder of the PoolManager balance.
    Credits ONLY what the project can withdraw; asserts nobody else holds in-range liquidity in our pool."""
    posm = w3.eth.contract(address=cs(POSITION_MANAGER), abi=POSM_ABI)
    sv = w3.eth.contract(address=cs(STATE_VIEW), abi=STATE_VIEW_ABI)
    out, price, our_wei, our_pool_id, in_range_liq, current_tick = [], None, 0, None, 0, None
    for pid in V4_POSITIONS:
        key, info = posm.functions.getPoolAndPositionInfo(pid).call(block_identifier=block)
        liq = posm.functions.getPositionLiquidity(pid).call(block_identifier=block)
        owner = posm.functions.ownerOf(pid).call(block_identifier=block)
        c0, c1, fee, spacing, hooks = key
        pool_id = keccak(abi_encode(["address", "address", "uint24", "int24", "address"], [c0, c1, fee, spacing, hooks]))
        tick_lower, tick_upper = sign24((info >> 8) & 0xFFFFFF), sign24((info >> 32) & 0xFFFFFF)
        if (info >> 56) != int.from_bytes(pool_id[:25], "big"):
            errors.append(f"V4 position {pid}: positionInfo poolId prefix mismatch")
        if not (c0.lower() == ZERO and c1.lower() == TPRO.lower()):
            errors.append(f"V4 position {pid}: unexpected pool key {key}")
        if owner != cs(LIQUIDITY):
            errors.append(f"V4 position {pid} owner {owner} is not the Liquidity wallet")
        sqrt_x96, tick, _pf, lp_fee = sv.functions.getSlot0(pool_id).call(block_identifier=block)
        sa, sb = get_sqrt_price_at_tick(tick_lower), get_sqrt_price_at_tick(tick_upper)
        a0, a1 = amounts_for_liquidity(sqrt_x96, sa, sb, liq)
        liq_sv, fg0_last, fg1_last = sv.functions.getPositionInfo(pool_id, cs(POSITION_MANAGER), tick_lower, tick_upper,
                                                                  pid.to_bytes(32, "big")).call(block_identifier=block)
        if liq_sv != liq:
            errors.append(f"V4 position {pid}: liquidity {liq} (PositionManager) != {liq_sv} (StateView)")
        fg0, fg1 = sv.functions.getFeeGrowthInside(pool_id, tick_lower, tick_upper).call(block_identifier=block)
        fees0 = ((fg0 - fg0_last) % U256) * liq // Q128
        fees1 = ((fg1 - fg1_last) % U256) * liq // Q128
        in_range = tick_lower <= tick < tick_upper
        if our_pool_id is None:
            our_pool_id, current_tick = pool_id, tick
            sp = Decimal(sqrt_x96) / (Decimal(2) ** 96)
            tpro_per_eth = sp * sp                      # token1 (TPRO) per token0 (ETH)
            price = {"sqrtPriceX96": str(sqrt_x96), "tick": tick, "tpro_per_eth": f"{tpro_per_eth:.6f}",
                     "eth_per_tpro": f"{(1 / tpro_per_eth):.18f}", "lp_fee_ppm": lp_fee}
        if in_range:
            in_range_liq += liq
        our_wei += a1 + fees1
        out.append({"tokenId": pid, "owner": owner, "pool_id": "0x" + pool_id.hex(), "fee_ppm": fee, "tickSpacing": spacing,
                    "hooks": hooks, "tickLower": tick_lower, "tickUpper": tick_upper, "liquidity": str(liq), "in_range": in_range,
                    "eth_principal_wei": str(a0), "eth_principal": f"{Decimal(a0) / E18:.6f}",
                    "tpro_principal_wei": str(a1), "tpro_principal": tpro(a1),
                    "eth_fees_owed_wei": str(fees0), "eth_fees_owed": f"{Decimal(fees0) / E18:.6f}",
                    "tpro_fees_owed_wei": str(fees1), "tpro_fees_owed": tpro(fees1)})
    pool_liq = sv.functions.getLiquidity(our_pool_id).call(block_identifier=block) if our_pool_id else 0
    if pool_liq != in_range_liq:
        errors.append(f"V4 pool: active liquidity {pool_liq} != the project's in-range liquidity {in_range_liq} - "
                      f"somebody else provides liquidity in our pool; classify before the table")
    residual = pm_balance - our_wei
    if residual < 0:
        errors.append(f"V4 PoolManager balance {pm_balance} < the project's principal + fees {our_wei}")
    # other TPRO pools on the PoolManager (informational): Initialize events with TPRO as currency0 or currency1
    other_pools = []
    try:
        t_tpro = "0x" + "0" * 24 + TPRO[2:].lower()
        seen = set()
        for topic_kw in ({"topic2": t_tpro}, {"topic1": t_tpro}):
            for lg in es.logs_by_topics(POOL_MANAGER, 0, block, topic0=V4_INITIALIZE_TOPIC, **topic_kw):
                pid_hex = hexstr(lg["topics"][0]) if len(lg["topics"]) > 1 else ""
                pid_hex = hexstr(lg["topics"][1])
                if pid_hex in seen:
                    continue
                seen.add(pid_hex)
                other_pools.append({"pool_id": "0x" + pid_hex, "init_block": int(lg["blockNumber"], 16),
                                    "ours": our_pool_id is not None and pid_hex == our_pool_id.hex()})
    except Exception as e:
        other_pools = [{"error": scrub(e)}]
    eth_usd = None
    try:
        rd = w3.eth.contract(address=cs(CHAINLINK_ETH_USD), abi=CHAINLINK_ABI).functions.latestRoundData().call(block_identifier=block)
        eth_usd = f"{Decimal(rd[1]) / Decimal(10**8):.2f}"
    except Exception as e:
        eth_usd = f"unavailable ({type(e).__name__})"     # type only - never the message (may embed the RPC URL)
    if price:
        price["eth_usd_chainlink"] = eth_usd
        try:
            price["usd_per_tpro"] = f"{Decimal(price['eth_per_tpro']) * Decimal(eth_usd):.8f}"
        except Exception:
            pass
    record = {"positions": out, "price_at_block": price, "current_tick": current_tick,
              "pool_active_liquidity": str(pool_liq), "project_in_range_liquidity": str(in_range_liq),
              "pool_manager_tpro_balance_wei": str(pm_balance), "pool_manager_tpro_balance": tpro(pm_balance),
              "project_credit_wei": str(our_wei), "project_credit": tpro(our_wei),
              "third_party_residual_wei": str(residual), "third_party_residual": tpro(residual),
              "tpro_pools_on_pool_manager": other_pools,
              "note": "project credit = EXACT principal + fees owed of #21106 and #45911 (integer math, rounded down); "
                      "the residual belongs to other pools' LPs / ERC-6909 claim holders and is excluded (round 4, R2-3)"}
    return record, our_wei, residual


# ---------------------------------------------------------------- bridge decomposition (round 4, R2-4)
def bridge_report(eth_logs, pol_logs, surplus, eth_block):
    """Polygon burns (child Transfer -> 0x0) that were never claimed on Ethereum (tunnel Transfer -> user)
    make up the 'surplus'. Match by (user, amount) as multisets; the unmatched burns are the unclaimed exits,
    each with an identifiable owner. Also: tunnel deposits without a child mint = holders in transit."""
    from collections import Counter
    burns = Counter(); burn_detail = {}
    for lg in pol_logs:
        if lg["to"] == ZERO:
            k = (lg["from"], int(lg["value"]))
            burns[k] += 1
            burn_detail.setdefault(k, []).append({"polygon_block": lg["block"], "tx": lg["tx"]})
    exits = Counter((lg["to"], int(lg["value"])) for lg in eth_logs if lg["from"] == cs(ROOT_TUNNEL))
    deposits = Counter((lg["from"], int(lg["value"])) for lg in eth_logs if lg["to"] == cs(ROOT_TUNNEL))
    mints = Counter((lg["to"], int(lg["value"])) for lg in pol_logs if lg["from"] == ZERO and int(lg["value"]) > 0)
    unmatched_burns = burns - exits
    unmatched_exits = exits - burns
    unmatched_deposits = deposits - mints
    unclaimed = []
    for (addr, amt), n in sorted(unmatched_burns.items(), key=lambda kv: -kv[0][1]):
        for i in range(n):
            d = burn_detail[(addr, amt)][i] if i < len(burn_detail[(addr, amt)]) else {}
            unclaimed.append({"address": addr, "amount_wei": str(amt), "amount": tpro(amt), **d})
    total_unclaimed = sum(int(u["amount_wei"]) for u in unclaimed)
    in_transit = [{"address": a, "amount_wei": str(m), "amount": tpro(m), "count": n} for (a, m), n in unmatched_deposits.items()]
    errs = []
    if total_unclaimed != surplus:
        errs.append(f"bridge: unmatched Polygon burns sum {total_unclaimed} != tunnel surplus {surplus} - the surplus is not explained")
    if unmatched_exits:
        errs.append(f"bridge: {sum(unmatched_exits.values())} tunnel exits without a matching Polygon burn")
    record = {"surplus_wei": str(surplus), "surplus": tpro(surplus), "unclaimed_exits": unclaimed,
              "unclaimed_exits_total_wei": str(total_unclaimed), "unclaimed_exits_total": tpro(total_unclaimed),
              "deposits_in_transit": in_transit, "matched_exits": sum((burns & exits).values()),
              "note": "every unclaimed exit has an identifiable owner (the Polygon burner); credited to the burner by default "
                      "(policy 2026-09-07, amendment 27) - --exclude-unclaimed-exits (dry runs only) excludes them under decision 4"}
    return record, in_transit, errs


# ---------------------------------------------------------------- indirect beneficiaries (external review 2026-09-08, S02)
INDIRECT_PREFIXES = ("staking-", "vesting-", "polygon-staking-", "bridge-unclaimed-exit")


def source_chain(label):
    """The chain a source label reads from: Polygon stakings on Polygon, everything else on Ethereum."""
    return "polygon" if label.startswith("polygon-") else "eth"


def eligibility(chain, addr, code, known_excluded, owners):
    """ONE chain-aware eligibility decision for an INDIRECT beneficiary (a staker read from userInfo, a vesting
    beneficiary, a Polygon staker, a bridge burner) BEFORE it is credited - the same policy the direct holders get,
    applied on the chain of the SOURCE. Returns one of
      ("credit", addr)          - an EOA or an EIP-7702 delegated EOA (claims normally on Base)
      ("readdress", base_addr)  - a contract with a VERIFIED designation in contract_owners.json
      ("error", reason)         - a contract without a designation (fail-closed: the designation path), or an address
                                  of the known excluded set (0x0 / 0xdead / bucket / MEXC custody / TPRO / child /
                                  tunnel / PoolManager) - a row for those can never be right."""
    a = cs(addr)
    if a.lower() in known_excluded:
        return "error", f"{chain}:{a} is an excluded address (custody / bucket / token / bridge / pool) and cannot be credited"
    kind = classify_code(code or "0x")
    if kind != "contract":
        return "credit", a
    o = owners.get((chain, a))
    if o:
        return "readdress", o["base_address"]
    return "error", f"{chain}:{a} is a contract without a designation - fail-closed (designation path, contract_owners.json)"


class Ledger:
    """Row accumulator (one row per address, sources merged) + exclusions + the contract-wallet report, with the
    eligibility decision built into every INDIRECT credit (S02). Errors go to the shared strict list."""

    def __init__(self, errors):
        self.rows, self.sources, self.exclusions, self.contract_report, self.errors = {}, {}, [], [], errors
        self.by_source = {}
        self.indirect = {"eoa": 0, "eoa-7702": 0, "readdressed": 0, "refused": 0}

    def credit(self, addr, wei, src):
        if wei <= 0:
            return
        addr = cs(addr)
        self.by_source[src] = self.by_source.get(src, 0) + wei
        self.rows[addr] = self.rows.get(addr, 0) + wei
        self.sources.setdefault(addr, [])
        if src not in self.sources[addr]:
            self.sources[addr].append(src)

    def exclude(self, chain, addr, label, wei, reason):
        self.exclusions.append({"chain": chain, "address": addr, "label": label, "amount_wei": str(wei), "amount": tpro(wei), "reason": reason})

    def credit_indirect(self, chain, addr, wei, label, code, known_excluded, owners):
        """Credit an indirect beneficiary after the eligibility decision; a refusal is a STRICT ERROR and the amount
        is excluded (labeled) so the supply identity still holds and the failure is visible in the summary."""
        if wei <= 0:
            return
        verdict, target = eligibility(chain, addr, code, known_excluded, owners)
        a = cs(addr)
        if verdict == "credit":
            self.indirect["eoa-7702" if classify_code(code or "0x") == "eoa-7702" else "eoa"] += 1
            self.credit(a, wei, label)
        elif verdict == "readdress":
            self.indirect["readdressed"] += 1
            self.credit(target, wei, f"contract-wallet:{a}")
            self.contract_report.append({"chain": chain, "address": a, "balance_wei": str(wei), "balance": tpro(wei),
                                         "status": f"re-addressed -> {target} (indirect source {label}; designation verified)",
                                         "label": f"contract beneficiary of {label} - designation path", "contract_name": "", "code_prefix": (code or "0x")[:12]})
        else:
            self.indirect["refused"] += 1
            self.errors.append(f"indirect beneficiary {a} ({label}, {tpro(wei)} TPRO): {target}")
            self.exclude(chain, a, f"{label} beneficiary REFUSED - {target}", wei, "S02 fail-closed: fix the data (designation) or the classification, then re-run")
            if classify_code(code or "0x") == "contract":
                self.contract_report.append({"chain": chain, "address": a, "balance_wei": str(wei), "balance": tpro(wei),
                                             "status": f"EXCLUDED - contract beneficiary of {label} without a designation (strict error)",
                                             "label": f"contract beneficiary of {label} - designation path", "contract_name": "", "code_prefix": (code or "0x")[:12]})


# ---------------------------------------------------------------- accepted unsolicited transfers (external review 2026-09-08, S07)
ACCEPT_LABELS = set(RESERVE_WEI) | {"bridge-tunnel", "morpheus-bucket"}


def parse_acceptances(items):
    """`--accept-explained LABEL:WEI:TX[,TX...]` (repeatable) -> {label: {"wei": int, "txs": [lowercase hashes]}}.
    Strict shapes: a known label at most once, WEI a positive integer (a deficit is never accepted), one or more
    distinct 32-byte transaction hashes."""
    out = {}
    for it in items or []:
        parts = it.strip().split(":")
        if len(parts) != 3:
            raise SystemExit(f"--accept-explained {it!r}: expected LABEL:WEI:TX[,TX...]")
        label, wei, txs = parts
        if label not in ACCEPT_LABELS:
            raise SystemExit(f"--accept-explained {it!r}: unknown label {label!r} (one of {sorted(ACCEPT_LABELS)})")
        if label in out:
            raise SystemExit(f"--accept-explained: label {label!r} given twice")
        if not wei.isdigit() or int(wei) <= 0:
            raise SystemExit(f"--accept-explained {it!r}: WEI must be a positive integer (a deficit is never accepted)")
        tx_list = [t.strip().lower() for t in txs.split(",") if t.strip()]
        if not tx_list or any(not RX_TX.match(t) for t in tx_list) or len(set(tx_list)) != len(tx_list):
            raise SystemExit(f"--accept-explained {it!r}: TX must be one or more distinct 0x-prefixed 32-byte hashes")
        out[label] = {"wei": int(wei), "txs": tx_list}
    return out


def verify_explained(w3, token, target, block, label, acc):
    """(S07) Chain-verified proof that a pinned target's surplus consists of the NAMED unsolicited transfers, to
    the wei: every named tx is mined at or before `block` with status 1, is NOT a direct call to the target,
    carries NO log emitted by the target itself (a Deposited / addBeneficiaries / bridge event would make it a
    normal flow), and carries TPRO Transfer log(s) INTO the target; the transfers over all named txs sum to
    exactly acc["wei"]. The receipt is the ground truth; the explorer's inflow index is the cross-check
    (`explain_inflows`, applied by the caller). Returns (errors, record)."""
    errs, txrecs, total = [], [], 0
    for tx in acc["txs"]:
        try:
            t = w3.eth.get_transaction(tx)
            rc = w3.eth.get_transaction_receipt(tx)
        except Exception as e:
            errs.append(f"{label}: tx {tx} not found ({scrub(e)})")
            continue
        if rc["status"] != 1:
            errs.append(f"{label}: tx {tx} failed on-chain")
            continue
        if rc["blockNumber"] > block:
            errs.append(f"{label}: tx {tx} mined in block {rc['blockNumber']} > snapshot block {block}")
            continue
        if str(t.get("to") or "").lower() == target.lower():
            errs.append(f"{label}: tx {tx} is a direct call to the target - a normal flow, not an unsolicited transfer")
            continue
        own = [l for l in rc["logs"] if str(l["address"]).lower() == target.lower()]
        if own:
            errs.append(f"{label}: tx {tx} carries {len(own)} event(s) emitted by the target itself - a normal flow, not an unsolicited transfer")
            continue
        inflow = 0
        for l in rc["logs"]:
            if str(l["address"]).lower() != token.lower():
                continue
            topics = [hexstr(x) for x in l["topics"]]
            if len(topics) != 3 or topics[0] != TRANSFER_TOPIC[2:] or ("0x" + topics[2][-40:]) != target.lower():
                continue
            data = l["data"]
            data = data.hex() if not isinstance(data, str) else data
            inflow += int(data, 16) if data not in ("0x", "") else 0
        if inflow == 0:
            errs.append(f"{label}: tx {tx} has no TPRO Transfer into the target")
            continue
        total += inflow
        txrecs.append({"tx": tx, "block": rc["blockNumber"], "transfer_in_wei": str(inflow), "transfer_in": tpro(inflow)})
    if not errs and total != acc["wei"]:
        errs.append(f"{label}: the named transfers sum to {total} wei, the accepted surplus is {acc['wei']} wei - an exact match is required")
    record = {"label": label, "target": cs(target), "wei": str(acc["wei"]), "amount": tpro(acc["wei"]), "txs": txrecs,
              "exclusion_label": f"unsolicited transfer into {label} (burned)", "verified": not errs, "errors": errs}
    return errs, record


# ---------------------------------------------------------------- verdict plumbing (S01) + public provenance (public repo, item 13)
def failed_dir(out):
    """A failed --final run must never leave a deployable-looking bundle: the output directory is renamed to
    <out>-FAILED (a UTC time suffix is added when that exists). index.json inside carries strictOk=false too."""
    dst = out.rstrip("/") + "-FAILED"
    if os.path.exists(dst):
        dst += "-" + time.strftime("%H%M%S", time.gmtime())
    os.rename(out, dst)
    return dst


def git_short_sha():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=REPO).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def git_dirty():
    """True when a TRACKED file differs from HEAD (untracked scratch under tmp/ does not count)."""
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, cwd=REPO)
        return bool(r.stdout.strip())
    except Exception:
        return True


def tool_ref(sha, final, public_ref_path=None):
    """index.json `tool`: the PUBLIC repository commit that carries this exact tool, from PUBLIC_REF (written by
    scripts/export-public.sh as '<private sha> <public sha>' lines; the last matching line wins). Without a mapping
    for this commit the private form 'scripts/snapshot/snapshot.py@<sha>' is used - and `--final` REFUSES to run
    (the published table must point at code anyone can read)."""
    p = public_ref_path or os.path.join(REPO, "PUBLIC_REF")
    public = None
    if os.path.exists(p) and sha != "unknown":
        for line in open(p):
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith(sha):
                public = parts[1]
    if public:
        return f"{PUBLIC_REPO}@{public}"
    if final:
        raise SystemExit("--final requires PUBLIC_REF to map this commit to the public repository (run scripts/export-public.sh first)")
    return f"scripts/snapshot/snapshot.py@{sha}"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eth-block", type=int)
    ap.add_argument("--polygon-block", type=int)
    ap.add_argument("--at-utc", help="pin both blocks from an instant (UTC, e.g. 2026-01-01T00:00:00Z)")
    ap.add_argument("--final", action="store_true", help="the real table: every guard on, nothing optional (runbook B1)")
    ap.add_argument("--eth-rpc", default=None, help="Ethereum archive RPC (default: ETH_ARCHIVE_RPC from the environment or .env - keyed URLs never on argv)")
    ap.add_argument("--polygon-rpc", default=None, help="Polygon archive RPC (default: POLYGON_ARCHIVE_RPC from the environment or .env)")
    ap.add_argument("--eth-only", action="store_true", help="skip the Polygon side (dry runs only - refused in --final)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", help="output dir (default tmp/snapshot-<eth>-<poly>[-final]/)")
    ap.add_argument("--no-strict", action="store_true", help="report only, exit 0 (refused in --final)")
    ap.add_argument("--no-second-source", action="store_true", help="skip the independent transfer index (refused in --final)")
    ap.add_argument("--designation-address", default=load_env_value("DESIGNATION_ADDRESS"),
                    help="the published designation address (Ethereum) - required when contract_owners.json has entries")
    ap.add_argument("--credit-unclaimed-exits", action="store_true",
                    help="credit unclaimed bridge exits to their Polygon burners - the DEFAULT since 2026-09-07 (amendment 27); kept for old command lines")
    ap.add_argument("--exclude-unclaimed-exits", action="store_true",
                    help="dry runs only: exclude the unclaimed bridge exits (decision 4 shape, pre-policy) - refused in --final")
    ap.add_argument("--accept-explained", action="append", metavar="LABEL:WEI:TX[,TX...]",
                    help="accept a pinned target's surplus that consists EXACTLY of the named unsolicited transfers (chain-verified; "
                         "S07). Labels: staking-T1..T6, vesting-1..3, polygon-staking-<0x..>, bridge-tunnel, "
                         "morpheus-bucket (the bucket ONLY with the written GO - runbook B1). Repeatable.")
    a = ap.parse_args()
    final = a.final
    acceptances = parse_acceptances(a.accept_explained)
    if a.credit_unclaimed_exits and a.exclude_unclaimed_exits:
        raise SystemExit("--credit-unclaimed-exits and --exclude-unclaimed-exits contradict each other")
    credit_exits = not a.exclude_unclaimed_exits          # policy default (amendment 27)
    if final and a.exclude_unclaimed_exits:
        raise SystemExit("--final refuses --exclude-unclaimed-exits (policy 2026-09-07: the unclaimed bridge exits are credited to their burners)")
    if a.designation_address:
        if not is_checksum_address(a.designation_address) or a.designation_address.lower() == ZERO:
            raise SystemExit("--designation-address must be a non-zero EIP-55 checksummed address")

    # ---- guards on the invocation
    if final:
        if a.eth_only or a.no_strict or a.no_second_source:
            raise SystemExit("--final refuses --eth-only / --no-strict / --no-second-source")
        if a.workers > 4:
            raise SystemExit("--final refuses --workers > 4 (rate limits turned into read errors on the keyed run)")
        if a.eth_rpc or a.polygon_rpc:
            raise SystemExit("--final refuses RPC URLs on argv - set ETH_ARCHIVE_RPC / POLYGON_ARCHIVE_RPC in .env")
        if not a.at_utc:
            raise SystemExit("--final requires --at-utc (the published pin rule)")
    eth_rpc = a.eth_rpc or load_env_value("ETH_ARCHIVE_RPC")
    pol_rpc = a.polygon_rpc or load_env_value("POLYGON_ARCHIVE_RPC")
    if not eth_rpc or (not pol_rpc and not a.eth_only):
        if final:
            raise SystemExit("--final requires ETH_ARCHIVE_RPC and POLYGON_ARCHIVE_RPC (keyed archive endpoints) in the environment or .env")
        eth_rpc = eth_rpc or "https://eth.drpc.org"
        pol_rpc = pol_rpc or "https://polygon.drpc.org"
        print("[warn] public default RPC in use - dry runs only, never for the real table", flush=True)
    eth_rpc2, pol_rpc2 = load_env_value("ETH_ARCHIVE_RPC_2"), load_env_value("POLYGON_ARCHIVE_RPC_2")
    for u in (eth_rpc, pol_rpc, eth_rpc2, pol_rpc2):
        register_secret_url(u)
    if final:
        for u in (eth_rpc, pol_rpc):
            if urlsplit(u).hostname not in ALLOWED_ARCHIVE_HOSTS:
                raise SystemExit(f"--final refuses the endpoint {rpc_label(u)} - not a keyed archive host")
        if not eth_rpc2 or not pol_rpc2:
            raise SystemExit("--final requires a SECOND RPC provider for both chains (ETH_ARCHIVE_RPC_2 / POLYGON_ARCHIVE_RPC_2)")
        if git_dirty():
            raise SystemExit("--final requires a clean working tree (tracked files) at the frozen commit - commit or stash first")
    sha = git_short_sha()
    tool = tool_ref(sha, final)              # --final refuses to run without the public-repo mapping (PUBLIC_REF, item 13)

    key = load_key()
    w3e = make_w3(eth_rpc, "eth")
    w3p = make_w3(pol_rpc, "polygon") if not a.eth_only else None      # Polygon PoS (Bor) blocks carry POA extraData
    w3e2 = make_w3(eth_rpc2, "eth") if eth_rpc2 else None              # second provider: balance cross-check + callTracer (S03)
    w3p2 = make_w3(pol_rpc2, "polygon") if (pol_rpc2 and not a.eth_only) else None
    es_eth, es_pol = Etherscan(key, 1), Etherscan(key, 137)
    assert w3e.eth.chain_id == 1, "eth rpc is not Ethereum mainnet"
    if w3p is not None:
        assert w3p.eth.chain_id == 137, "polygon rpc is not Polygon PoS"
    errors, findings = [], []
    accepted_explained = []        # S07 records (verified or not) for summary.json

    def own_txs_of(es, addr, cb, block):
        """Transactions in which `addr` itself emitted an event (its own deposits / exits / vesting calls)."""
        return {lg["transactionHash"].lower() for lg in es.logs(cs(addr), cb, block)}

    def accept_check(label, w3x, esx, token, target, blk, cb, own_txs, surplus):
        """(S07) Returns the ACCEPTED wei for `label`: 0 when nothing was requested or the proof failed (the caller's
        own strict check then reports the surplus as before). The proof = the receipts (ground truth) + the
        explorer's inflow index (cross-check); every failure is a strict error."""
        acc = acceptances.get(label)
        if not acc:
            return 0
        if surplus != acc["wei"]:
            errors.append(f"{label}: --accept-explained names {acc['wei']} wei but the surplus above the pin is {surplus} wei - nothing accepted")
            return 0
        errs, rec = verify_explained(w3x, token, target, blk, label, acc)
        if not errs:
            try:
                stray = {s["tx"].lower() for s in explain_inflows(esx, token, cs(target), cb, blk, own_txs)}
                missing = [t for t in acc["txs"] if t not in stray]
                if missing:
                    errs.append(f"{label}: tx {missing[0]} is not among the unsolicited inflows in the explorer index (cross-check failed)")
            except Exception as e:
                errs.append(f"{label}: explorer inflow cross-check failed ({scrub(e)})")
        rec["errors"], rec["verified"] = errs, not errs
        accepted_explained.append(rec)
        errors.extend(errs)
        return acc["wei"] if not errs else 0

    # ---- block pinning (over FINALIZED heads only)
    eth_final = finalized_number(w3e, "eth", errors, final)
    pol_final = finalized_number(w3p, "polygon", errors, final) if w3p is not None else None
    ts = parse_utc(a.at_utc) if a.at_utc else None
    pin_verified = False
    if a.at_utc and not a.eth_block:
        eth_block = find_block(w3e, ts, "first_at_or_after", eth_final)
        print(f"[pin] instant {a.at_utc} (unix {ts}) -> ETH block {eth_block} (first at/after; finalized head {eth_final})", flush=True)
    else:
        eth_block = a.eth_block or eth_final
    eth_blk = w3e.eth.get_block(eth_block)
    eth_ts, eth_hash = eth_blk["timestamp"], eth_blk["hash"].hex()
    poly_block = poly_ts = poly_hash = None
    if w3p is not None:
        poly_block = a.polygon_block or find_block(w3p, eth_ts, "last_at_or_before", pol_final)
        pblk = w3p.eth.get_block(poly_block)
        poly_ts, poly_hash = pblk["timestamp"], pblk["hash"].hex()
        print(f"[pin] ETH block {eth_block} ts {eth_ts} | Polygon block {poly_block} ts {poly_ts} (delta {poly_ts-eth_ts:+d}s)", flush=True)
    if eth_block > eth_final:
        errors.append(f"ETH block {eth_block} is not finalized yet (finalized head {eth_final})")
    if poly_block is not None and pol_final is not None and poly_block > pol_final:
        errors.append(f"Polygon block {poly_block} is not finalized yet (finalized head {pol_final})")
    if ts is not None:
        pin_errs = verify_pin(w3e, w3p, eth_block, poly_block, ts)
        errors += pin_errs
        pin_verified = not pin_errs
        print(f"[pin] rule verified on the pinned blocks: {pin_verified}", flush=True)
    if not eth_hash.startswith("0x"):
        eth_hash = "0x" + eth_hash
    if poly_hash and not poly_hash.startswith("0x"):
        poly_hash = "0x" + poly_hash

    out = a.out or os.path.join(REPO, "tmp", f"snapshot-{eth_block}-{poly_block or 'ethonly'}{'-final' if final else ''}")
    out_raw = os.path.join(out, "raw")
    if final and os.path.isdir(out_raw) and os.listdir(out_raw):
        raise SystemExit(f"--final requires an EMPTY raw/ cache ({out_raw} is not empty) - use a fresh --out")
    os.makedirs(out_raw, exist_ok=True)
    owners_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contract_owners.json")

    # ---- ETH wallets (1a)
    eth = chain_side("eth", w3e, es_eth, TPRO, eth_block, out_raw, a.workers, errors)
    S_eth = int(eth["total_supply_wei"])
    bal_e = eth["balances"]

    # ---- staking + vesting census on ETH (1b, 1c) - asserted, never labeled
    tpro_c = w3e.eth.contract(address=cs(TPRO), abi=ERC20)
    census = []
    for label, addr in STAKING.items():
        census.append(census_staking(w3e, es_eth, tpro_c, eth_block, label, addr, a.workers))
    for label, addr in VESTING.items():
        census.append(census_vesting(w3e, es_eth, tpro_c, eth_block, label, addr, a.workers))
    accepted_wei = {}              # label -> accepted surplus (S07); consumed by the verdicts and the exclusion split
    for c in census:
        surplus = int(c["diff_wei"]) - RESERVE_WEI.get(c["label"], 0)
        accepted_wei[c["label"]] = accept_check(c["label"], w3e, es_eth, TPRO, c["contract"], eth_block, c["creation_block"],
                                                set(c.get("own_txs", [])), surplus)
        errors += census_verdict(c, accepted_wei[c["label"]])

    # ---- Polygon side (1d)
    pol, pol_census = None, []
    pol_codes = {}
    if w3p is not None:
        pol = chain_side("polygon", w3p, es_pol, CHILD, poly_block, out_raw, a.workers, errors)
        child_c = w3p.eth.contract(address=cs(CHILD), abi=ERC20)
        pol_codes = get_codes(w3p, "polygon", sorted(pol["balances"]), poly_block, os.path.join(out_raw, f"polygon-codes-{poly_block}.json"), a.workers)
        pinned_pol = {cs(v): k for k, v in POLYGON_STAKING.items()}
        for addr in sorted(pol["balances"]):
            if addr in pinned_pol:
                pol_census.append(census_staking(w3p, es_pol, child_c, poly_block, pinned_pol[addr], addr, a.workers))
                continue
            if classify_code(pol_codes[addr]) != "contract" or addr == cs(DEAD):
                continue
            errors += unknown_staking_check(es_pol, "polygon", addr, pol_codes[addr])       # H06: bytecode fingerprint + strict ABI lookup
        for label, addr in POLYGON_STAKING.items():
            if cs(addr) not in pol["balances"]:
                findings.append(f"{label} ({addr}) holds 0 child TPRO at the block - nothing to credit")
        for c in pol_census:
            surplus = int(c["diff_wei"]) - RESERVE_WEI.get(c["label"], 0)
            accepted_wei[c["label"]] = accept_check(c["label"], w3p, es_pol, CHILD, c["contract"], poly_block, c["creation_block"],
                                                    set(c.get("own_txs", [])), surplus)
            errors += census_verdict(c, accepted_wei[c["label"]])

    # ---- second sources (round 4, R2-14): an independent transfer index + a second RPC provider
    def second_sources(chain, w3, url, url2, token, block, side):
        if a.no_second_source:
            findings.append(f"{chain}: second transfer index SKIPPED (--no-second-source)")
        elif urlsplit(url).hostname and urlsplit(url).hostname.endswith("alchemy.com"):
            t0 = time.time()
            try:
                tr = alchemy_transfers(w3, token, block)
                diffs, disputed = second_source_diff(side["logs"], tr)
                if len(diffs) != len(disputed):
                    raise SecondSourceError(f"{len(diffs)} differences but {len(disputed)} disputed transactions - every difference must map to a disputed tx settled by receipt")
                print(f"[{chain}] second transfer index: {len(tr)} transfers vs {len(side['logs'])} replayed -> "
                      f"{'IDENTICAL' if not diffs else str(len(diffs)) + ' DIFFERENCES in ' + str(len(disputed)) + ' tx(s), reconciling by receipt'} ({time.time()-t0:.0f}s)", flush=True)
                for d_ in diffs[:10]:
                    print(f"    {d_}", flush=True)
                rec_errs, rec_notes = reconcile_disputed_txs(w3, token, side["logs"], disputed)
                for n_ in rec_notes:
                    print(f"    {n_}", flush=True)
                errors.extend(rec_errs)
                findings.extend(rec_notes)
                side["second_source"] = {"provider": "alchemy_getAssetTransfers", "transfers": len(tr), "differences": len(diffs),
                                         "disputed_txs": len(disputed), "receipt_confirmed": len(rec_notes), "errors": len(rec_errs)}
            except SecondSourceError as e:
                errors.append(f"{chain}: second transfer index MALFORMED or INCOMPLETE - strict error (S04): {scrub(e)}")
            except Exception as e:
                errors.append(f"{chain}: second transfer index failed ({scrub(e)}) - re-run, or skip explicitly with --no-second-source (dry runs only)")
        else:
            errors.append(f"{chain}: second transfer index unavailable on {rpc_label(url)} (Alchemy endpoint required) - skip explicitly with --no-second-source (dry runs only)")
        w3b = w3e2 if chain == "eth" else w3p2
        if w3b is not None:
            try:
                errs2 = second_rpc_check(w3b, chain, token, block, int(side["total_supply_wei"]), side["balances"])
                errors.extend(errs2)
                side["second_rpc"] = {"host": rpc_label(url2), "differences": len(errs2)}
                print(f"[{chain}] second RPC provider {rpc_label(url2)}: {'AGREES' if not errs2 else 'DISAGREES'}", flush=True)
            except Exception as e:
                errors.append(f"{chain}: second RPC check failed ({scrub(e)})")
        else:
            (errors if final else findings).append(f"{chain}: no second RPC provider configured ({chain.upper()}_ARCHIVE_RPC_2) - required in --final")
    second_sources("eth", w3e, eth_rpc, eth_rpc2, TPRO, eth_block, eth)
    if pol is not None:
        second_sources("polygon", w3p, pol_rpc, pol_rpc2, CHILD, poly_block, pol)

    # ---- assemble rows (one per address) + exclusions
    L = Ledger(errors)       # rows / sources / exclusions / contract_report + the S02 eligibility decision on indirect credits
    rows, sources, exclusions, contract_report = L.rows, L.sources, L.exclusions, L.contract_report
    credit, exclude = L.credit, L.exclude

    census_by_addr = {c["contract"]: c for c in census}
    pol_census_by_addr = {c["contract"]: c for c in pol_census}
    eth_codes = get_codes(w3e, "eth", sorted(bal_e), eth_block, os.path.join(out_raw, f"eth-codes-{eth_block}.json"), a.workers)

    # MEXC custody (pinned) + the exceptions reserve
    reserve_exceptions, mexc_errs = mexc_check([load_env_value(f"MEXC_ETH_ADDRESS_{i}") for i in range(1, 5)], eth_codes, bal_e)
    errors += mexc_errs
    known_eth = {cs(DEAD): "0xdead (burned)", cs(ROOT_TUNNEL): "bridge root tunnel", cs(POOL_MANAGER): "Uniswap V4 PoolManager"}
    for m in MEXC_CUSTODY:
        known_eth[cs(m)] = "MEXC custody (excluded - withdraw before the block was the instruction; sum = the exceptions reserve)"

    # Morpheus bucket: MUST be empty at the block (amendment 17; decision 2026-09-09: the rule STAYS). The only way
    # past a non-zero bucket in --final is --accept-explained morpheus-bucket:<wei>:<tx,...> with the project lead's WRITTEN
    # GO on the day (runbook B1) - and even then every named transfer is verified on-chain (S07).
    bucket_wei = int(bal_e.get(cs(BUCKET), 0))
    bucket_accepted = accept_check("morpheus-bucket", w3e, es_eth, TPRO, BUCKET, eth_block, eth["creation_block"],
                                   own_txs_of(es_eth, BUCKET, eth["creation_block"], eth_block) if "morpheus-bucket" in acceptances else set(),
                                   bucket_wei)
    if bucket_wei and bucket_wei == bucket_accepted:
        known_eth[cs(BUCKET)] = "unsolicited transfer into morpheus-bucket (burned) - ACCEPTED by --accept-explained with the written GO (S07)"
        findings.append(f"Morpheus bucket: {tpro(bucket_wei)} TPRO at the block ACCEPTED as chain-verified unsolicited transfers (written GO logged in the runbook status updates)")
    elif bucket_wei:
        msg = f"Morpheus bucket {BUCKET} holds {tpro(bucket_wei)} TPRO at the block - it must be BURNED before the snapshot block"
        (errors if final else findings).append(msg + ("" if final else " (preview: excluded under a truthful label)"))
        known_eth[cs(BUCKET)] = "Morpheus bucket - STILL HOLDS TOKENS at this block (to be burned by the project before the snapshot block)"
    else:
        known_eth[cs(BUCKET)] = "Morpheus bucket (empty at the block, as required)"

    # designations (contract_owners.json) - validated, chain-scoped, verified on-chain
    known_excluded = list(known_eth) + [cs(TPRO), cs(CHILD), ZERO]
    owners, owner_errs = load_owners(owners_path, a.designation_address, known_excluded)
    errors += owner_errs
    for (chain, contract), entry in owners.items():
        w3x, esx, blk, w3t = (w3e, es_eth, eth_block, w3e2) if chain == "eth" else (w3p, es_pol, poly_block, w3p2)
        if w3x is None:
            errors.append(f"designation {chain}:{contract} cannot be verified in an --eth-only run")
            continue
        errors += verify_designation(w3x, esx, chain, contract, entry, blk, a.designation_address or "", w3t)

    # Uniswap V4: exact credit + third-party residual
    pm_bal = int(bal_e.get(cs(POOL_MANAGER), 0))
    v4, v4_credit, v4_residual = v4_report(w3e, es_eth, eth_block, pm_bal, errors)

    # bridge decomposition (needs both chains' logs)
    child_supply = int(pol["total_supply_wei"]) if pol else 0
    tunnel_bal = int(bal_e.get(cs(ROOT_TUNNEL), 0))
    surplus = tunnel_bal - child_supply
    tunnel_accepted = 0
    if pol and "bridge-tunnel" in acceptances:
        # the part of the surplus ABOVE the unclaimed exits is what an acceptance may explain; the rest decomposes as usual
        pre, _, _ = bridge_report(eth["logs"], pol["logs"], surplus, eth_block)
        tunnel_cb = es_eth.creation_block(cs(ROOT_TUNNEL), w3e)
        tunnel_accepted = accept_check("bridge-tunnel", w3e, es_eth, TPRO, ROOT_TUNNEL, eth_block, tunnel_cb,
                                       own_txs_of(es_eth, ROOT_TUNNEL, tunnel_cb, eth_block), surplus - int(pre["unclaimed_exits_total_wei"]))
    bridge, in_transit, bridge_errs = (bridge_report(eth["logs"], pol["logs"], surplus - tunnel_accepted, eth_block) if pol else ({}, [], []))
    errors += bridge_errs
    if in_transit:
        msg = f"bridge: {len(in_transit)} deposit(s) into the root tunnel without a child mint at the Polygon block (holder in transit)"
        (errors if final else findings).append(msg)
    credited_exits = {}
    if credit_exits and pol:
        for u in bridge["unclaimed_exits"]:
            credited_exits[u["address"]] = credited_exits.get(u["address"], 0) + int(u["amount_wei"])
    if pol:
        bridge["policy"] = "credited to the Polygon burners (default, amendment 27)" if credit_exits else "excluded (decision 4 shape, --exclude-unclaimed-exits, dry runs only)"

    # S02: code lookups for EVERY indirect beneficiary on the chain of its source, BEFORE anything is credited. Bridge
    # burners acted on Polygon (they burned the child token there), so their nature is read on Polygon.
    indirect_eth = sorted({cs(u) for c in census for u, _ in c["rows"]})
    indirect_pol = sorted({cs(u) for c in pol_census for u, _ in c["rows"]} | {cs(u) for u in credited_exits})
    eth_codes.update(get_codes(w3e, "eth", [u for u in indirect_eth if u not in eth_codes], eth_block,
                               os.path.join(out_raw, f"eth-codes-indirect-{eth_block}.json"), a.workers))
    if pol:
        pol_codes.update(get_codes(w3p, "polygon", [u for u in indirect_pol if u not in pol_codes], poly_block,
                                   os.path.join(out_raw, f"polygon-codes-indirect-{poly_block}.json"), a.workers))
    codes_by_chain = {"eth": eth_codes, "polygon": pol_codes}
    excluded_set = {x.lower() for x in known_eth} | {TPRO.lower(), CHILD.lower(), ZERO}

    def credit_indirect(chain, u, amt, label):
        L.credit_indirect(chain, u, amt, label, codes_by_chain[chain].get(cs(u), "0x"), excluded_set, owners)

    for addr, wei in sorted(bal_e.items(), key=lambda kv: -kv[1]):
        if addr in census_by_addr:
            c = census_by_addr[addr]
            for u, amt in c["rows"]:
                credit_indirect("eth", u, amt, c["label"])
            acc_w = accepted_wei.get(c["label"], 0)
            diff = int(c["diff_wei"]) - acc_w
            if diff:
                kind = ("staking reward reserve, unused (burned by the project on 2026-09-03; pinned)" if c["label"].startswith("staking")
                        else "vesting funding surplus (contract arithmetic; pinned)")
                exclude("eth", addr, c["label"], diff, kind)
            if acc_w:
                exclude("eth", addr, f"unsolicited transfer into {c['label']} (burned)", acc_w, "S07: chain-verified unsolicited transfers accepted by --accept-explained")
            continue
        if addr == cs(POOL_MANAGER):
            credit(LIQUIDITY, v4_credit, "uniswap-v4-LP")
            if v4_residual:
                exclude("eth", addr, "Uniswap V4 PoolManager - other pools' liquidity and claims (not the project's)", v4_residual,
                        "balance minus the project's exact principal + fees owed (round 4, R2-3)")
            continue
        if addr == cs(ROOT_TUNNEL):
            if pol is None:
                exclude("eth", addr, "bridge root tunnel", wei, "ETH-ONLY RUN: whole tunnel balance excluded (Polygon side not credited)")
            else:
                for u, amt in credited_exits.items():
                    credit_indirect("polygon", u, amt, "bridge-unclaimed-exit")
                remaining = surplus - tunnel_accepted - sum(credited_exits.values())
                exclude("eth", addr, "bridge surplus (unclaimed L1 exits of identified Polygon burners - see bridge.unclaimed_exits)",
                        remaining, f"root tunnel {tpro(wei)} minus child supply {tpro(child_supply)} (decision 4)")
                if tunnel_accepted:
                    exclude("eth", addr, "unsolicited transfer into bridge-tunnel (burned)", tunnel_accepted, "S07: chain-verified unsolicited transfers accepted by --accept-explained")
                if surplus < 0:
                    errors.append(f"root tunnel holds LESS than the child supply ({wei} < {child_supply})")
            continue
        if addr in known_eth:
            exclude("eth", addr, known_eth[addr], wei, "decision 4")
            continue
        kind = classify_code(eth_codes[addr])
        if kind == "contract":
            errors += unknown_staking_check(es_eth, "eth", addr, eth_codes[addr])          # H06 on Ethereum as well
            name = contract_name(es_eth, addr)
            label = classify_contract(name, eth_codes[addr], "eth", addr)
            if ("eth", addr) in owners:
                o = owners[("eth", addr)]
                credit(o["base_address"], wei, f"contract-wallet:{addr}")
                contract_report.append({"chain": "eth", "address": addr, "balance_wei": str(wei), "balance": tpro(wei),
                                        "status": f"re-addressed -> {o['base_address']} (tx {o['tx']}, verified by {o['verified_by']} {o['date']})",
                                        "label": label, "contract_name": name, "code_prefix": eth_codes[addr][:12]})
            else:
                exclude("eth", addr, label, wei, "decision 4/6 - contract-wallet path (designation tx) before the final table")
                contract_report.append({"chain": "eth", "address": addr, "balance_wei": str(wei), "balance": tpro(wei),
                                        "status": "EXCLUDED - no designation", "label": label, "contract_name": name, "code_prefix": eth_codes[addr][:12]})
            continue
        credit(addr, wei, "eth-wallet" if kind == "eoa" else "eth-wallet-7702")

    if pol:
        for addr, wei in sorted(pol["balances"].items(), key=lambda kv: -kv[1]):
            if addr in pol_census_by_addr:
                c = pol_census_by_addr[addr]
                for u, amt in c["rows"]:
                    credit_indirect("polygon", u, amt, c["label"])
                acc_w = accepted_wei.get(c["label"], 0)
                diff = int(c["diff_wei"]) - acc_w
                if diff:
                    exclude("polygon", addr, c["label"], diff, "pinned difference (must be 0 on Polygon - an error otherwise)")
                if acc_w:
                    exclude("polygon", addr, f"unsolicited transfer into {c['label']} (burned)", acc_w, "S07: chain-verified unsolicited transfers accepted by --accept-explained")
                continue
            if addr == cs(DEAD):
                exclude("polygon", addr, "0xdead (burned)", wei, "decision 4")
                continue
            kind = classify_code(pol_codes[addr])
            if kind == "contract":
                name = contract_name(es_pol, addr)
                label = classify_contract(name, pol_codes[addr], "polygon", addr)
                if ("polygon", addr) in owners:
                    o = owners[("polygon", addr)]
                    credit(o["base_address"], wei, f"contract-wallet:{addr}")
                    contract_report.append({"chain": "polygon", "address": addr, "balance_wei": str(wei), "balance": tpro(wei),
                                            "status": f"re-addressed -> {o['base_address']} (tx {o['tx']}, verified by {o['verified_by']} {o['date']})",
                                            "label": label, "contract_name": name, "code_prefix": pol_codes[addr][:12]})
                else:
                    exclude("polygon", addr, label, wei, "decision 4/6")
                    contract_report.append({"chain": "polygon", "address": addr, "balance_wei": str(wei), "balance": tpro(wei),
                                            "status": "EXCLUDED - no designation", "label": label, "contract_name": name, "code_prefix": pol_codes[addr][:12]})
                continue
            credit(addr, wei, "polygon-wallet" if kind == "eoa" else "polygon-wallet-7702")

    # ---- reconciliation of the assembled table against the two supplies
    # Identity: every wei of the Ethereum supply is either a row or a labeled exclusion. The root tunnel's
    # balance is decomposed into (bridge surplus) + (Polygon child supply) and the child supply into
    # (Polygon rows) + (Polygon exclusions), so rows + exclusions == eth totalSupply exactly, on every run.
    # NOTE: this identity is a self-consistency check of the assembly - it holds for ANY partition of the
    # balances into rows and exclusions. Correctness of the partition rests on the census verdicts, the
    # pinned constants, the second sources and the exact V4 math above (round 4, R2 section 3).
    table_total = sum(rows.values())
    excl_total = sum(int(e["amount_wei"]) for e in exclusions)
    expected = S_eth
    identity_ok = table_total + excl_total == expected
    if not identity_ok:
        errors.append(f"table identity broken: rows {table_total} + exclusions {excl_total} != eth totalSupply {expected} (diff {table_total + excl_total - expected} wei)")

    # ---- carve-outs (amendment 15): computed here, never typed
    treasury_row = rows.get(cs(TREASURY), 0)
    treasury_lock = treasury_row - COMMUNITY_TRANCHE_WEI - reserve_exceptions
    if treasury_lock <= 0:
        errors.append(f"carve-outs: Treasury row {treasury_row} - community tranche {COMMUNITY_TRANCHE_WEI} - reserve {reserve_exceptions} <= 0")
    carveouts = {"treasury_row_wei": str(treasury_row), "treasury_row": tpro(treasury_row),
                 "community_tranche_wei": str(COMMUNITY_TRANCHE_WEI), "community_tranche": tpro(COMMUNITY_TRANCHE_WEI),
                 "reserve_exceptions_wei": str(reserve_exceptions), "reserve_exceptions": tpro(reserve_exceptions),
                 "treasury_lock_wei": str(treasury_lock), "treasury_lock": tpro(treasury_lock),
                 "rule": "lock = Treasury row - 25,000,000 community tranche - MEXC custody sum at the block (amendment 15)"}

    # ---- merkle root + outputs
    table = sorted(rows.items(), key=lambda kv: (-kv[1], kv[0]))
    tree = StandardMerkleTree(table)
    bad = sum(1 for addr, amt, pf in tree.entries() if not verify(tree.root, addr, amt, pf))
    if bad:
        errors.append(f"{bad} merkle proofs failed self-verification")
    tag = f"{eth_block}-{poly_block if pol else 'ethonly'}"
    csv_name = f"snapshot-{tag}.csv"
    csv_path = os.path.join(out, csv_name)
    # operator copy (next to the run, never served): the per-row source labels stay internal (project rule 2026-09-14)
    with open(csv_path, "w", newline="") as f:
        wr = csv.writer(f, lineterminator="\n")
        wr.writerow(["address", "amount_wei", "sources"])
        for addr, amt in table:
            wr.writerow([addr, amt, ";".join(sorted(sources[addr]))])
    table_dir = os.path.join(out, "table")
    os.makedirs(table_dir, exist_ok=True)
    write_shards(tree, os.path.join(table_dir, "proofs"))
    # the PUBLISHED table: address,amount_wei only - exactly the merkle leaves; csvSha256 pins this file
    public_csv = os.path.join(table_dir, csv_name)
    with open(public_csv, "w", newline="") as f:
        wr = csv.writer(f, lineterminator="\n")
        wr.writerow(["address", "amount_wei"])
        for addr, amt in table:
            wr.writerow([addr, amt])
    csv_sha = sha256_file(public_csv)
    cw_fields = ["chain", "address", "balance_wei", "balance", "status", "label", "contract_name", "code_prefix"]
    with open(os.path.join(out, "contract-wallets.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cw_fields, lineterminator="\n")
        wr.writeheader()
        for r in sorted(contract_report, key=lambda r: (r["chain"], -int(r["balance_wei"]))):
            wr.writerow({k: r.get(k, "") for k in cw_fields})
    # published next to the proofs so the claim page can mark contract holders (amendment 21, round 4 R3-2). Keyed
    # "<chain>:<0xlower>" (S08): the same address can be a contract on both chains with different balances and
    # statuses - an address-only key let the Polygon record overwrite the Ethereum one (4 collisions on 09-07).
    cw_json = {}
    for r in sorted(contract_report, key=lambda r: (r["chain"], -int(r["balance_wei"]))):
        k = f"{r['chain']}:{r['address'].lower()}"
        rec = {f: r[f] for f in cw_fields if f != "address"}
        if k in cw_json:
            cw_json[k].setdefault("also", []).append(rec)      # the same contract as a direct holder AND an indirect beneficiary
        else:
            cw_json[k] = rec
    with open(os.path.join(table_dir, "contract-wallets.json"), "w") as f:
        json.dump(cw_json, f, separators=(",", ":"), sort_keys=True)

    per_source = {}
    def src_key(label):
        if label.startswith("eth-wallet"):
            return "eth-wallet(+7702)"
        if label.startswith("polygon-wallet"):
            return "polygon-wallet(+7702)"
        if label.startswith("contract-wallet:"):
            return "contract-wallet re-addressed"
        return label
    for addr, amt in table:
        for s_ in {src_key(x) for x in sources[addr]}:
            per_source.setdefault(s_, {"rows": 0})
            per_source[s_]["rows"] += 1
    src_amounts = {}                       # exact: what the ledger CREDITED under each source (refused indirect rows are not in it)
    for s_, w_ in L.by_source.items():
        src_amounts[src_key(s_)] = src_amounts.get(src_key(s_), 0) + w_
    for c in census + pol_census:
        src_amounts.setdefault(c["label"], 0)

    strip = lambda d: {k: v for k, v in d.items() if k not in ("balances", "logs")}
    summary = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": tool,
        "mode": "final" if final else "preview", "instant_utc": a.at_utc, "pin_verified": pin_verified,
        "eth": {"block": eth_block, "timestamp": eth_ts, "hash": eth_hash, "finalized_head_at_run": eth_final, "rpc": rpc_label(eth_rpc), **strip(eth)},
        "polygon": None if not pol else {"block": poly_block, "timestamp": poly_ts, "hash": poly_hash, "finalized_head_at_run": pol_final,
                                         "rpc": rpc_label(pol_rpc), **strip(pol)},
        "genesis_wei": str(table_total), "genesis": tpro(table_total), "rows": len(table),
        "merkle_root": tree.root, "leaf_encoding": ["address", "uint256"], "csv": csv_name, "csv_sha256": csv_sha,
        "identity": {"rows_total_wei": str(table_total), "exclusions_total_wei": str(excl_total),
                     "eth_total_supply_wei": str(expected), "note": "rows + exclusions == eth totalSupply (root tunnel = bridge surplus + child supply; child supply = polygon rows + polygon exclusions). Self-consistency only - see the census verdicts and second sources for correctness.", "ok": identity_ok},
        "sources": {k: {"rows": per_source.get(k, {}).get("rows", 0), "amount_wei": str(v), "amount": tpro(v)} for k, v in src_amounts.items()},
        "exclusions": exclusions, "exclusions_total": tpro(excl_total),
        "reserve_exceptions_wei": str(reserve_exceptions), "reserve_exceptions": tpro(reserve_exceptions),
        "mexc_custody": [cs(m) for m in MEXC_CUSTODY],
        "morpheus_bucket_wei_at_block": str(bucket_wei),
        "carveouts": carveouts,
        "census": [{k: v for k, v in c.items() if k not in ("rows", "functions", "explain", "own_txs")} for c in census + pol_census],
        "census_pinned_reserves_wei": {k: str(v) for k, v in RESERVE_WEI.items()},
        "accepted_explained": accepted_explained, "accepted_wei": {k: str(v) for k, v in accepted_wei.items() if v},
        "indirect_beneficiaries": {"classified_on_source_chain": True, **L.indirect},
        "contract_wallets": contract_report,
        "designations": {f"{k[0]}:{k[1]}": v for k, v in owners.items()}, "designation_address": a.designation_address,
        "labels": {"treasury": {"address": cs(TREASURY), "row_wei": str(rows.get(cs(TREASURY), 0)), "row": tpro(rows.get(cs(TREASURY), 0)), "note": "the ONLY locked wallet (decision 7)"},
                   "liquidity": {"address": cs(LIQUIDITY), "row_wei": str(rows.get(cs(LIQUIDITY), 0)), "row": tpro(rows.get(cs(LIQUIDITY), 0)), "sources": sorted(sources.get(cs(LIQUIDITY), []))}},
        "uniswap_v4": v4, "bridge": bridge, "top20": [{"address": a_, "amount": tpro(m), "sources": sorted(sources[a_])} for a_, m in table[:20]],
        "findings": findings, "errors": errors, "strict_ok": not errors,
    }
    jdump(summary, os.path.join(out, "summary.json"))        # operator-private: NOT written into table/, never served (2026-09-14)
    index = {"version": 1, "leafEncoding": ["address", "uint256"], "ethBlock": eth_block, "ethTimestamp": eth_ts, "ethBlockHash": eth_hash,
             "polygonBlock": poly_block, "polygonTimestamp": (poly_ts if pol else None), "polygonBlockHash": poly_hash,
             "rows": len(table), "totalWei": str(table_total), "merkleRoot": tree.root, "csv": csv_name, "csvSha256": csv_sha, "shards": 256,
             "contractWallets": "contract-wallets.json", "reserveExceptionsWei": str(reserve_exceptions),
             "mode": summary["mode"], "pinVerified": pin_verified, "strictOk": not errors, "errors": len(errors),
             "generatedAt": summary["generated"], "tool": summary["tool"]}
    jdump(index, os.path.join(table_dir, "index.json"))
    def write_sums():
        lines = []
        for root_, _, files in os.walk(table_dir):
            for fn in sorted(files):
                if fn == "SHA256SUMS":
                    continue
                p_ = os.path.join(root_, fn)
                lines.append(f"{sha256_file(p_)}  {os.path.relpath(p_, table_dir)}\n")
        with open(sums_path, "w") as f:
            f.writelines(lines)
        # content hash = the same lines WITHOUT index.json / summary.json (run provenance: generatedAt, tool sha,
        # endpoints), sorted - identical for two runs of the same table, unlike sha256(SHA256SUMS) (R5-11)
        body = "".join(sorted(l for l in lines if not l.endswith(("  index.json\n", "  summary.json\n"))))
        return hashlib.sha256(body.encode()).hexdigest()

    sums_path = os.path.join(table_dir, "SHA256SUMS")
    content_sha = write_sums()
    index["contentSha256"] = content_sha                     # [R5] deterministic per table; the B1 re-run gate compares it
    summary["content_sha256"] = content_sha
    jdump(index, os.path.join(table_dir, "index.json"))
    jdump(summary, os.path.join(out, "summary.json"))       # operator-private (2026-09-14): never into table/
    write_sums()                                            # index.json changed -> its line again
    sums_sha = sha256_file(sums_path)

    print("\n==== SNAPSHOT SUMMARY ====")
    print(f"mode {summary['mode']} | ETH block {eth_block} (ts {eth_ts}, hash {eth_hash}) | Polygon block {poly_block if pol else '-'}"
          f"{' (hash ' + poly_hash + ')' if poly_hash else ''} | pin rule verified: {pin_verified}")
    print(f"rows {len(table)} | genesis {tpro(table_total)} TPRO")
    print(f"merkle root       {tree.root}\ncsv sha256        {csv_sha}\nSHA256SUMS sha256 {sums_sha}   (pin all three in website/config.js)")
    print(f"content sha256    {content_sha}   (SHA256SUMS minus index.json: identical for every run of the same table - the B1 re-run gate)")
    print("sources:")
    for k, v in summary["sources"].items():
        print(f"  {k:<28} rows {v['rows']:>6}   {v['amount']:>22}")
    print("exclusions:")
    names = {r["address"]: r.get("contract_name", "") for r in contract_report}
    for e in exclusions:
        print(f"  {e['amount']:>22}  {e['label'][:70]:<70} {e['address']} {names.get(e['address'], '')}")
    print(f"  {summary['exclusions_total']:>22}  TOTAL EXCLUDED")
    print(f"identity rows + exclusions == eth totalSupply: {identity_ok}  ({tpro(table_total)} + {tpro(excl_total)} vs {tpro(expected)})")
    print(f"exceptions reserve (MEXC custody sum at the block): {tpro(reserve_exceptions)} | carve-outs: treasury row {carveouts['treasury_row']}"
          f" - tranche {carveouts['community_tranche']} - reserve {carveouts['reserve_exceptions']} = lock {carveouts['treasury_lock']}")
    print(f"Uniswap V4: PoolManager {v4['pool_manager_tpro_balance']} = project {v4['project_credit']} + third-party residual {v4['third_party_residual']}")
    if pol:
        print(f"bridge: surplus {bridge['surplus']} = {len(bridge['unclaimed_exits'])} unclaimed exits {bridge['unclaimed_exits_total']}"
              f"{' (CREDITED to their burners - policy default)' if credited_exits else ' (EXCLUDED - --exclude-unclaimed-exits, dry run shape)'}; deposits in transit {len(in_transit)}")
    print("findings:")
    for f_ in findings:
        print(f"  - {f_}")
    if accepted_explained:
        print("accepted unsolicited transfers (--accept-explained, S07):")
        for r in accepted_explained:
            print(f"  {'OK ' if r['verified'] else 'BAD'} {r['label']:<26} {r['amount']:>22}  txs {', '.join(t['tx'] for t in r['txs']) or '-'}")
    print(f"indirect beneficiaries classified on their source chain (S02): {L.indirect}")
    print("ERRORS:" if errors else "errors: none")
    for e in errors:
        print(f"  ! {e}")
    if errors and final:
        out = failed_dir(out)
        print(f"strict verdict: FAIL -> {out}   (renamed: a failed --final run is never a deployable bundle; index.json strictOk=false)")
        sys.exit(1)
    print(f"strict verdict: {'OK' if not errors else 'FAIL'} -> {out}")
    if errors and not a.no_strict:
        sys.exit(1)


if __name__ == "__main__":
    fatal_guard(main)
