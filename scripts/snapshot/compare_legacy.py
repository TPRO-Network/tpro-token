#!/usr/bin/env python3
"""compare_legacy.py - cross-check census.py against the original dev team's 2023 pipeline (legacy/tokenomia-contracts, private repo).

That pipeline writes, per staking contract, runs/<T>/staking-1.db.csv (addr,amount,virtAmount) and
prints "{ sumAmount: '<wei>' }" (deposits minus withdrawals replayed from events) into the run log
runs/<T>.log. census.py writes <out>/census.csv (label,contract,address,amount_wei,amount_tpro).
This script compares them per address and per contract and exits 1 on any difference - AND on a missing or empty
reference set: all six directories T1..T6 with their CSV are REQUIRED, the census must contain staking rows, and at
least one address must have been compared (external review 2026-09-08, S09: `--runs /nonexistent` used to print
ALL MATCH and exit 0). It prints the number of comparisons.

Usage:
  .venv/bin/python scripts/snapshot/compare_legacy.py --runs <dir with T1..T6 subdirs + T*.log> \
                                                     --census tmp/census-<block>/census.csv
"""
import argparse, csv, glob, os, re, sys

EXPECTED = ("T1", "T2", "T3", "T4", "T5", "T6")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", required=True)
    ap.add_argument("--census", required=True)
    a = ap.parse_args()

    if not os.path.isdir(a.runs):
        sys.exit(f"FAIL: --runs {a.runs} is not a directory (the reference runs are required)")
    missing = [t for t in EXPECTED if not os.path.exists(os.path.join(a.runs, t, "staking-1.db.csv"))]
    if missing:
        sys.exit(f"FAIL: reference CSV missing for {', '.join(missing)} (all of T1..T6 are required under --runs)")
    if not os.path.exists(a.census):
        sys.exit(f"FAIL: --census {a.census} does not exist")
    census, staking_rows = {}, 0
    for r in csv.DictReader(open(a.census)):
        if r["label"].startswith("staking-"):
            census.setdefault(r["label"].replace("staking-", ""), {})[r["address"].lower()] = int(r["amount_wei"])
            staking_rows += 1
    if staking_rows == 0:
        sys.exit(f"FAIL: {a.census} has no staking rows - nothing to compare")
    replay = {}
    for lg in glob.glob(os.path.join(a.runs, "T*.log")):
        m = re.search(r"sumAmount: '(\d+)'", open(lg).read())
        if m:
            replay[os.path.basename(lg)[:-4]] = int(m.group(1))

    print(f"{'contract':<8} {'ref rows':>10} {'ref>0':>8} {'census>0':>9} {'match':>6} {'mismatch':>8} "
          f"{'ref sum':>20} {'census sum':>20} {'replay sum':>20}")
    all_ok, compared, contracts = True, 0, 0
    for t in EXPECTED:
        d = os.path.join(a.runs, t)
        f = os.path.join(d, "staking-1.db.csv")
        kam = {}
        for line in open(f):
            parts = line.strip().split(",")
            if len(parts) >= 2 and parts[0].startswith("0x"):
                kam[parts[0].lower()] = int(parts[1])
        cen = census.get(t, {})
        contracts += 1
        kpos = {u: v for u, v in kam.items() if v > 0}
        match = sum(1 for u, v in kpos.items() if cen.get(u) == v)
        mism = [(u, v, cen.get(u)) for u, v in kpos.items() if cen.get(u) != v] + \
               [(u, None, v) for u, v in cen.items() if u not in kpos]
        ks, cs, rs = sum(kpos.values()), sum(cen.values()), replay.get(t, 0)
        ok = not mism and ks == cs and (rs == 0 or rs == ks)
        all_ok &= ok
        compared += match + len(mism)
        print(f"{t:<8} {len(kam):>10} {len(kpos):>8} {len(cen):>9} {match:>6} {len(mism):>8} "
              f"{ks/1e18:>20,.2f} {cs/1e18:>20,.2f} {rs/1e18:>20,.2f}{'' if ok else '  <-- CHECK'}")
        for u, kv, cv in mism[:20]:
            print(f"    {u}  ref={'-' if kv is None else f'{kv/1e18:,.2f}'}  census={'-' if cv is None else f'{cv/1e18:,.2f}'}")
    print(f"compared {compared} address(es) across {contracts} contracts")
    if compared == 0:
        print("FAIL: no address was compared (empty reference set or empty census) - this is not a match")
        sys.exit(1)
    print("ALL MATCH" if all_ok else "DIFFERENCES FOUND")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
