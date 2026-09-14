#!/usr/bin/env python3
"""make_kat.py - regenerate the REAL-TABLE known-answer fixtures for the Foundry suite from a published
table directory (round 4, R1-8: the synthetic KAT never touched a real root; the real-table KAT must be
regenerated from the FINAL table on the snapshot day, before the broadcast).

  .venv/bin/python scripts/snapshot/make_kat.py <out-dir>/table

Writes test/fixtures/real-table-kat.json (24 rows: the 8 largest, the 8 smallest, 8 seeded-random, plus the
Liquidity wallet's row if not already picked; proofs copied VERBATIM from the published shards) and
test/fixtures/real-table-index.json (a copy of index.json). `forge test` then proves that the published
artefacts and the contracts' leaf encoding agree, and the deploy-script tests cross-check against the same
index.json the deployer will read.
"""
import csv, hashlib, json, os, random, shutil, sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIQUIDITY = "0x786fDf0d8570c1637FcEcdC1B06405DFE715492B"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    table = sys.argv[1]
    idx = json.load(open(os.path.join(table, "index.json")))
    if idx.get("strictOk") is not True:
        sys.exit(f"index.json strictOk={idx.get('strictOk')!r} (errors={idx.get('errors')!r}) - fixtures are made from strict-OK tables only (S01)")
    rows = []
    with open(os.path.join(table, idx["csv"]), newline="") as f:
        for r in csv.DictReader(f):
            rows.append((r["address"], int(r["amount_wei"])))
    csv_sha = hashlib.sha256(open(os.path.join(table, idx["csv"]), "rb").read()).hexdigest()
    assert csv_sha == idx["csvSha256"], "csv sha256 != index.json"
    assert sum(m for _, m in rows) == int(idx["totalWei"]), "sum(rows) != totalWei"
    by_amount = sorted(rows, key=lambda r: (-r[1], r[0]))
    rng = random.Random(int(idx["merkleRoot"][2:10], 16))
    pick = {r[0] for r in by_amount[:8]} | {r[0] for r in by_amount[-8:]}
    pool = [r[0] for r in rows if r[0] not in pick]
    pick |= set(rng.sample(pool, 8))
    if any(a.lower() == LIQUIDITY.lower() for a, _ in rows):
        pick.add(next(a for a, _ in rows if a.lower() == LIQUIDITY.lower()))
    out_rows = []
    for a, m in by_amount:
        if a not in pick:
            continue
        shard = json.load(open(os.path.join(table, "proofs", a[2:4].lower() + ".json")))
        e = shard[a.lower()]
        assert int(e["amount"]) == m, f"shard amount != csv for {a}"
        out_rows.append({"address": a, "amount": str(m), "proof": e["proof"]})
    fixture = {"source": {"table": os.path.relpath(table, REPO), "ethBlock": idx["ethBlock"], "polygonBlock": idx["polygonBlock"],
                          "rows": idx["rows"], "csvSha256": csv_sha, "tool": idx.get("tool")},
               "root": idx["merkleRoot"], "totalWei": idx["totalWei"], "leafEncoding": idx["leafEncoding"], "rows": out_rows}
    dst = os.path.join(REPO, "test", "fixtures")
    with open(os.path.join(dst, "real-table-kat.json"), "w") as f:
        json.dump(fixture, f, indent=1)
    shutil.copyfile(os.path.join(table, "index.json"), os.path.join(dst, "real-table-index.json"))
    # the mainnet-shaped deploy-script tests need a FINAL-mode index (the script refuses a preview index on chainid 8453);
    # this variant is a TEST FIXTURE ONLY - the real final index is written by snapshot.py --final
    final = dict(idx); final["mode"] = "final"
    with open(os.path.join(dst, "real-table-index-final.json"), "w") as f:
        json.dump(final, f, indent=1)
    print(f"wrote {len(out_rows)} rows -> test/fixtures/real-table-kat.json + real-table-index.json (+ real-table-index-final.json, test-only) "
          f"(root {idx['merkleRoot']}, totalWei {idx['totalWei']}, mode {idx.get('mode')})")
    print("now: forge test --match-path 'test/audit/R1_RealTableKAT.t.sol' && forge test --match-path test/DeployV3Script.t.sol")


if __name__ == "__main__":
    main()
