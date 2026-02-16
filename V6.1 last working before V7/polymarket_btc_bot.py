"""
POLYMARKET BTC BOT v6 — INTELLIGENT TRADING ENGINE
Built on v5 proxy wallet infrastructure + new intelligence layer.

NEW IN v6:
1. Trend Engine — shared brain classifies market regime every tick
2. Adaptive Sizing — auto-adjusts trade sizes based on win rate per strategy
3. Loss Streak Protection — pauses strategies after consecutive losses
4. Time-of-Day Awareness — tracks which hours are profitable
5. Volatility Regime — adjusts behavior in low/high vol environments
6. Trend-Aware Strategies — only trades in direction of trend
7. Confirmation Delay — waits for BTC to confirm move before entering
8. Smart Flash — only buys dips that align with trend

pip install py-clob-client python-dotenv requests numpy colorama web3
"""
import os, sys, time, json, logging, traceback, math
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from collections import deque
from typing import Optional
import requests, numpy as np
from dotenv import load_dotenv
load_dotenv()

try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "colorama", "-q"])
    from colorama import init, Fore, Back, Style
    init(autoreset=True)

H1=Fore.CYAN+Style.BRIGHT; H2=Fore.MAGENTA+Style.BRIGHT; LBL=Fore.WHITE+Style.BRIGHT
VAL=Fore.YELLOW+Style.BRIGHT; OK=Fore.GREEN+Style.BRIGHT; ERR=Fore.RED+Style.BRIGHT
WARN=Fore.YELLOW; BTC=Fore.YELLOW+Style.BRIGHT; MKT=Fore.CYAN; STRAT=Fore.MAGENTA
TRAD=Fore.BLUE+Style.BRIGHT; POS=Fore.CYAN+Style.BRIGHT; EVT=Fore.WHITE+Style.DIM
DIM=Fore.WHITE+Style.DIM; R=Style.RESET_ALL

def pnl_c2(v):
    if v > 0: return f"{OK}+${v:.2f}{R}"
    if v < 0: return f"{ERR}-${abs(v):.2f}{R}"
    return f"${v:.2f}"
def bal_c(v): return f"{OK}${v:.6f}{R}"

# ─── CONFIG ───
@dataclass
class Config:
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 1
    dry_run: bool = False
    starting_balance: float = 24.0
    # Percentage-based sizing (% of current balance per trade)
    arb_pct: float = 0.05        # 5% of balance
    latency_pct: float = 0.06    # 6% of balance
    momentum_pct: float = 0.05   # 5% of balance
    flash_pct: float = 0.04      # 4% of balance
    # Legacy fixed sizes (used as fallback if pct = 0)
    arb_size: float = 2.0
    latency_size: float = 2.5
    momentum_size: float = 2.0
    flash_size: float = 2.0
    # Strategy config
    arb_enabled: bool = True
    arb_max_pair_cost: float = 0.99
    latency_enabled: bool = True
    latency_threshold: float = 0.003
    latency_min_edge: float = 0.08
    momentum_enabled: bool = True
    momentum_conf: float = 0.65
    flash_enabled: bool = True
    flash_threshold: float = 0.30
    squeeze_enabled: bool = True
    squeeze_pct: float = 0.05     # 5% of balance
    max_daily_loss: float = 9999.0
    max_positions: int = 7
    poll_sec: int = 2
    assets: list = field(default_factory=lambda: ["btc"])
    # v6: Adaptive settings
    min_trades_to_adapt: int = 15
    max_size_multiplier: float = 2.5
    min_size_multiplier: float = 0.3
    streak_pause_count: int = 3
    streak_pause_sec: int = 1800

    def get_base_size(s, strat, balance):
        """Get base trade size — percentage of balance or fixed fallback."""
        pct_map = {"ARB": s.arb_pct, "LATENCY": s.latency_pct,
                   "MOMENTUM": s.momentum_pct, "FLASH": s.flash_pct,
                   "SQUEEZE": s.squeeze_pct}
        fixed_map = {"ARB": s.arb_size, "LATENCY": s.latency_size,
                     "MOMENTUM": s.momentum_size, "FLASH": s.flash_size}
        pct = pct_map.get(strat, 0.05)
        if pct > 0:
            return round(balance * pct, 2)
        return fixed_map.get(strat, 2.0)

    @classmethod
    def from_env(cls):
        pk = os.getenv("PRIVATE_KEY", "")
        clean = pk[2:] if pk.startswith("0x") else pk
        return cls(
            private_key=clean,
            funder_address=os.getenv("FUNDER_ADDRESS", ""),
            signature_type=int(os.getenv("SIGNATURE_TYPE", "1")),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            starting_balance=float(os.getenv("STARTING_BALANCE", "24.0")),
            arb_pct=float(os.getenv("ARB_PCT", "0.05")),
            latency_pct=float(os.getenv("LATENCY_PCT", "0.06")),
            momentum_pct=float(os.getenv("MOMENTUM_PCT", "0.05")),
            flash_pct=float(os.getenv("FLASH_PCT", "0.04")),
            arb_size=float(os.getenv("ARB_SIZE", "2.0")),
            latency_size=float(os.getenv("LATENCY_SIZE", "2.5")),
            momentum_size=float(os.getenv("MOMENTUM_SIZE", "2.0")),
            flash_size=float(os.getenv("FLASH_SIZE", "2.0")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "9999.0")),
        )

log = logging.getLogger("Bot"); log.setLevel(logging.DEBUG)
from logging.handlers import RotatingFileHandler
_fh = RotatingFileHandler("polybot.log", maxBytes=5*1024*1024, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s|%(levelname)s|%(message)s"))
log.addHandler(_fh)

# ─── DATA CLASSES ───
@dataclass
class Market:
    slug: str; cid: str; question: str
    tok_yes: str; tok_no: str; end: datetime
    yes_p: float = 0.5; no_p: float = 0.5
    active: bool = True; open_btc: float = 0.0

@dataclass
class Pos:
    id: str; strat: str; slug: str; side: str
    entry: float; shares: float; cost: float
    pnl: float = 0.0; opened: datetime = None; status: str = "OPEN"
    market_end: datetime = None

@dataclass
class Trd:
    ts: datetime; strat: str; slug: str; side: str
    price: float; size: float; pnl: float = 0.0; oid: str = ""

class Conn:
    def __init__(s):
        s.gamma = "..."; s.clob = "..."; s.auth = "..."
        s.binance = "..."; s.can_trade = False; s.errors = []
    def err(s, e):
        s.errors.append(str(e)[:60])
        if len(s.errors) > 5: s.errors = s.errors[-5:]

# ─── PRICE FEED ───
class Feed:
    def __init__(s):
        s.data = deque(maxlen=500)
        s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/6"
    def poll(s):
        for fn in [s._b, s._c]:
            try:
                p = fn()
                if p: s.data.append({"t": time.time(), "p": p}); return p
            except: continue
        return None
    def _b(s):
        r = s.s.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=3)
        return float(r.json()["price"])
    def _c(s):
        r = s.s.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=3)
        return float(r.json()["data"]["amount"])
    @property
    def price(s): return s.data[-1]["p"] if s.data else 0
    @property
    def n(s): return len(s.data)
    def arr(s, n=50):
        d = list(s.data)[-n:]
        return np.array([x["p"] for x in d]) if d else np.array([])
    def chg(s, sec=60):
        if len(s.data) < 2: return 0
        now = s.data[-1]; cut = now["t"] - sec; old = s.data[0]
        for p in s.data:
            if p["t"] >= cut: old = p; break
        return (now["p"] - old["p"]) / old["p"] if old["p"] else 0
    def volatility(s, sec=300):
        p = s.arr(min(150, s.n))
        if len(p) < 10: return 0
        returns = np.diff(np.log(p))
        return float(np.std(returns) * np.sqrt(len(returns)))
    def vwap_trend(s, sec=120):
        if len(s.data) < 10: return 0
        cut = time.time() - sec
        prices = [(d["p"], d["t"]) for d in s.data if d["t"] >= cut]
        if len(prices) < 5: return 0
        vwap = np.mean([p for p, _ in prices])
        return (prices[-1][0] - vwap) / vwap
    def consecutive_direction(s, n=5):
        """Count consecutive candles in same direction. Returns (+n, -n, or 0)."""
        if len(s.data) < n + 1: return 0
        recent = list(s.data)[-n-1:]
        ups = 0; downs = 0
        for i in range(1, len(recent)):
            if recent[i]["p"] > recent[i-1]["p"]: ups += 1; downs = 0
            elif recent[i]["p"] < recent[i-1]["p"]: downs += 1; ups = 0
            else: ups = 0; downs = 0
        if ups >= n: return ups
        if downs >= n: return -downs
        return 0

# ═══════════════════════════════════════════════════════════════
# ─── v6: TREND ENGINE — THE SHARED BRAIN ───
# ═══════════════════════════════════════════════════════════════
class TrendEngine:
    """Classifies market into regimes and provides trend data to all strategies."""
    REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "CHOPPY", "BREAKOUT", "FLAT"]

    def __init__(s):
        s.regime = "FLAT"
        s.trend_strength = 0.0      # -1.0 to +1.0
        s.volatility_regime = "NORMAL"  # LOW, NORMAL, HIGH
        s.vol_level = 0.0
        s.chg_1m = 0.0
        s.chg_5m = 0.0
        s.chg_15m = 0.0
        s.consecutive = 0
        s.ema_fast = 0.0
        s.ema_slow = 0.0
        s.rsi = 50.0
        s.trend_dir = 0             # +1 up, -1 down, 0 neutral
        s._last_update = 0

    def update(s, f: Feed):
        """Update trend state from latest feed data. Call every tick."""
        if time.time() - s._last_update < 1.0: return  # max 1 update/sec
        s._last_update = time.time()
        if f.n < 15: return

        # Price changes over multiple timeframes
        s.chg_1m = f.chg(60)
        s.chg_5m = f.chg(300)
        s.chg_15m = f.chg(900)

        # Volatility — calibrated for BTC 2-sec polling over 5 min
        s.vol_level = f.volatility(300)
        if s.vol_level < 0.0008: s.volatility_regime = "LOW"
        elif s.vol_level > 0.003: s.volatility_regime = "HIGH"
        else: s.volatility_regime = "NORMAL"

        # Consecutive direction
        s.consecutive = f.consecutive_direction(5)

        # EMA trend
        p = f.arr(60)
        if len(p) >= 21:
            s.ema_fast = s._ema(p, 8)
            s.ema_slow = s._ema(p, 21)

        # RSI
        if len(p) >= 15:
            s.rsi = s._rsi(p, 14)

        # Classify regime
        s._classify(f)

    def _classify(s, f):
        """Determine market regime from all signals."""
        # Trend strength: combination of timeframe changes
        raw = s.chg_1m * 3.0 + s.chg_5m * 2.0 + s.chg_15m * 1.0
        s.trend_strength = float(np.clip(raw * 100, -1.0, 1.0))

        # Trend direction
        if s.trend_strength > 0.15: s.trend_dir = 1
        elif s.trend_strength < -0.15: s.trend_dir = -1
        else: s.trend_dir = 0

        # Regime classification
        abs_str = abs(s.trend_strength)

        if s.volatility_regime == "LOW" and abs_str < 0.10:
            s.regime = "FLAT"
        elif s.volatility_regime == "HIGH" and abs_str > 0.40:
            s.regime = "BREAKOUT"
        elif abs_str > 0.25:
            s.regime = "TRENDING_UP" if s.trend_dir > 0 else "TRENDING_DOWN"
        elif s.volatility_regime == "HIGH" and abs_str < 0.15:
            s.regime = "CHOPPY"
        elif abs_str > 0.10:
            s.regime = "TRENDING_UP" if s.trend_dir > 0 else "TRENDING_DOWN"
        else:
            s.regime = "FLAT"

    def should_trade(s, strat, side_is_yes):
        """Returns (should_trade: bool, size_multiplier: float) based on regime."""
        # ARB is always regime-agnostic
        if strat == "ARB": return True, 1.0

        # LATENCY: edge is SPEED, not trend. Allow in ALL regimes.
        # Boost with trend, reduce against trend, but never block.
        if strat == "LATENCY":
            if s.regime == "FLAT": return True, 0.8
            if s.regime == "CHOPPY": return True, 0.6
            if s.regime in ("TRENDING_UP", "TRENDING_DOWN"):
                trend_up = s.regime == "TRENDING_UP"
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                if with_trend:
                    bonus = min(abs(s.trend_strength) * 2, 0.5)
                    return True, 1.0 + bonus
                else:
                    return True, 0.5  # reduced but not blocked
            if s.regime == "BREAKOUT":
                trend_up = s.trend_dir > 0
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                return True, 1.5 if with_trend else 0.4
            return True, 1.0

        # FLASH: dip buying works in most regimes
        if strat == "FLASH":
            if s.regime == "FLAT": return True, 0.7
            if s.regime == "CHOPPY": return True, 0.5
            if s.regime in ("TRENDING_UP", "TRENDING_DOWN"):
                trend_up = s.regime == "TRENDING_UP"
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                return True, 1.0 if with_trend else 0.4
            if s.regime == "BREAKOUT":
                trend_up = s.trend_dir > 0
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                return True, 1.3 if with_trend else 0.3
            return True, 0.7

        # MOMENTUM/SQUEEZE: needs some directional signal, but don't fully block in FLAT
        if strat in ("MOMENTUM", "SQUEEZE"):
            if s.regime == "FLAT": return True, 0.5  # was blocked, now allowed at half size
            if s.regime == "CHOPPY": return False, 0.0  # still too risky in chop
            if s.regime in ("TRENDING_UP", "TRENDING_DOWN"):
                trend_up = s.regime == "TRENDING_UP"
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                if with_trend:
                    bonus = min(abs(s.trend_strength) * 2, 0.5)
                    return True, 1.0 + bonus
                else:
                    return False, 0.0  # never trade against trend with momentum
            if s.regime == "BREAKOUT":
                trend_up = s.trend_dir > 0
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                return (True, 1.5) if with_trend else (False, 0.0)

        return True, 1.0

    def display_str(s):
        """Compact string for dashboard display."""
        arrows = {"TRENDING_UP": "↑↑", "TRENDING_DOWN": "↓↓", "CHOPPY": "↕↕",
                  "BREAKOUT": "⚡", "FLAT": "──"}
        arrow = arrows.get(s.regime, "??")
        vol_icon = {"LOW": "▁", "NORMAL": "▃", "HIGH": "▇"}
        vi = vol_icon.get(s.volatility_regime, "?")
        return f"{arrow} {s.regime:12} str:{s.trend_strength:+.2f} vol:{vi}{s.volatility_regime:6} RSI:{s.rsi:.0f}"

    @staticmethod
    def _ema(p, n):
        k = 2 / (n + 1); e = p[0]
        for x in p[1:]: e = x * k + e * (1 - k)
        return e

    @staticmethod
    def _rsi(p, n):
        if len(p) < n + 1: return 50
        d = np.diff(p[-n-1:])
        g = np.mean(np.maximum(d, 0)); l = np.mean(np.maximum(-d, 0))
        return 100 if l == 0 else 100 - 100 / (1 + g / l)


# ═══════════════════════════════════════════════════════════════
# ─── v6: ADAPTIVE SIZING ENGINE ───
# ═══════════════════════════════════════════════════════════════
class AdaptiveSizer:
    """Adjusts trade sizes based on per-strategy performance."""
    DATA_FILE = "trade_data.json"

    def __init__(s, c):
        s.c = c
        s.history = []          # all trade records
        s.multipliers = {"ARB": 1.0, "LATENCY": 1.0, "MOMENTUM": 1.0, "FLASH": 1.0, "SQUEEZE": 1.0}
        s.paused_until = {"ARB": 0, "LATENCY": 0, "MOMENTUM": 0, "FLASH": 0, "SQUEEZE": 0}
        s.streaks = {"ARB": 0, "LATENCY": 0, "MOMENTUM": 0, "FLASH": 0, "SQUEEZE": 0}  # negative = losses
        s.side_streaks = {"YES": 0, "NO": 0}  # track per-side streaks
        s.hourly_stats = {}     # {hour: {wins, losses, pnl}}
        s._load()

    def _load(s):
        """Load historical trade data from file."""
        try:
            if os.path.exists(s.DATA_FILE):
                with open(s.DATA_FILE, "r") as f:
                    data = json.load(f)
                s.history = data.get("history", [])
                s.hourly_stats = data.get("hourly_stats", {})
                s._recalculate()
                log.info(f"Loaded {len(s.history)} historical trades for adaptive sizing")
        except: pass

    def _save(s):
        """Save trade data to file."""
        try:
            with open(s.DATA_FILE, "w") as f:
                json.dump({"history": s.history[-500:], "hourly_stats": s.hourly_stats}, f)
        except: pass

    def record(s, strat, side, won, pnl, price, hour, regime="UNKNOWN"):
        """Record a completed trade and recalculate."""
        s.history.append({
            "strat": strat, "side": side, "won": won, "pnl": pnl,
            "price": price, "hour": hour, "regime": regime, "ts": time.time()
        })
        # Update streaks
        if won:
            s.streaks[strat] = max(0, s.streaks.get(strat, 0)) + 1
            s.side_streaks[side] = max(0, s.side_streaks.get(side, 0)) + 1
        else:
            s.streaks[strat] = min(0, s.streaks.get(strat, 0)) - 1
            s.side_streaks[side] = min(0, s.side_streaks.get(side, 0)) - 1
            # Check for pause
            if abs(s.streaks[strat]) >= s.c.streak_pause_count:
                s.paused_until[strat] = time.time() + s.c.streak_pause_sec
                log.info(f"PAUSED {strat} for {s.c.streak_pause_sec}s after {abs(s.streaks[strat])} losses")
        # Update hourly stats
        h = str(hour)
        if h not in s.hourly_stats:
            s.hourly_stats[h] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if won: s.hourly_stats[h]["wins"] += 1
        else: s.hourly_stats[h]["losses"] += 1
        s.hourly_stats[h]["pnl"] += pnl
        s._recalculate()
        s._save()

    def _recalculate(s):
        """Recalculate size multipliers from history."""
        if len(s.history) < s.c.min_trades_to_adapt: return
        for strat in ["ARB", "LATENCY", "MOMENTUM", "FLASH", "SQUEEZE"]:
            # Use last 30 trades for this strategy
            recent = [t for t in s.history if t["strat"] == strat][-30:]
            if len(recent) < 5:
                s.multipliers[strat] = 1.0
                continue
            wins = sum(1 for t in recent if t["won"])
            total = len(recent)
            wr = wins / total
            avg_pnl = sum(t["pnl"] for t in recent) / total
            # Calculate multiplier
            if wr >= 0.70:
                s.multipliers[strat] = min(s.c.max_size_multiplier, 1.0 + (wr - 0.50) * 3.0)
            elif wr >= 0.55:
                s.multipliers[strat] = 1.0 + (wr - 0.55) * 2.0
            elif wr >= 0.45:
                s.multipliers[strat] = 1.0
            elif wr >= 0.35:
                s.multipliers[strat] = max(s.c.min_size_multiplier, 0.5)
            else:
                s.multipliers[strat] = s.c.min_size_multiplier
            # Boost if avg_pnl is positive
            if avg_pnl > 0.5:
                s.multipliers[strat] = min(s.c.max_size_multiplier, s.multipliers[strat] * 1.2)

    def get_size(s, strat, base_size, balance, start_bal=None, same_strat_count=0):
        """Get adjusted trade size.
        base_size already scales with balance (percentage-based).
        same_strat_count: diminishes for same strategy stacking."""
        if time.time() < s.paused_until.get(strat, 0):
            return 0
        mult = s.multipliers.get(strat, 1.0)
        size = base_size * mult
        # Same strategy stacking: diminish 100% / 60% / 40% / blocked
        if same_strat_count > 0:
            stack_mult = [1.0, 0.6, 0.4]
            if same_strat_count < len(stack_mult):
                size *= stack_mult[same_strat_count]
            else:
                return 0  # max 3 per strategy per market
        # Never exceed 12% of balance per position
        max_size = balance * 0.12
        size = min(size, max_size)
        return max(1.0, round(size, 2))

    def is_paused(s, strat):
        return time.time() < s.paused_until.get(strat, 0)

    def pause_remaining(s, strat):
        r = s.paused_until.get(strat, 0) - time.time()
        return max(0, int(r))

    def is_side_cold(s, side):
        """Is this side (YES/NO) on a losing streak?"""
        return s.side_streaks.get(side, 0) <= -5

    def is_good_hour(s):
        """Is the current hour historically profitable?"""
        h = str(datetime.now(timezone.utc).hour)
        stats = s.hourly_stats.get(h)
        if not stats: return True  # no data, assume OK
        total = stats["wins"] + stats["losses"]
        if total < 5: return True  # not enough data
        wr = stats["wins"] / total
        return wr >= 0.35  # only skip if historically terrible

    def display_str(s):
        parts = []
        for strat in ["ARB", "LATENCY", "MOMENTUM", "FLASH", "SQUEEZE"]:
            m = s.multipliers[strat]
            if s.is_paused(strat):
                parts.append(f"{strat[:3]}:PAUSED")
            elif m > 1.1:
                parts.append(f"{strat[:3]}:x{m:.1f}↑")
            elif m < 0.9:
                parts.append(f"{strat[:3]}:x{m:.1f}↓")
            else:
                parts.append(f"{strat[:3]}:x{m:.1f}")
        return "  ".join(parts)


# ═══════════════════════════════════════════════════════════════
# ─── v6: CONFIRMATION ENGINE ───
# ═══════════════════════════════════════════════════════════════
class ConfirmationEngine:
    """Waits for BTC to confirm a move before entering."""
    def __init__(s):
        s.pending = {}  # {strat: {"signal": sig, "btc_at_signal": price, "time": t}}

    def submit(s, strat, signal, btc_price):
        """Submit a signal for confirmation. Returns True if already confirmed."""
        key = f"{strat}_{signal.get('dir', '')}"
        if key in s.pending:
            p = s.pending[key]
            elapsed = time.time() - p["time"]
            if elapsed > 15: del s.pending[key]; return False  # too old
            # Check if BTC continued in the signal direction
            if signal.get("dir") == "YES":
                confirmed = btc_price > p["btc_at_signal"]
            else:
                confirmed = btc_price < p["btc_at_signal"]
            if confirmed:
                del s.pending[key]
                return True
            if elapsed > 10:
                del s.pending[key]  # didn't confirm in time
                return False
            return False  # waiting
        else:
            # First time seeing this signal — register it
            s.pending[key] = {"signal": signal, "btc_at_signal": btc_price, "time": time.time()}
            return False  # needs confirmation next tick


# ═══════════════════════════════════════════════════════════════
# ─── STRATEGIES (v7: Research-backed for Polymarket 15-min BTC) ───
# Sources: gabagool ($58/trade), $313→$414K latency bot (98% WR),
# "Efficient Coder" spike bot (86% ROI), Dutch book arbitrage,
# Polymarket taker fee analysis, QuantVPS HFT research
# ═══════════════════════════════════════════════════════════════

class S_Arb:
    """TRUE ARBITRAGE: Buy the cheaper side when YES+NO sum < $0.96.
    Research: gabagool buys BOTH sides at different times when each
    gets temporarily cheap. Pair cost $0.966 → guaranteed $0.034 profit.
    With $48 we can't fully hedge, so we buy the cheaper side only
    when the edge is substantial (4%+ after the 2% winner fee).
    Key: This is the safest strategy — it doesn't predict direction."""
    def __init__(s, c): s.c = c; s.market_slug = None
    def reset(s, slug):
        if s.market_slug != slug: s.market_slug = slug
    def check(s, m, trend):
        if not s.c.arb_enabled: return None
        s.reset(m.slug)
        yp, np_ = m.yes_p, m.no_p
        pair = yp + np_
        # Need sum < $0.96 → 4%+ edge. After 2% winner fee, still 2%+ profit
        if pair >= 0.96: return None
        buy_yes = yp < np_
        price = yp if buy_yes else np_
        # Only buy cheap sides — max $0.38
        if price < 0.08 or price > 0.38: return None
        side = "YES" if buy_yes else "NO"
        return {"s": "ARB", "side": side, "yes": buy_yes, "price": price,
                "pair": pair, "profit": 1.0 - pair, "sz": 0}


class S_Latency:
    """LATENCY ARBITRAGE — The proven $313→$414K strategy (98% WR).
    Research: Bot monitors Binance BTC. When BTC moves 0.15%+ but
    Polymarket hasn't repriced, buys the underpriced side FAST.
    "Enters when actual prob is ~85% but market still shows 50/50."
    
    Polymarket now charges ~3% taker fee at 50c odds. So we MUST
    buy cheap sides (< $0.35) where the fee is lower and R:R is good.
    
    This is our #1 money-maker. Speed IS the edge — no confirmation."""
    def __init__(s, c): s.c = c
    def check(s, m, f, trend):
        if not s.c.latency_enabled or f.n < 10: return None
        # How much has BTC moved vs market open?
        chg_open = 0
        if m.open_btc > 0:
            chg_open = (f.price - m.open_btc) / m.open_btc
        # Also check recent fast moves
        chg_30 = f.chg(30)
        chg_60 = f.chg(60)
        # Use the biggest move signal
        chg = max(chg_open, chg_30, chg_60, key=abs)

        # Need at least 0.10% move
        if abs(chg) < 0.0010: return None

        up = chg > 0
        # The side we want: BTC up → buy YES (if cheap), BTC down → buy NO (if cheap)
        target_price = m.yes_p if up else m.no_p

        # CRITICAL: Only enter when price is reasonable (< $0.45)
        # At $0.45: risk $0.45, reward $0.55 = 1.2:1 risk/reward
        if target_price > 0.45 or target_price < 0.08: return None

        # Other side shouldn't be too cheap (if both cheap → ARB handles it)
        other_price = m.no_p if up else m.yes_p
        if other_price < 0.15: return None

        # Confidence: bigger BTC move = market is more wrong
        confidence = min(0.95, 0.60 + abs(chg) * 100)

        # Trend bonus
        if (up and trend.trend_dir > 0) or (not up and trend.trend_dir < 0):
            confidence = min(0.95, confidence + 0.05)

        # Need 15%+ edge to overcome fees + variance
        edge = confidence - target_price
        if edge < 0.15: return None

        return {"s": "LATENCY", "dir": "YES" if up else "NO", "yes": up,
                "edge": edge, "pred": confidence, "p": target_price, "chg": chg, "sz": 0}


class S_Momentum:
    """BTC VELOCITY DETECTOR — Based on "Efficient Coder" bot (86% ROI).
    Research: Bot monitored for large price drop within 3-second window.
    Turned $1,000 into $1,869 in days. Key: detect BIG FAST moves.
    
    Different from Latency: Latency catches 0.15% moves over 30-60s.
    Velocity catches EXPLOSIVE moves: 0.25%+ in 6-10 seconds.
    These are whale orders, liquidation cascades, news spikes.
    They create massive Polymarket mispricing that lasts 5-15 seconds."""
    def __init__(s, c): s.c = c; s.scores = {}
    def check(s, m, f, trend):
        if not s.c.momentum_enabled or f.n < 10: return None
        # Check multiple fast windows
        chg_6 = f.chg(6)    # 6 seconds (3 ticks)
        chg_10 = f.chg(10)  # 10 seconds (5 ticks)
        chg_20 = f.chg(20)  # 20 seconds (10 ticks)

        # Find the fastest significant move
        moves = [(6, chg_6), (10, chg_10), (20, chg_20)]
        best_window, best_chg = max(moves, key=lambda x: abs(x[1]))

        # Need meaningful move: scaled by window
        thresholds = {6: 0.0015, 10: 0.0020, 20: 0.0025}
        if abs(best_chg) < thresholds.get(best_window, 0.003): return None

        up = best_chg > 0
        target_price = m.yes_p if up else m.no_p

        # Only buy when price is reasonable
        if target_price > 0.45 or target_price < 0.08: return None

        # Velocity score
        velocity = abs(best_chg) / (best_window / 2)
        conf = min(0.95, 0.65 + velocity * 500)
        s.scores = {"velocity": velocity, "chg": best_chg, "window": best_window}

        return {"s": "MOMENTUM", "dir": "YES" if up else "NO", "yes": up,
                "conf": conf, "comp": best_chg, "sig": s.scores, "rsi": 50, "sz": 0}


class S_Flash:
    """ACCUMULATOR — Gabagool-lite for small balances.
    Research: gabagool buys YES when cheap, NO when cheap, at different
    moments. With $1000+ you hedge both sides. With $48 we can't fully
    hedge, but we can buy at extreme risk/reward (3.5:1+).
    
    Rules:
    - Only buy ≤ $0.22 (risk $0.22, reward $0.78 = 3.5:1)  
    - YES cheap → buy if BTC recovering or flat (not crashing more)
    - NO cheap → buy if BTC pulling back or flat (not pumping more)
    - Extreme value: < $0.15 → buy even if conditions aren't perfect
    - Needs 3+ minutes left for price to move back"""
    def __init__(s, c): s.c = c
    def check(s, m, f, trend):
        if not s.c.flash_enabled or f.n < 10: return None
        yes_cheap = 0.05 <= m.yes_p <= 0.28
        no_cheap = 0.05 <= m.no_p <= 0.28
        if not yes_cheap and not no_cheap: return None
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 180: return None

        btc_chg = f.chg(120)  # 2-min direction

        # v6.2 FIX: Don't buy against a strong regime
        # If TRENDING_UP or BREAKOUT with upward trend, don't buy NO
        # If TRENDING_DOWN or BREAKOUT with downward trend, don't buy YES
        regime = trend.regime if trend else ""
        strong_up = regime in ("TRENDING_UP",) or (regime == "BREAKOUT" and trend.trend_dir > 0)
        strong_down = regime in ("TRENDING_DOWN",) or (regime == "BREAKOUT" and trend.trend_dir < 0)

        # YES cheap → BTC dropped → buy YES if any sign of recovery
        if yes_cheap and not strong_down:
            recovering = f.chg(30) > 0
            flat_enough = btc_chg > -0.02
            if recovering or (flat_enough and trend.trend_dir >= 0):
                return {"s": "FLASH", "dir": "YES", "yes": True, "price": m.yes_p, "sz": 0}
            # Extreme value: $0.05-0.15 = great R:R, buy even if BTC still falling
            if 0.05 <= m.yes_p < 0.15 and flat_enough and not strong_down:
                return {"s": "FLASH", "dir": "YES", "yes": True, "price": m.yes_p, "sz": 0}

        # NO cheap → BTC pumped → buy NO if any pullback
        if no_cheap and not strong_up:
            pulling_back = f.chg(30) < 0
            flat_enough = btc_chg < 0.02
            if pulling_back or (flat_enough and trend.trend_dir <= 0):
                return {"s": "FLASH", "dir": "NO", "yes": False, "price": m.no_p, "sz": 0}
            if 0.05 <= m.no_p < 0.15 and flat_enough and not strong_up:
                return {"s": "FLASH", "dir": "NO", "yes": False, "price": m.no_p, "sz": 0}

        return None


class S_Squeeze:
    """LATE GAME — Lottery tickets in the final minutes.
    Research: 15-min markets get volatile near expiry. One side drops
    to $0.05-$0.15 but can spike if BTC reverses last-second.
    Risk $1-2 for potential $5-8 return. Win rate ~15-20% but R:R is 5:1+.
    
    Rules:
    - Only in last 5 minutes (< 300s), not last 60s (too late)
    - Side must be ≤ $0.15 (6:1+ risk/reward)
    - BTC must show ANY sign of turning (30s change in our direction)
    - Small size: 2-3% of balance — it's a lottery ticket
    - 45s cooldown between signals"""
    def __init__(s, c):
        s.c = c
        s.was_squeezing = False
        s.squeeze_count = 0
        s._last_signal_time = 0

    def check(s, m, f, trend):
        if f.n < 10: return None
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()

        # Update squeeze count for dashboard display
        if tl <= 300 and tl > 60:
            s.squeeze_count = int(300 - tl)  # counting up toward expiry
        else:
            s.squeeze_count = 0

        # Only in final 5 minutes, not last 60 seconds
        if tl > 300 or tl < 60: return None

        yes_cheap = 0.05 <= m.yes_p <= 0.20
        no_cheap = 0.05 <= m.no_p <= 0.20
        if not yes_cheap and not no_cheap: return None

        # 45-second cooldown
        if time.time() - s._last_signal_time < 45: return None

        # v6.2 FIX: Don't buy against strong regime
        regime = trend.regime if trend else ""
        strong_up = regime in ("TRENDING_UP",) or (regime == "BREAKOUT" and trend.trend_dir > 0)
        strong_down = regime in ("TRENDING_DOWN",) or (regime == "BREAKOUT" and trend.trend_dir < 0)

        # YES very cheap → buy if BTC turning up (and not strong downtrend)
        if yes_cheap and not strong_down:
            turning_up = f.chg(30) > 0.0003
            if turning_up:
                s._last_signal_time = time.time()
                return {"s": "SQUEEZE", "dir": "YES", "yes": True,
                        "price": m.yes_p, "adx": 0, "di_plus": 0, "di_minus": 0,
                        "mom_value": f.chg(30), "squeeze_count": s.squeeze_count,
                        "fired": True, "sz": 0}

        # NO very cheap → buy if BTC turning down (and not strong uptrend)
        if no_cheap and not strong_up:
            turning_down = f.chg(30) < -0.0003
            if turning_down:
                s._last_signal_time = time.time()
                return {"s": "SQUEEZE", "dir": "NO", "yes": False,
                        "price": m.no_p, "adx": 0, "di_plus": 0, "di_minus": 0,
                        "mom_value": f.chg(30), "squeeze_count": s.squeeze_count,
                        "fired": True, "sz": 0}

        return None


# ─── MARKET FINDER ───
class Finder:
    def __init__(s, c):
        s.c = c; s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/6"; s.cache = {}
    def test(s):
        try:
            r = s.s.get(f"{s.c.gamma_host}/markets", params={"limit": 1}, timeout=10)
            return r.status_code == 200
        except: return False
    def find(s, asset="btc"):
        now = datetime.now(timezone.utc)
        mb = (now.minute // 15) * 15
        base = now.replace(minute=mb, second=0, microsecond=0)
        for off in [0, -15, 15, -30]:
            ts = int((base + timedelta(minutes=off)).timestamp())
            m = s._get(f"{asset}-updown-15m-{ts}")
            if m and m.active:
                tl = (m.end - now).total_seconds()
                if tl > 30: s.cache[asset] = m; return m
        cached = s.cache.get(asset)
        if cached:
            tl = (cached.end - now).total_seconds()
            if tl > 30 and cached.active: return cached
        return None
    def _get(s, slug):
        try:
            r = s.s.get(f"{s.c.gamma_host}/markets", params={"slug": slug}, timeout=8)
            if r.status_code != 200: return None
            d = r.json()
            if isinstance(d, list): d = d[0] if d else None
            if not d or not (d.get("condition_id") or d.get("conditionId")): return None
            return s._parse(d)
        except: return None
    def _parse(s, d):
        try:
            tok = d.get("clobTokenIds") or d.get("clob_token_ids") or ""
            if isinstance(tok, str): tok = json.loads(tok) if tok.startswith("[") else tok.split(",")
            if not tok or len(tok) < 2: return None
            pr = d.get("outcomePrices") or d.get("outcome_prices") or ""
            if isinstance(pr, str):
                try: pr = json.loads(pr)
                except: pr = [0.5, 0.5]
            ed = d.get("endDate") or d.get("end_date_iso") or ""
            try: et = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            except: et = datetime.now(timezone.utc) + timedelta(minutes=15)
            return Market(slug=d.get("slug", ""), cid=d.get("condition_id") or d.get("conditionId", ""),
                question=d.get("question", ""), tok_yes=tok[0].strip().strip('"'),
                tok_no=tok[1].strip().strip('"'), end=et,
                yes_p=float(pr[0]) if pr else 0.5, no_p=float(pr[1]) if len(pr) > 1 else 0.5,
                active=not d.get("closed", False))
        except: return None

# ─── EXECUTOR (same as v5) ───
class Executor:
    def __init__(s, c): s.c = c; s.client = None; s.authed = False; s._signer_addr = None
    def _get_signer_addr(s):
        if s._signer_addr: return s._signer_addr
        try:
            from eth_account import Account
            pk = s.c.private_key
            if not pk.startswith("0x"): pk = "0x" + pk
            s._signer_addr = Account.from_key(pk).address
        except: pass
        return s._signer_addr
    def test_public(s):
        try:
            from py_clob_client.client import ClobClient
            tmp = ClobClient(s.c.clob_host)
            return str(tmp.get_ok()).upper() in ["OK", "TRUE"]
        except: return False
    def connect(s, conn):
        from py_clob_client.client import ClobClient
        pk = s.c.private_key
        if not pk: conn.auth = "NO KEY"; return False
        types = [s.c.signature_type] + [t for t in [0, 1, 2] if t != s.c.signature_type]
        for st in types:
            try:
                kw = {"host": s.c.clob_host, "key": pk, "chain_id": s.c.chain_id, "signature_type": st}
                if s.c.funder_address and st in [1, 2]:
                    kw["funder"] = s.c.funder_address
                client = ClobClient(**kw)
                creds = client.derive_api_key()
                client.set_api_creds(creds)
                client.get_ok()
                s.client = client
                s.authed = True; s._auth_type = st
                conn.auth = f"OK (type={st})"; conn.can_trade = True
                log.info(f"Auth OK type={st} funder={s.c.funder_address}")
                return True
            except Exception as e:
                conn.err(f"type={st}: {str(e)[:40]}")
        conn.auth = "FAILED"; return False
    def get_balance(s):
        if s.authed:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                r = s.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                if isinstance(r, dict) and "balance" in r:
                    b = int(r["balance"]) / 1e6
                    if b > 0: return b
            except: pass
        if s.c.funder_address:
            bal = s._check_usdc(s.c.funder_address)
            if bal is not None and bal > 0: return bal
        signer = s._get_signer_addr()
        if signer:
            bal = s._check_usdc(signer)
            if bal is not None and bal > 0: return bal
        return None
    def _check_usdc(s, addr):
        try:
            usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            clean = addr.lower().replace("0x", "").zfill(64)
            data = "0x70a08231" + clean
            for rpc in ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]:
                try:
                    r = requests.post(rpc, json={"jsonrpc": "2.0", "method": "eth_call",
                        "params": [{"to": usdc, "data": data}, "latest"], "id": 1}, timeout=5)
                    if r.status_code == 200:
                        result = r.json().get("result", "0x0")
                        if result and result != "0x": return int(result, 16) / 1e6
                except: continue
        except: pass
        return None
    def order(s, market, is_yes, price, size):
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        label = "YES" if is_yes else "NO"
        if price < 0.01 or price > 0.95: return None, None
        dollar_amount = round(price * size, 2)
        if dollar_amount < 0.50: dollar_amount = 0.50
        if s.c.dry_run:
            oid = f"DRY-{int(time.time()*1000)%99999}"
            log.info(f"DRY: ${dollar_amount:.2f} {label}")
            return oid, None
        if not s.authed: return None, None
        tid = market.tok_yes if is_yes else market.tok_no
        try:
            market_order = MarketOrderArgs(token_id=tid, amount=dollar_amount, side=BUY)
            signed = s.client.create_market_order(market_order)
            resp = s.client.post_order(signed, OrderType.FOK)
            if isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("id") or resp.get("order_id") or "?"
                status = resp.get("status", "")
                actual_shares = None
                taking = resp.get("takingAmount")
                if taking:
                    try:
                        val = float(taking)
                        actual_shares = val / 1e6 if val > 1000 else val
                    except: pass
                log.info(f"MARKET ORDER: ${dollar_amount:.2f} {label} id={oid} st={status} shares={actual_shares}")
                if oid != "?": return oid, actual_shares
            elif isinstance(resp, str) and len(resp) > 5:
                log.info(f"MARKET ORDER: ${dollar_amount:.2f} {label} resp={resp[:60]}")
                return resp, None
        except Exception as e:
            log.error(f"Market order fail: {e}")
        try:
            maker_price = round(max(0.01, min(price - 0.01, 0.99)), 2)
            limit_size = max(size, 5.0)
            signed = s.client.create_order(OrderArgs(
                price=maker_price, size=round(limit_size, 2), side=BUY, token_id=tid))
            resp = s.client.post_order(signed, OrderType.GTC)
            if isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("id") or resp.get("order_id") or "?"
                status = resp.get("status", "")
                log.info(f"LIMIT ORDER: ${maker_price*limit_size:.2f} {label} ({limit_size:.2f}sh @ ${maker_price}) id={oid} st={status}")
                if oid != "?": return oid, None
            elif isinstance(resp, str) and len(resp) > 5:
                return resp, None
        except Exception as e:
            log.error(f"Limit order fail: {e}")
        return None, None
    def prices(s, m):
        """Get current YES/NO prices. Prioritizes order book (matches Polymarket UI)."""
        if not s.client or not s.authed: return m.yes_p, m.no_p
        # Method 1: Order book — most accurate, matches what Polymarket website shows
        try:
            ybook = s.client.get_order_book(m.tok_yes)
            nbook = s.client.get_order_book(m.tok_no)
            yp = s._book_price(ybook)
            np_ = s._book_price(nbook)
            if yp and np_ and 0 < yp < 1 and 0 < np_ < 1:
                return yp, np_
        except: pass
        # Method 2: Midpoint API — can be slightly stale
        try:
            ymid = s.client.get_midpoint(m.tok_yes)
            nmid = s.client.get_midpoint(m.tok_no)
            yp = float(ymid["mid"]) if isinstance(ymid, dict) else float(ymid)
            np_ = float(nmid["mid"]) if isinstance(nmid, dict) else float(nmid)
            if 0 < yp < 1 and 0 < np_ < 1: return yp, np_
        except: pass
        return m.yes_p, m.no_p
    def _book_price(s, book):
        """Get the best price from order book.
        For buying: use best ask (lowest sell offer) — this is what you'd actually pay.
        Falls back to midpoint if only one side exists."""
        if not isinstance(book, dict): return None
        bids = book.get("bids", []); asks = book.get("asks", [])
        if asks:
            # Best ask = what you'd pay to buy = what Polymarket shows
            return float(asks[0].get("price", 0))
        elif bids:
            return float(bids[0].get("price", 0))
        return None
    def _book_mid(s, book):
        if not isinstance(book, dict): return None
        bids = book.get("bids", []); asks = book.get("asks", [])
        if bids and asks:
            bb = float(bids[0].get("price", 0)); ba = float(asks[0].get("price", 0))
            if bb > 0 and ba > 0: return (bb + ba) / 2
        elif bids: return float(bids[0].get("price", 0))
        elif asks: return float(asks[0].get("price", 0))
        return None
    def check_spread(s, token_id):
        """Check bid-ask spread. Returns spread as decimal (0.05 = 5 cent spread).
        Returns None if can't check. Spread > 0.08 means poor liquidity."""
        if not s.client or not s.authed: return None
        try:
            book = s.client.get_order_book(token_id)
            if not isinstance(book, dict): return None
            bids = book.get("bids", []); asks = book.get("asks", [])
            if not bids or not asks: return 0.99  # no liquidity
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            if best_bid <= 0 or best_ask <= 0: return 0.99
            return best_ask - best_bid
        except: return None
    def check_liquidity(s, token_id, min_depth=5.0):
        """Check if order book has enough depth. Returns True if OK."""
        if not s.client or not s.authed: return True  # assume OK
        try:
            book = s.client.get_order_book(token_id)
            if not isinstance(book, dict): return False
            asks = book.get("asks", [])
            total = 0
            for a in asks[:5]:
                total += float(a.get("size", 0)) * float(a.get("price", 0))
            return total >= min_depth
        except: return True  # assume OK on error
    def get_positions(s):
        addrs = []
        if s.c.funder_address: addrs.append(s.c.funder_address)
        signer = s._get_signer_addr()
        if signer and signer.lower() != (s.c.funder_address or "").lower(): addrs.append(signer)
        for addr in addrs:
            try:
                r = requests.get("https://data-api.polymarket.com/positions",
                    params={"user": addr}, timeout=8)
                if r.status_code == 200:
                    data = r.json() if isinstance(r.json(), list) else []
                    if data: return data
            except: continue
        return []
    def get_open_orders(s):
        if not s.authed: return []
        try:
            from py_clob_client.clob_types import OpenOrderParams
            result = s.client.get_orders(OpenOrderParams())
            if isinstance(result, list): return result
            if isinstance(result, dict): return result.get("orders", result.get("data", []))
            return []
        except: return []
    def cancel_all(s):
        if s.c.dry_run or not s.authed: return
        try: s.client.cancel_all()
        except: pass
    def redeem_positions(s, condition_ids):
        """Redeem resolved conditional tokens back to USDC.
        Method 1: Use py-clob-client (handles proxy routing automatically).
        Method 2: Direct contract call from signer (for EOA wallets).
        Method 3: Polymarket redeem API endpoint."""
        if s.c.dry_run or not s.c.private_key: return []
        redeemed = []

        # Method 1: Try py-clob-client (best for proxy wallets)
        if s.authed and s.client:
            for cid in condition_ids:
                try:
                    # The CLOB client's internal methods handle proxy routing
                    resp = s.client.post(f"{s.c.clob_host}/redeem", json={"conditionId": cid})
                    if hasattr(resp, 'status_code') and resp.status_code in [200, 201]:
                        redeemed.append(cid)
                        log.info(f"REDEEMED via API {cid[:16]}...")
                        time.sleep(5)
                        continue
                except: pass

                # Try the client's redeem if it exists
                for method_name in ["redeem", "redeem_positions", "redeemPositions"]:
                    try:
                        fn = getattr(s.client, method_name, None)
                        if fn:
                            result = fn(cid)
                            if result:
                                redeemed.append(cid)
                                log.info(f"REDEEMED via client.{method_name} {cid[:16]}...")
                                time.sleep(5)
                                break
                    except: continue

        # Method 2: Direct on-chain redeem (works for EOA wallets, signer-held tokens)
        remaining = [c for c in condition_ids if c not in redeemed]
        if remaining:
            try:
                from web3 import Web3
                from eth_account import Account
                pk = s.c.private_key
                if not pk.startswith("0x"): pk = "0x" + pk
                signer = Account.from_key(pk).address
                w3 = None
                for rpc in ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon",
                             "https://polygon.llamarpc.com", "https://polygon-bor-rpc.publicnode.com"]:
                    try:
                        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                        if w3.is_connected(): break
                    except: continue
                if not w3 or not w3.is_connected(): return redeemed
                ctf_addr = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
                usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                abi = json.loads('[{"constant":false,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"name":"conditionId","type":"bytes32"}],"name":"payoutDenominator","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
                ctf = w3.eth.contract(address=Web3.to_checksum_address(ctf_addr), abi=abi)

                # Also add ERC1155 balanceOf check to verify who holds tokens
                erc1155_abi = json.loads('[{"constant":true,"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
                erc1155 = w3.eth.contract(address=Web3.to_checksum_address(ctf_addr), abi=erc1155_abi)

                for cid in remaining:
                    try:
                        cid_bytes = bytes.fromhex(cid.replace("0x", ""))
                        time.sleep(5)
                        payout = ctf.functions.payoutDenominator(cid_bytes).call()
                        if payout == 0: continue

                        # Check who holds the tokens: signer or proxy
                        cid_int = int(cid, 16) if cid.startswith("0x") else int("0x" + cid, 16)
                        signer_bal = 0
                        proxy_bal = 0
                        try:
                            signer_bal = erc1155.functions.balanceOf(
                                Web3.to_checksum_address(signer), cid_int).call()
                        except: pass
                        if s.c.funder_address:
                            try:
                                proxy_bal = erc1155.functions.balanceOf(
                                    Web3.to_checksum_address(s.c.funder_address), cid_int).call()
                            except: pass

                        # Only redeem from signer if signer actually holds tokens
                        if signer_bal > 0:
                            try:
                                signer_cs = Web3.to_checksum_address(signer)
                                nonce = w3.eth.get_transaction_count(signer_cs)
                                txn = ctf.functions.redeemPositions(
                                    Web3.to_checksum_address(usdc_addr),
                                    b'\x00' * 32, cid_bytes, [1, 2]
                                ).build_transaction({
                                    'from': signer_cs, 'nonce': nonce,
                                    'gas': 250000, 'gasPrice': w3.eth.gas_price, 'chainId': 137,
                                })
                                signed_tx = w3.eth.account.sign_transaction(txn, pk)
                                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                                if receipt.status == 1:
                                    redeemed.append(cid)
                                    log.info(f"REDEEMED on-chain {cid[:16]}... from=signer tx={tx_hash.hex()[:16]}")
                                time.sleep(5)
                            except Exception as e:
                                log.debug(f"Signer redeem fail: {e}")

                        if proxy_bal > 0 and cid not in redeemed:
                            log.info(f"Tokens on PROXY {s.c.funder_address[:10]}... — need manual claim or API redeem")
                            # Can't redeem from proxy with signer key directly
                            # The proxy is a smart contract — needs to be called through its own interface

                    except Exception as e:
                        log.debug(f"Redeem check fail: {e}")
            except Exception as e:
                log.debug(f"Redeem setup fail: {e}")

        return redeemed

# ─── RISK MANAGER ───
class Risk:
    def __init__(s, c):
        s.c = c; s.bal = c.starting_balance; s.real_bal = None
        s.start_bal = None
        s.dpnl = 0.0; s.tpnl = 0.0; s.total_bet = 0.0
        s.trades = []; s.positions = []
        s._lifetime_wins = 0; s._lifetime_losses = 0
    def set_real(s, b):
        if b is not None and b > 0:
            if s.start_bal is None: s.start_bal = b
            s.real_bal = b; s.bal = b
            s.tpnl = b - s.start_bal
    @property
    def show_bal(s): return s.real_bal if s.real_bal is not None else s.bal
    @property
    def open_risk(s): return sum(p.cost for p in s.positions if p.status == "OPEN")
    @property
    def available(s): return s.show_bal - s.open_risk
    def ok(s):
        if s.start_bal and s.real_bal:
            real_loss = s.start_bal - s.real_bal
            if real_loss >= s.c.max_daily_loss: return False
        if len([p for p in s.positions if p.status == "OPEN"]) >= s.c.max_positions: return False
        return s.available >= 1.0
    def open(s, t, market_end=None, actual_shares=None):
        shares = actual_shares if actual_shares else t.size / t.price
        p = Pos(id=t.oid, strat=t.strat, slug=t.slug, side=t.side,
            entry=t.price, shares=shares, cost=t.size, opened=t.ts, market_end=market_end)
        s.positions.append(p); s.trades.append(t); s.total_bet += t.size; return p
    def resolve(s, pos, won):
        if won:
            gross_payout = pos.shares * 1.0
            fee = gross_payout * 0.02
            net_payout = gross_payout - fee
            pnl = net_payout - pos.cost
        else:
            pnl = -pos.cost
        pos.pnl = round(pnl, 2); pos.status = "WON" if won else "LOST"
        s.dpnl += pnl
        for t in s.trades:
            if t.oid == pos.id: t.pnl = pnl
    def check_exp(s, f):
        now = datetime.now(timezone.utc)
        resolved_list = []
        for p in s.positions:
            if p.status != "OPEN" or not p.opened: continue
            if p.market_end:
                past_end = (now - p.market_end).total_seconds()
                if past_end < 60: continue
            else:
                age = (now - p.opened).total_seconds()
                if age < 960: continue
            # Step 1: Always try Gamma API first (most accurate)
            resolved = s._check_resolution(p)
            if resolved is not None:
                s.resolve(p, resolved)
                resolved_list.append(p)
                continue
            # Step 2: SYNCED positions — ONLY use Gamma API, never BTC fallback
            # Because their 'opened' time is wrong (set to restart time)
            if p.strat == "SYNCED":
                if p.market_end and (now - p.market_end).total_seconds() > 600:
                    # 10 min past end and still no Gamma resolution — skip, don't guess
                    log.info(f"SYNCED position {p.slug} unresolvable — dropping")
                    p.status = "UNKNOWN"; p.pnl = 0.0
                    resolved_list.append(p)
                continue  # keep waiting for Gamma
            # Step 3: Non-synced positions — BTC direction fallback
            # Use market start time (market_end - 15min) not opened time
            if p.market_end and (now - p.market_end).total_seconds() > 300:
                market_start = p.market_end - timedelta(seconds=900)
                start_ts = market_start.timestamp()
                op = cp = None
                for x in f.data:
                    if x["t"] >= start_ts and op is None: op = x["p"]
                    cp = x["p"]
                if op and cp:
                    up = cp > op
                    s.resolve(p, (up and "YES" in p.side) or (not up and "NO" in p.side))
                else: s.resolve(p, False)
                resolved_list.append(p)
            elif not p.market_end:
                age = (now - p.opened).total_seconds()
                if age > 1200:
                    op = cp = None
                    for x in f.data:
                        if x["t"] >= p.opened.timestamp() and op is None: op = x["p"]
                        cp = x["p"]
                    if op and cp:
                        up = cp > op
                        s.resolve(p, (up and "YES" in p.side) or (not up and "NO" in p.side))
                    else: s.resolve(p, False)
                    resolved_list.append(p)
        return resolved_list
    def _check_resolution(s, p):
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets",
                params={"slug": p.slug}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    m = data[0]
                    if m.get("closed", False):
                        pr = m.get("outcomePrices") or m.get("outcome_prices") or ""
                        if isinstance(pr, str):
                            try: pr = json.loads(pr)
                            except: return None
                        if len(pr) >= 2:
                            yes_final = float(pr[0])
                            if yes_final > 0.9:
                                return "YES" in p.side
                            elif yes_final < 0.1:
                                return "NO" in p.side
        except: pass
        return None
    def stats(s):
        w = s._lifetime_wins + sum(1 for t in s.trades if t.pnl > 0)
        l = s._lifetime_losses + sum(1 for t in s.trades if t.pnl < 0)
        return w, l, (w / (w + l) * 100 if w + l else 0)

# ─── DASHBOARD (v6.2 — clean professional UI) ───
class Dash:
    def __init__(s): s.evts = deque(maxlen=8)
    def ev(s, e): s.evts.append(f"{datetime.now().strftime('%H:%M:%S')} {e}")
    def render(s, c, conn, f, risk, mkt, strats, scores, orders, poly_pos,
               start_time=None, past_trades=None, trend=None, sizer=None):
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.now().strftime("%H:%M:%S")
        rt = ""
        if start_time:
            elapsed = int(time.time() - start_time)
            hrs, rem = divmod(elapsed, 3600)
            mins, secs = divmod(rem, 60)
            rt = f"{hrs}h {mins}m {secs}s"

        # ╔══ HEADER ══╗
        print(f"\n  {H1}╔{'═'*60}╗{R}")
        print(f"  {H1}║  POLYMARKET BTC BOT v6.2          {DIM}{now}  ⏱ {rt}{R}  {H1}║{R}")
        print(f"  {H1}╚{'═'*60}╝{R}")

        # ── CONNECTION STATUS (one clean line) ──
        gc = f"{OK}●{R}" if conn.gamma == "OK" else f"{ERR}●{R}"
        cc = f"{OK}●{R}" if conn.clob == "OK" else f"{ERR}●{R}"
        ac = f"{OK}●{R}" if conn.can_trade else f"{ERR}●{R}"
        bc = f"{OK}●{R}" if "OK" in conn.binance else f"{ERR}●{R}"
        mode = f"{OK}LIVE{R}" if not c.dry_run else f"{WARN}DRY RUN{R}"
        print(f"  {DIM}Gamma{gc} CLOB{cc} Auth{ac} Binance{bc}  Mode: {mode}{R}")

        # ╔══ BALANCE BAR ══╗
        print(f"\n  {H1}┌{'─'*60}┐{R}")
        pnl_str = pnl_c2(risk.tpnl)
        w, l, wr = risk.stats()
        print(f"  {H1}│{R}  {LBL}Balance{R}  {OK}${risk.show_bal:.2f}{R}     {LBL}Available{R}  {VAL}${risk.available:.2f}{R}     {LBL}P&L{R}  {pnl_str}  {H1}│{R}")
        print(f"  {H1}│{R}  {LBL}Record{R}   {OK}{w}W{R}/{ERR}{l}L{R} ({VAL}{wr:.0f}%{R})    {LBL}At Risk{R}    {WARN}${risk.open_risk:.2f}{R}              {H1}│{R}")
        print(f"  {H1}└{'─'*60}┘{R}")

        # ╔══ BTC + MARKET ══╗
        if mkt:
            tl = (mkt.end - datetime.now(timezone.utc)).total_seconds()
            mins_left = int(tl // 60)
            secs_left = int(tl % 60)
            # Time progress bar
            progress = max(0, min(1.0 - tl / 900, 1.0))
            bar_len = int(progress * 20)
            time_bar = f"{OK}{'━' * bar_len}{DIM}{'─' * (20 - bar_len)}{R}"

            print(f"\n  {LBL}BTC{R}  {BTC}${f.price:,.2f}{R}   {VAL}{f.chg(60)*100:+.2f}%{R} 1m   {VAL}{f.chg(300)*100:+.2f}%{R} 5m   vol:{VAL}{f.volatility()*100:.3f}%{R}")
            print(f"  {LBL}MKT{R}  YES {OK}${mkt.yes_p:.2f}{R}  NO {ERR}${mkt.no_p:.2f}{R}  Sum {VAL}${mkt.yes_p + mkt.no_p:.3f}{R}   {time_bar} {VAL}{mins_left}:{secs_left:02d}{R}")

        # ╔══ TREND + SIZING ══╗
        if trend:
            regime = trend.regime
            if "UP" in regime: rc = OK; arrow = "▲"
            elif "DOWN" in regime: rc = ERR; arrow = "▼"
            elif regime == "BREAKOUT": rc = H2; arrow = "◆"
            elif regime == "CHOPPY": rc = WARN; arrow = "~"
            else: rc = DIM; arrow = "─"
            sizing_str = sizer.display_str() if sizer else ""
            bad_hr = ""
            if sizer and not sizer.is_good_hour(): bad_hr = f"  {WARN}⚠ bad hour{R}"
            print(f"  {LBL}AI{R}   {rc}{arrow} {regime}{R}  {DIM}│{R}  {sizing_str}{bad_hr}")

        # ╔══ STRATEGIES ══╗
        print(f"\n  {H1}┌{'─'*60}┐{R}")
        print(f"  {H1}│{R}  {LBL}STRATEGIES{R}                                               {H1}│{R}")
        print(f"  {H1}├{'─'*60}┤{R}")
        icons = {"ARB": "♦", "LATENCY": "⚡", "MOMENTUM": "↗", "FLASH": "⚡", "SQUEEZE": "◈"}
        for k, v in strats.items():
            ic = icons.get(k, "•")
            paused = sizer and sizer.is_paused(k)
            # Get current base size for display
            base_sz = c.get_base_size(k, risk.show_bal)
            if paused:
                pr = sizer.pause_remaining(k)
                line = f"  {ERR}⏸ {ic} {k:10}{R}  {ERR}PAUSED ({pr}s left){R}"
            elif "ACTIVE" in str(v):
                line = f"  {OK}● {ic} {k:10}{R}  {OK}{v}{R}"
            elif "lock" in str(v) or "blocked" in str(v) or "bad hour" in str(v):
                line = f"  {ERR}○ {ic} {k:10}{R}  {ERR}{v}{R}"
            elif "confirming" in str(v):
                line = f"  {WARN}◎ {ic} {k:10}{R}  {WARN}{v}{R}"
            else:
                line = f"  {DIM}○ {ic} {k:10}{R}  {DIM}{v}{R}"
            sz_str = f"{DIM}${base_sz:.2f}{R}"
            print(f"  {H1}│{R}{line}  {sz_str}  {H1}│{R}")
        print(f"  {H1}└{'─'*60}┘{R}")

        # Momentum signals (compact)
        if scores:
            parts = []
            for k, v in scores.items():
                c2 = OK if v > 0.1 else ERR if v < -0.1 else DIM
                parts.append(f"{k}:{c2}{v:+.1f}{R}")
            print(f"  {DIM}Signals: {'  '.join(parts)}{R}")

        # ╔══ POSITIONS ══╗
        open_pos = [p for p in risk.positions if p.status == "OPEN"]
        if open_pos:
            print(f"\n  {H1}┌{'─'*60}┐{R}")
            print(f"  {H1}│{R}  {LBL}OPEN POSITIONS{R}  ({len(open_pos)})  ${risk.open_risk:.2f} at risk             {H1}│{R}")
            print(f"  {H1}├{'─'*60}┤{R}")
            for p in open_pos[-5:]:
                if p.market_end:
                    remaining = max(0, (p.market_end - datetime.now(timezone.utc)).total_seconds())
                    bar_pct = max(0, min(1.0 - remaining / 900, 1.0))
                    time_str = f"{int(remaining//60)}:{int(remaining%60):02d}"
                else:
                    age = (datetime.now(timezone.utc) - p.opened).total_seconds() if p.opened else 0
                    bar_pct = min(age / 900, 1.0)
                    time_str = f"{int(age//60)}:{int(age%60):02d}"
                bar_len = int(bar_pct * 8)
                bar = f"{OK}{'█' * bar_len}{DIM}{'░' * (8 - bar_len)}{R}"
                side_col = OK if p.side == "YES" else ERR
                print(f"  {H1}│{R}  {side_col}{p.side:3}{R}  {DIM}[{p.strat[:5]:5}]{R}  ${p.cost:.2f} @ ${p.entry:.2f}  {bar} {VAL}{time_str}{R}  {H1}│{R}")
            print(f"  {H1}└{'─'*60}┘{R}")

        # ╔══ EVENTS (compact) ══╗
        evts = list(s.evts)[-5:]
        if evts:
            print(f"\n  {LBL}EVENTS{R}")
            for e in evts:
                if "ACTIVE" in e or "ARB" in e or "LAT" in e or "MOM" in e or "FLASH" in e:
                    print(f"  {OK}  {e}{R}")
                elif "LOST" in e or "Err" in e:
                    print(f"  {ERR}  {e}{R}")
                elif "WON" in e or "REDEEMED" in e:
                    print(f"  {OK}  {e}{R}")
                else:
                    print(f"  {DIM}  {e}{R}")

        # ╔══ TRADE HISTORY ══╗
        all_ended = []
        if past_trades:
            for t in past_trades: all_ended.append(t)
        closed = [p for p in risk.positions if p.status != "OPEN"]
        for p in closed:
            all_ended.append({
                "ts": p.opened.strftime('%H:%M %m/%d') if p.opened else "?",
                "status": "WIN" if p.pnl > 0 else "LOSS",
                "strat": p.strat, "side": p.side, "cost": p.cost,
                "entry": p.entry, "pnl": p.pnl, "slug": p.slug,
            })
        if all_ended:
            # Sort by timestamp string (newest first), take last 10
            recent = all_ended[-10:]
            recent.reverse()  # newest on top
            w_count = sum(1 for t in all_ended if t["pnl"] > 0)
            l_count = sum(1 for t in all_ended if t["pnl"] <= 0)
            total_pnl = sum(t["pnl"] for t in all_ended)
            print(f"\n  {H1}┌{'─'*60}┐{R}")
            print(f"  {H1}│{R}  {LBL}TRADE HISTORY{R}  {OK}{w_count}W{R} {ERR}{l_count}L{R}  Total: {pnl_c2(total_pnl)}               {H1}│{R}")
            print(f"  {H1}├{'─'*60}┤{R}")
            for t in recent:
                if t["pnl"] > 0:
                    icon = f"{OK}✓{R}"; col = OK
                else:
                    icon = f"{ERR}✗{R}"; col = ERR
                ts = t.get("ts", "?")
                # Format timestamp: try to show HH:MM MM/DD
                try:
                    if len(str(ts)) >= 16:  # full datetime like "2026-02-15 19:35:08"
                        parts = str(ts).split(" ")
                        date_p = parts[0].split("-")  # [2026, 02, 15]
                        time_p = parts[1][:5]  # HH:MM
                        ts = f"{time_p} {date_p[1]}/{date_p[2]}"
                except: pass
                print(f"  {H1}│{R}  {icon} {DIM}{ts:11}{R} {col}{t['side']:3}{R} [{t['strat'][:5]:5}] ${t['cost']:.2f}@${t['entry']:.2f} {pnl_c2(t['pnl'])} {H1}│{R}")
            print(f"  {H1}└{'─'*60}┘{R}")

        # ── FOOTER ──
        print(f"\n  {DIM}{'─'*60}")
        print(f"  Ctrl+C to stop{R}")

# ─── MAIN BOT v6 ───
class Bot:
    HISTORY_FILE = "trade_history.txt"

    def __init__(s):
        s.c = Config.from_env(); s.conn = Conn(); s.feed = Feed()
        s.finder = Finder(s.c); s.ex = Executor(s.c); s.risk = Risk(s.c)
        s.dash = Dash()
        # v6: Intelligence engines
        s.trend = TrendEngine()
        s.sizer = AdaptiveSizer(s.c)
        s.confirm = ConfirmationEngine()
        # Strategies
        s.s1 = S_Arb(s.c); s.s2 = S_Latency(s.c); s.s3 = S_Momentum(s.c); s.s4 = S_Flash(s.c); s.s5 = S_Squeeze(s.c)
        s.mkt = None; s.strats = {"ARB": "...", "LATENCY": "...", "MOMENTUM": "...", "FLASH": "...", "SQUEEZE": "..."}
        s.cd = {}; s._traded_cids = set()
        s.start_time = time.time()
        s._logged_positions = set()
        s._past_trades = []
        # Load saved condition IDs for redeeming
        s._load_traded_cids()

    def _load_traded_cids(s):
        """Load condition IDs from file so we can redeem after restart."""
        try:
            if os.path.exists("traded_cids.json"):
                with open("traded_cids.json", "r") as f:
                    data = json.load(f)
                s._traded_cids = set(data.get("cids", []))
                if s._traded_cids:
                    log.info(f"Loaded {len(s._traded_cids)} condition IDs for redemption")
        except: pass

    def _save_traded_cids(s):
        """Save condition IDs to file."""
        try:
            with open("traded_cids.json", "w") as f:
                json.dump({"cids": list(s._traded_cids)}, f)
        except: pass

    @staticmethod
    def _beep():
        try:
            if sys.platform == "win32":
                import winsound
                winsound.Beep(800, 150); winsound.Beep(1000, 150); winsound.Beep(1200, 200)
            else: print("\a", end="", flush=True)
        except: print("\a", end="", flush=True)

    def _update_env_balance(s, balance):
        """Update STARTING_BALANCE in .env file so compounding scales from current bankroll."""
        try:
            env_path = ".env"
            if not os.path.exists(env_path): return
            with open(env_path, "r") as f:
                lines = f.readlines()
            found = False
            new_lines = []
            for line in lines:
                if line.strip().startswith("STARTING_BALANCE"):
                    new_lines.append(f"STARTING_BALANCE={balance:.2f}\n")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"STARTING_BALANCE={balance:.2f}\n")
            with open(env_path, "w") as f:
                f.writelines(new_lines)
            log.info(f"Updated STARTING_BALANCE to ${balance:.2f}")
        except Exception as e:
            log.debug(f"Failed to update .env balance: {e}")

    def _init_history(s):
        s._past_trades = s._load_history()
        with open(s.HISTORY_FILE, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"  BOT v6 STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"  Balance: ${s.risk.show_bal:.2f}\n")
            f.write(f"  Mode: {'LIVE' if not s.c.dry_run else 'DRY RUN'}\n")
            f.write(f"{'='*60}\n")
        if s._past_trades:
            s.dash.ev(f"Loaded {len(s._past_trades)} past trades from history")

    def _load_history(s):
        trades = []
        try:
            if not os.path.exists(s.HISTORY_FILE): return []
            with open(s.HISTORY_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("["): continue
                    try:
                        ts_end = line.index("]")
                        ts_str = line[1:ts_end]
                        rest = line[ts_end+1:].strip()
                        parts = rest.split()
                        if len(parts) < 6: continue
                        status = parts[0]; strat = parts[1]; side = parts[2]
                        cost = float(parts[3].replace("$", ""))
                        entry = 0.0
                        for i, p in enumerate(parts):
                            if p == "@" and i+1 < len(parts):
                                entry = float(parts[i+1].replace("$", "")); break
                        pnl = 0.0
                        for i, p in enumerate(parts):
                            if p == "P&L:":
                                if i+1 < len(parts):
                                    pnl_str = parts[i+1].replace("+$", "").replace("-$", "-").replace("$", "")
                                    pnl = float(pnl_str)
                                    if parts[i+1].startswith("-"): pnl = -abs(pnl)
                                break
                        slug = ""
                        if "(" in line and ")" in line:
                            slug = line[line.rindex("(")+1:line.rindex(")")]
                        trades.append({"ts": ts_str, "status": status, "strat": strat,
                            "side": side, "cost": cost, "entry": entry, "pnl": pnl, "slug": slug})
                    except: continue
        except: pass
        return trades

    def _log_trade(s, pos):
        if pos.id in s._logged_positions: return
        s._logged_positions.add(pos.id)
        try:
            with open(s.HISTORY_FILE, "a") as f:
                ts = pos.opened.strftime('%Y-%m-%d %H:%M:%S') if pos.opened else "?"
                icon = "WIN " if pos.pnl > 0 else "LOSS"
                pnl = f"+${pos.pnl:.2f}" if pos.pnl > 0 else f"-${abs(pos.pnl):.2f}"
                regime = s.trend.regime if s.trend else "?"
                f.write(f"  [{ts}] {icon}  {pos.strat:10} {pos.side:4}  ${pos.cost:.2f} @ ${pos.entry:.4f}  P&L: {pnl}  [{regime}]  ({pos.slug})\n")
        except: pass

    def _close_history(s):
        try:
            w, l, wr = s.risk.stats()
            with open(s.HISTORY_FILE, "a") as f:
                f.write(f"{'─'*60}\n")
                f.write(f"  BOT v6 STOPPED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                elapsed = int(time.time() - s.start_time)
                hrs, rem = divmod(elapsed, 3600)
                mins, secs = divmod(rem, 60)
                f.write(f"  Runtime: {hrs}h {mins}m {secs}s\n")
                f.write(f"  Balance: ${s.risk.show_bal:.2f}  |  Real P&L: {'+'if s.risk.tpnl>=0 else ''}{s.risk.tpnl:.2f}\n")
                f.write(f"  Trades: {len(s.risk.trades)}  |  W: {w}  L: {l}  WR: {wr:.0f}%\n")
                f.write(f"  Wagered: ${s.risk.total_bet:.2f}\n")
                f.write(f"{'='*60}\n\n")
        except: pass

    def _sync_existing_positions(s):
        try:
            positions = s.ex.get_positions()
            if not positions: return
            synced = 0; now = datetime.now(timezone.utc)
            for p in positions:
                title = p.get("title") or p.get("question") or p.get("market", {}).get("question", "")
                slug = p.get("slug") or p.get("market", {}).get("slug", "")
                if not title and not slug: continue
                title_lower = (title or "").lower()
                if "bitcoin" not in title_lower and "btc" not in title_lower: continue
                market_end = None
                if slug:
                    try:
                        parts = slug.split("-")
                        for part in parts:
                            if part.isdigit() and len(part) >= 10:
                                market_end = datetime.fromtimestamp(int(part) + 900, tz=timezone.utc); break
                    except: pass
                if market_end and now > market_end: continue
                size_val = p.get("size") or p.get("shares") or p.get("currentValue") or 0
                try: size_val = float(size_val)
                except: size_val = 0
                if size_val <= 0: continue
                avg_price = p.get("avgPrice") or p.get("avg_price") or 0
                try: avg_price = float(avg_price)
                except: avg_price = 0
                outcome = p.get("outcome") or p.get("side") or ""
                side = "YES" if "yes" in str(outcome).lower() or "up" in str(outcome).lower() else "NO"
                cost = p.get("initialValue") or p.get("cost") or p.get("bet") or 0
                try: cost = float(cost)
                except: cost = avg_price * size_val if avg_price else 0
                if any(pos.slug == slug and pos.status == "OPEN" for pos in s.risk.positions): continue
                pos = Pos(id=f"SYNC-{slug[:20]}-{int(time.time())}", strat="SYNCED",
                    slug=slug or title[:30], side=side,
                    entry=avg_price if avg_price > 0 else 0.50, shares=size_val,
                    cost=cost if cost > 0 else size_val * (avg_price if avg_price else 0.50),
                    opened=datetime.now(timezone.utc), market_end=market_end)
                s.risk.positions.append(pos); synced += 1
                s.dash.ev(f"Synced: {side} {size_val:.1f}sh @ ${avg_price:.2f}")
            if synced: s.dash.ev(f"Loaded {synced} existing position(s)")
        except Exception as e: log.debug(f"Position sync error: {e}")

    def run(s):
        os.system("cls" if os.name == "nt" else "clear")
        print(f"\n  {H1}{'='*55}\n  |  POLYMARKET BTC BOT v6 — INTELLIGENT ENGINE\n  {'='*55}{R}\n")
        print(f"  {H2}[1/4]{R} Gamma..."); s.conn.gamma = "OK" if s.finder.test() else "FAILED"
        print(f"        {'OK' if s.conn.gamma == 'OK' else 'FAILED'}")
        print(f"  {H2}[2/4]{R} CLOB..."); s.conn.clob = "OK" if s.ex.test_public() else "FAILED"
        print(f"        {'OK' if s.conn.clob == 'OK' else 'FAILED'}")
        print(f"  {H2}[3/4]{R} Auth (proxy wallet)...")
        if s.c.private_key:
            print(f"        Signer: {s.ex._get_signer_addr()}")
            print(f"        Proxy:  {s.c.funder_address}")
            if s.ex.connect(s.conn):
                print(f"        {OK}Authenticated!{R}")
                rb = s.ex.get_balance()
                if rb:
                    s.risk.set_real(rb)
                    # v6.2: Auto-update starting balance from real balance
                    s.c.starting_balance = rb
                    s.risk.start_bal = rb
                    s.risk.bal = rb
                    s._update_env_balance(rb)
                    print(f"        Balance: ${rb:.6f}")
                    print(f"        {OK}Starting balance updated to ${rb:.2f}{R}")
                else: print(f"        {WARN}Balance check failed{R}")
            else:
                print(f"        {ERR}Auth failed{R}")
                for e in s.conn.errors[-3:]: print(f"        {DIM}{e}{R}")
                if not s.c.dry_run: input("  Press Enter for dry-run..."); s.c.dry_run = True
        else: s.c.dry_run = True
        print(f"  {H2}[4/4]{R} Binance...")
        binance_ok = False
        for _ in range(5):
            p = s.feed.poll()
            if p: s.conn.binance = f"OK — ${p:,.2f}"; print(f"        BTC: ${p:,.2f}"); binance_ok = True; break
            time.sleep(1)
        if not binance_ok: s.conn.binance = "FAILED"; print(f"        {ERR}Failed{R}")
        # v6: Show intelligence status
        print(f"\n  {H2}Intelligence:{R}")
        print(f"    Trend Engine: {OK}Active{R}")
        print(f"    Adaptive Sizing: {OK}Active{R} ({len(s.sizer.history)} historical trades)")
        print(f"    Confirmation: {OK}Active{R}")
        print(f"    Loss Streak Protection: {OK}Active{R} (pause after {s.c.streak_pause_count} losses)")
        print(f"\n  {H1}{'='*55}{R}")
        print(f"  {'LIVE TRADING v6' if not s.c.dry_run else 'DRY RUN v6'}")
        print(f"  {H1}{'='*55}{R}")
        time.sleep(3); s._init_history(); s._sync_existing_positions(); s.dash.ev("Bot v6 started"); s._loop()

    def _loop(s):
        ctr = 0; s._orders = []; s._poly_pos = []
        while True:
            try:
                s.feed.poll(); ctr += 1
                # v6: Update trend engine every tick
                s.trend.update(s.feed)
                resolved = s.risk.check_exp(s.feed); s._cancel_exp()
                for p in resolved:
                    s.dash.ev(f"[{p.strat[:3]}] {p.status} P&L:{p.pnl:+.2f}")
                    # v6: Record in adaptive sizer
                    hour = p.opened.hour if p.opened else datetime.now(timezone.utc).hour
                    # Don't record SYNCED/UNKNOWN trades — unreliable P&L
                    if p.strat not in ("SYNCED", "UNKNOWN"):
                        s.sizer.record(p.strat, p.side, p.pnl > 0, p.pnl, p.entry, hour, s.trend.regime)
                for p in s.risk.positions:
                    if p.status != "OPEN": s._log_trade(p)
                if s.mkt:
                    try: yp, np_ = s.ex.prices(s.mkt); s.mkt.yes_p, s.mkt.no_p = yp, np_
                    except: pass
                if ctr % 5 == 0:
                    for asset in s.c.assets:
                        m = s.finder.find(asset)
                        if m:
                            s.conn.gamma = "OK"  # Gamma works if we found a market
                            try: yp, np_ = s.ex.prices(m); m.yes_p, m.no_p = yp, np_
                            except: pass
                            new_market = (s.mkt is None or s.mkt.slug != m.slug)
                            s.mkt = m
                            if new_market:
                                m.open_btc = s.feed.price if s.feed.price else 0
                                s.dash.ev(f"New market: {m.slug[-20:]}")
                                s.s1.reset(m.slug)
                            if s.conn.can_trade or s.c.dry_run: s._trade(m)
                if ctr % 30 == 0 and s.ex.authed:
                    rb = s.ex.get_balance()
                    if rb: s.risk.set_real(rb)
                    s._orders = s.ex.get_open_orders()
                    s._poly_pos = s.ex.get_positions()
                if ctr % 90 == 0 and not s.c.dry_run and s._traded_cids:
                    s._auto_redeem()
                # Periodic cleanup — keeps memory stable for long runs
                if ctr % 500 == 0:
                    s._cleanup()
                s.dash.render(s.c, s.conn, s.feed, s.risk, s.mkt, s.strats, s.s3.scores,
                    s._orders, s._poly_pos, s.start_time, s._past_trades, s.trend, s.sizer)
                time.sleep(s.c.poll_sec)
            except KeyboardInterrupt:
                s.ex.cancel_all(); s._auto_redeem(); s._close_history(); s._summary(); break
            except Exception as e:
                log.error(f"Loop: {e}\n{traceback.format_exc()}")
                s.dash.ev(f"Err: {str(e)[:40]}"); time.sleep(3)

    def _cancel_exp(s):
        now = datetime.now(timezone.utc)
        if s.mkt:
            tl = (s.mkt.end - now).total_seconds()
            if 0 < tl < 120 and s._orders:
                try: s.ex.cancel_all(); s._orders = []; s.dash.ev("Cancelled — expiring")
                except: pass

    def _auto_redeem(s):
        if not s._traded_cids: return
        try:
            # Only try 3 CIDs at a time to avoid rate limits
            batch = list(s._traded_cids)[:3]
            redeemed = s.ex.redeem_positions(batch)
            for cid in redeemed:
                s.dash.ev(f"REDEEMED {cid[:12]}...")
                s._traded_cids.discard(cid)
            s._save_traded_cids()
            if redeemed:
                time.sleep(5)  # Wait before balance check to avoid rate limit
                rb = s.ex.get_balance()
                if rb: s.risk.set_real(rb)
        except Exception as e:
            log.debug(f"Auto-redeem error: {e}")

    def _cleanup(s):
        """Periodic cleanup for long-running stability.
        Only cleans MEMORY — all learning data stays in trade_data.json."""
        now = datetime.now(timezone.utc)

        # 1. Remove resolved positions older than 30 min from memory
        # (they're already logged to trade_history.txt and trade_data.json)
        before = len(s.risk.positions)
        s.risk.positions = [p for p in s.risk.positions
                            if p.status == "OPEN" or
                            (p.opened and (now - p.opened).total_seconds() < 1800)]
        removed = before - len(s.risk.positions)

        # 2. Trim trades list but preserve lifetime stats
        # Trades are already recorded in sizer — this list is only for dashboard display
        if len(s.risk.trades) > 200:
            # Save lifetime W/L before trimming
            s.risk._lifetime_wins = s.risk._lifetime_wins + sum(1 for t in s.risk.trades[:-200] if t.pnl > 0)
            s.risk._lifetime_losses = s.risk._lifetime_losses + sum(1 for t in s.risk.trades[:-200] if t.pnl < 0)
            s.risk.trades = s.risk.trades[-200:]

        # 3. Clean stale confirmation entries
        stale_keys = [k for k, v in s.confirm.pending.items()
                      if time.time() - v["time"] > 30]
        for k in stale_keys:
            del s.confirm.pending[k]

        # 4. Cap traded_cids at 50 (oldest ones probably already resolved)
        if len(s._traded_cids) > 50:
            cid_list = list(s._traded_cids)
            s._traded_cids = set(cid_list[-50:])
            s._save_traded_cids()

        if removed > 0:
            log.debug(f"Cleanup: removed {removed} old positions, {len(stale_keys)} stale confirms")

    def _trade(s, m):
        if not s.risk.ok(): return
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 90: return
        av = s.risk.available
        if av < 1.0: return

        # ── POSITION AWARENESS ──
        open_here = [p for p in s.risk.positions if p.status == "OPEN" and p.slug == m.slug]
        open_count = len(open_here)
        if open_count >= 7: return  # max 7 positions per market

        # Side lock: first trade sets direction, all others must match
        locked_side = None
        if open_here:
            locked_side = open_here[0].side  # "YES" or "NO"

        # Count positions per strategy on this market
        strat_counts = {}
        for p in open_here:
            strat_counts[p.strat] = strat_counts.get(p.strat, 0) + 1

        # Best entry price per strategy (for stack-only-when-cheaper on same strategy)
        strat_best_entry = {}
        for p in open_here:
            if p.strat not in strat_best_entry or p.entry < strat_best_entry[p.strat]:
                strat_best_entry[p.strat] = p.entry

        # Total risk on this market (for 25% balance cap)
        market_risk = sum(p.cost for p in open_here)
        max_market_risk = s.risk.show_bal * 0.25
        if market_risk >= max_market_risk: return

        # Same-strategy stacking requires: 5+ min left, TRENDING/BREAKOUT regime
        can_same_stack = (tl >= 300 and
                          s.trend.regime in ("TRENDING_UP", "TRENDING_DOWN", "BREAKOUT"))

        # ── SPREAD CHECK ──
        spread = s.ex.check_spread(m.tok_yes)
        if spread is not None and spread > 0.08:
            s.strats["ARB"] = f"wide spread ${spread:.2f}"
            s.strats["LATENCY"] = "wide spread"; s.strats["MOMENTUM"] = "wide spread"
            s.strats["FLASH"] = "wide spread"
            return

        # ── BAD HOUR (directional only, ARB exempt) ──
        bad_hour = not s.sizer.is_good_hour()
        if bad_hour:
            s.strats["LATENCY"] = "bad hour"; s.strats["MOMENTUM"] = "bad hour"; s.strats["FLASH"] = "bad hour"

        # ── HELPER: check if a trade is allowed ──
        def allowed(strat, side, price):
            """Returns (ok, reason, same_strat_count).
            Different strategy joining = always allowed at full size (same_strat_count=0).
            Same strategy stacking = diminishing size, needs cheaper price + trend."""
            # Smart side lock: normally must match existing direction,
            # BUT release if trend has clearly reversed and risk is manageable
            if locked_side and side != locked_side:
                # Check if we should release the lock:
                # 1. Trend must clearly support the new direction
                trend_supports_new = (
                    (side == "YES" and s.trend.trend_dir > 0 and s.trend.regime in ("TRENDING_UP", "BREAKOUT")) or
                    (side == "NO" and s.trend.trend_dir < 0 and s.trend.regime in ("TRENDING_DOWN", "BREAKOUT"))
                )
                # 2. Existing risk must be small (< 8% of balance)
                small_risk = market_risk < s.risk.show_bal * 0.08
                # 3. Must be early enough to recover (7+ min left)
                early_enough = tl >= 420
                # 4. Only for directional strategies (not ARB)
                directional = strat in ("LATENCY", "MOMENTUM", "SQUEEZE")

                if trend_supports_new and small_risk and early_enough and directional:
                    # Release the lock — trend reversed, old position is small
                    log.info(f"Side lock released: {locked_side}→{side} (trend={s.trend.regime}, risk=${market_risk:.2f})")
                    pass  # fall through to allow the trade
                else:
                    return False, f"side lock ({locked_side})", 0
            # Market risk cap
            remaining_risk = max_market_risk - market_risk
            if remaining_risk < 1.0 and open_count > 0:
                return False, "market risk cap", 0
            # How many times has THIS strategy already traded this market?
            same_count = strat_counts.get(strat, 0)
            if same_count == 0:
                # Different strategy (or first entry) — full size, no extra checks
                return True, "", 0
            else:
                # Same strategy wants to stack — apply stacking rules
                if same_count >= 3:
                    return False, f"{strat} maxed (3)", same_count
                if not can_same_stack:
                    if tl < 300:
                        return False, "too late to stack", same_count
                    return False, f"no stack in {s.trend.regime}", same_count
                # Same strategy stacking: only when price is same or cheaper
                best = strat_best_entry.get(strat)
                if best is not None and price > best + 0.02:
                    return False, f"price worse (have @${best:.2f})", same_count
                return True, "", same_count

        # ── S1: ARB (trend-agnostic, always runs) ──
        sig = s.s1.check(m, s.trend)
        if sig and not s.sizer.is_paused("ARB"):
            ok, reason, same_count = allowed("ARB", sig["side"], sig["price"])
            if ok:
                sz = s.sizer.get_size("ARB", s.c.get_base_size("ARB", s.risk.show_bal), s.risk.show_bal, same_strat_count=same_count)
                sz = min(sz, av, max_market_risk - market_risk)
                if sz >= 1.0:
                    s.strats["ARB"] = f"ACTIVE {sig['side']} pair=${sig['pair']:.4f}"
                    s.dash.ev(f"[ARB] {sig['side']} ${sz:.2f} pair=${sig['pair']:.3f}")
                    shares = sz / sig["price"]
                    oid, actual_shares = s.ex.order(m, sig["yes"], sig["price"], shares)
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "ARB", m.slug, sig["side"], sig["price"], sz, oid=oid)
                        s.risk.open(t, market_end=m.end, actual_shares=actual_shares)
                        if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                        s._beep()
                    # Don't return — let other strategies also check this tick
            else:
                s.strats["ARB"] = reason
        s.strats["ARB"] = s.strats.get("ARB", f"sum=${m.yes_p + m.no_p:.4f}")
        if "ACTIVE" not in s.strats["ARB"] and "lock" not in s.strats["ARB"]:
            s.strats["ARB"] = f"sum=${m.yes_p + m.no_p:.4f}"

        # ── S2: LATENCY (speed-based, fires immediately) ──
        sig = s.s2.check(m, s.feed, s.trend)
        if sig and time.time() - s.cd.get("lat", 0) > 15 and not s.sizer.is_paused("LATENCY") and not bad_hour:
            p = sig["p"]
            ok, reason, same_count = allowed("LATENCY", sig["dir"], p)
            if not ok:
                s.strats["LATENCY"] = reason
            else:
                should, trend_mult = s.trend.should_trade("LATENCY", sig["yes"])
                if should and not s.sizer.is_side_cold(sig["dir"]):
                    sz = s.sizer.get_size("LATENCY", s.c.get_base_size("LATENCY", s.risk.show_bal), s.risk.show_bal, same_strat_count=same_count)
                    sz = sz * trend_mult
                    sz = min(sz, av, max_market_risk - market_risk)
                    if sz >= 1.0 and 0.08 <= p <= 0.45:
                        # No confirmation delay — latency edge IS speed
                        s.strats["LATENCY"] = f"ACTIVE {sig['dir']} edge={sig['edge']*100:.1f}%"
                        sh = max(sz / p, 5.0)
                        if sh * p > av: sh = av / p
                        s.dash.ev(f"[LAT] {sig['dir']} ${sz:.2f} BTC{sig['chg']*100:+.2f}%")
                        oid, actual_shares = s.ex.order(m, sig["yes"], p, sh)
                        if oid:
                            t = Trd(datetime.now(timezone.utc), "LATENCY", m.slug, sig["dir"], p, sz, oid=oid)
                            s.risk.open(t, market_end=m.end, actual_shares=actual_shares); s.cd["lat"] = time.time()
                            if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                            s._beep()
                    else: s.strats["LATENCY"] = f"signal! {sig['dir']} price=${p:.2f}"
                else:
                    s.strats["LATENCY"] = f"reduced ({s.trend.regime})"
        elif not bad_hour:
            s.strats["LATENCY"] = f"btc {s.feed.chg(60)*100:+.2f}%"

        # ── S3: VELOCITY (big fast BTC moves — no confirmation, speed matters) ──
        sig = s.s3.check(m, s.feed, s.trend)
        if sig and time.time() - s.cd.get("mom", 0) > 30 and not s.sizer.is_paused("MOMENTUM") and not bad_hour:
            p = m.yes_p if sig["yes"] else m.no_p
            ok, reason, same_count = allowed("MOMENTUM", sig["dir"], p)
            if not ok:
                s.strats["MOMENTUM"] = reason
            else:
                should, trend_mult = s.trend.should_trade("MOMENTUM", sig["yes"])
                if should and not s.sizer.is_side_cold(sig["dir"]):
                    sz = s.sizer.get_size("MOMENTUM", s.c.get_base_size("MOMENTUM", s.risk.show_bal), s.risk.show_bal, same_strat_count=same_count)
                    sz = sz * trend_mult
                    sz = min(sz, av, max_market_risk - market_risk)
                    if sz >= 1.0 and 0.08 <= p <= 0.45:
                        # NO confirmation — velocity is about speed
                        v_info = sig.get("sig", {})
                        s.strats["MOMENTUM"] = f"SPIKE {sig['dir']} {v_info.get('chg',0)*100:.2f}% in {v_info.get('window',0)}s"
                        sh = max(sz / p, 5.0)
                        if sh * p > av: sh = av / p
                        s.dash.ev(f"[VEL] {sig['dir']} ${sz:.2f} {v_info.get('chg',0)*100:.2f}%/{v_info.get('window',0)}s")
                        oid, actual_shares = s.ex.order(m, sig["yes"], p, sh)
                        if oid:
                            t = Trd(datetime.now(timezone.utc), "MOMENTUM", m.slug, sig["dir"], p, sz, oid=oid)
                            s.risk.open(t, market_end=m.end, actual_shares=actual_shares); s.cd["mom"] = time.time()
                            if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                            s._beep()
                    else: s.strats["MOMENTUM"] = f"signal! {sig['dir']} p=${p:.2f}"
                else:
                    reason = "against trend" if not should else "side cold"
                    s.strats["MOMENTUM"] = f"blocked ({reason})"
        elif not bad_hour:
            s.strats["MOMENTUM"] = f"scanning"

        # ── S4: FLASH (trend-aware) ──
        sig = s.s4.check(m, s.feed, s.trend)
        if sig and time.time() - s.cd.get("flash", 0) > 120 and not s.sizer.is_paused("FLASH") and not bad_hour:
            p = sig["price"]
            ok, reason, same_count = allowed("FLASH", sig["dir"], p)
            if not ok:
                s.strats["FLASH"] = reason
            else:
                should, trend_mult = s.trend.should_trade("FLASH", sig["yes"])
                if should and not s.sizer.is_side_cold(sig["dir"]):
                    sz = s.sizer.get_size("FLASH", s.c.get_base_size("FLASH", s.risk.show_bal), s.risk.show_bal, same_strat_count=same_count)
                    sz = sz * trend_mult
                    sz = min(sz, av, max_market_risk - market_risk)
                    if sz >= 1.0:
                        s.strats["FLASH"] = f"ACTIVE {sig['dir']} @ ${p:.4f}"
                        sh = max(sz / p, 5.0)
                        if sh * p > av: sh = av / p
                        s.dash.ev(f"[FLASH] {sig['dir']} ${sz:.2f} @ ${p:.4f}")
                        oid, actual_shares = s.ex.order(m, sig["yes"], p, sh)
                        if oid:
                            t = Trd(datetime.now(timezone.utc), "FLASH", m.slug, sig["dir"], p, sz, oid=oid)
                            s.risk.open(t, market_end=m.end, actual_shares=actual_shares); s.cd["flash"] = time.time()
                            if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                            s._beep()
                    else:
                        s.strats["FLASH"] = f"signal! {sig['dir']} sz too small"
                else:
                    reason = "against trend" if not should else "side cold"
                    s.strats["FLASH"] = f"blocked ({reason})"
        elif not bad_hour:
            s.strats["FLASH"] = f"lo=${min(m.yes_p, m.no_p):.4f}"

        # ── S5: SQUEEZE → LATE GAME (lottery tickets in final minutes) ──
        if s.c.squeeze_enabled:
            sig = s.s5.check(m, s.feed, s.trend)
            if sig and not s.sizer.is_paused("SQUEEZE"):
                p = sig["price"]
                ok, reason, same_count = allowed("SQUEEZE", sig["dir"], p)
                if not ok:
                    s.strats["SQUEEZE"] = reason
                else:
                    # Late game doesn't need trend check — it's a lottery ticket
                    if not s.sizer.is_side_cold(sig["dir"]):
                        # Small size: 2-3% of balance for lottery tickets
                        base = min(s.c.get_base_size("SQUEEZE", s.risk.show_bal), s.risk.show_bal * 0.03)
                        sz = s.sizer.get_size("SQUEEZE", base, s.risk.show_bal, same_strat_count=same_count)
                        sz = min(sz, av, max_market_risk - market_risk)
                        if sz >= 0.50 and p <= 0.20:
                            tl_left = sig["squeeze_count"]
                            s.strats["SQUEEZE"] = f"LOTTERY {sig['dir']} ${p:.2f} {tl_left}s left"
                            sh = max(sz / p, 5.0)
                            if sh * p > av: sh = av / p
                            s.dash.ev(f"[LATE] {sig['dir']} ${sz:.2f} @{p:.2f} lottery")
                            oid, actual_shares = s.ex.order(m, sig["yes"], p, sh)
                            if oid:
                                t = Trd(datetime.now(timezone.utc), "SQUEEZE", m.slug, sig["dir"], p, sz, oid=oid)
                                s.risk.open(t, market_end=m.end, actual_shares=actual_shares); s.cd["sqz"] = time.time()
                                if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                                s._beep()
                        else: s.strats["SQUEEZE"] = f"signal {sig['dir']} p=${p:.2f}"
                    else:
                        s.strats["SQUEEZE"] = f"side cold"
            else:
                tl_now = (m.end - datetime.now(timezone.utc)).total_seconds() if m.end else 999
                if tl_now <= 300:
                    s.strats["SQUEEZE"] = f"watching ({int(tl_now)}s left)"
                else:
                    s.strats["SQUEEZE"] = f"waiting (<5min)"

    def _summary(s):
        os.system("cls" if os.name == "nt" else "clear")
        w, l, wr = s.risk.stats()
        print(f"\n{H1}{'═'*60}")
        print(f"  {LBL}SESSION SUMMARY — BOT v6{R}")
        print(f"{H1}{'═'*60}{R}")
        print(f"  {LBL}Balance:{R}  {bal_c(s.risk.show_bal)} USDC")
        print(f"  {LBL}Real P&L:{R} {pnl_c2(s.risk.tpnl)}")
        print(f"  {LBL}Wagered:{R}  {VAL}${s.risk.total_bet:.2f}{R}  ({len(s.risk.trades)} trades)")
        print(f"  {LBL}Record:{R}   {OK}{w}W{R} / {ERR}{l}L{R} / {VAL}{wr:.0f}%{R}")
        print(f"  {LBL}Sizing:{R}   {s.sizer.display_str()}")
        print(f"{H1}{'─'*60}{R}")
        for t in s.risk.trades[-10:]:
            pn = pnl_c2(t.pnl) if t.pnl else f"{DIM}pending{R}"
            icon = f"{OK}✓{R}" if t.pnl and t.pnl > 0 else f"{ERR}✗{R}" if t.pnl and t.pnl < 0 else f"{DIM}○{R}"
            print(f"  {icon} {t.ts.strftime('%H:%M:%S')} [{t.strat[:5]:5}] {t.side:6} ${t.size:.2f} @ ${t.price:.4f}  {pn}")
        print(f"{H1}{'═'*60}{R}\n")

if __name__ == "__main__":
    Bot().run()
