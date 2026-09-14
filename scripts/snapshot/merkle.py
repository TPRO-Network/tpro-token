#!/usr/bin/env python3
"""merkle.py - OpenZeppelin StandardMerkleTree (leaf encoding ["address","uint256"]) in pure Python.

Reproduces `StandardMerkleTree.of(rows, ["address","uint256"])` from @openzeppelin/merkle-tree bit for bit,
so anyone can recompute the published root from the CSV with either implementation:
  leaf   = keccak256(keccak256(abi.encode(address, uint256)))     (double hash: a leaf can never collide
                                                                    with an internal node - second-preimage guard)
  leaves sorted ASCENDING by hash; tree = array of 2n-1 nodes, leaf i stored at index 2n-2-i;
  node i = keccak256(sorted concat of children at 2i+1 and 2i+2); proof = sibling hashes on the path.

Usage (from the repo root, with the .venv from scripts/snapshot/requirements.lock):
  .venv/bin/python scripts/snapshot/merkle.py <table.csv> [--proofs-out DIR] [--verify]
    <table.csv> has a header and columns address,amount_wei[,sources]; prints the root.
    --proofs-out DIR writes 256 shard files DIR/<xx>.json ({"0xaddr": {"amount": "..", "proof": [..]}})
    --verify re-verifies every proof against the root before exiting (exit 1 on any failure).
"""
import csv, json, os, sys
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address


def leaf_hash(address: str, amount: int) -> bytes:
    return keccak(keccak(abi_encode(["address", "uint256"], [to_checksum_address(address), int(amount)])))


def hash_pair(a: bytes, b: bytes) -> bytes:
    return keccak(a + b) if a <= b else keccak(b + a)


class StandardMerkleTree:
    """rows: list of (address, amount_wei). Addresses must be unique (asserted)."""

    def __init__(self, rows):
        if not rows:
            raise ValueError("empty table")
        seen = set()
        for a, _ in rows:
            k = a.lower()
            if k in seen:
                raise ValueError(f"duplicate address in table: {a}")
            seen.add(k)
        self.rows = [(to_checksum_address(a), int(m)) for a, m in rows]
        hashed = sorted(((leaf_hash(a, m), i) for i, (a, m) in enumerate(self.rows)), key=lambda t: t[0])
        n = len(hashed)
        self.tree = [b""] * (2 * n - 1)
        self.leaf_index = {}          # row index -> tree index
        for pos, (h, row_i) in enumerate(hashed):
            ti = len(self.tree) - 1 - pos
            self.tree[ti] = h
            self.leaf_index[row_i] = ti
        for i in range(len(self.tree) - 1 - n, -1, -1):
            self.tree[i] = hash_pair(self.tree[2 * i + 1], self.tree[2 * i + 2])

    @property
    def root(self) -> str:
        return "0x" + self.tree[0].hex()

    def proof(self, row_i: int):
        i = self.leaf_index[row_i]
        out = []
        while i > 0:
            sib = i + 1 if i % 2 == 1 else i - 1
            out.append("0x" + self.tree[sib].hex())
            i = (i - 1) // 2
        return out

    def entries(self):
        for i, (a, m) in enumerate(self.rows):
            yield a, m, self.proof(i)


def verify(root: str, address: str, amount: int, proof) -> bool:
    h = leaf_hash(address, amount)
    for p in proof:
        h = hash_pair(h, bytes.fromhex(p[2:]))
    return "0x" + h.hex() == root.lower()


def write_shards(tree: StandardMerkleTree, out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    shards = {}
    for a, m, pf in tree.entries():
        shards.setdefault(a[2:4].lower(), {})[a.lower()] = {"amount": str(m), "proof": pf}
    for xx in (f"{i:02x}" for i in range(256)):
        with open(os.path.join(out_dir, f"{xx}.json"), "w") as f:
            json.dump(shards.get(xx, {}), f, separators=(",", ":"), sort_keys=True)
    return len(shards)


def read_table(path):
    rows = []
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        for r in rd:
            rows.append((r["address"], int(r["amount_wei"])))
    return rows


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--proofs-out")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    rows = read_table(a.csv)
    t = StandardMerkleTree(rows)
    print(f"rows {len(rows)}  total_wei {sum(m for _, m in rows)}  root {t.root}")
    if a.proofs_out:
        n = write_shards(t, a.proofs_out)
        print(f"proof shards written to {a.proofs_out} ({n} non-empty of 256)")
    if a.verify:
        bad = [x for x in t.entries() if not verify(t.root, x[0], x[1], x[2])]
        print(f"verify: {len(rows) - len(bad)} ok, {len(bad)} FAILED")
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
