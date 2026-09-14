#!/usr/bin/env python3
"""selftest.py - OFFLINE checks of the snapshot tool's guard logic (no network, no keys). Run after every
edit of snapshot.py / census.py:  .venv/bin/python scripts/snapshot/selftest.py
Covers the round-4 fixes: DST-proof --at-utc (R2-5), finalized-head pinning guards (R2-6/R2-10), pinned
census verdicts (R2-1), MEXC set + reserve (R2-8), designation file validation (R2-9), second-source diff
(R2-14), exact Uniswap V4 math (R2-3), bridge decomposition (R2-4), cache headers (R2-11), labels (R2-12);
and the external-review fix round of 2026-09-09: strict second-index records + pagination (S04), the StakingV2
bytecode fingerprint + strict explorer lookups (H06), the eligibility decision on indirect beneficiaries (S02),
designation binding through the call tracer (S03), accepted unsolicited transfers (S07), the verdict plumbing
(S01: strictOk / FAILED dir / PUBLIC_REF), and compare_legacy.py's required reference set (S09)."""
import json, os, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snapshot as S  # noqa: E402
import census as CE  # noqa: E402

FAILS = []
HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{(' - ' + detail) if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


def expect_exit(fn, *args, **kw):
    try:
        fn(*args, **kw)
        return False
    except SystemExit:
        return True


def raises(exc, fn, *args, **kw):
    try:
        fn(*args, **kw)
        return False
    except exc:
        return True


class Eth:
    def __init__(self, ts): self.ts = ts
    @property
    def block_number(self): return len(self.ts) - 1
    def get_block(self, n): return {"timestamp": self.ts[n]}


class W3:
    def __init__(self, ts): self.eth = Eth(ts)


def H(c, n=64):
    return "0x" + (c * n)[:n]


print("[parse_utc]")
check("epoch", S.parse_utc("1970-01-01T00:00:00Z") == 0)
check("a fixed instant", S.parse_utc("2026-01-01T00:00:00Z") == 1767225600)
for tz in ("Europe/Warsaw", "America/New_York", "Asia/Phnom_Penh", "UTC"):
    os.environ["TZ"] = tz; time.tzset()
    check(f"DST-proof under {tz}", S.parse_utc("2026-07-01T12:30:00Z") == 1782909000)
os.environ.pop("TZ", None); time.tzset()

print("[find_block]")
w = W3([0, 10, 20, 30, 40, 100, 100, 100, 112, 124, 136])
check("first_at_or_after(100) = 5", S.find_block(w, 100, "first_at_or_after", 10) == 5)
check("first_at_or_after(101) = 8", S.find_block(w, 101, "first_at_or_after", 10) == 8)
check("last_at_or_before(100) = 7", S.find_block(w, 100, "last_at_or_before", 10) == 7)
check("last_at_or_before(99) = 4", S.find_block(w, 99, "last_at_or_before", 10) == 4)
check("future instant aborts", expect_exit(S.find_block, w, 200, "first_at_or_after", 10))
check("lagging head aborts (last_at_or_before past the head)", expect_exit(S.find_block, w, 150, "last_at_or_before", 10))
check("head == instant is not enough for last_at_or_before", expect_exit(S.find_block, w, 136, "last_at_or_before", 10))
check("search bounded by the finalized head", S.find_block(w, 100, "first_at_or_after", 6) == 5 and expect_exit(S.find_block, w, 130, "first_at_or_after", 8))

print("[census_verdict]")
c = {"label": "staking-T6", "diff_wei": str(S.RESERVE_WEI["staking-T6"]), "read_errors": []}
check("pinned reserve reconciles", S.census_verdict(c) == [])
c2 = dict(c, diff_wei=str(S.RESERVE_WEI["staking-T6"] + 1))
check("1 wei off the pinned reserve = error", len(S.census_verdict(c2)) == 1)
c3 = dict(c, diff_wei=str(S.RESERVE_WEI["staking-T6"] + 14705882350000000000000000))
check("a missing 14.7M staker = error", len(S.census_verdict(c3)) == 1)
c4 = dict(c, read_errors=[("0xabc", "429")])
check("read errors = error", any("read errors" in e for e in S.census_verdict(c4)))
check("negative diff = error", len(S.census_verdict(dict(c, diff_wei="-1"))) == 1)
check("unknown contract label = error", len(S.census_verdict({"label": "staking-T9", "diff_wei": "0", "read_errors": []})) == 1)
check("Polygon staking must reconcile to 0", S.census_verdict({"label": "polygon-staking-0x9687", "diff_wei": "0", "read_errors": []}) == []
      and len(S.census_verdict({"label": "polygon-staking-0x9687", "diff_wei": "5", "read_errors": []})) == 1)
check("vesting-2 surplus pinned to 0.0301", S.census_verdict({"label": "vesting-2", "diff_wei": "30100000000000000", "read_errors": []}) == [])
check("S07: pinned + ACCEPTED surplus reconciles", S.census_verdict(c2, accepted_wei=1) == [])
check("S07: an acceptance that does not match the surplus = error", len(S.census_verdict(c2, accepted_wei=2)) == 1)
check("S07: a negative acceptance is refused", len(S.census_verdict(c, accepted_wei=-1)) == 1)
check("S07: an acceptance never turns a DEFICIT into OK", len(S.census_verdict(dict(c, diff_wei=str(S.RESERVE_WEI["staking-T6"] - 1)), accepted_wei=1)) == 1)

print("[mexc_check]")
codes = {S.cs(a): "0x" for a in S.MEXC_CUSTODY}
bal = {S.cs(S.MEXC_CUSTODY[0]): 5, S.cs(S.MEXC_CUSTODY[2]): 7}
r, e = S.mexc_check(S.MEXC_CUSTODY, codes, bal)
check("reserve = custody sum, env matches", r == 12 and e == [])
r, e = S.mexc_check([S.MEXC_CUSTODY[0]] * 4, codes, bal)
check("duplicated env addresses = error", len(e) == 1)
r, e = S.mexc_check([S.MEXC_CUSTODY[0], S.MEXC_CUSTODY[1], S.MEXC_CUSTODY[2], "0x000000000000000000000000000000000000dEaD"], codes, bal)
check("a wrong env address = error", len(e) == 1)
r, e = S.mexc_check([], codes, bal)
check("no env = constants rule, no error", r == 12 and e == [])
codes2 = dict(codes); codes2[S.cs(S.MEXC_CUSTODY[1])] = "0x6080604052"
r, e = S.mexc_check([], codes2, bal)
check("custody with code = error", len(e) == 1)

print("[load_owners]")
tmp = tempfile.mkdtemp()
def owners_file(d):
    p = os.path.join(tmp, "o.json"); json.dump(d, open(p, "w")); return p
good = {"eth:0x476e48a832C593EE17317c008870a8aa4E649610": {"base_address": "0x786fDf0d8570c1637FcEcdC1B06405DFE715492B", "tx": "0x" + "ab" * 32, "verified_by": "the project lead", "date": "2026-09-20"}}
o, e = S.load_owners(owners_file(good), "0x69e77E8146F43bb591211c0283f16549A36fefB6", [])
check("valid entry parses", len(o) == 1 and e == [])
o, e = S.load_owners(owners_file(good), "", [])
check("entries without a designation address = error", len(e) == 1)
bad_key = {"0x476e48a832C593EE17317c008870a8aa4E649610": good["eth:0x476e48a832C593EE17317c008870a8aa4E649610"]}
o, e = S.load_owners(owners_file(bad_key), "0x69e77E8146F43bb591211c0283f16549A36fefB6", [])
check("key without chain scope = error", len(e) == 1 and len(o) == 0)
typo = {"eth:0x476e48a832C593EE17317c008870a8aa4E649610": dict(good["eth:0x476e48a832C593EE17317c008870a8aa4E649610"], base_address="0x786fdf0d8570c1637FcEcdC1B06405DFE715492B")}
o, e = S.load_owners(owners_file(typo), "0x69e77E8146F43bb591211c0283f16549A36fefB6", [])
check("EIP-55 typo in base_address = error (eth_utils would have accepted it)", len(e) == 1 and len(o) == 0)
lower = {"eth:0x476e48a832C593EE17317c008870a8aa4E649610": dict(good["eth:0x476e48a832C593EE17317c008870a8aa4E649610"], base_address="0x786fdf0d8570c1637fcecdc1b06405dfe715492b")}
o, e = S.load_owners(owners_file(lower), "0x69e77E8146F43bb591211c0283f16549A36fefB6", [])
check("lowercase base_address refused (checksum required)", len(e) == 1)
notx = {"eth:0x476e48a832C593EE17317c008870a8aa4E649610": {"base_address": "0x786fDf0d8570c1637FcEcdC1B06405DFE715492B"}}
o, e = S.load_owners(owners_file(notx), "0x69e77E8146F43bb591211c0283f16549A36fefB6", [])
check("missing tx / verified_by / date = errors", len(e) == 2)
forb = {"eth:0x476e48a832C593EE17317c008870a8aa4E649610": dict(good["eth:0x476e48a832C593EE17317c008870a8aa4E649610"], base_address="0x000000000000000000000000000000000000dEaD")}
o, e = S.load_owners(owners_file(forb), "0x69e77E8146F43bb591211c0283f16549A36fefB6", ["0x000000000000000000000000000000000000dEaD"])
check("forbidden target = error", len(e) == 1)
check("missing file = no owners, no error", S.load_owners(os.path.join(tmp, "none.json"), "", []) == ({}, []))

print("[second_source_diff + index records (S04)]")
TX1, TX2, TX3 = H("aa"), H("bb"), H("cc")
A1, A2, A3, A4 = H("1", 40), H("2", 40), H("3", 40), H("4", 40)
logs = [{"tx": TX1.upper().replace("0X", "0x"), "logIndex": 1, "from": A1, "to": A2, "value": "5"},
        {"tx": TX2, "logIndex": 0, "from": A3, "to": A4, "value": "7"}]
tr = [{"uniqueId": f"{TX1}:log:1", "from": A1, "to": A2, "rawContract": {"value": "0x5"}},
      {"uniqueId": f"{TX2}:log:0", "from": A3, "to": A4, "rawContract": {"value": "0x7"}}]
check("identical indexes -> no diff", S.second_source_diff(logs, tr) == ([], set()))
check("a missing event -> the tx is disputed", S.second_source_diff(logs, tr[:1])[1] == {TX2})
check("an extra event -> the tx is disputed", S.second_source_diff(logs[:1], tr)[1] == {TX2})
tr2 = [dict(tr[0]), dict(tr[1], rawContract={"value": "0x8"})]
check("a value mismatch -> the tx is disputed", S.second_source_diff(logs, tr2)[1] == {TX2})
check("a re-numbered logIndex alone is NOT a difference", S.second_source_diff(logs, [dict(tr[0], uniqueId=f"{TX1}:log:77"), tr[1]]) == ([], set()))
d_, s_ = S.second_source_diff(logs, tr + [dict(tr[0], uniqueId=f"{TX1}:log:99")])
check("a duplicated index record IS a difference (settled by receipt)", s_ == {TX1} and len(d_) == len(s_))
d_, s_ = S.second_source_diff(logs, [dict(tr[0], rawContract={"value": "0x6"})])
check("every difference maps to exactly one disputed tx", len(d_) == len(s_) == 2)
bad = {"uniqueId": "x"}
check("malformed FIRST record raises (was: a 'difference' with an empty disputed set)", raises(S.SecondSourceError, S.second_source_diff, logs, [bad] + tr))
check("malformed MIDDLE record raises", raises(S.SecondSourceError, S.second_source_diff, logs, [tr[0], bad, tr[1]]))
check("malformed LAST record raises", raises(S.SecondSourceError, S.second_source_diff, logs, tr + [bad]))
check("missing rawContract.value raises", raises(S.SecondSourceError, S.second_source_diff, logs, [dict(tr[0], rawContract={})]))
check("non-hex value raises", raises(S.SecondSourceError, S.second_source_diff, logs, [dict(tr[0], rawContract={"value": "5"})]))
check("null `to` raises (Alchemy writes explicit zero addresses for burns)", raises(S.SecondSourceError, S.second_source_diff, logs, [dict(tr[0], to=None)]))
check("non-address `from` raises", raises(S.SecondSourceError, S.second_source_diff, logs, [dict(tr[0], **{"from": "0x12"})]))
check("non-object record raises", raises(S.SecondSourceError, S.second_source_diff, logs, ["junk"]))
check("uniqueId without a log index raises", raises(S.SecondSourceError, S.second_source_diff, logs, [dict(tr[0], uniqueId=TX1)]))
check("a mint / burn record with explicit zero addresses is well-formed", S.index_record({"uniqueId": f"{TX3}:log:0", "from": S.ZERO, "to": A1, "rawContract": {"value": "0x0"}}) == (TX3, S.ZERO, A1, 0))


class FakeProvider:
    def __init__(self, pages): self.pages, self.i = pages, 0
    def make_request(self, method, params):
        assert method == "alchemy_getAssetTransfers"
        r = self.pages[min(self.i, len(self.pages) - 1)]; self.i += 1
        return r
class FakeIdxW3:
    def __init__(self, pages): self.provider = FakeProvider(pages)
p1 = {"result": {"transfers": [tr[0]], "pageKey": "k1"}}
p2 = {"result": {"transfers": [tr[1]]}}
check("two well-formed pages are concatenated", S.alchemy_transfers(FakeIdxW3([p1, p2]), S.TPRO, 100) == [tr[0], tr[1]])
check("a page without a `transfers` list = incomplete pagination -> raises", raises(S.SecondSourceError, S.alchemy_transfers, FakeIdxW3([p1, {"result": {"pageKey": None}}]), S.TPRO, 100))
check("a repeated pageKey raises (no infinite loop, no silent stop)", raises(S.SecondSourceError, S.alchemy_transfers, FakeIdxW3([p1, p1]), S.TPRO, 100))
check("an error answer raises", raises(S.SecondSourceError, S.alchemy_transfers, FakeIdxW3([{"error": {"code": -32000, "message": "boom"}}]), S.TPRO, 100))
check("a malformed pageKey raises", raises(S.SecondSourceError, S.alchemy_transfers, FakeIdxW3([{"result": {"transfers": [], "pageKey": ""}}, p2]), S.TPRO, 100))
check("the page cap raises instead of listing forever", raises(S.SecondSourceError, S.alchemy_transfers, FakeIdxW3([{"result": {"transfers": [], "pageKey": "k"}}, {"result": {"transfers": [], "pageKey": "j"}}]), S.TPRO, 100, max_pages=1))
# receipt tie-breaker: a duplicate/re-numbered record on the index side is an artifact; a log missing from the replay is an error
class FakeEth:
    def __init__(self, receipts): self.r = receipts
    def get_transaction_receipt(self, tx): return self.r[tx]
class FakeW3:
    def __init__(self, receipts): self.eth = FakeEth(receipts)
T = S.TRANSFER_TOPIC
def lg(addr, frm, to, val, idx):
    return {"address": addr, "topics": [T, "0x" + "00" * 12 + frm[2:].rjust(40, "0"), "0x" + "00" * 12 + to[2:].rjust(40, "0")], "data": hex(val), "logIndex": idx}
A, B_, TOK = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "7" * 40
replay = [{"tx": "0xT1", "logIndex": 5, "from": A, "to": B_, "value": "9"}]
w3f = FakeW3({"0xt1": {"logs": [lg(TOK, A, B_, 9, 5), lg("0x" + "9" * 40, A, B_, 9, 6)]}})
e, n = S.reconcile_disputed_txs(w3f, TOK, replay, {"0xt1"})
check("receipt confirms the replay -> note, no error", e == [] and len(n) == 1)
w3f2 = FakeW3({"0xt1": {"logs": [lg(TOK, A, B_, 9, 5), lg(TOK, B_, A, 4, 7)]}})
e, n = S.reconcile_disputed_txs(w3f2, TOK, replay, {"0xt1"})
check("a Transfer log missing from the replay -> ERROR", len(e) == 1 and n == [])
e, n = S.reconcile_disputed_txs(w3f, TOK, replay, {f"0x{i}" for i in range(260)})
check("too many disputed txs -> STOP", len(e) == 1 and "too many" in e[0])

print("[uniswap v4 math]")
check("sqrt(tick 0) = 2^96", S.get_sqrt_price_at_tick(0) == 79228162514264337593543950336)
check("sqrt(MIN_TICK)", S.get_sqrt_price_at_tick(-887272) == 4295128739)
check("sqrt(MAX_TICK)", S.get_sqrt_price_at_tick(887272) == 1461446703485210103287273052203988822378723970342)
L, sp = 18170768677443871546743, 133729333333047363450795710934895
sa, sb = S.get_sqrt_price_at_tick(-886800), S.get_sqrt_price_at_tick(886800)
a0, a1 = S.amounts_for_liquidity(sp, sa, sb, L)
check("#21106 principal TPRO at the dry-run block (exact)", a1 == 30670467473558960610576691, str(a1))
check("#21106 principal ETH ~ 10.76530", 10765301 * 10**12 <= a0 < 10765303 * 10**12, str(a0))
sa2, sb2 = S.get_sqrt_price_at_tick(131400), S.get_sqrt_price_at_tick(146400)
a0b, a1b = S.amounts_for_liquidity(sp, sa2, sb2, 59446064848714633702115)
check("#45911 out of range: all TPRO, no ETH (exact)", a0b == 0 and a1b == 47349730960272753522577343, str(a1b))
check("fee growth wrap-around handled", ((5 - (S.U256 - 3)) % S.U256) == 8)

print("[bridge_report]")
T = S.cs(S.ROOT_TUNNEL)
eth_logs = [{"from": T, "to": "0xA", "value": "10", "block": 1, "tx": "0xe1"},              # exit claimed
            {"from": "0xD", "to": T, "value": "3", "block": 2, "tx": "0xe2"}]                # deposit
pol_logs = [{"from": "0xA", "to": S.ZERO, "value": "10", "block": 1, "tx": "0xp1"},         # burn, claimed
            {"from": "0xB", "to": S.ZERO, "value": "4", "block": 2, "tx": "0xp2"},          # burn, unclaimed
            {"from": S.ZERO, "to": "0xD", "value": "3", "block": 3, "tx": "0xp3"}]           # mint for the deposit
rec, transit, errs = S.bridge_report(eth_logs, pol_logs, 4, 100)
check("surplus decomposes into the unclaimed burns", errs == [] and rec["unclaimed_exits"][0]["address"] == "0xB" and rec["unclaimed_exits_total_wei"] == "4")
check("deposit with its mint = not in transit", transit == [])
rec, transit, errs = S.bridge_report(eth_logs, pol_logs, 5, 100)
check("surplus != unmatched burns = error", len(errs) == 1)
rec, transit, errs = S.bridge_report(eth_logs, pol_logs[:2], 4, 100)
check("deposit without a child mint = in transit", len(transit) == 1)
rec, transit, errs = S.bridge_report(eth_logs + [{"from": T, "to": "0xZ", "value": "1", "block": 3, "tx": "0xe3"}], pol_logs, 4, 100)
check("exit without a burn = error", any("without a matching Polygon burn" in e for e in errs))

print("[classification + labels]")
check("EIP-7702 designator = EOA", S.classify_code("0xef0100" + "11" * 20) == "eoa-7702")
check("7702-prefixed but longer = contract", S.classify_code("0xef0100" + "11" * 21) == "contract")
check("no code = EOA", S.classify_code("0x") == "eoa")
check("Safe = wallet label", "designation path" in S.classify_contract("SafeProxy", "0x6080", "eth", "0x1") and "wallet" in S.classify_contract("SafeProxy", "0x6080", "eth", "0x1"))
check("minimal proxy = wallet label", "EIP-1167" in S.classify_contract("(unverified)", "0x363d3d373d3d3d363d73", "polygon", "0x1"))
check("fee collector = infrastructure label", "not a holder" in S.classify_contract("FeeCollector", "0x6080", "eth", "0x1"))
check("child token itself", "unrecoverable" in S.classify_contract("PolygonERC20Token", "0x6080", "polygon", S.CHILD))
check("unknown contract = unclassified", "unclassified" in S.classify_contract("(unverified)", "0x6000", "eth", "0x1"))

print("[StakingV2 fingerprint + strict explorer lookups (H06)]")
SHAPED = "0x6080" + S.STAKING_V2_TOPICS[0] + "5b" + S.USERINFO_SELECTOR + "60" + S.STAKING_V2_TOPICS[1] + "00"
check("topics + selector in the bytecode = StakingV2-shaped", S.staking_v2_shaped(SHAPED))
check("one topic missing = not shaped", not S.staking_v2_shaped("0x6080" + S.STAKING_V2_TOPICS[0] + S.USERINFO_SELECTOR))
check("selector missing = not shaped", not S.staking_v2_shaped("0x" + S.STAKING_V2_TOPICS[0] + S.STAKING_V2_TOPICS[1]))
check("the fingerprint uses the real StakingV2 signatures", S.STAKING_V2_TOPICS[0] == "c490a74c1058132dffb93944d555ddd1817ae53b7367ea1126ff123b1b134a58" and S.USERINFO_SELECTOR == "93f1a40b")
class FakeEs:
    def __init__(self, answer, chainid=137): self.answer, self.chainid = answer, chainid
    def get(self, **params):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer
ABI_STAKING = json.dumps([{"type": "function", "name": "userInfo"}, {"type": "event", "name": "Deposited"}, {"type": "event", "name": "Withdrawn"}])
verified = {"status": "1", "result": [{"ABI": ABI_STAKING, "ContractName": "StakingV2"}]}
unverified = {"status": "0", "result": [{"ABI": "Contract source code not verified", "ContractName": ""}]}
S._FACTS.clear()
check("shaped + unverified -> ONE error (the fingerprint)", len(S.unknown_staking_check(FakeEs(unverified), "polygon", H("d", 40), SHAPED)) == 1)
check("not shaped + verified StakingV2 ABI -> ONE error (the ABI)", len(S.unknown_staking_check(FakeEs(verified), "polygon", H("e", 40), "0x6080")) == 1)
check("explorer failure -> ERROR, not a silent empty ABI (was: abi = [])", len(S.unknown_staking_check(FakeEs(RuntimeError("etherscan failed: rate limit")), "polygon", H("f", 40), "0x6080")) == 1)
check("not shaped + not verified -> no error", S.unknown_staking_check(FakeEs(unverified), "polygon", H("a", 40), "0x6080") == [])
check("contract_facts: verified -> name + abi", S.contract_facts(FakeEs(verified, 1), H("b", 40)) == {"name": "StakingV2", "abi": json.loads(ABI_STAKING)})
check("contract_facts: not verified -> (unverified), no abi", S.contract_facts(FakeEs(unverified, 1), H("c", 40)) == {"name": "(unverified)", "abi": None})
check("contract_facts: an unexpected answer RAISES", raises(RuntimeError, S.contract_facts, FakeEs({"status": "0", "result": "Max rate limit reached"}, 1), H("9", 40)))
check("contract_facts: a malformed shape RAISES", raises(RuntimeError, S.contract_facts, FakeEs({"status": "1", "result": []}, 1), H("8", 40)))
check("contract_name never raises (annotation only)", S.contract_name(FakeEs(RuntimeError("down"), 1), H("7", 40)) == "(lookup failed)")

print("[indirect beneficiaries: eligibility + ledger (S02)]")
CONTRACT, CONTRACT2, EOA, BASE = S.cs(H("1a", 40)), S.cs(H("2b", 40)), S.cs(H("3c", 40)), S.cs(H("4d", 40))
excluded = {S.MEXC_CUSTODY[0].lower(), S.BUCKET.lower(), S.ZERO}
owners = {("eth", CONTRACT): {"base_address": BASE, "tx": H("ee"), "verified_by": "x", "date": "2026-09-20"}}
check("contract staker without a designation -> ERROR (fail-closed)", S.eligibility("eth", CONTRACT2, "0x6080604052", excluded, owners)[0] == "error")
check("excluded beneficiary (MEXC custody as a staker) -> ERROR", S.eligibility("eth", S.MEXC_CUSTODY[0], "0x", excluded, owners)[0] == "error")
check("the bucket as a beneficiary -> ERROR", S.eligibility("eth", S.BUCKET, "0x", excluded, owners)[0] == "error")
check("EIP-7702 staker -> credit (a key-controlled EOA)", S.eligibility("eth", EOA, "0xef0100" + "11" * 20, excluded, owners) == ("credit", EOA))
check("plain EOA -> credit", S.eligibility("polygon", EOA, "0x", excluded, owners) == ("credit", EOA))
check("contract WITH a verified designation -> re-addressed to the Base address", S.eligibility("eth", CONTRACT, "0x6080", excluded, owners) == ("readdress", BASE))
check("the designation is chain-scoped (the same contract on Polygon has none)", S.eligibility("polygon", CONTRACT, "0x6080", excluded, owners)[0] == "error")
errs = []
Lg = S.Ledger(errs)
Lg.credit(EOA, 10, "eth-wallet")
Lg.credit_indirect("eth", EOA, 5, "staking-T4", "0x", excluded, owners)
Lg.credit_indirect("polygon", EOA, 2, "polygon-staking-0x34b9", "0xef0100" + "11" * 20, excluded, owners)
check("direct + indirect holdings merge into ONE row with every source", Lg.rows[EOA] == 17 and Lg.sources[EOA] == ["eth-wallet", "staking-T4", "polygon-staking-0x34b9"] and errs == [])
Lg.credit_indirect("eth", CONTRACT2, 7, "staking-T5", "0x6080604052", excluded, owners)
check("a refused contract staker: strict error + labeled exclusion + contract report, NO row", len(errs) == 1 and CONTRACT2 not in Lg.rows
      and Lg.exclusions[-1]["amount_wei"] == "7" and "REFUSED" in Lg.exclusions[-1]["label"] and Lg.contract_report[-1]["address"] == CONTRACT2)
Lg.credit_indirect("eth", S.MEXC_CUSTODY[0], 3, "vesting-1", "0x", excluded, owners)
check("a refused excluded beneficiary: strict error + exclusion, no contract report entry (an EOA)", len(errs) == 2 and len(Lg.contract_report) == 1 and Lg.exclusions[-1]["amount_wei"] == "3")
Lg.credit_indirect("eth", CONTRACT, 11, "staking-T6", "0x6080", excluded, owners)
check("a designated contract staker is re-addressed (row under the Base address, source contract-wallet:<contract>)",
      Lg.rows.get(BASE) == 11 and Lg.sources[BASE] == [f"contract-wallet:{CONTRACT}"] and len(errs) == 2)
check("classification counters", Lg.indirect == {"eoa": 1, "eoa-7702": 1, "readdressed": 1, "refused": 2})
check("per-source credited amounts are exact (refused amounts are not in them)", Lg.by_source == {"eth-wallet": 10, "staking-T4": 5, "polygon-staking-0x34b9": 2, f"contract-wallet:{CONTRACT}": 11})
check("supply identity: rows + exclusions == everything offered", sum(Lg.rows.values()) + sum(int(x["amount_wei"]) for x in Lg.exclusions) == 10 + 5 + 2 + 7 + 3 + 11)
check("source chain of a label", S.source_chain("polygon-staking-0x34b9") == "polygon" and S.source_chain("staking-T1") == "eth" and S.source_chain("bridge-unclaimed-exit") == "eth")

print("[designation binding through the call tracer (S03)]")
SAFE, DESIG, BASEA = H("5a", 40), H("6d", 40), H("7b", 40)
class TraceProv:
    def __init__(self, answer): self.answer = answer
    def make_request(self, method, params):
        assert method == "debug_traceTransaction" and params[1] == {"tracer": "callTracer"}
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer
class TraceW3:
    def __init__(self, answer): self.provider = TraceProv(answer)
def frame(typ, frm, to, inp, error=None, calls=None):
    f = {"type": typ, "from": frm, "to": to, "input": inp}
    if error: f["error"] = error
    if calls: f["calls"] = calls
    return f
inner_ok = frame("CALL", SAFE, DESIG, "0x" + BASEA[2:])
valid = {"result": frame("CALL", H("9", 40), SAFE, "0x6a761202" + "00" * 8 + BASEA[2:], calls=[inner_ok])}
check("one successful CALL contract -> designation with the Base address in ITS input = bound", S.trace_designation(TraceW3(valid), H("ab"), SAFE, DESIG, BASEA) == [])
check("the Base address as a padded 32-byte word in the input = bound", S.trace_designation(TraceW3({"result": frame("CALL", H("9", 40), SAFE, "0x", calls=[frame("CALL", SAFE, DESIG, "0x" + "00" * 12 + BASEA[2:])])}), H("ab"), SAFE, DESIG, BASEA) == [])
caught = {"result": frame("CALL", H("9", 40), SAFE, "0x" + BASEA[2:], calls=[frame("CALL", SAFE, DESIG, "0x" + BASEA[2:], error="execution reverted")])}
check("a caught-revert subcall does NOT bind (the reviewer's isError='1' case)", len(S.trace_designation(TraceW3(caught), H("ab"), SAFE, DESIG, BASEA)) == 1)
wrong_inner = {"result": frame("CALL", H("9", 40), SAFE, "0x6a761202" + BASEA[2:], calls=[frame("CALL", SAFE, DESIG, "0x" + H("8c", 40)[2:])])}
check("Base address only in the OUTER calldata, another recipient in the real call = NOT bound", len(S.trace_designation(TraceW3(wrong_inner), H("ab"), SAFE, DESIG, BASEA)) == 1)
two = {"result": frame("CALL", H("9", 40), SAFE, "0x", calls=[inner_ok, dict(inner_ok)])}
check("two successful designation calls in one tx = ambiguous = NOT bound", len(S.trace_designation(TraceW3(two), H("ab"), SAFE, DESIG, BASEA)) == 1)
delegate = {"result": frame("CALL", H("9", 40), SAFE, "0x", calls=[frame("DELEGATECALL", SAFE, DESIG, "0x" + BASEA[2:])])}
check("a DELEGATECALL / STATICCALL frame does not count", len(S.trace_designation(TraceW3(delegate), H("ab"), SAFE, DESIG, BASEA)) == 1
      and len(S.trace_designation(TraceW3({"result": frame("CALL", H("9", 40), SAFE, "0x", calls=[frame("STATICCALL", SAFE, DESIG, "0x" + BASEA[2:])])}), H("ab"), SAFE, DESIG, BASEA)) == 1)
under_revert = {"result": frame("CALL", H("9", 40), SAFE, "0x", calls=[frame("CALL", SAFE, H("4e", 40), "0x", error="execution reverted", calls=[inner_ok])])}
check("a successful hit under a REVERTED ancestor does not count", len(S.trace_designation(TraceW3(under_revert), H("ab"), SAFE, DESIG, BASEA)) == 1)
check("no call to the designation address at all = NOT bound", len(S.trace_designation(TraceW3({"result": frame("CALL", H("9", 40), SAFE, "0x" + BASEA[2:])}), H("ab"), SAFE, DESIG, BASEA)) == 1)
check("tracer unavailable / error answer = NOT bound", len(S.trace_designation(TraceW3({"error": {"code": -32601, "message": "method not found"}}), H("ab"), SAFE, DESIG, BASEA)) == 1
      and len(S.trace_designation(TraceW3(RuntimeError("down")), H("ab"), SAFE, DESIG, BASEA)) == 1)
check("verify_designation without a tracing provider = error (fail-closed)", any("tracing provider" in e for e in S.verify_designation(
    type("W", (), {"eth": type("E", (), {"get_transaction": staticmethod(lambda h: {"input": "0x" + BASEA[2:], "from": H("9", 40), "to": SAFE}),
                                          "get_transaction_receipt": staticmethod(lambda h: {"status": 1, "blockNumber": 5})})()})(),
    FakeEs({"status": "1", "result": [{"from": SAFE, "to": DESIG}]}, 1), "eth", SAFE, {"tx": H("ab"), "base_address": S.cs(BASEA)}, 10, DESIG, None)))

print("[accepted unsolicited transfers (S07)]")
check("parse: one label, one tx", S.parse_acceptances([f"staking-T4:5:{TX1}"]) == {"staking-T4": {"wei": 5, "txs": [TX1]}})
check("parse: several txs, the bucket and the tunnel labels", set(S.parse_acceptances([f"morpheus-bucket:1:{TX1},{TX2}", f"bridge-tunnel:2:{TX3}"])) == {"morpheus-bucket", "bridge-tunnel"})
check("parse: none -> empty", S.parse_acceptances(None) == {} and S.parse_acceptances([]) == {})
check("parse: unknown label refused", expect_exit(S.parse_acceptances, [f"staking-T9:5:{TX1}"]))
check("parse: a label twice refused", expect_exit(S.parse_acceptances, [f"staking-T4:5:{TX1}", f"staking-T4:6:{TX2}"]))
check("parse: zero / negative / non-integer wei refused (a deficit is never accepted)", expect_exit(S.parse_acceptances, [f"staking-T4:0:{TX1}"]) and expect_exit(S.parse_acceptances, [f"staking-T4:-5:{TX1}"]) and expect_exit(S.parse_acceptances, [f"staking-T4:5.5:{TX1}"]))
check("parse: malformed / duplicate tx refused", expect_exit(S.parse_acceptances, ["staking-T4:5:0x12"]) and expect_exit(S.parse_acceptances, [f"staking-T4:5:{TX1},{TX1}"]) and expect_exit(S.parse_acceptances, ["staking-T4:5:"]))
check("parse: wrong shape refused", expect_exit(S.parse_acceptances, ["staking-T4:5"]) and expect_exit(S.parse_acceptances, [f"staking-T4:5:{TX1}:extra"]))
check("the label set = the 13 pins + the tunnel + the bucket", len(S.ACCEPT_LABELS) == 15)
TARGET, TOKEN, SENDER = S.cs(H("aa", 40)), S.cs(S.TPRO), S.cs(H("bb", 40))
def tlog(frm, to, val, addr=None):
    return {"address": addr or TOKEN, "topics": [S.TRANSFER_TOPIC, "0x" + "00" * 12 + frm[2:].lower(), "0x" + "00" * 12 + to[2:].lower()], "data": hex(val)}
class AccEth:
    def __init__(self, txs): self.txs = txs
    def get_transaction(self, h): return self.txs[h]["tx"]
    def get_transaction_receipt(self, h): return self.txs[h]["rc"]
class AccW3:
    def __init__(self, txs): self.eth = AccEth(txs)
def mk(to=TOKEN, status=1, block=50, logs=None):
    return {"tx": {"to": to, "from": SENDER}, "rc": {"status": status, "blockNumber": block, "logs": logs if logs is not None else [tlog(SENDER, TARGET, 5)]}}
w3a = AccW3({TX1: mk(), TX2: mk(logs=[tlog(SENDER, TARGET, 3)]), TX3: mk(logs=[tlog(SENDER, TARGET, 5), {"address": TARGET, "topics": ["0x" + "11" * 32], "data": "0x"}])})
e, rec = S.verify_explained(w3a, TOKEN, TARGET, 100, "staking-T4", {"wei": 5, "txs": [TX1]})
check("exact chain-verified surplus (one plain transfer) = accepted", e == [] and rec["verified"] and rec["txs"][0]["transfer_in_wei"] == "5")
e, rec = S.verify_explained(w3a, TOKEN, TARGET, 100, "staking-T4", {"wei": 8, "txs": [TX1, TX2]})
check("two transfers summing exactly = accepted", e == [] and rec["verified"])
e, _ = S.verify_explained(w3a, TOKEN, TARGET, 100, "staking-T4", {"wei": 6, "txs": [TX1]})
check("sum != accepted wei = refused (exact match only)", len(e) == 1 and "exact match" in e[0])
e, _ = S.verify_explained(w3a, TOKEN, TARGET, 100, "staking-T4", {"wei": 5, "txs": [TX3]})
check("a tx with an event emitted by the target = a normal flow = refused", len(e) == 1 and "emitted by the target" in e[0])
e, _ = S.verify_explained(AccW3({TX1: mk(to=TARGET)}), TOKEN, TARGET, 100, "staking-T4", {"wei": 5, "txs": [TX1]})
check("a direct call to the target = refused", len(e) == 1 and "direct call" in e[0])
e, _ = S.verify_explained(AccW3({TX1: mk(block=101)}), TOKEN, TARGET, 100, "staking-T4", {"wei": 5, "txs": [TX1]})
check("a tx mined after the snapshot block = refused", len(e) == 1 and "> snapshot block" in e[0])
e, _ = S.verify_explained(AccW3({TX1: mk(status=0)}), TOKEN, TARGET, 100, "staking-T4", {"wei": 5, "txs": [TX1]})
check("a failed tx = refused", len(e) == 1 and "failed" in e[0])
e, _ = S.verify_explained(AccW3({TX1: mk(logs=[tlog(SENDER, S.cs(H("cc", 40)), 5)])}), TOKEN, TARGET, 100, "staking-T4", {"wei": 5, "txs": [TX1]})
check("a tx without a Transfer INTO the target = refused", len(e) == 1 and "no TPRO Transfer" in e[0])
e, _ = S.verify_explained(AccW3({TX1: mk(logs=[tlog(SENDER, TARGET, 5, addr=S.cs(H("dd", 40)))])}), TOKEN, TARGET, 100, "staking-T4", {"wei": 5, "txs": [TX1]})
check("a Transfer of ANOTHER token into the target does not count", len(e) == 1)
e, _ = S.verify_explained(AccW3({}), TOKEN, TARGET, 100, "staking-T4", {"wei": 5, "txs": [TX1]})
check("an unknown tx = refused", len(e) == 1 and "not found" in e[0])
class ExplainEs:
    def logs_by_topics(self, token, cb, block, **topics):
        return [{"transactionHash": TX1, "topics": [S.TRANSFER_TOPIC, "0x" + "00" * 12 + SENDER[2:].lower(), topics["topic2"]], "data": hex(5), "blockNumber": hex(40)},
                {"transactionHash": TX2, "topics": [S.TRANSFER_TOPIC, "0x" + "00" * 12 + SENDER[2:].lower(), topics["topic2"]], "data": hex(9), "blockNumber": hex(41)}]
stray = CE.explain_inflows(ExplainEs(), TOKEN, TARGET, 1, 100, {TX2})
check("explain_inflows: transfers-in minus the target's own transactions = the unsolicited inflows", stray == [{"from": SENDER, "amount_wei": 5, "tx": TX1, "block": 40}])

print("[verdict plumbing (S01) + public provenance]")
d1 = os.path.join(tmp, "snap-final"); os.makedirs(d1)
moved = S.failed_dir(d1)
check("a failed --final output is renamed <out>-FAILED", moved == d1 + "-FAILED" and os.path.isdir(moved) and not os.path.exists(d1))
os.makedirs(d1)
moved2 = S.failed_dir(d1)
check("a second failure the same day gets a time suffix (nothing overwritten)", moved2.startswith(d1 + "-FAILED-") and os.path.isdir(moved2) and os.path.isdir(moved))
ref = os.path.join(tmp, "PUBLIC_REF")
open(ref, "w").write("0123456789abcdef0123456789abcdef01234567 fedcba9876543210fedcba9876543210fedcba98\n")
check("PUBLIC_REF maps the (short) private sha to the public commit URL", S.tool_ref("0123456", False, ref) == S.PUBLIC_REPO + "@fedcba9876543210fedcba9876543210fedcba98")
check("no mapping -> the private form in preview mode", S.tool_ref("abcdef0", False, ref) == "scripts/snapshot/snapshot.py@abcdef0")
check("no mapping -> --final REFUSES to run", expect_exit(S.tool_ref, "abcdef0", True, ref))
check("no PUBLIC_REF file at all -> --final REFUSES", expect_exit(S.tool_ref, "0123456", True, os.path.join(tmp, "nope")))
check("an unknown sha never maps", S.tool_ref("unknown", False, ref) == "scripts/snapshot/snapshot.py@unknown")

print("[compare_legacy.py requires the reference set (S09)]")
CK = os.path.join(HERE, "compare_legacy.py")
def run_ck(runs, census):
    r = subprocess.run([PY, CK, "--runs", runs, "--census", census], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr
cen = os.path.join(tmp, "census.csv")
open(cen, "w").write("label,contract,address,amount_wei,amount_tpro\nstaking-T2,0xC,0x" + "a" * 40 + ",5000000000000000000,5\n")
code, out = run_ck(os.path.join(tmp, "nonexistent"), cen)
check("--runs /nonexistent -> exit 1 (was: ALL MATCH, exit 0)", code == 1 and "not a directory" in out)
runs = os.path.join(tmp, "runs")
for t in ("T1", "T2", "T3", "T4", "T5"):
    os.makedirs(os.path.join(runs, t)); open(os.path.join(runs, t, "staking-1.db.csv"), "w").write("addr,amount,virtAmount\n")
code, out = run_ck(runs, cen)
check("T6 missing -> exit 1, names it", code == 1 and "T6" in out)
os.makedirs(os.path.join(runs, "T6")); open(os.path.join(runs, "T6", "staking-1.db.csv"), "w").write("addr,amount,virtAmount\n")
code, out = run_ck(runs, cen)
check("six dirs but the census address is not in the original dev team's set -> DIFFERENCES, exit 1", code == 1 and "DIFFERENCES" in out)
open(os.path.join(runs, "T2", "staking-1.db.csv"), "w").write("addr,amount,virtAmount\n0x" + "a" * 40 + ",5000000000000000000,0\n")
code, out = run_ck(runs, cen)
check("a real matching set -> ALL MATCH, exit 0, with the comparison count", code == 0 and "ALL MATCH" in out and "compared 1 address" in out)
open(cen, "w").write("label,contract,address,amount_wei,amount_tpro\nvesting-1,0xC,0x" + "a" * 40 + ",5,5\n")
code, out = run_ck(runs, cen)
check("a census without staking rows -> exit 1", code == 1 and "no staking rows" in out)
open(cen, "w").write("label,contract,address,amount_wei,amount_tpro\nstaking-T2,0xC,0x" + "a" * 40 + ",5000000000000000000,5\n")
code, out = run_ck(runs, os.path.join(tmp, "missing.csv"))
check("a missing census file -> exit 1", code == 1)

print("[caches]")
p = os.path.join(tmp, "eth-transfers-1.json")
json.dump({"token": S.TPRO, "from_block": 1, "to_block": 2, "logs": []}, open(p, "w"))            # old format, no marker
check("cache without completion marker refused", expect_exit(S.fetch_transfers, None, "eth", S.TPRO, 1, 2, p))
json.dump({"chain": "polygon", "token": S.TPRO, "from_block": 1, "to_block": 2, "complete": True, "logs": []}, open(p, "w"))
check("cache for another chain refused", expect_exit(S.fetch_transfers, None, "eth", S.TPRO, 1, 2, p))
json.dump({"chain": "eth", "token": S.TPRO, "from_block": 1, "to_block": 2, "complete": True, "logs": [{"x": 1}]}, open(p, "w"))
check("complete cache accepted", S.fetch_transfers(None, "eth", S.TPRO, 1, 2, p) == [{"x": 1}])
q = os.path.join(tmp, "eth-codes-1.json")
json.dump({"0xabc": "0x"}, open(q, "w"))                                                                    # old format
check("code cache without header refused", expect_exit(S.get_codes, None, "eth", ["0xabc"], 1, q))
json.dump({"chain": "eth", "block": 2, "codes": {"0xabc": "0x"}}, open(q, "w"))
check("code cache for another block refused", expect_exit(S.get_codes, None, "eth", ["0xabc"], 1, q))
json.dump({"chain": "eth", "block": 1, "codes": {"0xabc": "0x"}}, open(q, "w"))
check("matching code cache accepted", S.get_codes(None, "eth", ["0xabc"], 1, q) == {"0xabc": "0x"})
check("an empty address list needs no provider", S.get_codes(None, "eth", [], 1, os.path.join(tmp, "eth-codes-none.json")) == {})

print("[fail-closed defaults]")
class W3Bad:
    class eth:
        @staticmethod
        def get_block(tag): raise RuntimeError("no finalized tag")
check("missing 'finalized' tag aborts in preview mode too", expect_exit(S.finalized_number, W3Bad(), "eth", [], False))
check("the env fallback outside the repo is gone (public-repo scrub)", "tpro-" "investors" not in open(os.path.join(HERE, "snapshot.py")).read() and "tpro-" "investors" not in open(os.path.join(HERE, "census.py")).read())

print("[constants]")
check("four distinct MEXC custody addresses, checksummed", len({S.cs(a) for a in S.MEXC_CUSTODY}) == 4 and all(a == S.cs(a) for a in S.MEXC_CUSTODY))
check("reserve constants sum to 222,222 TPRO minus 2 wei", sum(v for k, v in S.RESERVE_WEI.items() if k.startswith("staking")) == 222222 * 10**18 - 2)
check("four pinned Polygon stakings, checksummed", len(S.POLYGON_STAKING) == 4 and all(a == S.cs(a) for a in S.POLYGON_STAKING.values()))
check("community tranche 25M", S.COMMUNITY_TRANCHE_WEI == 25_000_000 * 10**18)

import shutil; shutil.rmtree(tmp)
print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
