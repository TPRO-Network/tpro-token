#!/usr/bin/env node
// make_sample.mjs - builds the 3-row SAMPLE claim table in the exact published
// format (docs/v3-interface.md section 2) so the claim page can be tested
// locally. Built-ins only (no npm). The addresses are Foundry/anvil test
// accounts, the amounts are made up. THIS IS NOT A SNAPSHOT.
//
//   cd website/table-sample && node make_sample.mjs
//   cd ../ && python3 -m http.server 8080   # then set table.url to "./table-sample/" in config.js
//
// Merkle algorithm = OpenZeppelin StandardMerkleTree ["address","uint256"]:
//   leaf   = keccak256(keccak256(abi.encode(address, uint256)))
//   leaves sorted ascending by hash; tree array of 2n-1 nodes, leaf i at index 2n-2-i,
//   node i = keccak256(sorted concat of children 2i+1 and 2i+2); proof = siblings up to the root.
import { writeFileSync, readFileSync, mkdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// ---- keccak256 (Keccak-f[1600], rate 136 bytes, pad 0x01..0x80) ----------------
const RC = [0x0000000000000001n, 0x0000000000008082n, 0x800000000000808an, 0x8000000080008000n,
  0x000000000000808bn, 0x0000000080000001n, 0x8000000080008081n, 0x8000000000008009n,
  0x000000000000008an, 0x0000000000000088n, 0x0000000080008009n, 0x000000008000000an,
  0x000000008000808bn, 0x800000000000008bn, 0x8000000000008089n, 0x8000000000008003n,
  0x8000000000008002n, 0x8000000000000080n, 0x000000000000800an, 0x800000008000000an,
  0x8000000080008081n, 0x8000000000008080n, 0x0000000080000001n, 0x8000000080008008n];
const ROT = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14];
const M64 = (1n << 64n) - 1n;
const rotl = (v, n) => (n === 0 ? v : (((v << BigInt(n)) | (v >> BigInt(64 - n))) & M64));
function keccakF(A) {
  for (let r = 0; r < 24; r++) {
    const C = [], D = [];
    for (let x = 0; x < 5; x++) C[x] = A[x] ^ A[x + 5] ^ A[x + 10] ^ A[x + 15] ^ A[x + 20];
    for (let x = 0; x < 5; x++) D[x] = C[(x + 4) % 5] ^ rotl(C[(x + 1) % 5], 1);
    for (let i = 0; i < 25; i++) A[i] ^= D[i % 5];
    const B = new Array(25);
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) B[y + 5 * ((2 * x + 3 * y) % 5)] = rotl(A[x + 5 * y], ROT[x + 5 * y]);
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) A[x + 5 * y] = B[x + 5 * y] ^ ((~B[((x + 1) % 5) + 5 * y] & M64) & B[((x + 2) % 5) + 5 * y]);
    A[0] ^= RC[r];
  }
}
export function keccak256(bytes) {
  const rate = 136;
  const padded = new Uint8Array(Math.floor(bytes.length / rate) * rate + rate);
  padded.set(bytes);
  padded[bytes.length] ^= 0x01;
  padded[padded.length - 1] ^= 0x80;
  const A = new Array(25).fill(0n);
  for (let off = 0; off < padded.length; off += rate) {
    for (let i = 0; i < 17; i++) {
      let lane = 0n;
      for (let b = 7; b >= 0; b--) lane = (lane << 8n) | BigInt(padded[off + 8 * i + b]);
      A[i] ^= lane;
    }
    keccakF(A);
  }
  let out = "";
  for (let i = 0; i < 4; i++) { let lane = A[i]; for (let b = 0; b < 8; b++) { out += Number(lane & 0xffn).toString(16).padStart(2, "0"); lane >>= 8n; } }
  return "0x" + out;
}
const hexToBytes = (h) => Uint8Array.from(h.replace(/^0x/, "").match(/../g) || [], (x) => parseInt(x, 16));
const word = (hexNo0x) => hexNo0x.padStart(64, "0");
const leafOf = (addr, amountWei) =>
  keccak256(hexToBytes(keccak256(hexToBytes(word(addr.toLowerCase().slice(2)) + word(BigInt(amountWei).toString(16))))));
const hashPair = (a, b) => keccak256(hexToBytes(a < b ? a + b.slice(2) : b + a.slice(2)));

// self-test against `cast keccak` vectors (fail loudly - never emit a table from a broken hash)
const enc = new TextEncoder();
const VECTORS = [
  [enc.encode(""), "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"],
  [enc.encode("abc"), "0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"],
  [new Uint8Array(200), "0xe1bb54e1bc3af48d01e5dbfc81015c98152a574f6428c6948aa4837c9c0baad9"],
  [new Uint8Array(136), "0x3a5912a7c5faa06ee4fe906253e339467a9ce87d533c65be3c15cb231cdb25f9"],
];
for (const [input, want] of VECTORS) if (keccak256(input) !== want) throw new Error("keccak self-test FAILED");
if (leafOf("0x0000000000000000000000000000000000000001", 1n) !== "0x66b32740ad8041bcc3b909c72d7e1afe60094ec55e3cde329b4b3a28501d826c") throw new Error("leaf self-test FAILED");

// ---- OZ StandardMerkleTree -----------------------------------------------------
export function buildTree(rows) { // rows: [{address, amount(BigInt)}]
  const hashed = rows.map((r, i) => ({ i, hash: leafOf(r.address, r.amount) })).sort((a, b) => (a.hash < b.hash ? -1 : a.hash > b.hash ? 1 : 0));
  const n = hashed.length, tree = new Array(2 * n - 1);
  hashed.forEach((h, k) => (tree[tree.length - 1 - k] = h.hash));
  for (let i = tree.length - 1 - n; i >= 0; i--) tree[i] = hashPair(tree[2 * i + 1], tree[2 * i + 2]);
  const proofs = rows.map(() => null);
  hashed.forEach((h, k) => {
    let idx = tree.length - 1 - k; const proof = [];
    while (idx > 0) { proof.push(tree[idx % 2 === 0 ? idx - 1 : idx + 1]); idx = Math.floor((idx - 1) / 2); }
    proofs[h.i] = proof;
  });
  return { root: tree[0], proofs, leaves: hashed };
}

// ---- the sample rows (anvil test accounts; made-up amounts) ----------------------
const ROWS = [
  { address: "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266", amount: 1234567500000000000000000n, sources: "eth-wallet" },
  { address: "0x70997970C51812dc3A010C7d01b50e0d17dc79C8", amount: 42000000000000000000n, sources: "eth-wallet;staking-T6" },
  { address: "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC", amount: 100000000123456789012345678n, sources: "polygon-wallet" },
];

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const here = dirname(fileURLToPath(import.meta.url));
  const { root, proofs } = buildTree(ROWS);
  for (const [k, r] of ROWS.entries()) { // every proof must verify before anything is written
    let h = leafOf(r.address, r.amount); for (const p of proofs[k]) h = hashPair(h, p);
    if (h !== root) throw new Error("proof self-check FAILED for " + r.address);
  }
  const ethBlock = 1, polygonBlock = 1;
  const csvName = `snapshot-${ethBlock}-${polygonBlock}.csv`;
  const csv = "address,amount_wei,sources\n" + ROWS.map((r) => `${r.address},${r.amount},${r.sources}`).join("\n") + "\n";
  const sha256 = (s) => createHash("sha256").update(s).digest("hex");
  mkdirSync(join(here, "proofs"), { recursive: true });
  // ALL 256 shards are written, empty ones as {} - so a missing shard file is
  // an ERROR for the page (fail-closed), never a silent "not in the table".
  const shards = {};
  for (let i = 0; i < 256; i++) shards[i.toString(16).padStart(2, "0")] = {};
  ROWS.forEach((r, k) => {
    const lower = r.address.toLowerCase(), xx = lower.slice(2, 4);
    shards[xx][lower] = { amount: r.amount.toString(), proof: proofs[k] };
  });
  const files = {};
  files[csvName] = csv;
  files["index.json"] = JSON.stringify({
    version: 1, leafEncoding: ["address", "uint256"],
    ethBlock, ethTimestamp: 0, polygonBlock, polygonTimestamp: 0,
    rows: ROWS.length, totalWei: ROWS.reduce((s, r) => s + r.amount, 0n).toString(), merkleRoot: root,
    csv: csvName, csvSha256: sha256(csv), shards: 256,
    generatedAt: "1970-01-01T00:00:00Z", tool: "website/table-sample/make_sample.mjs (SAMPLE - NOT A SNAPSHOT)",
  }, null, 1) + "\n";
  files["summary.json"] = JSON.stringify({ sample: true, note: "3 anvil test accounts with made-up amounts; format demo only", root }, null, 1) + "\n";
  for (const [xx, obj] of Object.entries(shards)) files[`proofs/${xx}.json`] = JSON.stringify(obj, null, 1) + "\n";
  for (const [name, body] of Object.entries(files)) writeFileSync(join(here, name), body);
  writeFileSync(join(here, "SHA256SUMS"), Object.keys(files).sort().map((n) => `${sha256(files[n])}  ${n}`).join("\n") + "\n");
  console.log("root", root);
  ROWS.forEach((r, k) => console.log(r.address, r.amount.toString(), "leaf", leafOf(r.address, r.amount), "proof", JSON.stringify(proofs[k])));
}
