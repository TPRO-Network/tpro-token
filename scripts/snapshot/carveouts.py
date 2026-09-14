#!/usr/bin/env python3
"""carveouts.py - print the three Treasury carve-out amounts from a snapshot summary.json (amendment 15;
round 4, R4-4). Nothing is typed by hand on deploy day: the numbers come from the tool's output, the
destination addresses from the environment, and the UNCX token fee (read live) adjusts the expected lock.

  .venv/bin/python scripts/snapshot/carveouts.py <out-dir>/summary.json \
      [--tranche-address 0x..] [--reserve-address 0x..] [--vesting-owner 0x..] [--uncx-token-fee-bps 35]

Prints, in wei and TPRO: the Treasury row, the community tranche (25,000,000), the exceptions reserve
(= MEXC custody sum at the block), the lock amount (row - tranche - reserve), and - when the addresses
are given - the exact `cast send` transfer lines plus the expected UNCX-locked remainder after the fee.
Exit 1 if the lock amount is not positive or the summary is not a strict-OK run.
"""
import argparse, json, sys
from decimal import Decimal

E18 = Decimal(10) ** 18


def tpro(wei):
    return f"{Decimal(wei) / E18:,.2f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("summary")
    ap.add_argument("--tranche-address")
    ap.add_argument("--reserve-address")
    ap.add_argument("--vesting-owner")
    ap.add_argument("--token", help="the new TPRO address on Base (for the cast lines)")
    ap.add_argument("--uncx-token-fee-bps", type=int, default=None, help="FEES().tokenFee read live on Base (35 = 0.35% on 2026-09-05)")
    a = ap.parse_args()
    s = json.load(open(a.summary))
    if not s.get("strict_ok"):
        sys.exit("summary.json is not a strict-OK run - no carve-outs from a failed table")
    c = s["carveouts"]
    row, tranche, reserve, lock = (int(c[k]) for k in ("treasury_row_wei", "community_tranche_wei", "reserve_exceptions_wei", "treasury_lock_wei"))
    if lock <= 0 or row - tranche - reserve != lock:
        sys.exit(f"carve-out arithmetic failed: {row} - {tranche} - {reserve} != {lock} or lock <= 0")
    print(f"snapshot: ETH block {s['eth']['block']} / Polygon block {(s.get('polygon') or {}).get('block')} | mode {s.get('mode')} | root {s['merkle_root']}")
    print(f"Treasury row (0x69e7)      {row:>32} wei  = {tpro(row)} TPRO")
    print(f"community tranche          {tranche:>32} wei  = {tpro(tranche)} TPRO")
    print(f"exceptions reserve (MEXC)  {reserve:>32} wei  = {tpro(reserve)} TPRO   (custody: {', '.join(s.get('mexc_custody', []))})")
    print(f"Treasury LOCK (UNCX)       {lock:>32} wei  = {tpro(lock)} TPRO")
    if a.uncx_token_fee_bps is not None:
        fee = lock * a.uncx_token_fee_bps // 10000
        print(f"UNCX token fee {a.uncx_token_fee_bps} bps  {fee:>32} wei  = {tpro(fee)} TPRO  -> locked remainder {lock - fee} wei = {tpro(lock - fee)} TPRO")
    if a.token and a.tranche_address and a.reserve_address:
        print("\n# transfers from the Treasury wallet (after it claimed its row) - amounts copy-pasted from above, never typed:")
        print(f"cast send {a.token} 'transfer(address,uint256)' {a.tranche_address} {tranche} --rpc-url base --ledger --from 0x69e77e8146f43bb591211c0283f16549a36fefb6")
        print(f"cast send {a.token} 'transfer(address,uint256)' {a.reserve_address} {reserve} --rpc-url base --ledger --from 0x69e77e8146f43bb591211c0283f16549a36fefb6")
        print(f"cast send {a.token} 'approve(address,uint256)' 0xa82685520c463a752d5319e6616e4e5fd0215e33 {lock} --rpc-url base --ledger --from 0x69e77e8146f43bb591211c0283f16549a36fefb6")
        if a.vesting_owner:
            print(f"# UNCX Token Vesting lock: owner {a.vesting_owner}, amount {lock}, startEmission = T0 + 12 months, endEmission = T0 + 36 months")
        print("# read back after each: cast call <token> 'balanceOf(address)(uint256)' <addr> --rpc-url base  == the number above")


if __name__ == "__main__":
    main()
