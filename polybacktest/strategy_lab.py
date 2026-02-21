#!/usr/bin/env python3
"""
STRATEGY LAB v2 — Every strategy gets variants + combo tests
30 days, 5m + 15m, Chainlink prices, $50 fixed bets

PART 1: Individual strategy variants (find best version of each)
PART 2: Combo tests (best variants combined into full bot configs)
"""

import requests, json, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

API_KEY = "pdm_K3hqRH80z3B2mRfcMij5HnLR3CoooweM"
BASE_URL = "https://api.polybacktest.com"
HEADERS = {"X-API-Key": API_KEY}
BACKTEST_DAYS = 30
BET_SIZE = 50.0

def _fp(val):
    try: return float(val) if val else 0
    except: return 0

class API:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._c = 0
    def get(self, path, params=None):
        self._c += 1
        if self._c % 80 == 0: time.sleep(0.3)
        try:
            r = self.s.get(f"{BASE_URL}{path}", params=params, timeout=30)
            if r.status_code == 429: time.sleep(5); return self.get(path, params)
            r.raise_for_status(); return r.json()
        except: return None
    def get_markets(self, mtype, resolved=True, limit=100, offset=0):
        return self.get("/v1/markets", {"market_type": mtype, "resolved": resolved, "limit": limit, "offset": offset})
    def get_snapshots(self, mid, limit=1000):
        return self.get(f"/v1/markets/{mid}/snapshots", {"limit": limit})

class BTC:
    def __init__(self): self.prices = []
    def update(self, ts, p):
        p = _fp(p)
        if p > 1000:
            self.prices.append((ts, p))
            self.prices = [(t, px) for t, px in self.prices if t > ts - 900]
    def chg(self, sec):
        if len(self.prices) < 2: return 0
        now = self.prices[-1]; tgt = now[0] - sec
        best = min(self.prices, key=lambda x: abs(x[0] - tgt))
        return (now[1] - best[1]) / best[1] if best[1] else 0
    def chg_from(self, op):
        if not self.prices or op <= 0: return 0
        return (self.prices[-1][1] - op) / op
    @property
    def price(self): return self.prices[-1][1] if self.prices else 0

def get_book(snap, side):
    bk = snap.get("orderbook_up" if side == "YES" else "orderbook_down")
    if not bk: return [], [], 0, 0
    bids = bk.get("bids", []); asks = bk.get("asks", [])
    bv = sum(_fp(b.get("size", 0)) for b in bids[:5])
    av = sum(_fp(a.get("size", 0)) for a in asks[:5])
    return bids, asks, bv, av

def selling_pressure(snap, side):
    _, _, bv, av = get_book(snap, side)
    return av > bv * 2.0 if av > 0 else False

def would_fill(snap, side, price):
    bk = snap.get("orderbook_up" if side == "YES" else "orderbook_down")
    if not bk: return True
    asks = bk.get("asks", []); bids = bk.get("bids", [])
    if not asks or not bids: return True
    spread = _fp(asks[0].get("price", 0)) - _fp(bids[0].get("price", 0))
    if spread <= 0.03: return True
    if price >= _fp(asks[0].get("price", 0)) - 0.02: return True
    return sum(_fp(a.get("size", 0)) for a in asks[:3]) > 100


# ═══════════════════════════════════════════════════════════════
#  LATENCY VARIANTS (4)
# ═══════════════════════════════════════════════════════════════
def _lat_core(snap, btc, tl, mtype, open_btc, min_chg, min_price, max_price, min_edge):
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0: return None
    if tl < (120 if mtype == "5m" else 240): return None
    c30 = btc.chg(30); c60 = btc.chg(60)
    c_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    chg = max(c_open, c30, c60, key=abs)
    if abs(chg) < min_chg: return None
    up = chg > 0
    if c_open != 0 and ((up and c_open < 0) or (not up and c_open > 0)): return None
    tp = yes_p if up else no_p
    if tp > max_price or tp < min_price: return None
    other = no_p if up else yes_p
    if other < 0.10: return None
    conf = min(0.95, 0.60 + abs(chg) * 100)
    if conf - tp < min_edge: return None
    return {"side": "YES" if up else "NO", "price": tp}

def lat1(s,b,t,m,o): r = _lat_core(s,b,t,m,o, 0.0005, 0.15, 0.40, 0.15); return {**r, "strategy":"LAT1_current"} if r else None
def lat2(s,b,t,m,o): r = _lat_core(s,b,t,m,o, 0.0005, 0.15, 0.55, 0.10); return {**r, "strategy":"LAT2_wider"} if r else None
def lat3(s,b,t,m,o): r = _lat_core(s,b,t,m,o, 0.0005, 0.35, 0.55, 0.05); return {**r, "strategy":"LAT3_mid"} if r else None
def lat4(s,b,t,m,o): r = _lat_core(s,b,t,m,o, 0.0008, 0.15, 0.50, 0.10); return {**r, "strategy":"LAT4_strong"} if r else None


# ═══════════════════════════════════════════════════════════════
#  FLASH VARIANTS (4) — based on Flash Lab winners
# ═══════════════════════════════════════════════════════════════
def _flash_core(snap, btc, tl, age, mtype, open_btc, min_p, max_p, need_open, only_5m):
    if only_5m and mtype != "5m": return None
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0: return None
    if tl < (120 if mtype == "5m" else 240): return None
    if age < (60 if mtype == "5m" else 120): return None
    c_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    c2m = btc.chg(120)
    if need_open and abs(c_open) < 0.0003: return None
    up_dir = c_open > 0.0003 if need_open else c2m > 0.0002
    dn_dir = c_open < -0.0003 if need_open else c2m < -0.0002
    if up_dir and min_p <= yes_p <= max_p:
        if not selling_pressure(snap, "YES"):
            return {"side": "YES", "price": yes_p}
    if dn_dir and min_p <= no_p <= max_p:
        if not selling_pressure(snap, "NO"):
            return {"side": "NO", "price": no_p}
    return None

def fla1(s,b,t,a,m,o): r = _flash_core(s,b,t,a,m,o, 0.15, 0.30, False, False); return {**r,"strategy":"FLA1_cheap"} if r else None
def fla2(s,b,t,a,m,o): r = _flash_core(s,b,t,a,m,o, 0.38, 0.55, True, True); return {**r,"strategy":"FLA2_5m_mid"} if r else None
def fla3(s,b,t,a,m,o): r = _flash_core(s,b,t,a,m,o, 0.38, 0.55, True, False); return {**r,"strategy":"FLA3_all_mid"} if r else None
def fla4(s,b,t,a,m,o): r = _flash_core(s,b,t,a,m,o, 0.30, 0.55, True, True); return {**r,"strategy":"FLA4_5m_wide"} if r else None


# ═══════════════════════════════════════════════════════════════
#  SNIPE VARIANTS (4)
# ═══════════════════════════════════════════════════════════════
def _snipe_core(snap, btc, tl, mtype, open_btc, max_tl, min_p, max_p, allow_15m):
    if not allow_15m and mtype != "5m": return None
    if tl > max_tl or tl < 5: return None
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0: return None
    c2m = btc.chg(120)
    c_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    if c2m > 0.0003 and c_open > 0.0001 and min_p <= yes_p <= max_p:
        return {"side": "YES", "price": yes_p}
    if c2m < -0.0003 and c_open < -0.0001 and min_p <= no_p <= max_p:
        return {"side": "NO", "price": no_p}
    return None

def snp1(s,b,t,m,o): r = _snipe_core(s,b,t,m,o, 45, 0.82, 0.94, False); return {**r,"strategy":"SNP1_current"} if r else None
def snp2(s,b,t,m,o): r = _snipe_core(s,b,t,m,o, 90, 0.75, 0.94, False); return {**r,"strategy":"SNP2_wider"} if r else None
def snp3(s,b,t,m,o): r = _snipe_core(s,b,t,m,o, 60, 0.80, 0.94, True); return {**r,"strategy":"SNP3_both"} if r else None
def snp4(s,b,t,m,o): r = _snipe_core(s,b,t,m,o, 45, 0.70, 0.90, False); return {**r,"strategy":"SNP4_cheaper"} if r else None


# ═══════════════════════════════════════════════════════════════
#  MEANREV VARIANTS (4)
# ═══════════════════════════════════════════════════════════════
def _mr_core(snap, btc, tl, mtype, min_p, max_p, other_min, chg30_thresh, chg2m_thresh):
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0: return None
    if tl < (90 if mtype == "5m" else 180): return None
    c30 = btc.chg(30); c2m = btc.chg(120)
    if min_p <= yes_p <= max_p and no_p >= other_min:
        if c30 > chg30_thresh and c2m < -chg2m_thresh:
            return {"side": "YES", "price": yes_p}
    if min_p <= no_p <= max_p and yes_p >= other_min:
        if c30 < -chg30_thresh and c2m > chg2m_thresh:
            return {"side": "NO", "price": no_p}
    return None

def mr1(s,b,t,m,o): r = _mr_core(s,b,t,m, 0.30, 0.45, 0.55, 0.0002, 0.0003); return {**r,"strategy":"MR1_current"} if r else None
def mr2(s,b,t,m,o): r = _mr_core(s,b,t,m, 0.25, 0.50, 0.50, 0.0002, 0.0003); return {**r,"strategy":"MR2_wider"} if r else None
def mr3(s,b,t,m,o): r = _mr_core(s,b,t,m, 0.30, 0.50, 0.50, 0.0005, 0.0008); return {**r,"strategy":"MR3_strong"} if r else None
def mr4(s,b,t,m,o): r = _mr_core(s,b,t,m, 0.35, 0.48, 0.52, 0.0003, 0.0005); return {**r,"strategy":"MR4_tight"} if r else None


# ═══════════════════════════════════════════════════════════════
#  ARB VARIANTS (4)
# ═══════════════════════════════════════════════════════════════
def _arb_core(snap, threshold):
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0: return None
    if yes_p + no_p >= threshold: return None
    return {"side": "YES" if yes_p <= no_p else "NO", "price": min(yes_p, no_p)}

def arb1(s,b,t,m,o): r = _arb_core(s, 0.97); return {**r,"strategy":"ARB1_97"} if r else None
def arb2(s,b,t,m,o): r = _arb_core(s, 0.95); return {**r,"strategy":"ARB2_95"} if r else None
def arb3(s,b,t,m,o): r = _arb_core(s, 0.99); return {**r,"strategy":"ARB3_99"} if r else None
def arb4(s,b,t,m,o): r = _arb_core(s, 0.93); return {**r,"strategy":"ARB4_93"} if r else None


# ═══════════════════════════════════════════════════════════════
#  SPIKE VARIANTS (3) — stateful (needs per-market tracker)
# ═══════════════════════════════════════════════════════════════
class _SpikeBase:
    def __init__(self, mult, min_vol, min_p, max_p, name):
        self.py = 0; self.pn = 0
        self.mult = mult; self.min_vol = min_vol
        self.min_p = min_p; self.max_p = max_p; self.name = name
    def check(self, snap, btc, tl, mtype):
        yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
        if yes_p <= 0 or no_p <= 0: return None
        if tl < (120 if mtype == "5m" else 240): return None
        _, _, _, ya = get_book(snap, "YES")
        _, _, _, na = get_book(snap, "NO")
        result = None
        if self.py > 10 and ya > self.py * self.mult and ya > self.min_vol:
            if self.min_p <= yes_p <= self.max_p and btc.chg(60) >= -0.003:
                result = {"side": "YES", "price": yes_p, "strategy": self.name}
        if result is None and self.pn > 10 and na > self.pn * self.mult and na > self.min_vol:
            if self.min_p <= no_p <= self.max_p and btc.chg(60) <= 0.003:
                result = {"side": "NO", "price": no_p, "strategy": self.name}
        self.py = ya; self.pn = na
        return result

SPIKE_DEFS = {
    "SPK1_current": (3.0, 100, 0.10, 0.28),
    "SPK2_wider":   (2.0, 50,  0.10, 0.45),
    "SPK3_strict":  (4.0, 150, 0.10, 0.25),
}


# ═══════════════════════════════════════════════════════════════
#  COMBO STRATEGIES (test full bot configurations)
# ═══════════════════════════════════════════════════════════════
# Each combo is a list of (name, function, type) — first match wins per market
COMBOS = {
    "COMBO1_current_bot": [
        ("ARB", arb1, "arb"), ("LAT", lat1, "lat"), ("MR", mr1, "mr"),
        ("FLA", fla1, "flash"), ("SNP", snp1, "snipe"),
    ],
    "COMBO2_optimized": [
        ("ARB", arb1, "arb"), ("LAT", lat3, "lat"), ("MR", mr2, "mr"),
        ("FLA", fla2, "flash"), ("SNP", snp2, "snipe"),
    ],
    "COMBO3_aggressive": [
        ("ARB", arb3, "arb"), ("LAT", lat2, "lat"), ("MR", mr2, "mr"),
        ("FLA", fla3, "flash"), ("SNP", snp3, "snipe"),
    ],
    "COMBO4_conservative": [
        ("ARB", arb2, "arb"), ("LAT", lat4, "lat"), ("MR", mr3, "mr"),
        ("FLA", fla2, "flash"), ("SNP", snp1, "snipe"),
    ],
    "COMBO5_no_flash": [
        ("ARB", arb1, "arb"), ("LAT", lat3, "lat"), ("MR", mr2, "mr"),
        ("SNP", snp2, "snipe"),
    ],
    "COMBO6_arb_lat_only": [
        ("ARB", arb1, "arb"), ("LAT", lat3, "lat"),
    ],
}


# ═══════════════════════════════════════════════════════════════
#  VARIANT REGISTRY
# ═══════════════════════════════════════════════════════════════
INDIVIDUAL = {
    # Latency
    "LAT1_current": ("lat", lat1), "LAT2_wider": ("lat", lat2),
    "LAT3_mid": ("lat", lat3), "LAT4_strong": ("lat", lat4),
    # Flash
    "FLA1_cheap": ("flash", fla1), "FLA2_5m_mid": ("flash", fla2),
    "FLA3_all_mid": ("flash", fla3), "FLA4_5m_wide": ("flash", fla4),
    # Snipe
    "SNP1_current": ("snipe", snp1), "SNP2_wider": ("snipe", snp2),
    "SNP3_both": ("snipe", snp3), "SNP4_cheaper": ("snipe", snp4),
    # MeanRev
    "MR1_current": ("mr", mr1), "MR2_wider": ("mr", mr2),
    "MR3_strong": ("mr", mr3), "MR4_tight": ("mr", mr4),
    # ARB
    "ARB1_97": ("arb", arb1), "ARB2_95": ("arb", arb2),
    "ARB3_99": ("arb", arb3), "ARB4_93": ("arb", arb4),
}


def run():
    api = API()

    # Init results for individual variants
    results = {}
    for n in INDIVIDUAL:
        results[n] = {"wins": 0, "losses": 0, "pnl": 0.0, "wagered": 0, "trades": []}
    for n in SPIKE_DEFS:
        results[n] = {"wins": 0, "losses": 0, "pnl": 0.0, "wagered": 0, "trades": []}

    # Init results for combos
    combo_results = {}
    for cn in COMBOS:
        combo_results[cn] = {"wins": 0, "losses": 0, "pnl": 0.0, "wagered": 0, "trades": []}

    total_names = len(INDIVIDUAL) + len(SPIKE_DEFS) + len(COMBOS)
    print("+" + "=" * 68 + "+")
    print("|  STRATEGY LAB v2 — ALL VARIANTS + COMBOS — 30 DAYS              |")
    print("+" + "=" * 68 + "+\n")
    print(f"  Period:      {BACKTEST_DAYS} days | ${BET_SIZE:.0f} bets")
    print(f"  Individual:  {len(INDIVIDUAL) + len(SPIKE_DEFS)} variants")
    print(f"    LATENCY:   4 variants")
    print(f"    FLASH:     4 variants")
    print(f"    SNIPE:     4 variants")
    print(f"    MEANREV:   4 variants")
    print(f"    ARB:       4 variants")
    print(f"    SPIKE:     3 variants")
    print(f"  Combos:      {len(COMBOS)} full bot configs")
    print(f"  Total:       {total_names} tests\n")

    total_mkts = 0
    for mtype in ["5m", "15m"]:
        print(f"{'=' * 60}")
        print(f"  Loading {mtype}...")
        print(f"{'=' * 60}")

        offset = 0; markets = []
        while True:
            d = api.get_markets(mtype, resolved=True, limit=100, offset=offset)
            if not d or not d.get("markets"): break
            markets.extend(d["markets"])
            if len(markets) % 500 < 100:
                print(f"    {len(markets)} fetched...")
            if len(d["markets"]) < 100: break
            offset += 100
            if len(markets) > 15000: break

        cutoff = datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS)
        btc_mkts = []
        for m in markets:
            if "btc" not in m.get("slug", "").lower(): continue
            try:
                et = datetime.fromisoformat(m["end_time"].replace("Z", "+00:00"))
                if et < cutoff: continue
            except: continue
            if not m.get("winner"): continue
            btc_mkts.append(m)

        print(f"  -> {len(btc_mkts)} BTC markets\n")
        total_mkts += len(btc_mkts)

        for i, mkt in enumerate(btc_mkts):
            if i % 100 == 0:
                tt = sum(r["wins"] + r["losses"] for r in results.values())
                print(f"  [{mtype}] {i}/{len(btc_mkts)} | {tt} individual trades so far")

            snaps = api.get_snapshots(mkt["market_id"], limit=1000)
            if not snaps or not snaps.get("snapshots"): continue

            try:
                et = datetime.fromisoformat(mkt["end_time"].replace("Z", "+00:00"))
                st = datetime.fromisoformat(mkt["start_time"].replace("Z", "+00:00"))
                dur = (et - st).total_seconds()
            except: continue

            open_btc = _fp(mkt.get("btc_price_start"))
            if open_btc <= 0 and snaps["snapshots"]:
                open_btc = _fp(snaps["snapshots"][0].get("btc_price"))
            winner = mkt.get("winner", "")
            if not winner: continue

            btc = BTC()
            traded_ind = {n: False for n in results}
            traded_combo = {cn: False for cn in COMBOS}

            # Fresh spike trackers
            spikes = {}
            for sn, (mult, minv, minp, maxp) in SPIKE_DEFS.items():
                spikes[sn] = _SpikeBase(mult, minv, minp, maxp, sn)

            for snap in snaps["snapshots"]:
                try:
                    snap_dt = datetime.fromisoformat(snap["time"].replace("Z", "+00:00"))
                    tl = (et - snap_dt).total_seconds()
                    age = dur - tl
                    ts = snap_dt.timestamp()
                except: continue

                bp = snap.get("btc_price")
                if bp: btc.update(ts, bp)

                # ── INDIVIDUAL VARIANTS ──
                for name, (vtype, fn) in INDIVIDUAL.items():
                    if traded_ind[name]: continue
                    if vtype == "flash":
                        sig = fn(snap, btc, tl, age, mtype, open_btc)
                    else:
                        sig = fn(snap, btc, tl, mtype, open_btc)
                    if not sig: continue
                    if not would_fill(snap, sig["side"], sig["price"]): continue
                    traded_ind[name] = True
                    _record(results, name, sig, winner)

                # ── SPIKE VARIANTS ──
                for sn, tracker in spikes.items():
                    if traded_ind[sn]: continue
                    sig = tracker.check(snap, btc, tl, mtype)
                    if not sig: continue
                    if not would_fill(snap, sig["side"], sig["price"]): continue
                    traded_ind[sn] = True
                    _record(results, sn, sig, winner)

                # ── COMBO TESTS ──
                for cn, strat_list in COMBOS.items():
                    if traded_combo[cn]: continue
                    for sname, fn, stype in strat_list:
                        if stype == "flash":
                            sig = fn(snap, btc, tl, age, mtype, open_btc)
                        else:
                            sig = fn(snap, btc, tl, mtype, open_btc)
                        if not sig: continue
                        if not would_fill(snap, sig["side"], sig["price"]): continue
                        traded_combo[cn] = True
                        sig["strategy"] = cn
                        _record(combo_results, cn, sig, winner)
                        break

    # ═══ PRINT RESULTS ═══
    _print_results(results, combo_results, total_mkts, api._c)


def _record(store, name, sig, winner):
    side = sig["side"]; price = sig["price"]
    won = ((side == "YES" and winner.upper() in ("UP", "YES")) or
           (side == "NO" and winner.upper() in ("DOWN", "NO")))
    if won:
        profit = (BET_SIZE / price) - BET_SIZE
        store[name]["wins"] += 1
        store[name]["pnl"] += profit
    else:
        store[name]["losses"] += 1
        store[name]["pnl"] -= BET_SIZE
    store[name]["wagered"] += BET_SIZE
    store[name]["trades"].append({
        "side": side, "price": price, "result": "WIN" if won else "LOSS",
        "pnl": profit if won else -BET_SIZE
    })


def _print_results(results, combo_results, total_mkts, api_calls):
    print(f"\n\n{'=' * 75}")
    print(f"  STRATEGY LAB RESULTS — {total_mkts} MARKETS × {BACKTEST_DAYS} DAYS")
    print(f"{'=' * 75}")

    # ── PART 1: INDIVIDUAL ──
    print(f"\n  PART 1: INDIVIDUAL STRATEGY VARIANTS")
    print(f"  {'─' * 70}")

    for group, prefix, label in [
        ("lat", "LAT", "LATENCY"), ("flash", "FLA", "FLASH"),
        ("snipe", "SNP", "SNIPE"), ("mr", "MR", "MEANREV"),
        ("arb", "ARB", "ARB"), ("spk", "SPK", "SPIKE")
    ]:
        names = sorted([n for n in results if n.startswith(prefix)],
                       key=lambda n: results[n]["pnl"], reverse=True)
        if not names: continue

        print(f"\n  {label}:")
        print(f"  {'Variant':<20} {'Trades':>6} {'W':>5} {'L':>5} {'WR':>6} {'P&L':>10} {'ROI':>7} {'$/Trd':>7}")
        print(f"  {'─' * 68}")
        for n in names:
            r = results[n]; t = r["wins"] + r["losses"]
            wr = r["wins"] / t * 100 if t else 0
            roi = r["pnl"] / r["wagered"] * 100 if r["wagered"] else 0
            pt = r["pnl"] / t if t else 0
            m = " >>>" if n == names[0] and r["pnl"] > 0 else ""
            print(f"  {n:<20} {t:>6} {r['wins']:>5} {r['losses']:>5} {wr:>5.1f}% ${r['pnl']:>+9.0f} {roi:>+6.1f}% ${pt:>+6.2f}{m}")

    # ── PART 2: COMBOS ──
    print(f"\n\n  PART 2: COMBO CONFIGURATIONS (full bot)")
    print(f"  {'─' * 70}")

    combo_sorted = sorted(combo_results.keys(), key=lambda n: combo_results[n]["pnl"], reverse=True)
    print(f"\n  {'Config':<25} {'Trades':>6} {'W':>5} {'L':>5} {'WR':>6} {'P&L':>10} {'ROI':>7} {'$/Trd':>7}")
    print(f"  {'─' * 72}")
    for cn in combo_sorted:
        r = combo_results[cn]; t = r["wins"] + r["losses"]
        wr = r["wins"] / t * 100 if t else 0
        roi = r["pnl"] / r["wagered"] * 100 if r["wagered"] else 0
        pt = r["pnl"] / t if t else 0
        m = " <<<BEST" if cn == combo_sorted[0] else ""
        print(f"  {cn:<25} {t:>6} {r['wins']:>5} {r['losses']:>5} {wr:>5.1f}% ${r['pnl']:>+9.0f} {roi:>+6.1f}% ${pt:>+6.2f}{m}")

    # Combo descriptions
    print(f"\n  COMBO DETAILS:")
    for cn in combo_sorted:
        strats = [s[0] for s in COMBOS[cn]]
        print(f"    {cn}: {' + '.join(strats)}")

    # ── PART 3: BEST OF EACH + RECOMMENDED BUILD ──
    print(f"\n\n{'=' * 75}")
    print(f"  RECOMMENDED BOT BUILD")
    print(f"{'=' * 75}")

    best_per_group = {}
    for group, prefix in [("LATENCY","LAT"), ("FLASH","FLA"), ("SNIPE","SNP"),
                           ("MEANREV","MR"), ("ARB","ARB"), ("SPIKE","SPK")]:
        names = [n for n in results if n.startswith(prefix)]
        if names:
            best = max(names, key=lambda n: results[n]["pnl"])
            r = results[best]; t = r["wins"] + r["losses"]
            wr = r["wins"] / t * 100 if t else 0
            profitable = r["pnl"] > 0
            best_per_group[group] = (best, r, profitable)
            status = "DEPLOY" if profitable else "DISABLE"
            print(f"\n  {group}:")
            print(f"    Best: {best} → {t}t {wr:.1f}%WR ${r['pnl']:+.0f} → {status}")

    print(f"\n  OPTIMAL BOT = ", end="")
    deploy = [g for g, (n, r, p) in best_per_group.items() if p]
    print(" + ".join(deploy) if deploy else "Nothing profitable!")

    # Save everything
    save = {
        "config": {"days": BACKTEST_DAYS, "bet": BET_SIZE, "markets": total_mkts},
        "individual": {},
        "combos": {},
        "recommended": {}
    }
    for n in results:
        r = results[n]; t = r["wins"] + r["losses"]
        save["individual"][n] = {
            "trades": t, "wins": r["wins"], "losses": r["losses"],
            "wr": r["wins"] / t * 100 if t else 0,
            "pnl": r["pnl"], "trade_list": r["trades"][:20]
        }
    for cn in combo_results:
        r = combo_results[cn]; t = r["wins"] + r["losses"]
        save["combos"][cn] = {
            "trades": t, "wins": r["wins"], "losses": r["losses"],
            "wr": r["wins"] / t * 100 if t else 0,
            "pnl": r["pnl"], "trade_list": r["trades"][:20]
        }
    for g, (n, r, p) in best_per_group.items():
        save["recommended"][g] = {"variant": n, "pnl": r["pnl"], "deploy": p}

    with open("strategy_lab_results.json", "w") as f:
        json.dump(save, f, indent=2, default=str)
    print(f"\n\n  Saved: strategy_lab_results.json")
    print(f"  API calls: {api_calls}")
    print(f"{'=' * 75}")


if __name__ == "__main__":
    run()
