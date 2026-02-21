#!/usr/bin/env python3
"""
Polymarket Bot v9.5 FULL Backtester — 30 Days
All 7 strategies, Chainlink prices, maker-only fills, recovery sizing.

Strategies: ARB, LATENCY, MEANREV, FLASH, SNIPE, PAIR, SPIKE
"""

import requests, json, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

API_KEY = "pdm_K3hqRH80z3B2mRfcMij5HnLR3CoooweM"
BASE_URL = "https://api.polybacktest.com"
HEADERS = {"X-API-Key": API_KEY}

STARTING_BALANCE = 5000.0
MAX_POSITIONS = 4
BACKTEST_DAYS = 30
MARKET_TYPES = ["5m", "15m"]

def _fp(val):
    """Safe float parser — handles None, strings, invalid values."""
    if val is None:
        return 0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0

# ═══════════════════════════════════════════════════════════
# API CLIENT
# ═══════════════════════════════════════════════════════════
class APIClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._calls = 0

    def get(self, path, params=None):
        self._calls += 1
        if self._calls % 80 == 0:
            time.sleep(0.3)
        try:
            r = self.session.get(f"{BASE_URL}{path}", params=params, timeout=30)
            if r.status_code == 429:
                print("    Rate limited, waiting 5s...")
                time.sleep(5)
                return self.get(path, params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if self._calls % 200 == 0:
                print(f"    API err: {e}")
            return None

    def get_markets(self, market_type, resolved=True, limit=100, offset=0):
        return self.get("/v1/markets", {
            "market_type": market_type,
            "resolved": resolved,
            "limit": limit,
            "offset": offset
        })

    def get_snapshots(self, market_id, limit=1000):
        return self.get(f"/v1/markets/{market_id}/snapshots", {"limit": limit})

# ═══════════════════════════════════════════════════════════
# BTC PRICE TRACKER (Chainlink from snapshots)
# ═══════════════════════════════════════════════════════════
class BTC:
    """Tracks BTC price history. In backtest, btc_price from snapshots
    IS the Chainlink price (what Polymarket settles on)."""

    def __init__(self):
        self.prices = []  # [(timestamp, price)]

    def update(self, ts, p):
        p = _fp(p)
        if p > 1000:  # sanity check
            self.prices.append((ts, p))
            # Keep last 15 minutes
            cutoff = ts - 900
            self.prices = [(t, px) for t, px in self.prices if t > cutoff]

    def chg(self, sec):
        """Price change over last N seconds."""
        if len(self.prices) < 2:
            return 0
        now_t, now_p = self.prices[-1]
        target_t = now_t - sec
        best = min(self.prices, key=lambda x: abs(x[0] - target_t))
        if best[1] <= 0:
            return 0
        return (now_p - best[1]) / best[1]

    def chg_from(self, open_price):
        """Change from a specific open price (settlement direction)."""
        if not self.prices or open_price <= 0:
            return 0
        return (self.prices[-1][1] - open_price) / open_price

    @property
    def price(self):
        return self.prices[-1][1] if self.prices else 0

# ═══════════════════════════════════════════════════════════
# ORDERBOOK HELPERS
# ═══════════════════════════════════════════════════════════
def get_book(snap, side):
    """Get orderbook data for a side. Returns (bids, asks, bid_vol, ask_vol)."""
    key = "orderbook_up" if side == "YES" else "orderbook_down"
    bk = snap.get(key)
    if not bk:
        return [], [], 0, 0
    bids = bk.get("bids", [])
    asks = bk.get("asks", [])
    bid_vol = sum(_fp(b.get("size", 0)) for b in bids[:5])
    ask_vol = sum(_fp(a.get("size", 0)) for a in asks[:5])
    return bids, asks, bid_vol, ask_vol

def book_support(snap, side):
    """Check if orderbook has bid support (buyers present)."""
    _, _, bv, av = get_book(snap, side)
    return bv > av * 0.5

def selling_pressure(snap, side):
    """Check if heavy selling pressure exists."""
    _, _, bv, av = get_book(snap, side)
    if av == 0:
        return False
    return av > bv * 2.0

def would_maker_fill(snap, side, price):
    """Simulate whether a maker order would fill based on orderbook."""
    key = "orderbook_up" if side == "YES" else "orderbook_down"
    bk = snap.get(key)
    if not bk:
        return True  # no book data, assume fill
    asks = bk.get("asks", [])
    bids = bk.get("bids", [])
    if not asks or not bids:
        return True
    best_ask = _fp(asks[0].get("price", 0))
    best_bid = _fp(bids[0].get("price", 0))
    if best_ask <= 0 or best_bid <= 0:
        return True
    spread = best_ask - best_bid
    # Tight spread = likely fill
    if spread <= 0.03:
        return True
    # Our price is competitive
    if price >= best_ask - 0.02:
        return True
    # Enough liquidity
    ask_vol = sum(_fp(a.get("size", 0)) for a in asks[:3])
    if ask_vol > 100:
        return True
    return False

# ═══════════════════════════════════════════════════════════
# S1: ARB — YES + NO < $0.97
# ═══════════════════════════════════════════════════════════
def check_arb(snap):
    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0:
        return None
    total = yes_p + no_p
    if total >= 0.97:
        return None
    # Buy cheaper side
    if yes_p <= no_p:
        return {"side": "YES", "price": yes_p, "strategy": "ARB"}
    return {"side": "NO", "price": no_p, "strategy": "ARB"}

# ═══════════════════════════════════════════════════════════
# S2: LATENCY — BTC moved, market hasn't repriced
# ═══════════════════════════════════════════════════════════
def check_latency(snap, btc, time_left, mtype, open_btc):
    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0:
        return None

    min_tl = 120 if mtype == "5m" else 240
    if time_left < min_tl:
        return None

    chg_30 = btc.chg(30)
    chg_60 = btc.chg(60)
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0
    chg = max(chg_open, chg_30, chg_60, key=abs)

    if abs(chg) < 0.0005:
        return None

    up = chg > 0

    # Chainlink direction must agree
    if chg_open != 0:
        if (up and chg_open < 0) or (not up and chg_open > 0):
            return None

    target_price = yes_p if up else no_p
    if target_price > 0.40 or target_price < 0.15:
        return None

    other = no_p if up else yes_p
    if other < 0.15:
        return None

    confidence = min(0.95, 0.60 + abs(chg) * 100)
    edge = confidence - target_price
    if edge < 0.15:
        return None

    return {"side": "YES" if up else "NO", "price": target_price,
            "strategy": "LATENCY", "chg": chg}

# ═══════════════════════════════════════════════════════════
# S3: MEANREV — Price dropped hard, buy the bounce
# ═══════════════════════════════════════════════════════════
def check_meanrev(snap, btc, time_left, mtype):
    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0:
        return None

    min_tl = 90 if mtype == "5m" else 180
    if time_left < min_tl:
        return None

    chg_30 = btc.chg(30)
    chg_2m = btc.chg(120)

    # YES dropped to $0.30-$0.45 + BTC bouncing up
    if 0.30 <= yes_p <= 0.45 and no_p >= 0.55:
        if chg_30 > 0.0002 and chg_2m < -0.0003:
            return {"side": "YES", "price": yes_p, "strategy": "MEANREV"}

    # NO dropped + BTC bouncing down
    if 0.30 <= no_p <= 0.45 and yes_p >= 0.55:
        if chg_30 < -0.0002 and chg_2m > 0.0003:
            return {"side": "NO", "price": no_p, "strategy": "MEANREV"}

    return None

# ═══════════════════════════════════════════════════════════
# S4: FLASH — Direction-aware, buy winning side
# Matches live bot S_Flash (including flat market logic)
# ═══════════════════════════════════════════════════════════
def check_flash(snap, btc, time_left, market_age, mtype, open_btc):
    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0:
        return None

    # Current live bot range: $0.15-$0.30
    yes_cheap = 0.15 <= yes_p <= 0.30
    no_cheap = 0.15 <= no_p <= 0.30
    if not yes_cheap and not no_cheap:
        return None

    min_tl = 120 if mtype == "5m" else 240
    if time_left < min_tl:
        return None
    min_age = 60 if mtype == "5m" else 120
    if market_age < min_age:
        return None

    btc_2m = btc.chg(120)
    btc_30s = btc.chg(30)

    btc_going_up = btc_2m > 0.0002 and btc_30s >= 0
    btc_going_down = btc_2m < -0.0002 and btc_30s <= 0

    # Buy YES when BTC going up
    if yes_cheap and btc_going_up:
        if not selling_pressure(snap, "YES"):
            return {"side": "YES", "price": yes_p, "strategy": "FLASH"}

    # Buy NO when BTC going down
    if no_cheap and btc_going_down:
        if not selling_pressure(snap, "NO"):
            return {"side": "NO", "price": no_p, "strategy": "FLASH"}

    # Flat market: buy cheapest with book support
    if not btc_going_up and not btc_going_down:
        for side, cheap, pkey in [("YES", yes_cheap, "price_up"),
                                   ("NO", no_cheap, "price_down")]:
            if not cheap:
                continue
            if not selling_pressure(snap, side) and book_support(snap, side):
                return {"side": side, "price": _fp(snap.get(pkey)),
                        "strategy": "FLASH"}

    return None

# ═══════════════════════════════════════════════════════════
# S5: SNIPE — Buy winning side in last 45s of 5m markets
# ═══════════════════════════════════════════════════════════
def check_snipe(snap, btc, time_left, mtype, open_btc):
    if mtype != "5m":
        return None
    if time_left > 45 or time_left < 8:
        return None

    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0:
        return None

    chg_2m = btc.chg(120)
    chg_open = btc.chg_from(open_btc) if open_btc > 0 else 0

    if chg_2m > 0.0003 and chg_open > 0.0001:
        if 0.82 <= yes_p <= 0.94:
            return {"side": "YES", "price": yes_p, "strategy": "SNIPE"}

    if chg_2m < -0.0003 and chg_open < -0.0001:
        if 0.82 <= no_p <= 0.94:
            return {"side": "NO", "price": no_p, "strategy": "SNIPE"}

    return None

# ═══════════════════════════════════════════════════════════
# S6: PAIR — Complete YES+NO pair for guaranteed profit
# ═══════════════════════════════════════════════════════════
def check_pair(snap, positions, market_id):
    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if yes_p <= 0 or no_p <= 0:
        return None
    for pos in positions:
        if pos["market_id"] != market_id:
            continue
        if pos["side"] == "YES" and no_p > 0:
            if pos["price"] + no_p < 0.97:
                return {"side": "NO", "price": no_p, "strategy": "PAIR"}
        elif pos["side"] == "NO" and yes_p > 0:
            if pos["price"] + yes_p < 0.97:
                return {"side": "YES", "price": yes_p, "strategy": "PAIR"}
    return None

# ═══════════════════════════════════════════════════════════
# S7: SPIKE — Buy panic-sold tokens (orderbook spike)
# ═══════════════════════════════════════════════════════════
class SpikeTracker:
    """Detects sudden sell pressure spikes in orderbook."""

    def __init__(self):
        self.prev_yes_ask = 0
        self.prev_no_ask = 0

    def check(self, snap, btc, time_left, mtype):
        yes_p = _fp(snap.get("price_up"))
        no_p = _fp(snap.get("price_down"))
        if yes_p <= 0 or no_p <= 0:
            return None

        min_tl = 120 if mtype == "5m" else 240
        if time_left < min_tl:
            return None

        _, _, _, yes_ask = get_book(snap, "YES")
        _, _, _, no_ask = get_book(snap, "NO")

        result = None

        # YES ask spiked 3x+ = someone dumped YES tokens
        if (self.prev_yes_ask > 10 and yes_ask > self.prev_yes_ask * 3.0
                and yes_ask > 100 and 0.10 <= yes_p <= 0.28):
            btc_1m = btc.chg(60)
            if btc_1m >= -0.003:  # not a justified crash
                result = {"side": "YES", "price": yes_p, "strategy": "SPIKE"}

        # NO ask spiked
        if (result is None and self.prev_no_ask > 10
                and no_ask > self.prev_no_ask * 3.0
                and no_ask > 100 and 0.10 <= no_p <= 0.28):
            btc_1m = btc.chg(60)
            if btc_1m <= 0.003:
                result = {"side": "NO", "price": no_p, "strategy": "SPIKE"}

        # Update previous
        self.prev_yes_ask = yes_ask
        self.prev_no_ask = no_ask

        return result

# ═══════════════════════════════════════════════════════════
# BACKTESTER
# ═══════════════════════════════════════════════════════════
ALL_STRATS = ["ARB", "LATENCY", "MEANREV", "FLASH", "SNIPE", "PAIR", "SPIKE"]

class Backtester:
    def __init__(self):
        self.balance = STARTING_BALANCE
        self.trades = []
        self.open_positions = []
        self.btc = BTC()
        self.api = APIClient()
        self.cooldowns = {}
        self.spike = SpikeTracker()
        self.stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})

    def bet_size(self, strat):
        """Match live bot sizing."""
        recovery = self.balance < 5500
        pct = {"ARB": 0.05, "LATENCY": 0.06, "MEANREV": 0.04, "FLASH": 0.03,
               "SNIPE": 0.04, "PAIR": 0.05, "SPIKE": 0.03}.get(strat, 0.03)
        base = self.balance * pct
        if recovery:
            base *= 0.5
        hard_max = 200 if recovery else 400
        return min(max(base, 1.0), hard_max, self.balance * 0.10)

    def open_trade(self, sig, market_id, winner, ts, mtype):
        if len(self.open_positions) >= MAX_POSITIONS:
            return False

        cd_key = f"{sig['strategy']}_{market_id}"
        cd_dur = {"ARB": 30, "LATENCY": 15, "MEANREV": 20, "FLASH": 30,
                  "SNIPE": 60, "PAIR": 15, "SPIKE": 10}.get(sig["strategy"], 30)
        if ts - self.cooldowns.get(cd_key, 0) < cd_dur:
            return False

        sz = self.bet_size(sig["strategy"])
        if sz < 1 or sz > self.balance:
            return False

        shares = sz / sig["price"]
        self.balance -= sz
        self.open_positions.append({
            "strategy": sig["strategy"],
            "side": sig["side"],
            "price": sig["price"],
            "size": sz,
            "shares": shares,
            "market_id": market_id,
            "winner": winner,
            "entry_time": ts,
            "mtype": mtype,  # tag market type for reporting
        })
        self.cooldowns[cd_key] = ts
        return True

    def resolve(self):
        """Resolve all open positions at market end."""
        for pos in self.open_positions:
            winner = pos.get("winner", "")
            if not winner:
                # Unknown winner — treat as loss
                self.stats[pos["strategy"]]["losses"] += 1
                self.stats[pos["strategy"]]["pnl"] -= pos["size"]
                self.stats[pos["strategy"]]["trades"] += 1
                self.trades.append({**pos, "result": "LOSS", "pnl": -pos["size"]})
                continue

            won = ((pos["side"] == "YES" and winner.upper() in ("UP", "YES")) or
                   (pos["side"] == "NO" and winner.upper() in ("DOWN", "NO")))

            if won:
                payout = pos["shares"]  # each share pays $1
                profit = payout - pos["size"]
                self.balance += payout
                self.stats[pos["strategy"]]["wins"] += 1
                self.stats[pos["strategy"]]["pnl"] += profit
                self.trades.append({**pos, "result": "WIN", "pnl": profit})
            else:
                self.stats[pos["strategy"]]["losses"] += 1
                self.stats[pos["strategy"]]["pnl"] -= pos["size"]
                self.trades.append({**pos, "result": "LOSS", "pnl": -pos["size"]})

            self.stats[pos["strategy"]]["trades"] += 1

        self.open_positions = []

    def run(self):
        print("+" + "=" * 64 + "+")
        print("|   POLYMARKET BOT v9.5 FULL BACKTESTER — 30 DAYS              |")
        print("|   All 7 strategies  •  Chainlink prices  •  Maker-only        |")
        print("+" + "=" * 64 + "+\n")
        print(f"  Balance:     ${STARTING_BALANCE:.0f}")
        print(f"  Period:      {BACKTEST_DAYS} days")
        print(f"  Markets:     {MARKET_TYPES}")
        print(f"  Strategies:  {', '.join(ALL_STRATS)}")
        print(f"  Max pos:     {MAX_POSITIONS}")
        print(f"  Price src:   Chainlink (settlement oracle)\n")

        total_mkts = 0

        for mtype in MARKET_TYPES:
            print(f"{'=' * 60}")
            print(f"  Loading {mtype} markets...")
            print(f"{'=' * 60}")

            # Fetch all resolved markets
            offset = 0
            markets = []
            while True:
                d = self.api.get_markets(mtype, resolved=True, limit=100, offset=offset)
                if not d or not d.get("markets"):
                    break
                markets.extend(d["markets"])
                print(f"    Fetched {len(markets)}...")
                if len(d["markets"]) < 100:
                    break
                offset += 100
                if len(markets) > 15000:
                    break

            # Filter to BTC markets in date range
            cutoff = datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS)
            btc_mkts = []
            for m in markets:
                slug = m.get("slug", "").lower()
                if "btc" not in slug and "bitcoin" not in slug:
                    continue
                try:
                    et = datetime.fromisoformat(m["end_time"].replace("Z", "+00:00"))
                    if et < cutoff:
                        continue
                except (KeyError, ValueError):
                    continue
                if not m.get("winner"):
                    continue  # skip unresolved
                btc_mkts.append(m)

            print(f"  -> {len(btc_mkts)} BTC markets in last {BACKTEST_DAYS} days\n")
            total_mkts += len(btc_mkts)

            # Process each market
            for i, mkt in enumerate(btc_mkts):
                if i % 100 == 0:
                    tt = sum(s["trades"] for s in self.stats.values())
                    tw = sum(s["wins"] for s in self.stats.values())
                    wr = tw / tt * 100 if tt else 0
                    print(f"  [{mtype}] {i}/{len(btc_mkts)} | ${self.balance:.0f} | {tt}t {wr:.0f}%WR")

                # Get snapshots
                snaps_data = self.api.get_snapshots(mkt["market_id"], limit=1000)
                if not snaps_data or not snaps_data.get("snapshots"):
                    continue

                # Parse market times
                try:
                    end_time = datetime.fromisoformat(mkt["end_time"].replace("Z", "+00:00"))
                    start_time = datetime.fromisoformat(mkt["start_time"].replace("Z", "+00:00"))
                    duration = (end_time - start_time).total_seconds()
                except (KeyError, ValueError):
                    continue

                market_id = mkt["market_id"]
                winner = mkt.get("winner", "")

                # Get open BTC price — try btc_price_start, fallback to first snapshot
                open_btc = _fp(mkt.get("btc_price_start"))
                if open_btc <= 0:
                    # Fallback: use first snapshot's btc_price
                    first_snap = snaps_data["snapshots"][0] if snaps_data["snapshots"] else {}
                    open_btc = _fp(first_snap.get("btc_price"))

                # Reset spike tracker per market
                self.spike = SpikeTracker()
                traded = {}  # strategy -> count for this market

                # Process each snapshot
                for snap in snaps_data["snapshots"]:
                    try:
                        snap_dt = datetime.fromisoformat(snap["time"].replace("Z", "+00:00"))
                        time_left = (end_time - snap_dt).total_seconds()
                        market_age = duration - time_left
                        snap_ts = snap_dt.timestamp()
                    except (KeyError, ValueError):
                        continue

                    # Update BTC price tracker
                    btc_price = snap.get("btc_price")
                    if btc_price:
                        self.btc.update(snap_ts, btc_price)

                    # Skip if full
                    if len(self.open_positions) >= MAX_POSITIONS:
                        continue

                    # Collect signals in priority order
                    signals = []

                    # S6: PAIR first (completes guaranteed pairs)
                    sig = check_pair(snap, self.open_positions, market_id)
                    if sig and traded.get("PAIR", 0) < 2:
                        signals.append(sig)

                    # S1: ARB
                    sig = check_arb(snap)
                    if sig and traded.get("ARB", 0) < 1:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # S2: LATENCY
                    sig = check_latency(snap, self.btc, time_left, mtype, open_btc)
                    if sig and traded.get("LATENCY", 0) < 2:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # S3: MEANREV
                    sig = check_meanrev(snap, self.btc, time_left, mtype)
                    if sig and traded.get("MEANREV", 0) < 1:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # S4: FLASH
                    sig = check_flash(snap, self.btc, time_left, market_age, mtype, open_btc)
                    if sig and traded.get("FLASH", 0) < 2:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # S5: SNIPE
                    sig = check_snipe(snap, self.btc, time_left, mtype, open_btc)
                    if sig and traded.get("SNIPE", 0) < 1:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # S7: SPIKE
                    sig = self.spike.check(snap, self.btc, time_left, mtype)
                    if sig and traded.get("SPIKE", 0) < 1:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # Execute first valid signal
                    for sig in signals:
                        if self.open_trade(sig, market_id, winner, snap_ts, mtype):
                            traded[sig["strategy"]] = traded.get(sig["strategy"], 0) + 1
                            break

                # Resolve all positions at market end
                self.resolve()

        # Print results
        self.print_results(total_mkts)

    def print_results(self, total_mkts):
        tt = sum(self.stats[s]["trades"] for s in ALL_STRATS)
        tw = sum(self.stats[s]["wins"] for s in ALL_STRATS)
        tl = sum(self.stats[s]["losses"] for s in ALL_STRATS)
        pnl = self.balance - STARTING_BALANCE
        wr = tw / tt * 100 if tt else 0

        # Max drawdown
        equity = STARTING_BALANCE
        peak = equity
        max_dd = 0
        for t in self.trades:
            equity += t["pnl"]
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        print(f"\n\n{'=' * 65}")
        print(f"  BACKTEST RESULTS — v9.5 FULL — {BACKTEST_DAYS} DAYS")
        print(f"{'=' * 65}\n")
        print(f"  Markets:    {total_mkts}")
        print(f"  Trades:     {tt}")
        print(f"  Start:      ${STARTING_BALANCE:.0f}")
        print(f"  End:        ${self.balance:.0f}")
        print(f"  P&L:        ${pnl:+.2f} ({pnl / STARTING_BALANCE * 100:+.1f}%)")
        print(f"  Win Rate:   {wr:.1f}%")
        print(f"  Max DD:     {max_dd:.1f}%")

        # Per strategy
        print(f"\n{'-' * 65}")
        print(f"  {'':>2} {'Strategy':<10} {'Trades':>7} {'W':>5} {'L':>5} {'WR':>6} {'P&L':>12}")
        print(f"{'-' * 65}")
        for strat in ALL_STRATS:
            s = self.stats[strat]
            t = s["trades"]; w = s["wins"]; l = s["losses"]
            r = w / t * 100 if t > 0 else 0
            p = s["pnl"]
            m = "+" if p > 0 else "-" if t > 0 else " "
            print(f"  {m} {strat:<10} {t:>7} {w:>5} {l:>5} {r:>5.1f}% ${p:>+11.2f}")
        print(f"{'-' * 65}")
        print(f"    {'TOTAL':<10} {tt:>7} {tw:>5} {tl:>5} {wr:>5.1f}% ${pnl:>+11.2f}")

        # By side
        print(f"\n  BY SIDE:")
        for sn in ["YES", "NO"]:
            st = [t for t in self.trades if t["side"] == sn]
            if not st:
                continue
            sw = sum(1 for t in st if t["result"] == "WIN")
            sp = sum(t["pnl"] for t in st)
            print(f"    {sn}: {len(st)}t {sw}W/{len(st) - sw}L {sw / len(st) * 100:.0f}%WR ${sp:+.2f}")

        # By entry price
        print(f"\n  BY ENTRY PRICE:")
        for lo, hi, lb in [(0.08, 0.15, "<$0.15"), (0.15, 0.25, "$0.15-25"),
                           (0.25, 0.30, "$0.25-30"), (0.30, 0.45, "$0.30-45"),
                           (0.45, 0.60, "$0.45-60"), (0.60, 1.0, "$0.60+")]:
            bt = [t for t in self.trades if lo <= t["price"] < hi]
            if not bt:
                continue
            bw = sum(1 for t in bt if t["result"] == "WIN")
            bp = sum(t["pnl"] for t in bt)
            print(f"    {lb:>10}: {len(bt)}t {bw}W/{len(bt) - bw}L {bw / len(bt) * 100:.0f}%WR ${bp:+.2f}")

        # By market type
        print(f"\n  BY MARKET TYPE:")
        for mt in ["5m", "15m"]:
            mt_trades = [t for t in self.trades if t.get("mtype") == mt]
            if not mt_trades:
                continue
            mw = sum(1 for t in mt_trades if t["result"] == "WIN")
            mp = sum(t["pnl"] for t in mt_trades)
            print(f"    {mt}: {len(mt_trades)}t {mw}W/{len(mt_trades) - mw}L {mw / len(mt_trades) * 100:.0f}%WR ${mp:+.2f}")

        # Per-strategy side breakdown
        print(f"\n  PER-STRATEGY SIDE BREAKDOWN:")
        for strat in ALL_STRATS:
            st = [t for t in self.trades if t["strategy"] == strat]
            if not st:
                continue
            for sn in ["YES", "NO"]:
                side_t = [t for t in st if t["side"] == sn]
                if not side_t:
                    continue
                sw = sum(1 for t in side_t if t["result"] == "WIN")
                sp = sum(t["pnl"] for t in side_t)
                print(f"    {strat:<8} {sn}: {len(side_t)}t {sw}W {sw / len(side_t) * 100:.0f}%WR ${sp:+.2f}")

        # Sizing stats
        avg_win = [t["pnl"] for t in self.trades if t["result"] == "WIN"]
        avg_loss = [t["pnl"] for t in self.trades if t["result"] == "LOSS"]
        if avg_win:
            print(f"\n  Avg win:   ${sum(avg_win) / len(avg_win):.2f}")
        if avg_loss:
            print(f"  Avg loss:  ${sum(avg_loss) / len(avg_loss):.2f}")
        if avg_win and avg_loss:
            ratio = (sum(avg_win) / len(avg_win)) / abs(sum(avg_loss) / len(avg_loss))
            print(f"  Win/Loss:  {ratio:.2f}x")

        # Save results
        save = {
            "config": {
                "balance": STARTING_BALANCE,
                "days": BACKTEST_DAYS,
                "max_pos": MAX_POSITIONS,
                "source": "chainlink",
                "strategies": ", ".join(ALL_STRATS),
            },
            "summary": {
                "balance": self.balance,
                "pnl": pnl,
                "trades": tt,
                "wr": wr,
                "mdd": max_dd,
            },
            "per_strategy": {s: dict(self.stats[s]) for s in ALL_STRATS},
            "trades": self.trades[:500],  # save more trades for analysis
        }
        with open("backtest_results.json", "w") as f:
            json.dump(save, f, indent=2, default=str)
        print(f"\n  Saved: backtest_results.json")
        print(f"  API calls: {self.api._calls}")
        print(f"{'=' * 65}\n")


if __name__ == "__main__":
    Backtester().run()
