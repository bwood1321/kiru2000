#!/usr/bin/env python3
"""
FLASH STRATEGY LAB v2 — FULL ACCESS (30 DAYS)
Tests 12 Flash variants head-to-head on 30 days of real market data.
Uses Chainlink prices (settlement source) from PolyBackTest snapshots.

Usage: python3 flash_lab.py
"""

import requests, json, time, sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

API_KEY = "pdm_K3hqRH80z3B2mRfcMij5HnLR3CoooweM"
BASE_URL = "https://api.polybacktest.com"
HEADERS = {"X-API-Key": API_KEY}
BACKTEST_DAYS = 30
BET_SIZE = 50.0  # fixed $50 for fair comparison

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
        except Exception as e:
            if self._c % 200 == 0: print(f"    API err: {e}")
            return None
    def get_markets(self, market_type, resolved=True, limit=100, offset=0):
        return self.get("/v1/markets", {"market_type": market_type, "resolved": resolved, "limit": limit, "offset": offset})
    def get_snapshots(self, market_id, limit=1000):
        return self.get(f"/v1/markets/{market_id}/snapshots", {"limit": limit})

class BTC:
    def __init__(self): self.prices = []
    def update(self, ts, p):
        try: p = float(p)
        except: return
        if p > 0:
            self.prices.append((ts, p))
            self.prices = [(t, px) for t, px in self.prices if t > ts - 900]
    def chg(self, sec):
        if len(self.prices) < 2: return 0
        now = self.prices[-1]; tgt = now[0] - sec
        best = min(self.prices, key=lambda x: abs(x[0] - tgt))
        return (now[1] - best[1]) / best[1] if best[1] else 0
    def chg_from(self, open_p):
        if not self.prices or open_p <= 0: return 0
        return (self.prices[-1][1] - open_p) / open_p
    @property
    def price(self): return self.prices[-1][1] if self.prices else 0

def book_support(snap, side):
    bk = snap.get("orderbook_up" if side == "YES" else "orderbook_down")
    if not bk: return True
    bids = bk.get("bids", []); asks = bk.get("asks", [])
    if not bids or not asks: return True
    bv = sum(_fp(b.get("size", 0)) for b in bids[:5])
    av = sum(_fp(a.get("size", 0)) for a in asks[:5])
    return bv > av * 0.5

def book_strong(snap, side):
    """Stricter book check — bids must EXCEED asks."""
    bk = snap.get("orderbook_up" if side == "YES" else "orderbook_down")
    if not bk: return False
    bids = bk.get("bids", []); asks = bk.get("asks", [])
    if not bids or not asks: return False
    bv = sum(_fp(b.get("size", 0)) for b in bids[:5])
    av = sum(_fp(a.get("size", 0)) for a in asks[:5])
    return bv > av * 1.2

# ═══════════════════════════════════════════════════════════
# 12 FLASH VARIANTS
# ═══════════════════════════════════════════════════════════

# --- GROUP A: PRICE RANGE TESTS (same direction logic, different entries) ---

def flash_A1_cheap(snap, btc, tl, age, mtype, open_btc):
    """A1: CHEAP $0.15-$0.30 (current Flash - known bad)"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120)
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and btc_2m > 0 and 0.15 <= yes_p <= 0.30:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < 0 and 0.15 <= no_p <= 0.30:
        return {"side": "NO", "price": no_p}
    return None

def flash_A2_low_mid(snap, btc, tl, age, mtype, open_btc):
    """A2: LOW-MID $0.30-$0.45"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120)
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and btc_2m > 0 and 0.30 <= yes_p <= 0.45:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < 0 and 0.30 <= no_p <= 0.45:
        return {"side": "NO", "price": no_p}
    return None

def flash_A3_mid(snap, btc, tl, age, mtype, open_btc):
    """A3: MID $0.38-$0.55"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120)
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and btc_2m > 0 and 0.38 <= yes_p <= 0.55:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < 0 and 0.38 <= no_p <= 0.55:
        return {"side": "NO", "price": no_p}
    return None

def flash_A4_high_mid(snap, btc, tl, age, mtype, open_btc):
    """A4: HIGH-MID $0.45-$0.58"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120)
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and btc_2m > 0 and 0.45 <= yes_p <= 0.58:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < 0 and 0.45 <= no_p <= 0.58:
        return {"side": "NO", "price": no_p}
    return None

# --- GROUP B: DIRECTION LOGIC TESTS (same $0.38-$0.55 range, different triggers) ---

def flash_B1_open_only(snap, btc, tl, age, mtype, open_btc):
    """B1: Open direction ONLY (simplest possible)"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and 0.38 <= yes_p <= 0.55:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and 0.38 <= no_p <= 0.55:
        return {"side": "NO", "price": no_p}
    return None

def flash_B2_strong_open(snap, btc, tl, age, mtype, open_btc):
    """B2: Strong open move (0.05%+) — higher threshold"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    if abs(chg_open) < 0.0005: return None  # 0.05% minimum
    if chg_open > 0.0005 and 0.38 <= yes_p <= 0.55:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0005 and 0.38 <= no_p <= 0.55:
        return {"side": "NO", "price": no_p}
    return None

def flash_B3_momentum(snap, btc, tl, age, mtype, open_btc):
    """B3: Full momentum — open + 2m + 1m + 30s ALL agree"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 90: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120); btc_1m = btc.chg(60); btc_30s = btc.chg(30)
    if chg_open > 0.0003 and btc_2m > 0 and btc_1m > 0 and btc_30s > 0:
        if 0.38 <= yes_p <= 0.55:
            return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < 0 and btc_1m < 0 and btc_30s < 0:
        if 0.38 <= no_p <= 0.55:
            return {"side": "NO", "price": no_p}
    return None

def flash_B4_open_plus_2m(snap, btc, tl, age, mtype, open_btc):
    """B4: Open direction + 2m agrees (balanced)"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120)
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and btc_2m > 0.0001 and 0.38 <= yes_p <= 0.55:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < -0.0001 and 0.38 <= no_p <= 0.55:
        return {"side": "NO", "price": no_p}
    return None

# --- GROUP C: FILTER/TIMING TESTS (same mid range + open direction, different filters) ---

def flash_C1_book_filter(snap, btc, tl, age, mtype, open_btc):
    """C1: Mid range + open direction + STRONG book support"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120)
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and btc_2m > 0 and 0.38 <= yes_p <= 0.55:
        if book_strong(snap, "YES"):
            return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < 0 and 0.38 <= no_p <= 0.55:
        if book_strong(snap, "NO"):
            return {"side": "NO", "price": no_p}
    return None

def flash_C2_late_entry(snap, btc, tl, age, mtype, open_btc):
    """C2: Wait 2+ minutes into market for better direction data"""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 90: return None
    min_age = 120 if mtype == "5m" else 180  # wait 2min/3min
    if age < min_age: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120)
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and btc_2m > 0 and 0.38 <= yes_p <= 0.55:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < 0 and 0.38 <= no_p <= 0.55:
        return {"side": "NO", "price": no_p}
    return None

def flash_C3_mispriced(snap, btc, tl, age, mtype, open_btc):
    """C3: MISPRICING — BTC clearly moved but token hasn't followed.
    BTC up 0.05%+ but YES still under $0.55 = market slow."""
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 120 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    if abs(chg_open) < 0.0003: return None
    # The edge: BTC moved enough that winning side should be >$0.55
    # but it's still $0.38-$0.55 = mispriced, buy the gap
    mispricing_threshold = 0.0003  # minimum BTC move
    if chg_open > mispricing_threshold and yes_p < 0.55 and yes_p >= 0.30:
        gap = 0.55 - yes_p  # how much mispriced
        if gap > 0.03:  # at least 3 cents mispriced
            return {"side": "YES", "price": yes_p}
    if chg_open < -mispricing_threshold and no_p < 0.55 and no_p >= 0.30:
        gap = 0.55 - no_p
        if gap > 0.03:
            return {"side": "NO", "price": no_p}
    return None

def flash_C4_5m_only(snap, btc, tl, age, mtype, open_btc):
    """C4: 5-minute markets ONLY (faster resolution, less noise)"""
    if mtype != "5m": return None
    yes_p = _fp(snap.get("price_up")); no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None
    if tl < 90 or age < 60: return None
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    btc_2m = btc.chg(120)
    if abs(chg_open) < 0.0003: return None
    if chg_open > 0.0003 and btc_2m > 0 and 0.38 <= yes_p <= 0.55:
        return {"side": "YES", "price": yes_p}
    if chg_open < -0.0003 and btc_2m < 0 and 0.38 <= no_p <= 0.55:
        return {"side": "NO", "price": no_p}
    return None

# ═══════════════════════════════════════════════════════════
# ALL 12 VARIANTS
# ═══════════════════════════════════════════════════════════
VARIANTS = {
    # Group A: Price ranges
    "A1_cheap_15-30":   flash_A1_cheap,
    "A2_lowmid_30-45":  flash_A2_low_mid,
    "A3_mid_38-55":     flash_A3_mid,
    "A4_highmid_45-58": flash_A4_high_mid,
    # Group B: Direction logic
    "B1_open_only":     flash_B1_open_only,
    "B2_strong_open":   flash_B2_strong_open,
    "B3_full_momentum": flash_B3_momentum,
    "B4_open_plus_2m":  flash_B4_open_plus_2m,
    # Group C: Filters & timing
    "C1_book_filter":   flash_C1_book_filter,
    "C2_late_entry":    flash_C2_late_entry,
    "C3_mispriced":     flash_C3_mispriced,
    "C4_5m_only":       flash_C4_5m_only,
}

# ═══════════════════════════════════════════════════════════
# TEST HARNESS
# ═══════════════════════════════════════════════════════════
def run():
    api = API()
    results = {n: {"wins":0,"losses":0,"pnl":0.0,"wagered":0,"trades":[]} for n in VARIANTS}

    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║     FLASH STRATEGY LAB v2 — 12 VARIANTS × 30 DAYS             ║")
    print("╚══════════════════════════════════════════════════════════════════╝\n")
    print(f"  Period:     {BACKTEST_DAYS} days")
    print(f"  Bet size:   ${BET_SIZE:.0f} fixed per trade")
    print(f"  Markets:    BTC 5m + 15m")
    print(f"  Price src:  Chainlink (settlement oracle)")
    print(f"  Variants:   {len(VARIANTS)}")
    print()
    for name in VARIANTS:
        print(f"    {name}")
    print()

    total_mkts = 0
    for mtype in ["5m", "15m"]:
        print(f"{'='*65}")
        print(f"  Loading {mtype} markets...")
        print(f"{'='*65}")

        offset = 0; markets = []
        while True:
            d = api.get_markets(mtype, resolved=True, limit=100, offset=offset)
            if not d or not d.get("markets"): break
            batch = d["markets"]
            markets.extend(batch)
            print(f"    Fetched {len(markets)} markets...")
            if len(batch) < 100: break
            offset += 100
            if len(markets) > 10000: break

        cutoff = datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS)
        btc_mkts = []
        for m in markets:
            if "btc" not in m.get("slug", "").lower(): continue
            try:
                et = datetime.fromisoformat(m["end_time"].replace("Z", "+00:00"))
                if et < cutoff: continue
            except: continue
            btc_mkts.append(m)

        print(f"  → {len(btc_mkts)} BTC markets in last {BACKTEST_DAYS} days\n")
        total_mkts += len(btc_mkts)

        for i, mkt in enumerate(btc_mkts):
            if i % 100 == 0:
                tt = sum(r["wins"]+r["losses"] for r in results.values())
                best_n = max(results, key=lambda n: results[n]["pnl"])
                best_p = results[best_n]["pnl"]
                print(f"  [{mtype}] {i}/{len(btc_mkts)} | total trades: {tt} | leader: {best_n} ${best_p:+.0f}")

            snaps = api.get_snapshots(mkt["market_id"], limit=1000)
            if not snaps or not snaps.get("snapshots"): continue

            try:
                et = datetime.fromisoformat(mkt["end_time"].replace("Z", "+00:00"))
                st = datetime.fromisoformat(mkt["start_time"].replace("Z", "+00:00"))
                dur = (et - st).total_seconds()
            except: continue

            open_btc = _fp(mkt.get("btc_price_start"))
            winner = mkt.get("winner", "")
            if not winner: continue

            btc_tracker = BTC()
            traded = {n: False for n in VARIANTS}

            for snap in snaps["snapshots"]:
                try:
                    snap_t = datetime.fromisoformat(snap["time"].replace("Z", "+00:00"))
                    tl = (et - snap_t).total_seconds()
                    age = dur - tl
                    ts = snap_t.timestamp()
                except: continue

                bp = snap.get("btc_price")
                if bp: btc_tracker.update(ts, bp)

                for name, func in VARIANTS.items():
                    if traded[name]: continue
                    sig = func(snap, btc_tracker, tl, age, mtype, open_btc)
                    if not sig: continue

                    traded[name] = True
                    side = sig["side"]
                    price = sig["price"]
                    shares = BET_SIZE / price

                    won = (side == "YES" and winner.upper() in ("UP", "YES")) or \
                          (side == "NO" and winner.upper() in ("DOWN", "NO"))

                    if won:
                        profit = shares - BET_SIZE
                        results[name]["wins"] += 1
                        results[name]["pnl"] += profit
                    else:
                        results[name]["losses"] += 1
                        results[name]["pnl"] -= BET_SIZE

                    results[name]["wagered"] += BET_SIZE
                    results[name]["trades"].append({
                        "side": side, "price": price,
                        "result": "WIN" if won else "LOSS",
                        "pnl": profit if won else -BET_SIZE,
                        "market": mkt["market_id"], "mtype": mtype
                    })

    # ═══════════════════════════════════════════════════════
    # RESULTS
    # ═══════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print(f"  FLASH LAB RESULTS — {total_mkts} MARKETS × {BACKTEST_DAYS} DAYS")
    print(f"{'='*70}\n")

    # Sort by P&L
    sorted_names = sorted(VARIANTS.keys(), key=lambda n: results[n]["pnl"], reverse=True)

    print(f"  {'#':>2} {'Variant':<22} {'Trades':>6} {'W':>5} {'L':>5} {'WR':>6} {'P&L':>10} {'ROI':>7}")
    print(f"  {'-'*68}")

    for rank, name in enumerate(sorted_names, 1):
        r = results[name]
        t = r["wins"] + r["losses"]
        wr = r["wins"]/t*100 if t else 0
        roi = r["pnl"]/r["wagered"]*100 if r["wagered"] else 0
        marker = "★" if rank == 1 else "✓" if r["pnl"] > 0 else "✗"
        print(f"  {marker}{rank:>1} {name:<22} {t:>6} {r['wins']:>5} {r['losses']:>5} {wr:>5.1f}% ${r['pnl']:>+9.0f} {roi:>+6.1f}%")

    # Top 3 detailed breakdown
    print(f"\n{'='*70}")
    print(f"  TOP 3 DETAILED BREAKDOWN")
    print(f"{'='*70}")

    for rank, name in enumerate(sorted_names[:3], 1):
        r = results[name]
        t = r["wins"] + r["losses"]
        if t == 0: continue
        wr = r["wins"]/t*100

        print(f"\n  #{rank} {name}")
        print(f"  {'─'*50}")
        print(f"  Trades: {t} | WR: {wr:.1f}% | P&L: ${r['pnl']:+.2f}")

        # By side
        for sn in ["YES", "NO"]:
            st = [x for x in r["trades"] if x["side"] == sn]
            if not st: continue
            sw = sum(1 for x in st if x["result"] == "WIN")
            sp = sum(x["pnl"] for x in st)
            print(f"    {sn}: {len(st)}t {sw}W/{len(st)-sw}L {sw/len(st)*100:.0f}%WR ${sp:+.2f}")

        # By price
        for lo, hi, lb in [(0.15,0.30,"$0.15-30"),(0.30,0.40,"$0.30-40"),
                           (0.40,0.50,"$0.40-50"),(0.50,0.60,"$0.50-60")]:
            bt = [x for x in r["trades"] if lo <= x["price"] < hi]
            if not bt: continue
            bw = sum(1 for x in bt if x["result"] == "WIN")
            bp = sum(x["pnl"] for x in bt)
            print(f"    {lb}: {len(bt)}t {bw}W {bw/len(bt)*100:.0f}%WR ${bp:+.2f}")

        # By market type
        for mt in ["5m", "15m"]:
            mt_t = [x for x in r["trades"] if x.get("mtype") == mt]
            if not mt_t: continue
            mw = sum(1 for x in mt_t if x["result"] == "WIN")
            mp = sum(x["pnl"] for x in mt_t)
            print(f"    {mt}: {len(mt_t)}t {mw}W {mw/len(mt_t)*100:.0f}%WR ${mp:+.2f}")

    # GROUP SUMMARY
    print(f"\n{'='*70}")
    print(f"  GROUP INSIGHTS")
    print(f"{'='*70}")

    # Best price range
    group_a = [(n, results[n]) for n in sorted_names if n.startswith("A")]
    if group_a:
        print(f"\n  PRICE RANGE (Group A):")
        for n, r in group_a:
            t = r["wins"]+r["losses"]
            wr = r["wins"]/t*100 if t else 0
            print(f"    {n}: {t}t {wr:.0f}%WR ${r['pnl']:+.0f}")

    # Best direction logic
    group_b = [(n, results[n]) for n in sorted_names if n.startswith("B")]
    if group_b:
        print(f"\n  DIRECTION LOGIC (Group B):")
        for n, r in group_b:
            t = r["wins"]+r["losses"]
            wr = r["wins"]/t*100 if t else 0
            print(f"    {n}: {t}t {wr:.0f}%WR ${r['pnl']:+.0f}")

    # Best filter
    group_c = [(n, results[n]) for n in sorted_names if n.startswith("C")]
    if group_c:
        print(f"\n  FILTERS & TIMING (Group C):")
        for n, r in group_c:
            t = r["wins"]+r["losses"]
            wr = r["wins"]/t*100 if t else 0
            print(f"    {n}: {t}t {wr:.0f}%WR ${r['pnl']:+.0f}")

    # RECOMMENDATION
    print(f"\n{'='*70}")
    best = sorted_names[0]
    r = results[best]
    t = r["wins"]+r["losses"]
    wr = r["wins"]/t*100 if t else 0
    print(f"  RECOMMENDATION: Deploy {best}")
    print(f"  WR: {wr:.1f}% | P&L: ${r['pnl']:+.2f} over {BACKTEST_DAYS} days")
    print(f"  Trades: {t} ({t/BACKTEST_DAYS:.1f}/day)")
    print(f"{'='*70}")

    # Save
    save = {"config": {"days": BACKTEST_DAYS, "bet": BET_SIZE, "markets": total_mkts}}
    for n in VARIANTS:
        r = results[n]
        t = r["wins"]+r["losses"]
        save[n] = {"trades":t, "wins":r["wins"], "losses":r["losses"],
                   "wr": r["wins"]/t*100 if t else 0, "pnl":r["pnl"],
                   "roi": r["pnl"]/r["wagered"]*100 if r["wagered"] else 0,
                   "trade_list": r["trades"][:30]}
    json.dump(save, open("flash_lab_results.json","w"), indent=2, default=str)
    print(f"\n  Saved: flash_lab_results.json")
    print(f"  API calls: {api._c}")

if __name__ == "__main__":
    run()
