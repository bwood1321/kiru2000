#!/usr/bin/env python3
"""
Polymarket Bot v9.5 Backtester
Uses PolyBackTest.com API to replay strategies against historical data.

Usage:
    python3 backtest_bot.py

Requires: pip install requests
"""

import requests, json, time, sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
API_KEY = "pdm_K3hqRH80z3B2mRfcMij5HnLR3CoooweM"
BASE_URL = "https://api.polybacktest.com"
HEADERS = {"X-API-Key": API_KEY}

# Backtest parameters
STARTING_BALANCE = 5000.0
MAX_POSITIONS = 4
RECOVERY_TARGET = 5500.0
FLASH_PCT = 0.03
LATENCY_PCT = 0.06
SNIPE_PCT = 0.04
RECOVERY_MAX_BET = 200.0
NORMAL_MAX_BET = 400.0
FLASH_MAX_PRICE = 0.30
FLASH_MIN_PRICE = 0.15
SNIPE_MIN_PRICE = 0.82
SNIPE_MAX_PRICE = 0.94
LATENCY_THRESHOLD = 0.0007  # 0.07% BTC move

# How many days back to test
BACKTEST_DAYS = 7
MARKET_TYPES = ["5m", "15m"]


# ═══════════════════════════════════════════════════════════════
# API CLIENT
# ═══════════════════════════════════════════════════════════════
class APIClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._calls = 0

    def get(self, path, params=None):
        self._calls += 1
        if self._calls % 50 == 0:
            time.sleep(0.5)  # rate limit safety
        try:
            r = self.session.get(f"{BASE_URL}{path}", params=params, timeout=30)
            if r.status_code == 429:
                print("  Rate limited, waiting 5s...")
                time.sleep(5)
                return self.get(path, params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  API error: {e}")
            return None

    def get_markets(self, market_type, resolved=True, limit=100, offset=0):
        return self.get("/v1/markets", {
            "market_type": market_type, "resolved": resolved,
            "limit": limit, "offset": offset
        })

    def get_snapshots(self, market_id, limit=1000, start_time=None, end_time=None):
        params = {"limit": limit}
        if start_time: params["start_time"] = start_time
        if end_time: params["end_time"] = end_time
        return self.get(f"/v1/markets/{market_id}/snapshots", params)


# ═══════════════════════════════════════════════════════════════
# BTC PRICE TRACKER (from snapshots)
# ═══════════════════════════════════════════════════════════════
class BTCTracker:
    """Tracks BTC price history from snapshot data."""
    def __init__(self):
        self.prices = []  # [(timestamp, price)]

    def update(self, ts, price):
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        if price > 0:
            self.prices.append((ts, price))
            # Keep last 600 seconds
            cutoff = ts - 600
            self.prices = [(t, p) for t, p in self.prices if t > cutoff]

    def chg(self, seconds):
        """BTC % change over last N seconds."""
        if len(self.prices) < 2: return 0
        now_price = self.prices[-1][1]
        now_time = self.prices[-1][0]
        target_time = now_time - seconds
        # Find closest price to target_time
        best = None
        for t, p in self.prices:
            if best is None or abs(t - target_time) < abs(best[0] - target_time):
                best = (t, p)
        if best and best[1] > 0:
            return (now_price - best[1]) / best[1]
        return 0

    @property
    def price(self):
        return self.prices[-1][1] if self.prices else 0


# ═══════════════════════════════════════════════════════════════
# STRATEGY SIGNALS
# ═══════════════════════════════════════════════════════════════
def _fp(val):
    """Safe float parse for API values that might be strings or None."""
    if val is None: return 0
    try: return float(val)
    except: return 0


def check_flash(snap, btc, time_left, market_age):
    """Flash: direction-aware dip buying."""
    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None

    yes_cheap = FLASH_MIN_PRICE <= yes_p <= FLASH_MAX_PRICE
    no_cheap = FLASH_MIN_PRICE <= no_p <= FLASH_MAX_PRICE
    if not yes_cheap and not no_cheap: return None
    if time_left < 120: return None  # too close to end
    if market_age < 60: return None  # too early

    btc_2m = btc.chg(120)
    btc_30s = btc.chg(30)
    btc_going_up = btc_2m > 0.0002 and btc_30s >= 0
    btc_going_down = btc_2m < -0.0002 and btc_30s <= 0

    # YES cheap + BTC falling = dip buy
    if yes_cheap and btc_going_down:
        bouncing = btc_30s > -0.0001
        if bouncing:
            return {"side": "YES", "price": yes_p, "strategy": "FLASH"}

    # NO cheap + BTC rising = pullback buy
    if no_cheap and btc_going_up:
        pulling_back = btc_30s < 0.0001
        if pulling_back:
            return {"side": "NO", "price": no_p, "strategy": "FLASH"}

    # Flat market — buy cheapest with caution
    if not btc_going_up and not btc_going_down:
        if yes_cheap and (not no_cheap or yes_p < no_p):
            return {"side": "YES", "price": yes_p, "strategy": "FLASH"}
        if no_cheap:
            return {"side": "NO", "price": no_p, "strategy": "FLASH"}

    return None


def check_latency(snap, btc, time_left):
    """Latency: BTC moved sharply, Polymarket hasn't caught up."""
    if time_left < 90: return None
    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None

    btc_chg = btc.chg(30)  # 30-second change
    if abs(btc_chg) < LATENCY_THRESHOLD: return None

    if btc_chg > LATENCY_THRESHOLD:
        # BTC spiked up → YES should be expensive, if still cheap = edge
        if yes_p <= 0.40:
            return {"side": "YES", "price": yes_p, "strategy": "LATENCY"}
    elif btc_chg < -LATENCY_THRESHOLD:
        # BTC dropped → NO should be expensive, if still cheap = edge
        if no_p <= 0.40:
            return {"side": "NO", "price": no_p, "strategy": "LATENCY"}

    return None


def check_snipe(snap, btc, time_left, market_type):
    """Snipe: buy winning side in last 45s of 5m markets."""
    if market_type != "5m": return None
    if time_left > 45 or time_left < 8: return None

    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None

    btc_2m = btc.chg(120)
    btc_30s = btc.chg(30)

    # BTC clearly going UP
    if btc_2m > 0.0003 and btc_30s > 0:
        if SNIPE_MIN_PRICE <= yes_p <= SNIPE_MAX_PRICE:
            return {"side": "YES", "price": yes_p, "strategy": "SNIPE"}

    # BTC clearly going DOWN
    if btc_2m < -0.0003 and btc_30s < 0:
        if SNIPE_MIN_PRICE <= no_p <= SNIPE_MAX_PRICE:
            return {"side": "NO", "price": no_p, "strategy": "SNIPE"}

    return None


def check_arb(snap):
    """ARB: YES + NO < $0.97 = guaranteed profit."""
    yes_p = _fp(snap.get("price_up"))
    no_p = _fp(snap.get("price_down"))
    if not yes_p or not no_p: return None

    total = yes_p + no_p
    if total < 0.97:
        # Buy whichever is cheaper
        if yes_p <= no_p:
            return {"side": "YES", "price": yes_p, "strategy": "ARB"}
        else:
            return {"side": "NO", "price": no_p, "strategy": "ARB"}
    return None


# ═══════════════════════════════════════════════════════════════
# MAKER FILL SIMULATOR
# ═══════════════════════════════════════════════════════════════
def would_maker_fill(snap, side, price):
    """Check if a maker order at `price` would fill based on orderbook."""
    book_key = "orderbook_up" if side == "YES" else "orderbook_down"
    book = snap.get(book_key)
    if not book: return True  # no book data, assume fills

    asks = book.get("asks", [])
    if not asks: return True  # no asks, our bid would sit

    # Maker order fills if someone sells INTO our bid
    # Our bid at `price` fills if there are sellers at or below our price
    # Actually: we post at best_bid+0.01. If ask comes down to meet us, we fill.
    # Simulate: if the spread is tight (ask - bid < 0.03), likely fills
    bids = book.get("bids", [])
    if bids and asks:
        best_bid = bids[0]["price"]
        best_ask = asks[0]["price"]
        spread = best_ask - best_bid
        # Tight spread = high fill probability
        if spread <= 0.03: return True
        # Our price is competitive (within 1c of best ask)
        if price >= best_ask - 0.02: return True
    return False


# ═══════════════════════════════════════════════════════════════
# BACKTESTER
# ═══════════════════════════════════════════════════════════════
class Backtester:
    def __init__(self):
        self.balance = STARTING_BALANCE
        self.trades = []
        self.open_positions = []
        self.btc = BTCTracker()
        self.api = APIClient()
        self.cooldowns = {}  # strategy -> last_trade_time
        self.stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})

    def get_bet_size(self, strategy, price):
        """Calculate bet size."""
        in_recovery = self.balance < RECOVERY_TARGET
        pct = {"FLASH": FLASH_PCT, "LATENCY": LATENCY_PCT, "SNIPE": SNIPE_PCT, "ARB": 0.05}.get(strategy, 0.03)
        base = self.balance * pct
        if in_recovery:
            base *= 0.5
        max_bet = RECOVERY_MAX_BET if in_recovery else NORMAL_MAX_BET
        sz = min(base, max_bet, self.balance * 0.10)
        # Counter-trend reduction (simplified)
        return max(sz, 1.0)

    def open_trade(self, signal, market, snap_time):
        """Open a position."""
        strategy = signal["strategy"]
        side = signal["side"]
        price = signal["price"]

        # Check position limits
        if len(self.open_positions) >= MAX_POSITIONS: return False
        # Check cooldowns
        cd_key = f"{strategy}_{market['market_id']}"
        last = self.cooldowns.get(cd_key, 0)
        cd_duration = {"FLASH": 30, "LATENCY": 15, "SNIPE": 60, "ARB": 30}.get(strategy, 30)
        if snap_time - last < cd_duration: return False

        sz = self.get_bet_size(strategy, price)
        if sz < 1.0 or sz > self.balance: return False

        shares = sz / price
        self.balance -= sz
        self.open_positions.append({
            "strategy": strategy, "side": side, "price": price,
            "size": sz, "shares": shares, "market_id": market["market_id"],
            "winner": market.get("winner"), "entry_time": snap_time
        })
        self.cooldowns[cd_key] = snap_time
        return True

    def resolve_trades(self):
        """Resolve all open positions based on market winner."""
        for pos in self.open_positions:
            winner = pos.get("winner")
            if not winner:
                # Assume loss if no winner data
                self.stats[pos["strategy"]]["losses"] += 1
                self.stats[pos["strategy"]]["pnl"] -= pos["size"]
                self.stats[pos["strategy"]]["trades"] += 1
                self.trades.append({**pos, "result": "LOSS", "pnl": -pos["size"]})
                continue

            # winner is "UP" or "DOWN"
            won = (pos["side"] == "YES" and winner.upper() in ("UP", "YES")) or \
                  (pos["side"] == "NO" and winner.upper() in ("DOWN", "NO"))

            if won:
                payout = pos["shares"] * 1.0  # $1 per share
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
        """Run the backtest."""
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║       POLYMARKET BOT v9.5 BACKTESTER                       ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")
        print(f"  Starting balance: ${STARTING_BALANCE:.2f}")
        print(f"  Backtest period: {BACKTEST_DAYS} days")
        print(f"  Market types: {MARKET_TYPES}")
        print(f"  Strategies: FLASH, LATENCY, SNIPE, ARB\n")

        total_markets = 0
        total_signals = 0

        for mtype in MARKET_TYPES:
            print(f"\n{'='*60}")
            print(f"  Processing {mtype} markets...")
            print(f"{'='*60}")

            # Fetch all resolved markets
            offset = 0
            markets = []
            while True:
                data = self.api.get_markets(mtype, resolved=True, limit=100, offset=offset)
                if not data or not data.get("markets"):
                    break
                batch = data["markets"]
                markets.extend(batch)
                print(f"  Fetched {len(markets)}/{data.get('total', '?')} markets...")
                if len(batch) < 100:
                    break
                offset += 100
                if len(markets) > 2000:  # safety cap
                    break

            # Filter to BTC markets in our time range
            cutoff = datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS)
            btc_markets = []
            for m in markets:
                slug = m.get("slug", "")
                if "btc" not in slug.lower() and "bitcoin" not in slug.lower():
                    continue
                try:
                    end_time = datetime.fromisoformat(m["end_time"].replace("Z", "+00:00"))
                    if end_time < cutoff:
                        continue
                except:
                    continue
                btc_markets.append(m)

            print(f"  BTC markets in range: {len(btc_markets)}")
            total_markets += len(btc_markets)

            # Process each market
            for i, market in enumerate(btc_markets):
                if i % 50 == 0 and i > 0:
                    print(f"  Processing market {i}/{len(btc_markets)}... Balance: ${self.balance:.2f}")

                mid = market["market_id"]
                snaps_data = self.api.get_snapshots(mid, limit=1000)
                if not snaps_data or not snaps_data.get("snapshots"):
                    continue

                snapshots = snaps_data["snapshots"]
                end_time = datetime.fromisoformat(market["end_time"].replace("Z", "+00:00"))

                # Reset per-market state
                traded_this_market = set()  # track which strategies fired

                for snap in snapshots:
                    try:
                        snap_time_dt = datetime.fromisoformat(snap["time"].replace("Z", "+00:00"))
                        snap_ts = snap_time_dt.timestamp()
                        time_left = (end_time - snap_time_dt).total_seconds()
                        market_age_total = (datetime.fromisoformat(market["end_time"].replace("Z", "+00:00")) -
                                          datetime.fromisoformat(market["start_time"].replace("Z", "+00:00"))).total_seconds()
                        market_age = market_age_total - time_left
                    except:
                        continue

                    # Update BTC tracker
                    btc_price = snap.get("btc_price")
                    if btc_price:
                        self.btc.update(snap_ts, btc_price)

                    # Skip if we already have max positions
                    if len(self.open_positions) >= MAX_POSITIONS:
                        continue

                    # Check strategies (priority order)
                    signals = []

                    # ARB
                    sig = check_arb(snap)
                    if sig and "ARB" not in traded_this_market:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # LATENCY
                    sig = check_latency(snap, self.btc, time_left)
                    if sig and "LATENCY" not in traded_this_market:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # FLASH
                    sig = check_flash(snap, self.btc, time_left, market_age)
                    if sig and "FLASH" not in traded_this_market:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # SNIPE (5m only, last 45s)
                    sig = check_snipe(snap, self.btc, time_left, mtype)
                    if sig and "SNIPE" not in traded_this_market:
                        if would_maker_fill(snap, sig["side"], sig["price"]):
                            signals.append(sig)

                    # Execute first valid signal
                    for sig in signals:
                        if self.open_trade(sig, market, snap_ts):
                            traded_this_market.add(sig["strategy"])
                            total_signals += 1
                            break

                # Resolve all positions at end of market
                self.resolve_trades()

        # ═══ RESULTS ═══
        self.print_results(total_markets, total_signals)

    def print_results(self, total_markets, total_signals):
        print("\n\n")
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║                    BACKTEST RESULTS                         ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")

        print(f"  Markets analyzed:  {total_markets}")
        print(f"  Total signals:     {total_signals}")
        print(f"  Total trades:      {len(self.trades)}")
        print(f"  Starting balance:  ${STARTING_BALANCE:.2f}")
        print(f"  Final balance:     ${self.balance:.2f}")
        total_pnl = self.balance - STARTING_BALANCE
        print(f"  Total P&L:         ${total_pnl:+.2f} ({total_pnl/STARTING_BALANCE*100:+.1f}%)")
        print(f"  API calls:         {self.api._calls}")

        # Per-strategy breakdown
        print(f"\n{'─'*60}")
        print(f"  {'Strategy':<12} {'Trades':>7} {'Wins':>6} {'Losses':>7} {'WR':>6} {'P&L':>10}")
        print(f"{'─'*60}")

        total_wins = 0; total_losses = 0
        for strat in ["FLASH", "LATENCY", "SNIPE", "ARB"]:
            s = self.stats[strat]
            trades = s["trades"]
            wins = s["wins"]; losses = s["losses"]
            total_wins += wins; total_losses += losses
            wr = wins / trades * 100 if trades > 0 else 0
            pnl = s["pnl"]
            print(f"  {strat:<12} {trades:>7} {wins:>6} {losses:>7} {wr:>5.1f}% ${pnl:>+9.2f}")

        all_trades = total_wins + total_losses
        overall_wr = total_wins / all_trades * 100 if all_trades > 0 else 0
        print(f"{'─'*60}")
        print(f"  {'TOTAL':<12} {all_trades:>7} {total_wins:>6} {total_losses:>7} {overall_wr:>5.1f}% ${total_pnl:>+9.2f}")

        # By side
        print(f"\n{'─'*60}")
        print(f"  BY SIDE:")
        yes_trades = [t for t in self.trades if t["side"] == "YES"]
        no_trades = [t for t in self.trades if t["side"] == "NO"]
        yes_wins = sum(1 for t in yes_trades if t["result"] == "WIN")
        no_wins = sum(1 for t in no_trades if t["result"] == "WIN")
        yes_pnl = sum(t["pnl"] for t in yes_trades)
        no_pnl = sum(t["pnl"] for t in no_trades)
        yes_wr = yes_wins / len(yes_trades) * 100 if yes_trades else 0
        no_wr = no_wins / len(no_trades) * 100 if no_trades else 0
        print(f"  YES: {len(yes_trades)} trades, {yes_wins}W, {yes_wr:.1f}% WR, ${yes_pnl:+.2f}")
        print(f"  NO:  {len(no_trades)} trades, {no_wins}W, {no_wr:.1f}% WR, ${no_pnl:+.2f}")

        # By entry price bucket
        print(f"\n{'─'*60}")
        print(f"  BY ENTRY PRICE:")
        buckets = [(0, 0.20, "<$0.20"), (0.20, 0.30, "$0.20-0.30"),
                   (0.30, 0.50, "$0.30-0.50"), (0.50, 1.0, "$0.50+")]
        for lo, hi, label in buckets:
            bucket_trades = [t for t in self.trades if lo <= t["price"] < hi]
            if not bucket_trades: continue
            bw = sum(1 for t in bucket_trades if t["result"] == "WIN")
            bp = sum(t["pnl"] for t in bucket_trades)
            bwr = bw / len(bucket_trades) * 100
            print(f"  {label:>12}: {len(bucket_trades)} trades, {bw}W, {bwr:.1f}% WR, ${bp:+.2f}")

        # Equity curve summary
        print(f"\n{'─'*60}")
        print(f"  EQUITY CURVE:")
        equity = STARTING_BALANCE
        peak = equity
        max_dd = 0
        for t in self.trades:
            equity += t["pnl"]
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100
            max_dd = max(max_dd, dd)
        print(f"  Max drawdown: {max_dd:.1f}%")
        print(f"  Sharpe estimate: {total_pnl / max(max_dd * STARTING_BALANCE / 100, 1):.2f}")

        # Save detailed results
        results = {
            "config": {
                "starting_balance": STARTING_BALANCE,
                "backtest_days": BACKTEST_DAYS,
                "market_types": MARKET_TYPES,
                "max_positions": MAX_POSITIONS,
            },
            "summary": {
                "final_balance": self.balance,
                "total_pnl": total_pnl,
                "total_trades": len(self.trades),
                "win_rate": overall_wr,
                "max_drawdown": max_dd,
            },
            "per_strategy": dict(self.stats),
            "trades": self.trades[:200],  # first 200 trades for analysis
        }

        with open("backtest_results.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n  Detailed results saved to: backtest_results.json")
        print(f"{'═'*60}\n")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    bt = Backtester()
    bt.run()
