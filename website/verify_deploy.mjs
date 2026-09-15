#!/usr/bin/env node
// verify_deploy.mjs - the pre-push / post-deploy gate of the claim page (round 4, R3-3 / R3-15;
// external review 2026-09-08: S01 validation gate, S08 chain-keyed contract wallets, S06 deadlines).
// Built-ins only. Refuses (exit 1) any deployment whose config.js pins do not match the table it
// serves, whose table files do not match their SHA256SUMS, whose index.json is not a strict-OK
// (and, once live, final-mode) table, or whose live contract disagrees.
//
//   node website/verify_deploy.mjs <site-dir>                      # local: config.js + table/ consistency
//   node website/verify_deploy.mjs <site-dir> --rpc                 # + read merkleRoot/genesis/token/sunsetTime on Base
//   node website/verify_deploy.mjs <site-dir> --table ./table-sample/    # the gate on the sample table (the public-repo export runs this)
//   node website/verify_deploy.mjs <site-dir> --served https://claim.tpro.network
//                                                                   # + compare the SERVED files with the local ones,
//                                                                   #   check the security headers and the DNS host
// <site-dir> holds index.html, config.js and table/ (the tool's table output copied next to the page).
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";
import vm from "node:vm";

const args = process.argv.slice(2);
const site = args[0];
if (!site) { console.error("usage: verify_deploy.mjs <site-dir> [--rpc] [--served https://host] [--table ./table-sample/]"); process.exit(2); }
const useRpc = args.includes("--rpc");
const served = args.includes("--served") ? args[args.indexOf("--served") + 1] : null;
let fails = 0;
const ok = (name, cond, detail = "") => { console.log(`  ${cond ? "ok  " : "FAIL"} ${name}${cond || !detail ? "" : " - " + detail}`); if (!cond) fails++; };
const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

// config.js is a browser file: it must be ONE object literal assigned to window.CLAIM_CONFIG and nothing else.
// The shape is checked BEFORE evaluation (node:vm is not a security boundary); anything that looks like code
// (calls, imports, property access on globals) is refused - the file is a data surface, not a program.
const cfgSrc = readFileSync(join(site, "config.js"), "utf8");
// blank out string literals first (URLs contain "//"), then drop the comments, then check the shape
const skeleton = cfgSrc.replace(/"(?:[^"\\\n]|\\.)*"/g, '""').replace(/'(?:[^'\\\n]|\\.)*'/g, "''").replace(/\/\/[^\n]*/g, "");
if (!/^\s*window\.CLAIM_CONFIG\s*=\s*\{[\s\S]*\};\s*$/.test(skeleton)) { console.error("config.js is not a single window.CLAIM_CONFIG = {...}; object literal"); process.exit(1); }
if (/\b(require|import|process|globalThis|Function|eval|fetch|window\.[a-zA-Z_]+\s*\()|=>|\bfunction\b|`|\(/.test(skeleton)) { console.error("config.js contains code (call, import, function, template) - refused"); process.exit(1); }
const sandbox = { window: {} };
vm.runInNewContext(cfgSrc, sandbox, { timeout: 1000 });
const C = sandbox.window.CLAIM_CONFIG;
if (!C || typeof C !== "object") { console.error("config.js did not define window.CLAIM_CONFIG"); process.exit(1); }
// --table <relative url> overrides config.js table.url (the public-repo export runs the gate on table-sample/)
const tableOverride = args.includes("--table") ? args[args.indexOf("--table") + 1] : null;
const tableDir = join(site, (tableOverride || C.table.url || "./table/").replace(/^\.\//, ""));
console.log(`[config] LIVE=${C.LIVE} distributor=${C.base.distributor} token=${C.base.token} table=${tableDir}`);
const preview = /^0x0{40}$/i.test(C.base.distributor);
const pinRoot = (C.table.merkleRoot || "").toLowerCase(), pinCsv = (C.table.csvSha256 || "").toLowerCase(), pinSums = (C.table.sha256sumsSha256 || "").toLowerCase();
const rpcs = Array.isArray(C.base.rpc) ? C.base.rpc : [C.base.rpc];

console.log("[table]");
const index = JSON.parse(readFileSync(join(tableDir, "index.json"), "utf8"));
const csvBuf = readFileSync(join(tableDir, index.csv));
const sumsBuf = readFileSync(join(tableDir, "SHA256SUMS"));
ok("index.json csvSha256 == sha256(csv)", sha256(csvBuf) === index.csvSha256.toLowerCase());
const sums = new Map(readFileSync(join(tableDir, "SHA256SUMS"), "utf8").split("\n").map((l) => l.trim().split(/\s+\*?/)).filter((p) => p.length === 2).map(([h, f]) => [f, h.toLowerCase()]));
let bad = 0;
for (const [f, h] of sums) { const p = join(tableDir, f); if (!existsSync(p) || sha256(readFileSync(p)) !== h) { bad++; console.log("    mismatch:", f); } }
ok(`every SHA256SUMS line matches (${sums.size} files)`, bad === 0);
ok("index.json is listed in SHA256SUMS and matches", sums.get("index.json") === sha256(readFileSync(join(tableDir, "index.json"))));
const shards = existsSync(join(tableDir, "proofs")) ? readdirSync(join(tableDir, "proofs")).filter((f) => /^[0-9a-f]{2}\.json$/.test(f)).length : 0;
ok("256 proof shards present", shards === 256, String(shards));
ok("contract-wallets.json present and listed", !index.contractWallets || (existsSync(join(tableDir, index.contractWallets)) && sums.has(index.contractWallets)));
// every file the page reads must be listed
for (const f of ["index.json", index.csv, ...(index.contractWallets ? [index.contractWallets] : [])]) ok(`listed: ${f}`, sums.has(f));
const proofsListed = [...sums.keys()].filter((f) => f.startsWith("proofs/")).length;
ok("all shards listed in SHA256SUMS", proofsListed === 256, String(proofsListed));
// contract-wallets.json is keyed "<chain>:<0xlower>" (S08): the same address on Ethereum and Polygon are two records
if (index.contractWallets && existsSync(join(tableDir, index.contractWallets))) {
  let cw = null;
  try { cw = JSON.parse(readFileSync(join(tableDir, index.contractWallets), "utf8")); } catch (e) { cw = null; }
  const isObj = !!cw && typeof cw === "object" && !Array.isArray(cw);
  ok("contract-wallets.json is a JSON object", isObj);
  if (isObj) {
    const keys = Object.keys(cw);
    const badKeys = keys.filter((k) => !/^(eth|polygon):0x[0-9a-f]{40}$/.test(k));
    ok(`contract-wallets.json keys are <eth|polygon>:<0xlower> (${keys.length} entries)`, badKeys.length === 0, badKeys.slice(0, 3).join(", "));
    const badChain = keys.filter((k) => !cw[k] || typeof cw[k] !== "object" || cw[k].chain !== k.split(":")[0]);
    ok("contract-wallets.json entry.chain equals the key prefix", badChain.length === 0, badChain.slice(0, 3).join(", "));
  }
}

// The table's own validation verdict (S01): a --final run that failed its checks still writes a complete-looking
// bundle; the page host must never serve one. LIVE requires strictOk === true and errors === 0 in index.json,
// and a LIVE deployment with a distributor (the T0 flip) requires mode === "final" - the same rule the deploy
// script enforces on chainid 8453.
console.log("[validation]");
if (C.LIVE) {
  ok("index.json strictOk === true (required when LIVE)", index.strictOk === true, `strictOk=${JSON.stringify(index.strictOk)}`);
  ok("index.json errors === 0 (required when LIVE)", index.errors === 0, `errors=${JSON.stringify(index.errors)}`);
} else console.log(`  info strictOk=${JSON.stringify(index.strictOk)} errors=${JSON.stringify(index.errors)} (both required when LIVE)`);
if (C.LIVE && !preview) ok('index.json mode === "final" (required for a LIVE deployment with a distributor)', index.mode === "final", `mode=${JSON.stringify(index.mode)}`);
else console.log(`  info mode=${JSON.stringify(index.mode)}${C.LIVE ? ' ("final" required at the T0 flip)' : ""}`);

console.log("[pins]");
if (pinRoot && !/^0x0{64}$/.test(pinRoot)) ok("config root == index.json root", pinRoot === index.merkleRoot.toLowerCase(), `${pinRoot} vs ${index.merkleRoot}`);
else ok("root pinned", !C.LIVE, "LIVE requires a pinned root");
if (pinCsv) ok("config csvSha256 == index.json csvSha256", pinCsv === index.csvSha256.toLowerCase());
else ok("csvSha256 pinned", !C.LIVE, "LIVE requires a pinned csvSha256");
if (pinSums) ok("config sha256sumsSha256 == sha256(SHA256SUMS)", pinSums === sha256(sumsBuf), `pin ${pinSums} vs file ${sha256(sumsBuf)}`);
else ok("sha256sumsSha256 pinned", !C.LIVE, "LIVE requires a pinned sha256sumsSha256");
if (C.table.ethBlock) ok("config ethBlock == index.json", C.table.ethBlock === index.ethBlock);
if (C.table.polygonBlock) ok("config polygonBlock == index.json", C.table.polygonBlock === index.polygonBlock);
console.log(`  pins to paste (from this table): root ${index.merkleRoot}  csvSha256 ${index.csvSha256}  sha256sumsSha256 ${sha256(sumsBuf)}`);

console.log("[mode]");
ok("token set iff distributor set", preview === /^0x0{40}$/i.test(C.base.token), "a live deployment needs both, a preview neither");
ok("rpc list has >= 2 https endpoints", rpcs.filter((u) => /^https:\/\//.test(u)).length >= 2);
const html = readFileSync(join(site, "index.html"), "utf8");
const csp = /connect-src ([^;"]+)/.exec(html);
const cspHosts = csp ? csp[1].split(/\s+/) : [];
for (const u of rpcs) { const host = new URL(u).origin; ok(`CSP connect-src allows ${host}`, cspHosts.includes(host)); }
ok("no external script/link/import in index.html", !/<script[^>]+src=["'](?!config\.js)/.test(html) && !/<link\s/.test(html) && !/@import|import\(/.test(html));

// every request has a deadline (S06) - a stalled endpoint costs one timeout and fails over, never a hang
const DEADLINE_MS = 10_000;
async function rpcCall(to, data) {
  for (const url of rpcs) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_call", params: [{ to, data }, "latest"] }), signal: AbortSignal.timeout(DEADLINE_MS) });
      const j = await r.json(); if (j.result && /^0x[0-9a-f]{64}$/i.test(j.result)) return j.result.toLowerCase();
    } catch (e) { console.log(`    (${new URL(url).origin}: ${e.name === "TimeoutError" ? "timeout" : e.message})`); }
  }
  throw new Error("no RPC answered within the deadline");
}
if (useRpc && !preview) {
  console.log("[chain]");
  const d = C.base.distributor;
  const root = await rpcCall(d, "0x2eb4a7ab"); ok("merkleRoot() == index.json root", root === index.merkleRoot.toLowerCase(), root);
  const gen = BigInt(await rpcCall(d, "0xa7f0b3de")); ok("genesis() == index.json totalWei", gen === BigInt(index.totalWei), gen.toString());
  const tok = "0x" + (await rpcCall(d, "0xfc0c546a")).slice(-40); ok("token() == config token", tok === C.base.token.toLowerCase(), tok);
  const sunset = BigInt(await rpcCall(d, "0x6c63c400")); console.log(`  sunsetTime() = ${sunset} = ${new Date(Number(sunset) * 1000).toISOString()}`);
  ok("sunsetTime() == config.js base.sunsetTime (pinned at launch) when on mainnet", C.base.chainId !== 8453 || (Number(C.base.sunsetTime) > 0 && sunset === BigInt(C.base.sunsetTime)), sunset.toString());
} else if (useRpc) console.log("[chain] preview mode - nothing deployed to read");

if (served) {
  console.log(`[served] ${served}`);
  const local = { "index.html": sha256(readFileSync(join(site, "index.html"))), "config.js": sha256(readFileSync(join(site, "config.js"))),
    "table/index.json": sha256(readFileSync(join(tableDir, "index.json"))), "table/SHA256SUMS": sha256(sumsBuf),
    ["table/" + index.csv]: sha256(csvBuf), ...(index.contractWallets ? { ["table/" + index.contractWallets]: sha256(readFileSync(join(tableDir, index.contractWallets))) } : {}),
    // two spot-check shards: every shard is bound to SHA256SUMS, which is pinned - the page verifies the rest per lookup
    "table/proofs/00.json": sha256(readFileSync(join(tableDir, "proofs/00.json"))), "table/proofs/ff.json": sha256(readFileSync(join(tableDir, "proofs/ff.json"))) };
  for (const [path, want] of Object.entries(local)) {
    try { const r = await fetch(served.replace(/\/$/, "") + "/" + path, { cache: "no-store", signal: AbortSignal.timeout(DEADLINE_MS) }); const got = sha256(Buffer.from(await r.arrayBuffer())); ok(`served ${path} == local`, r.ok && got === want, `${r.status} ${got}`); }
    catch (e) { ok(`served ${path} reachable`, false, e.message); }
  }
  try {
    const r = await fetch(served, { method: "HEAD", signal: AbortSignal.timeout(DEADLINE_MS) });
    for (const h of ["content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy", "strict-transport-security"]) ok(`header ${h}`, !!r.headers.get(h), "missing");
    ok("CSP header has frame-ancestors 'none'", /frame-ancestors 'none'/.test(r.headers.get("content-security-policy") || ""));
  } catch (e) { ok("HEAD request", false, e.message); }
  ok("served host == officialHost", new URL(served).hostname === C.officialHost, `${new URL(served).hostname} vs ${C.officialHost}`);
}
console.log(fails ? `\n${fails} CHECK(S) FAILED - do not publish` : "\nALL OK");
process.exit(fails ? 1 : 0);
