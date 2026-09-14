#!/usr/bin/env python3
"""monitor_pins.py - daily watch of the 15 pinned targets whose balances the final snapshot asserts EXACTLY
(external review 2026-09-08, S07): the six Ethereum stakings, the three vestings, the Morpheus bucket and the bridge
root tunnel on Ethereum; the four Polygon stakings on Polygon. ONE Alchemy transfer-index query per chain since the
last run (state file under tmp/), then every transfer INTO or OUT OF a target is classified from its receipt:
  normal  - the target itself emitted an event in that transaction (a deposit / withdrawal / bridge / vesting flow)
            or the transaction called the target directly;
  STRAY   - a plain token transfer INTO a target with no event of the target = a surplus the census cannot explain
            (the S07 case: the final run STOPS on it unless --accept-explained names it with the written go);
  RESERVE - an OUTFLOW from a target without a Withdrawn event = the pinned reserve changed (a deficit: no acceptance
            path - STOP and re-pin before the freeze);
  BUCKET  - any inflow into the Morpheus bucket (mint payments: the project lead burns them before the block; the rule is 0).
Empty output under "flagged" = fine. Exit 1 when anything is flagged, 2 on a tooling error (no silent success).

  .venv/bin/python scripts/snapshot/monitor_pins.py [--state tmp/monitor-pins-state.json] [--lookback-hours 24] [--json]

Reads ETH_ARCHIVE_RPC / POLYGON_ARCHIVE_RPC (Alchemy, the same keyed endpoints as the tool) from the environment or
the repo's .env; endpoints are never printed. The decision log runs it daily until the snapshot (runbook Phase 0).
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snapshot as S  # noqa: E402
from census import STAKING, VESTING, REPO, scrub, fatal_guard, register_secret_url, hexstr, TRANSFER_TOPIC  # noqa: E402

WITHDRAWN_TOPIC = S.STAKING_V2_TOPICS[1]
TARGETS = {
    "eth": {**{S.cs(a): k for k, a in STAKING.items()}, **{S.cs(a): k for k, a in VESTING.items()},
            S.cs(S.BUCKET): "morpheus-bucket", S.cs(S.ROOT_TUNNEL): "bridge-tunnel"},
    "polygon": {S.cs(a): k for k, a in S.POLYGON_STAKING.items()},
}
TOKENS = {"eth": S.TPRO, "polygon": S.CHILD}
BLOCKS_PER_HOUR = {"eth": 300, "polygon": 1700}


def scan(chain, w3, since, upto):
    """All token transfers in (since, upto] from the Alchemy index (one paginated query), filtered to the targets."""
    out, page_key = [], None
    while True:
        params = {"fromBlock": hex(since + 1), "toBlock": hex(upto), "contractAddresses": [TOKENS[chain]], "category": ["erc20"],
                  "withMetadata": False, "excludeZeroValue": False, "maxCount": "0x3e8", "order": "asc"}
        if page_key:
            params["pageKey"] = page_key
        r = w3.provider.make_request("alchemy_getAssetTransfers", [params])
        if "error" in r or not isinstance(r.get("result"), dict):
            raise RuntimeError(f"{chain}: alchemy_getAssetTransfers failed: {scrub(str(r.get('error', r))[:120])}")
        for t in r["result"].get("transfers", []):
            tx, frm, to, val = S.index_record(t)
            for side, addr in (("in", to), ("out", frm)):
                tgt = TARGETS[chain].get(S.cs(addr))
                if tgt:
                    out.append({"chain": chain, "tx": tx, "side": side, "target": tgt, "address": S.cs(addr),
                                "counterparty": S.cs(frm if side == "in" else to), "wei": val, "amount": S.tpro(val)})
        page_key = r["result"].get("pageKey")
        if not page_key:
            return out


def classify(w3, hit):
    """Receipt-based: does the target itself appear as an emitter (or as the callee) in that transaction?"""
    rc = w3.eth.get_transaction_receipt(hit["tx"])
    tx = w3.eth.get_transaction(hit["tx"])
    hit["block"] = rc["blockNumber"]
    own = [l for l in rc["logs"] if str(l["address"]).lower() == hit["address"].lower()]
    direct = str(tx.get("to") or "").lower() == hit["address"].lower()
    withdrawn = any(hexstr(l["topics"][0]) == WITHDRAWN_TOPIC for l in own if l["topics"])
    if hit["target"] == "morpheus-bucket":
        hit["class"] = "BUCKET" if hit["side"] == "in" else "bucket-out (burn / move)"
        hit["flag"] = hit["side"] == "in"
    elif hit["side"] == "in":
        hit["class"] = "normal" if (own or direct) else "STRAY"
        hit["flag"] = not (own or direct)
    else:
        hit["class"] = "normal" if withdrawn or (hit["target"] == "bridge-tunnel" and own) or (hit["target"].startswith("vesting") and (own or direct)) else "RESERVE"
        hit["flag"] = hit["class"] == "RESERVE"
    return hit


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=os.path.join(REPO, "tmp", "monitor-pins-state.json"))
    ap.add_argument("--lookback-hours", type=int, default=24, help="window for the FIRST run (no state yet)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()
    state = S.jload(a.state) if os.path.exists(a.state) else {}
    report, flagged = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "chains": {}}, []
    for chain, var in (("eth", "ETH_ARCHIVE_RPC"), ("polygon", "POLYGON_ARCHIVE_RPC")):
        url = S.load_env_value(var)
        if not url or "alchemy.com" not in url:
            raise SystemExit(f"{var} must be the keyed Alchemy endpoint (the transfer index lives there)")
        register_secret_url(url)
        w3 = S.make_w3(url, chain)
        upto = w3.eth.get_block("finalized")["number"]
        since = int(state.get(chain, {}).get("cursor") or (upto - a.lookback_hours * BLOCKS_PER_HOUR[chain]))
        hits = [classify(w3, h) for h in scan(chain, w3, since, upto)]
        report["chains"][chain] = {"from_block": since + 1, "to_block": upto, "transfers_touching_targets": len(hits), "hits": hits}
        flagged += [h for h in hits if h["flag"]]
        state[chain] = {"cursor": upto, "scanned_at": report["generated"]}
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    S.jdump(state, a.state)
    if a.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        for chain, c in report["chains"].items():
            print(f"[{chain}] blocks {c['from_block']}..{c['to_block']} (finalized): {c['transfers_touching_targets']} transfer(s) touching the targets")
            for h in c["hits"]:
                print(f"    {h['class']:<12} {h['side']:<3} {h['target']:<24} {h['amount']:>20} TPRO  {'from' if h['side'] == 'in' else 'to'} {h['counterparty']}  tx {h['tx']}  block {h['block']}")
        print(f"flagged: {len(flagged)}" + ("" if not flagged else "  <-- STRAY / RESERVE / BUCKET lines above need a decision before the snapshot"))
    sys.exit(1 if flagged else 0)


if __name__ == "__main__":
    fatal_guard(main)
