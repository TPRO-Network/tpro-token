#!/usr/bin/env python3
"""census.py - reusable, READ-ONLY census of TPRO held in the staking + vesting contracts on Ethereum.

Why it exists: the snapshot table (redeploy v3) must credit stakers and vesting beneficiaries with the
exact amounts they hold INSIDE the contracts. The contracts expose per-user views (userInfo(pid, user)
on the stakings, currentBalance(user) on the vestings) but no list of users - so the address set is
rebuilt from the contracts' OWN on-chain footprint (their events + the decoded addBeneficiaries calldata),
and the invariant sum(per-user amounts) == TPRO.balanceOf(contract) at the same block proves the set is
complete. Any difference is a finding to explain, never a number to tune.

Usage (from the repo root; Python 3.12 + scripts/snapshot/requirements.lock in .venv):
  .venv/bin/python scripts/snapshot/census.py [--block N] [--rpc URL] [--out DIR]
                                              [--only staking|vesting] [--workers 6] [--no-strict]
Defaults: --block latest, --rpc https://ethereum.publicnode.com, --out tmp/census-<block>/.
Exit code 1 (strict mode) if any contract does not reconcile or any read failed.

Secrets: ETHERSCAN_API_KEY from the environment, else <repo>/.env. The key is never printed. Reads only -
no keys, no transactions.
Reading at a PAST --block needs an archive RPC (publicnode serves recent state; verify before relying on it).
"""
import argparse, csv, json, os, re, sys, time, urllib.parse, urllib.request
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from eth_abi import decode as abi_decode
from web3 import Web3

TPRO = "0x3540abe4f288b280a0740ad5121aec337c404d15"
STAKING = {
    "staking-T1": "0xE6925d1E109439dfBbC39373Ed3Ef449cBC15232",
    "staking-T2": "0x5c9977cA74Be8028a2715229A4ce1e7cABd6eFC6",
    "staking-T3": "0x764C205a50Ef89A55b2E8FE13B0e06E50391716e",
    "staking-T4": "0xa3c6dfaDF1ABA174e54406e692b09F61c4bFD689",
    "staking-T5": "0x674EeEF1Da64b9e398210621A61d1d2a8Bf27387",
    "staking-T6": "0x1C2AC88532f54A046f842B0651f0C2F6A80E729D",
}
VESTING = {
    # the three vesting contracts in creation order; neutral public labels (the 2022 round names are not published - project rule 2026-09-14)
    "vesting-1": "0xF00C073DEe3A4388eA21bb19E7b4c00A1F661515",
    "vesting-2": "0x5d409C45742d832F8e0B03c68CEe3775065c9CB3",
    "vesting-3": "0x9949a95b837d70e27a79c91e7026149c0cbe781c",
}
ES = "https://api.etherscan.io/v2/api"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ERC20 = [{"inputs": [{"type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}],
          "stateMutability": "view", "type": "function"}]


def load_key():
    k = os.environ.get("ETHERSCAN_API_KEY")
    if k:
        return k
    p = os.path.join(REPO, ".env")
    if os.path.exists(p):
        for line in open(p):
            s = line.strip()
            if s.startswith("ETHERSCAN_API_KEY="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("ETHERSCAN_API_KEY not found (environment or .env)")


_URL = re.compile(r"https?://[^\s'\"<>)\]]+", re.I)
_KV = re.compile(r"((?:dkey|apikey|api_key|key|token|auth|secret)=)[^&\s'\"]+", re.I)


def _host_only(m):
    u = urlsplit(m.group(0))
    return f"{u.scheme}://{u.hostname or '?'}/<redacted>"      # drops userinfo, path, query, fragment


_SECRETS = set()      # literal fragments of configured endpoints (path/query/userinfo) - always redacted
_RELURL = re.compile(r"(url:\s*)/\S*", re.I)              # requests-style "url: /v2/<key>" (no scheme/host)


def register_secret_url(url):
    """Register a configured RPC URL so that its path, query and userinfo are redacted wherever they appear
    (client libraries print the relative path without the host - the regexes alone would miss it)."""
    if not url:
        return
    u = urlsplit(url)
    frag = u.path + (("?" + u.query) if u.query else "")
    for f in (url, frag, u.username, u.password):
        if f and len(f) > 1:
            _SECRETS.add(f)


def scrub(e):
    """Exception (or text) -> text safe to print/persist: registered endpoint fragments, every URL (reduced to
    scheme://host), relative request paths and key-like query params are redacted. RPC/HTTP client errors embed
    the request URL, and a keyed archive endpoint carries its key in the path or query (or user:pass@)."""
    msg = str(e)
    for f in sorted(_SECRETS, key=len, reverse=True):
        msg = msg.replace(f, "<redacted>")
    msg = _KV.sub(r"\1<redacted>", _RELURL.sub(r"\1/<redacted>", _URL.sub(_host_only, msg)))
    return f"{type(e).__name__}: {msg[:200]}" if isinstance(e, BaseException) else msg[:400]


def fatal_guard(main_fn):
    """Run main(); on any unexpected exception print ONE scrubbed line and exit 1 - never a traceback
    (a traceback would print the provider URL, i.e. the key, to the terminal or a log file)."""
    try:
        main_fn()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:      # noqa: BLE001 - deliberate catch-all at the process boundary
        print(f"FATAL: {scrub(e)}", file=sys.stderr, flush=True)
        sys.exit(1)


def hexstr(b):
    h = b.hex() if not isinstance(b, str) else b
    return h[2:].lower() if h.startswith("0x") else h.lower()


def is_addr(v):
    return isinstance(v, str) and len(v) == 42 and v.startswith("0x")


class Etherscan:
    """Etherscan API v2 client (one key, every chain): chainid 1 = Ethereum, 137 = Polygon, 8453 = Base."""

    def __init__(self, key, chainid=1):
        self.key, self.last, self.chainid = key, 0.0, chainid

    EMPTY_MESSAGES = ("no records found", "no transactions found")
    RETRY_MARKERS = ("rate limit", "max calls", "busy", "timeout", "max rate")
    FATAL_MARKERS = ("invalid api key", "missing/invalid api key", "too many invalid api key")

    def get(self, **params):
        """One Etherscan call, FAIL-CLOSED (round 4, R2-2): returns the JSON object only when status == "1"
        or when the answer is the documented EMPTY shape (status "0" + "No records/transactions found",
        result = []). Any other status-"0" answer (rate limit, invalid key, "Too many invalid api key
        attempts", query timeout, ...) is retried and then RAISED - it is never returned to a caller that
        would mistake it for the end of a listing. Exception: module=contract (getabi / getsourcecode /
        getcontractcreation), where status "0" legitimately means "not verified" - returned to the caller."""
        params.update(chainid=self.chainid, apikey=self.key)
        url = ES + "?" + urllib.parse.urlencode(params)
        last = "no answer"
        for i in range(8):
            gap = 0.25 - (time.time() - self.last)
            if gap > 0:
                time.sleep(gap)
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    j = json.loads(r.read().decode())
                self.last = time.time()
            except Exception as e:
                last = scrub(e)
                time.sleep(1 + i)
                continue
            if not isinstance(j, dict):
                last = "non-object JSON answer"
                time.sleep(1 + i)
                continue
            status, res, msg = str(j.get("status")), j.get("result"), str(j.get("message", ""))
            if status == "1":
                return j
            text = (res if isinstance(res, str) else "").lower()
            if any(k in text for k in self.RETRY_MARKERS):
                last = f"transient: {text[:80]}"
                time.sleep(1 + i)
                continue
            if any(k in text for k in self.FATAL_MARKERS):
                raise RuntimeError(f"etherscan rejected the request: {params.get('module')}/{params.get('action')}: {scrub(text[:80])}")
            if params.get("module") == "contract":
                return j                        # "Contract source code not verified" / "No data found" are answers, not errors
            if msg.lower().startswith(self.EMPTY_MESSAGES) and (res == [] or res is None or isinstance(res, list)):
                j["result"] = []
                return j
            last = f"status {status} message {msg[:60]} result {str(res)[:80]}"
            time.sleep(1 + i)
        raise RuntimeError(f"etherscan failed: {params.get('module')}/{params.get('action')}: {scrub(last)}")

    def abi(self, addr):
        j = self.get(module="contract", action="getabi", address=addr)
        if j.get("status") != "1":
            raise RuntimeError(f"no verified ABI for {addr}: {j.get('result')}")
        return json.loads(j["result"])

    def creation_block(self, addr, w3):
        res = self.get(module="contract", action="getcontractcreation", contractaddresses=addr).get("result")
        if not isinstance(res, list) or not res or not isinstance(res[0], dict):
            raise RuntimeError(f"etherscan getcontractcreation({addr}) returned no record: {scrub(str(res)[:80])}")
        r = res[0]
        if r.get("blockNumber"):
            return int(r["blockNumber"])
        return w3.eth.get_transaction_receipt(r["txHash"])["blockNumber"]

    def logs(self, addr, from_block, to_block):
        """All logs emitted by addr in [from_block, to_block]; block-cursor pagination, no range limit."""
        out, seen, fb, page = [], set(), from_block, 1
        while True:
            res = self.get(module="logs", action="getLogs", address=addr, fromBlock=fb, toBlock=to_block,
                           page=page, offset=1000).get("result") or []
            if not isinstance(res, list):
                raise RuntimeError(f"etherscan getLogs returned a non-list result mid-pagination: {scrub(str(res))}")
            for lg in res:
                li = lg.get("logIndex") or "0x0"
                k = (lg["transactionHash"], int(li, 16) if li != "0x" else 0)
                if k not in seen:
                    seen.add(k)
                    out.append(lg)
            if len(res) < 1000:
                break
            first, last = int(res[0]["blockNumber"], 16), int(res[-1]["blockNumber"], 16)
            if first == last:
                page += 1          # more than 1000 logs inside one block: page within the window
            else:
                fb, page = last, 1  # re-fetch the last block (dedupe above) and continue
        return out

    def logs_by_topics(self, addr, from_block, to_block, **topics):
        """Logs emitted by addr filtered by topic0/topic1/topic2 (all given topics ANDed)."""
        out, seen, fb, page = [], set(), from_block, 1
        keys = sorted(topics)
        extra = {k: topics[k] for k in keys}
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                extra[f"{keys[i]}_{keys[j]}_opr"] = "and"
        while True:
            res = self.get(module="logs", action="getLogs", address=addr, fromBlock=fb, toBlock=to_block,
                           page=page, offset=1000, **extra).get("result") or []
            if not isinstance(res, list):
                raise RuntimeError(f"etherscan getLogs(topics) returned a non-list result mid-pagination: {scrub(str(res))}")
            for lg in res:
                li = lg.get("logIndex") or "0x0"
                k = (lg["transactionHash"], int(li, 16) if li != "0x" else 0)
                if k not in seen:
                    seen.add(k)
                    out.append(lg)
            if len(res) < 1000:
                break
            first, last = int(res[0]["blockNumber"], 16), int(res[-1]["blockNumber"], 16)
            if first == last:
                page += 1
            else:
                fb, page = last, 1
        return out

    def txlist(self, addr):
        out, start = [], 0
        while True:
            res = self.get(module="account", action="txlist", address=addr, startblock=start, endblock=99999999,
                           page=1, offset=1000, sort="asc").get("result") or []
            if not isinstance(res, list):
                raise RuntimeError(f"etherscan txlist returned a non-list result mid-pagination: {scrub(str(res))}")
            out += res
            if len(res) < 1000:
                break
            start = int(res[-1]["blockNumber"])
        seen, ded = set(), []
        for t in out:
            if t["hash"] not in seen:
                seen.add(t["hash"])
                ded.append(t)
        return ded


def event_index(w3, abi):
    idx = {}
    for e in abi:
        if e.get("type") != "event":
            continue
        sig = f"{e['name']}({','.join(i['type'] for i in e['inputs'])})"
        idx[hexstr(w3.keccak(text=sig))] = (e["name"], [i for i in e["inputs"] if i.get("indexed")],
                                            [i for i in e["inputs"] if not i.get("indexed")])
    return idx


def decode_log(idx, lg):
    ev = idx.get(hexstr(lg["topics"][0]))
    if not ev:
        return None, {}
    name, indexed, plain = ev
    args = {}
    for i, inp in enumerate(indexed):
        raw = bytes.fromhex(hexstr(lg["topics"][i + 1]))
        try:
            args[inp.get("name") or f"i{i}"] = abi_decode([inp["type"]], raw)[0]
        except Exception:
            args[inp.get("name") or f"i{i}"] = raw.hex()
    data = lg.get("data") or "0x"
    if plain and data != "0x":
        try:
            for p, v in zip(plain, abi_decode([p["type"] for p in plain], bytes.fromhex(data[2:]))):
                args[p.get("name") or "d"] = v
        except Exception:
            pass
    return name, args


def read_many(fn, items, workers):
    ok, errors = {}, []
    def one(u):
        for i in range(3):
            try:
                return u, fn(u), None
            except Exception as e:
                err = e
                time.sleep(0.5 * (i + 1))
        return u, None, scrub(err)
    with ThreadPoolExecutor(workers) as ex:
        for u, v, err in ex.map(one, items):
            if err:
                errors.append((u, err))
            else:
                ok[u] = v
    return ok, errors


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def explain_staking(w3, es, block, label, addr, cb, logs, idx):
    """Match every TPRO Transfer into/out of the contract with the contract's own Deposited/Withdrawn
    events (same tx). Transfers without a matching event = stray inflows / outflows to explain."""
    topic = "0x" + "0" * 24 + addr[2:].lower()
    ins = es.logs_by_topics(TPRO, cb, block, topic0=TRANSFER_TOPIC, topic2=topic)
    outs = es.logs_by_topics(TPRO, cb, block, topic0=TRANSFER_TOPIC, topic1=topic)
    dep_tx, wd_tx, dep_sum, wd_sum = set(), set(), 0, 0
    for lg in logs:
        name, args = decode_log(idx, lg)
        amt = next((v for k, v in args.items() if isinstance(v, int) and k != "pid"), 0)
        if name == "Deposited":
            dep_tx.add(lg["transactionHash"].lower()); dep_sum += amt
        elif name == "Withdrawn":
            wd_tx.add(lg["transactionHash"].lower()); wd_sum += amt
    def amt_of(lg):
        return int(lg["data"], 16) if lg.get("data") not in (None, "0x") else 0
    stray_in = [(Web3.to_checksum_address("0x" + hexstr(lg["topics"][1])[-40:]), amt_of(lg), lg["transactionHash"],
                 int(lg["blockNumber"], 16)) for lg in ins if lg["transactionHash"].lower() not in dep_tx]
    stray_out = [(Web3.to_checksum_address("0x" + hexstr(lg["topics"][2])[-40:]), amt_of(lg), lg["transactionHash"],
                  int(lg["blockNumber"], 16)) for lg in outs if lg["transactionHash"].lower() not in wd_tx]
    tin, tout = sum(amt_of(l) for l in ins), sum(amt_of(l) for l in outs)
    print(f"    explain: transfers in {len(ins)} = {tin/1e18:,.2f} (Deposited {dep_sum/1e18:,.2f}) | "
          f"transfers out {len(outs)} = {tout/1e18:,.2f} (Withdrawn {wd_sum/1e18:,.2f}) | "
          f"stray in {len(stray_in)} = {sum(x[1] for x in stray_in)/1e18:,.2f} | "
          f"stray out {len(stray_out)} = {sum(x[1] for x in stray_out)/1e18:,.2f}", flush=True)
    for frm, amt, tx, blk in stray_in:
        print(f"      stray IN  {amt/1e18:>16,.2f} from {frm} tx {tx} block {blk}", flush=True)
    for to, amt, tx, blk in stray_out:
        print(f"      stray OUT {amt/1e18:>16,.2f} to   {to} tx {tx} block {blk}", flush=True)
    return {"transfers_in": len(ins), "in_sum": tin / 1e18, "deposited_sum": dep_sum / 1e18,
            "transfers_out": len(outs), "out_sum": tout / 1e18, "withdrawn_sum": wd_sum / 1e18,
            "stray_in": stray_in, "stray_out": stray_out}


def explain_inflows(es, token, addr, cb, block, own_txs):
    """Token Transfer events INTO `addr` up to `block` whose transaction carries NO event emitted by `addr` itself
    (`own_txs` = the hashes of the contract's own logs, or an empty set for an EOA such as the bucket): the
    unsolicited inflows. The machinery behind `census.py --explain`, generalized to vestings, the bridge tunnel and
    the bucket for `snapshot.py --accept-explained` (external review 2026-09-08, S07). Explorer view only - the
    RPC receipts are the ground truth, this is the independent cross-check."""
    topic = "0x" + "0" * 24 + addr[2:].lower()
    ins = es.logs_by_topics(token, cb, block, topic0=TRANSFER_TOPIC, topic2=topic)
    own = {t.lower() for t in own_txs}
    out = []
    for lg in ins:
        if lg["transactionHash"].lower() in own:
            continue
        out.append({"from": Web3.to_checksum_address("0x" + hexstr(lg["topics"][1])[-40:]),
                    "amount_wei": int(lg["data"], 16) if lg.get("data") not in (None, "0x") else 0,
                    "tx": lg["transactionHash"].lower(), "block": int(lg["blockNumber"], 16)})
    return out


def census_staking(w3, es, tpro, block, label, addr, workers, explain=False):
    t0 = time.time()
    addr = Web3.to_checksum_address(addr)
    abi = es.abi(addr)
    c = w3.eth.contract(address=addr, abi=abi)
    cb = es.creation_block(addr, w3)
    logs = es.logs(addr, cb, block)
    idx = event_index(w3, abi)
    counts, users, pids = {}, set(), set()
    for lg in logs:
        name, args = decode_log(idx, lg)
        counts[name or "?unknown"] = counts.get(name or "?unknown", 0) + 1
        for k, v in args.items():
            if is_addr(v):
                users.add(Web3.to_checksum_address(v))
            if k == "pid" and isinstance(v, int):
                pids.add(v)
    fns = sorted({f["name"] for f in abi if f.get("type") == "function"})
    # StakingV2 (both the Ethereum and the Polygon build) exposes `maxPid` = the NUMBER of pools (pid 0..maxPid-1);
    # `poolLength` exists on neither (round 4, R2-13). Pids seen in events must fit that range - a pid outside it
    # is a FINDING (raised: the address set would be built on an unknown pool).
    declared = None
    for name in ("maxPid", "poolLength"):
        if name in fns:
            try:
                declared = int(getattr(c.functions, name)().call(block_identifier=block))
                break
            except Exception:
                continue
    if declared is not None:
        if declared > 64:
            raise RuntimeError(f"{label}: {declared} pools declared - unexpected, refusing to guess the pid range")
        if any(p >= declared for p in pids):
            raise RuntimeError(f"{label}: event pids {sorted(pids)} outside the declared range 0..{declared - 1}")
        pids |= set(range(declared))
    pids = sorted(pids) or [0]

    def amount(u):
        return sum(c.functions.userInfo(pid, u).call(block_identifier=block)[0] for pid in pids)

    got, errors = read_many(amount, sorted(users), min(workers, 4))
    rows = sorted(((u, a) for u, a in got.items() if a > 0), key=lambda r: -r[1])
    total = sum(a for _, a in rows)
    bal = tpro.functions.balanceOf(addr).call(block_identifier=block)
    print(f"[{label}] {addr} created {cb} | logs {len(logs)} {counts} | addresses in events {len(users)} | "
          f"stakers>0 {len(rows)} | pids {pids} | sum userInfo {total/1e18:,.2f} | balanceOf {bal/1e18:,.2f} | "
          f"DIFF {(bal-total)/1e18:,.2f} | read errors {len(errors)} | {time.time()-t0:.0f}s", flush=True)
    vendors, i = [], 0
    while i < 20:
        try:
            asset = c.functions.vendors(i).call(block_identifier=block)
        except Exception:
            break
        try:
            vaddr = c.functions.vendorInfo(asset).call(block_identifier=block)
            vbal = tpro.functions.balanceOf(vaddr).call(block_identifier=block)
        except Exception:
            vaddr, vbal = None, 0
        vendors.append({"asset": asset, "vendor": vaddr, "tpro_balance": vbal / 1e18})
        i += 1
    if vendors:
        print(f"    reward vendors: " + "; ".join(f"asset {v['asset'][:10]}.. vendor {str(v['vendor'])[:10]}.. TPRO {v['tpro_balance']:,.2f}"
                                                for v in vendors), flush=True)
    ex = explain_staking(w3, es, block, label, addr, cb, logs, idx) if explain and bal != total else None
    return {"label": label, "contract": addr, "creation_block": cb, "logs": len(logs), "events": counts,
            "addresses_in_events": len(users), "holders": len(rows), "pids": pids, "sum_wei": str(total),
            "balance_wei": str(bal), "diff_wei": str(bal - total), "sum": total / 1e18, "balance": bal / 1e18,
            "diff": (bal - total) / 1e18, "read_errors": errors, "functions": fns, "vendors": vendors,
            "explain": ex, "rows": rows, "own_txs": sorted({lg["transactionHash"].lower() for lg in logs})}


def census_vesting(w3, es, tpro, block, label, addr, workers):
    t0 = time.time()
    addr = Web3.to_checksum_address(addr)
    abi = es.abi(addr)
    c = w3.eth.contract(address=addr, abi=abi)
    fn = next((f for f in abi if f.get("type") == "function" and f["name"] == "addBeneficiaries"), None)
    bene, allocated = {}, 0
    if fn:
        sel = hexstr(w3.keccak(text=f"addBeneficiaries({','.join(i['type'] for i in fn['inputs'])})")[:4])
        for t in es.txlist(addr):
            inp = t.get("input", "")
            if hexstr(inp[:10]) == sel and t.get("isError", "0") == "0" and t.get("to", "").lower() == addr.lower():
                try:
                    _, params = c.decode_function_input(inp)
                except Exception as e:
                    raise RuntimeError(f"{label}: addBeneficiaries call {t.get('hash')} failed to decode: {scrub(e)}")
                lists = [v for v in params.values() if isinstance(v, (list, tuple)) and v]
                addrs = next((v for v in lists if is_addr(v[0])), [])
                amts = next((v for v in lists if isinstance(v[0], int)), [0] * len(addrs))
                for a, m in zip(addrs, amts):
                    a = Web3.to_checksum_address(a)
                    bene[a] = bene.get(a, 0) + m
                    allocated += m
    cb = es.creation_block(addr, w3)
    logs = es.logs(addr, cb, block)
    idx = event_index(w3, abi)
    counts = {}
    for lg in logs:
        name, args = decode_log(idx, lg)
        counts[name or "?unknown"] = counts.get(name or "?unknown", 0) + 1
        for v in args.values():
            if is_addr(v):
                bene.setdefault(Web3.to_checksum_address(v), 0)

    def amount(u):
        return c.functions.currentBalance(u).call(block_identifier=block)

    got, errors = read_many(amount, sorted(bene), min(workers, 4))
    rows = sorted(((u, a) for u, a in got.items() if a > 0), key=lambda r: -r[1])
    total = sum(a for _, a in rows)
    bal = tpro.functions.balanceOf(addr).call(block_identifier=block)
    print(f"[{label}] {addr} created {cb} | logs {len(logs)} {counts} | beneficiaries {len(bene)} "
          f"(allocated {allocated/1e18:,.2f}) | unclaimed>0 {len(rows)} | sum currentBalance {total/1e18:,.2f} | "
          f"balanceOf {bal/1e18:,.2f} | DIFF {(bal-total)/1e18:,.2f} | read errors {len(errors)} | "
          f"{time.time()-t0:.0f}s", flush=True)
    return {"label": label, "contract": addr, "creation_block": cb, "logs": len(logs), "events": counts,
            "beneficiaries": len(bene), "allocated": allocated / 1e18, "holders": len(rows),
            "sum_wei": str(total), "balance_wei": str(bal), "diff_wei": str(bal - total), "sum": total / 1e18,
            "balance": bal / 1e18, "diff": (bal - total) / 1e18, "read_errors": errors, "rows": rows,
            "own_txs": sorted({lg["transactionHash"].lower() for lg in logs})}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--block", type=int, default=None, help="Ethereum block to read at (default: latest)")
    ap.add_argument("--rpc", default=os.environ.get("ETH_ARCHIVE_RPC", "https://ethereum.publicnode.com"))
    ap.add_argument("--out", default=None, help="output dir (default: tmp/census-<block>/ in the repo)")
    ap.add_argument("--only", choices=["staking", "vesting"], default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-strict", action="store_true", help="exit 0 even if a contract does not reconcile")
    ap.add_argument("--explain", action="store_true",
                    help="for a staking contract that does not reconcile: match TPRO transfers with its events")
    a = ap.parse_args()

    es = Etherscan(load_key())
    register_secret_url(a.rpc)
    w3 = Web3(Web3.HTTPProvider(a.rpc, request_kwargs={"timeout": 90}))
    block = a.block or w3.eth.block_number
    ts = w3.eth.get_block(block)["timestamp"]
    out = a.out or os.path.join(REPO, "tmp", f"census-{block}")
    os.makedirs(out, exist_ok=True)
    tpro = w3.eth.contract(address=Web3.to_checksum_address(TPRO), abi=ERC20)
    rpc_label = f"{urlsplit(a.rpc).scheme}://{urlsplit(a.rpc).hostname}"   # never print/persist a keyed URL
    print(f"[eth] block {block} (unix {ts}) rpc {rpc_label} -> {out}", flush=True)

    results = []
    if a.only != "vesting":
        for label, addr in STAKING.items():
            results.append(census_staking(w3, es, tpro, block, label, addr, a.workers, a.explain))
    if a.only != "staking":
        for label, addr in VESTING.items():
            results.append(census_vesting(w3, es, tpro, block, label, addr, a.workers))

    clean = True
    with open(os.path.join(out, "census.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["label", "contract", "address", "amount_wei", "amount_tpro"])
        for r in results:
            for u, amt in r["rows"]:
                wr.writerow([r["label"], r["contract"], u, amt, f"{amt/1e18:.18f}".rstrip("0").rstrip(".")])
            if int(r["diff_wei"]) != 0 or r["read_errors"]:
                clean = False
    summary = {"block": block, "timestamp": ts, "rpc": rpc_label, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "reconciled": clean, "contracts": [{k: v for k, v in r.items() if k not in ("rows", "own_txs")} for r in results]}
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)

    print("\nlabel               holders   sum (TPRO)          balanceOf (TPRO)    diff")
    for r in results:
        print(f"{r['label']:<19} {r['holders']:>7}   {r['sum']:>18,.2f}  {r['balance']:>18,.2f}  {r['diff']:,.2f}"
              + ("" if not r["read_errors"] else f"  READ ERRORS {len(r['read_errors'])}"))
    print(f"\nreconciled: {clean}  ->  {out}/census.csv, summary.json")
    sys.exit(0 if clean or a.no_strict else 1)


if __name__ == "__main__":
    fatal_guard(main)
