"""
POLYMARKET BOT v10.1 — BACKTEST-OPTIMIZED ENGINE
BTC (5m+15m) · ETH · SOL · XRP (15m)

v9.5 changes:
- Multi-asset: BTC, ETH, SOL, XRP — 5 market slots
- BTC trades both 5-minute and 15-minute markets
- Per-asset price feeds (Binance WebSocket), trends, momentum guards
- Dashboard shows all active markets with asset labels
- Flash widened to $0.35 for more volume
- Latency threshold lowered to 0.07% for more signals
- Max positions bumped to 8 for multi-asset
- 4-min trade cutoff (2-min for 5m markets) to avoid Gamma reversals

v9.4 changes (data-driven from 190 real trades):
- Entry range $0.15-$0.32 (only profitable range)
- Max 2 orders per market (no averaging down)
- $400 hard cap per trade
- Prefers Down/NO side (+$3,936 vs Up +$75)
- Grinder REMOVED → replaced by PairAccum + Spike
- PairAccum: Gabagool strategy — buy both sides cheap, guaranteed profit
- Spike: Detect order book panic dumps, buy fire sale tokens
- Maker Rebate Farming: passive income from limit order placement
- All multiplier floors raised (worst case 0.20x not 0.04x)

The Cortex is a unified intelligence that replaces the v8 StrategyManager.
Instead of 7 separate systems each doing their own thing, the Cortex:
- Has direct access to ALL raw data (BTC feed, token prices, order book)
- Tracks market outcomes across 15-min windows (macro momentum)
- Learns which strategy+regime combos make money (pattern discovery)
- Detects BTC price danger zones from loss history
- Manages session P&L and adapts aggression in real time
- BOOSTS when conditions are right (profit maximizer, not risk manager)
- Never reduces Latency or Squeeze below 1.0x (mechanical edges stay untouched)

Philosophy: Optimize for EXPECTED VALUE, not win rate.
The bot makes money from a few huge wins, not from winning often.
The Cortex protects big wins by staying aggressive when conditions are right.

Built on v8.1 proven architecture (5 strategies, 16 risk protections).

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
    momentum_pct: float = 0.03   # 3% of balance (MeanReversion — still earning trust)
    flash_pct: float = 0.03      # 3% of balance (reduced from 4% — earn it back)
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
    squeeze_pct: float = 0.02     # 2% of balance — needs more data before bigger bets
    max_daily_loss: float = 9999.0   # Set via MAX_DAILY_LOSS env var
    max_positions: int = 4
    poll_sec: int = 2
    assets: list = field(default_factory=lambda: ["btc"])
    # v9.4: Multi-asset slot definitions (asset, timeframe)
    # BTC gets both 5m and 15m. Others get 15m only.
    slots: list = field(default_factory=lambda: [
        ("btc", 15), ("btc", 5),
    ])
    # v6: Adaptive settings
    min_trades_to_adapt: int = 15
    max_size_multiplier: float = 2.5
    min_size_multiplier: float = 0.3
    streak_pause_count: int = 8     # v10.1: was 5, raised to 8 — backtest had no pause and made +$8K
    streak_pause_sec: int = 1800

    # v9.5: Recovery mode — half sizes until balance recovers
    recovery_target: float = 5500.0

    def get_base_size(s, strat, balance):
        """Get base trade size — percentage of balance or fixed fallback.
        v9.5: Half sizes when below recovery_target for capital preservation."""
        pct_map = {"ARB": s.arb_pct, "LATENCY": s.latency_pct,
                   "MOMENTUM": s.momentum_pct, "MEANREV": s.momentum_pct, "FLASH": s.flash_pct,
                   "SQUEEZE": s.squeeze_pct, "PAIR": 0.06, "SPIKE": 0.04}
        fixed_map = {"ARB": s.arb_size, "LATENCY": s.latency_size,
                     "MOMENTUM": s.momentum_size, "FLASH": s.flash_size}
        pct = pct_map.get(strat, 0.05)
        if pct > 0:
            size = round(balance * pct, 2)
        else:
            size = fixed_map.get(strat, 2.0)
        # v9.5: Recovery mode — half sizes below target
        if balance < s.recovery_target:
            size = round(size * 0.50, 2)
        return size

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
            flash_pct=float(os.getenv("FLASH_PCT", "0.03")),
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
    asset: str = "btc"; timeframe: int = 15

@dataclass
class Pos:
    id: str; strat: str; slug: str; side: str
    entry: float; shares: float; cost: float
    pnl: float = 0.0; opened: datetime = None; status: str = "OPEN"
    market_end: datetime = None; entry_regime: str = "UNKNOWN"

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

# ─── PRICE FEED (v9.1: WebSocket primary, HTTP fallback) ───
import threading
try:
    import websocket as _ws_lib
    _HAS_WS = True
except ImportError:
    _HAS_WS = False
    log.info("websocket-client not installed — using HTTP polling (pip install websocket-client for 20x faster feed)")

class Feed:
    """Price feed for any asset. 
    Primary: Binance WebSocket (100ms updates).
    Secondary: Chainlink on-chain oracle (what Polymarket actually settles on).
    Fallback: HTTP polling (if WS disconnects).
    
    v9.5: Added Chainlink feed for BTC. Polymarket 5m/15m markets resolve on
    Chainlink, NOT Binance. When they disagree, Chainlink is truth."""
    SYMBOLS = {
        "btc": ("btcusdt", "BTCUSDT", "BTC-USD"),
        "eth": ("ethusdt", "ETHUSDT", "ETH-USD"),
        "sol": ("solusdt", "SOLUSDT", "SOL-USD"),
        "xrp": ("xrpusdt", "XRPUSDT", "XRP-USD"),
    }
    # Chainlink BTC/USD on Ethereum Mainnet
    CHAINLINK_BTC_ADDR = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
    CHAINLINK_RPC = "https://eth-mainnet.g.alchemy.com/v2/oOZNCpMzhzk3MrzbpNGsR"
    # ABI for latestRoundData(): returns (roundId, answer, startedAt, updatedAt, answeredInRound)
    CHAINLINK_ABI_SIG = "0xfeaf968c"  # function selector for latestRoundData()
    
    def __init__(s, asset="btc"):
        s.asset = asset
        s._sym_ws, s._sym_http, s._sym_cb = s.SYMBOLS.get(asset, s.SYMBOLS["btc"])
        s.data = deque(maxlen=1000)
        s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/10.1"
        s._ws = None; s._ws_thread = None; s._ws_alive = False
        s._ws_last = 0; s._ws_retries = 0
        # Chainlink state
        s.chainlink_price = 0
        s.chainlink_time = 0  # timestamp of last Chainlink update
        s._cl_last_poll = 0
        s._cl_data = deque(maxlen=200)  # Chainlink price history
        if _HAS_WS:
            s._start_ws()
        # Start Chainlink polling thread for BTC
        if asset == "btc":
            s._cl_thread = threading.Thread(target=s._chainlink_loop, daemon=True)
            s._cl_thread.start()
    
    def _chainlink_loop(s):
        """Poll Chainlink every 10 seconds for BTC price."""
        while True:
            try:
                s._poll_chainlink()
            except Exception as e:
                log.debug(f"Chainlink poll error: {e}")
            time.sleep(10)
    
    def _poll_chainlink(s):
        """Read Chainlink BTC/USD price via eth_call to Alchemy RPC."""
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{
                "to": s.CHAINLINK_BTC_ADDR,
                "data": s.CHAINLINK_ABI_SIG
            }, "latest"],
            "id": 1
        }
        r = s.s.post(s.CHAINLINK_RPC, json=payload, timeout=5)
        result = r.json().get("result", "")
        if result and len(result) >= 130:
            # latestRoundData returns 5 uint256 values packed in hex
            # answer is the 2nd value (bytes 66-130)
            answer_hex = result[66:130]
            answer = int(answer_hex, 16)
            # Chainlink BTC/USD has 8 decimals
            price = answer / 1e8
            if price > 1000:  # sanity check
                now = time.time()
                s.chainlink_price = price
                s.chainlink_time = now
                s._cl_data.append({"t": now, "p": price})
                s._cl_last_poll = now
    
    def cl_chg(s, sec=60):
        """Chainlink price change over last N seconds."""
        if len(s._cl_data) < 2: return 0
        snap = list(s._cl_data)
        now = snap[-1]; cut = now["t"] - sec; old = snap[0]
        for p in snap:
            if p["t"] >= cut: old = p; break
        return (now["p"] - old["p"]) / old["p"] if old["p"] else 0
    
    @property
    def cl_price(s):
        """Current Chainlink price (0 if not available)."""
        return s.chainlink_price
    
    @property
    def cl_age(s):
        """Seconds since last Chainlink update."""
        if s.chainlink_time == 0: return 9999
        return time.time() - s.chainlink_time
    
    @property
    def cl_binance_agree(s):
        """Do Chainlink and Binance agree on direction?"""
        if s.chainlink_price == 0 or len(s.data) < 2: return True  # no data, assume agree
        binance_p = s.data[-1]["p"] if s.data else 0
        if binance_p == 0: return True
        # They agree if within 0.1% of each other
        diff_pct = abs(binance_p - s.chainlink_price) / s.chainlink_price
        return diff_pct < 0.001
    
    @property
    def price_divergence(s):
        """How much Binance and Chainlink disagree (as %)."""
        if s.chainlink_price == 0 or not s.data: return 0
        binance_p = s.data[-1]["p"]
        return (binance_p - s.chainlink_price) / s.chainlink_price
    
    @property
    def settlement_price(s):
        """The price that Polymarket will use for settlement.
        Uses Chainlink if available and recent, otherwise Binance."""
        if s.chainlink_price > 0 and s.cl_age < 300:
            return s.chainlink_price
        return s.data[-1]["p"] if s.data else 0
    
    def _start_ws(s):
        """Start Binance WebSocket in background thread."""
        ws_url = f"wss://stream.binance.com:9443/ws/{s._sym_ws}@trade"
        def _run():
            while True:
                try:
                    ws = _ws_lib.WebSocketApp(
                        ws_url,
                        on_message=s._on_ws_msg,
                        on_error=lambda ws, e: log.debug(f"WS err: {e}"),
                        on_close=lambda ws, c, m: setattr(s, '_ws_alive', False),
                        on_open=lambda ws: setattr(s, '_ws_alive', True)
                    )
                    s._ws = ws
                    ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:
                    log.debug(f"WS connect fail: {e}")
                s._ws_alive = False
                s._ws_retries += 1
                time.sleep(min(5 * s._ws_retries, 30))  # backoff
        
        s._ws_thread = threading.Thread(target=_run, daemon=True)
        s._ws_thread.start()
    
    def _on_ws_msg(s, ws, msg):
        """Handle incoming WebSocket trade message."""
        try:
            d = json.loads(msg)
            p = float(d.get("p", 0))
            if p > 0:
                now = time.time()
                # Throttle: max 5 updates/sec to avoid flooding deque
                if now - s._ws_last >= 0.2:
                    s.data.append({"t": now, "p": p})
                    s._ws_last = now
                    s._ws_retries = 0  # reset backoff on success
        except: pass
    
    def poll(s):
        """Called every tick. If WS is alive and recent, skip HTTP.
        If WS is stale (>5s), fall back to HTTP polling."""
        if s._ws_alive and s._ws_last > 0 and (time.time() - s._ws_last) < 5:
            return s.data[-1]["p"] if s.data else None
        # HTTP fallback
        for fn in [s._b, s._c]:
            try:
                p = fn()
                if p: s.data.append({"t": time.time(), "p": p}); return p
            except: continue
        return None
    def _b(s):
        r = s.s.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": s._sym_http}, timeout=3)
        return float(r.json()["price"])
    def _c(s):
        r = s.s.get(f"https://api.coinbase.com/v2/prices/{s._sym_cb}/spot", timeout=3)
        return float(r.json()["data"]["amount"])
    @property
    def price(s): return s.data[-1]["p"] if s.data else 0
    @property
    def n(s): return len(s.data)
    @property
    def ws_status(s):
        """For dashboard display."""
        if s._ws_alive and (time.time() - s._ws_last) < 5: return "WS"
        return "HTTP"
    def arr(s, n=50):
        d = list(s.data)[-n:]
        return np.array([x["p"] for x in d]) if d else np.array([])
    def chg(s, sec=60):
        if len(s.data) < 2: return 0
        snap = list(s.data)
        if len(snap) < 2: return 0
        now = snap[-1]; cut = now["t"] - sec; old = snap[0]
        for p in snap:
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
        snap = list(s.data)
        prices = [(d["p"], d["t"]) for d in snap if d["t"] >= cut]
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
        # v9.1: Flipped weights — longer timeframes are MORE reliable, not less.
        # Old: 1m*3 + 5m*2 + 15m*1 → 1-min bounce erased 10-min downtrend
        # New: 1m*1 + 5m*2.5 + 15m*3 → sustained trends dominate classification
        raw = s.chg_1m * 1.0 + s.chg_5m * 2.5 + s.chg_15m * 3.0
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

        # LATENCY: edge is SPEED, not trend. Allow in most regimes.
        # v9.1: Counter-trend Latency reduced further — today's data shows
        # YES in downtrend lost 5/5 for -$2,646. Speed can't outrun a trend.
        if strat == "LATENCY":
            if s.regime == "FLAT": return True, 0.8
            if s.regime == "CHOPPY": return True, 0.5
            if s.regime in ("TRENDING_UP", "TRENDING_DOWN"):
                trend_up = s.regime == "TRENDING_UP"
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                if with_trend:
                    bonus = min(abs(s.trend_strength) * 2, 0.5)
                    return True, 1.0 + bonus
                else:
                    return True, 0.3  # v9.1: was 0.5, reduced — counter-trend Latency is a trap
            if s.regime == "BREAKOUT":
                trend_up = s.trend_dir > 0
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                return True, 1.5 if with_trend else 0.3  # v9.1: was 0.4
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

        # MEANREV/SQUEEZE: needs some directional signal, but don't fully block in FLAT
        if strat in ("MOMENTUM", "MEANREV", "SQUEEZE"):
            if s.regime == "FLAT": return True, 0.5  # was blocked, now allowed at half size
            if s.regime == "CHOPPY": return True, 0.3  # v9.5: was blocked, allow at 30%
            if s.regime in ("TRENDING_UP", "TRENDING_DOWN"):
                trend_up = s.regime == "TRENDING_UP"
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                if with_trend:
                    bonus = min(abs(s.trend_strength) * 2, 0.5)
                    return True, 1.0 + bonus
                else:
                    return True, 0.3  # v9.5: was blocked, allow at 30% — MeanRev IS counter-trend
            if s.regime == "BREAKOUT":
                trend_up = s.trend_dir > 0
                with_trend = (side_is_yes and trend_up) or (not side_is_yes and not trend_up)
                return (True, 1.5) if with_trend else (True, 0.3)  # v9.5: allow at 30%

        # PAIR: always allowed, strategy does own safety checks
        if strat == "PAIR": return True, 1.0
        
        # SPIKE: similar to FLASH but more aggressive — panic sells happen in all regimes
        if strat == "SPIKE": return True, 1.0

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
        s.multipliers = {"ARB": 1.0, "LATENCY": 1.0, "MEANREV": 1.0, "FLASH": 1.0, "SQUEEZE": 1.0}
        s.paused_until = {"ARB": 0, "LATENCY": 0, "MEANREV": 0, "FLASH": 0, "SQUEEZE": 0}
        s.streaks = {"ARB": 0, "LATENCY": 0, "MEANREV": 0, "FLASH": 0, "SQUEEZE": 0}  # negative = losses
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

    def record(s, strat, side, won, pnl, price, hour, regime="UNKNOWN", btc_price=0):
        """Record a completed trade and recalculate."""
        s.history.append({
            "strat": strat, "side": side, "won": won, "pnl": pnl,
            "price": price, "hour": hour, "regime": regime, "ts": time.time(),
            "btc_price": btc_price
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
        for strat in ["ARB", "LATENCY", "MEANREV", "FLASH", "SQUEEZE"]:
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
        same_strat_count: diminishes for same strategy stacking.
        v9: Multiplier REMOVED — Cortex handles trust-based sizing now.
        AdaptiveSizer still handles: pausing, stacking, max cap."""
        if time.time() < s.paused_until.get(strat, 0):
            return 0
        # v9: No more adaptive multiplier here — Cortex trust replaces it
        size = base_size
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
        for strat in ["ARB", "LATENCY", "MEANREV", "FLASH", "SQUEEZE"]:
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
# ─── v7: CONVICTION ENGINE ───
# ═══════════════════════════════════════════════════════════════
class ConvictionEngine:
    """Tracks when multiple strategies agree on the same side in the same market.
    When 2+ strategies fire on the same side = high conviction = bonus sizing."""
    def __init__(s):
        s._signals = {}   # {market_slug: {side: [strat_names]}}
        s._current_slug = None

    def reset(s, slug):
        if slug != s._current_slug:
            s._signals = {}
            s._current_slug = slug

    def record_signal(s, slug, strat, side):
        """Record that a strategy fired on a side."""
        s.reset(slug)
        if slug not in s._signals:
            s._signals[slug] = {"YES": [], "NO": []}
        if strat not in s._signals[slug][side]:
            s._signals[slug][side].append(strat)

    def get_bonus(s, slug, side):
        """Returns size multiplier based on how many strategies agree.
        1 strategy = 1.0x (normal), 2 = 1.5x, 3+ = 1.8x"""
        if slug not in s._signals: return 1.0
        count = len(s._signals[slug].get(side, []))
        if count >= 3: return 1.8
        if count >= 2: return 1.5
        return 1.0

    def display_str(s):
        if not s._signals: return ""
        parts = []
        for slug, sides in s._signals.items():
            for side, strats in sides.items():
                if len(strats) >= 2:
                    parts.append(f"{side}:{'+'.join(s[:3] for s in strats)}")
        return " ".join(parts) if parts else ""


# ═══════════════════════════════════════════════════════════════
# ─── v7: MOMENTUM GUARD ───
# ═══════════════════════════════════════════════════════════════
class MomentumGuard:
    """Tracks how long BTC has been trending in one direction.
    If trending 5+ min straight, even 'FLAT' regime shouldn't trigger counter-trades."""
    def __init__(s):
        s.direction_since = 0    # timestamp when current direction started
        s.current_dir = 0        # +1 up, -1 down, 0 neutral
        s.sustained_minutes = 0  # how many minutes in this direction

    def update(s, feed):
        if feed.n < 30: return
        chg_2m = feed.chg(120)
        chg_30s = feed.chg(30)

        # Determine current dominant direction
        if chg_2m > 0.0005 and chg_30s > 0:
            new_dir = 1
        elif chg_2m < -0.0005 and chg_30s < 0:
            new_dir = -1
        else:
            new_dir = 0

        if new_dir != s.current_dir:
            s.current_dir = new_dir
            s.direction_since = time.time()

        if s.current_dir != 0:
            s.sustained_minutes = (time.time() - s.direction_since) / 60
        else:
            s.sustained_minutes = 0

    def should_block(s, side_is_yes):
        """v9.5: Changed from block to advisory — returns True if counter-trend.
        Callers use this for logging only, not blocking. The counter_trend_mult
        in allowed() already handles size reduction."""
        if s.sustained_minutes < 3: return False
        if s.current_dir > 0 and not side_is_yes: return False  # v9.5: was True (blocked)
        if s.current_dir < 0 and side_is_yes: return False  # v9.5: was True (blocked)
        return False


# ═══════════════════════════════════════════════════════════════
# ─── v7: WIN STREAK SIZER ───
# ═══════════════════════════════════════════════════════════════
class WinStreakSizer:
    """After consecutive wins, boost sizing. After losses, reduce.
    Captures hot streaks when market conditions favor the bot."""
    def __init__(s):
        s.global_streak = 0   # positive = wins, negative = losses
        s._last_results = deque(maxlen=10)

    def record(s, won):
        s._last_results.append(won)
        if won:
            s.global_streak = max(0, s.global_streak) + 1
        else:
            s.global_streak = min(0, s.global_streak) - 1

    def get_multiplier(s):
        """Win streak bonus: 3W=1.2x, 4W=1.3x, 5W+=1.4x. Loss streak: no penalty (handled by AdaptiveSizer)."""
        if s.global_streak >= 5: return 1.4
        if s.global_streak >= 4: return 1.3
        if s.global_streak >= 3: return 1.2
        return 1.0

    def display_str(s):
        if s.global_streak >= 3: return f"🔥{s.global_streak}W"
        if s.global_streak <= -3: return f"❄️{abs(s.global_streak)}L"
        return f"streak:{s.global_streak:+d}"


# ═══════════════════════════════════════════════════════════════
# ─── v8: TOKEN PRICE FEED ───
# ═══════════════════════════════════════════════════════════════

# v9.1: Polymarket CLOB WebSocket — real-time token prices + order book
class PolyWebSocket:
    """Streams YES/NO price changes and order book updates from Polymarket.
    Without this: HTTP poll every 2-10 seconds (stale data).
    With this: instant updates when any order changes on the market."""
    
    def __init__(s):
        s.yes_p = 0.0; s.no_p = 0.0
        s.yes_bid = 0.0; s.yes_ask = 0.0
        s.no_bid = 0.0; s.no_ask = 0.0
        s._alive = False; s._thread = None
        s._ws = None; s._asset_ids = []
        s._last_update = 0; s._retries = 0
        s._slug = None
    
    def subscribe(s, tok_yes, tok_no, slug=""):
        """Subscribe to a new market. Call when market changes."""
        s._asset_ids = [tok_yes, tok_no]
        s._slug = slug
        # Close existing connection — will auto-reconnect with new subscription
        if s._ws:
            try: s._ws.close()
            except: pass
        if not s._thread or not s._thread.is_alive():
            s._start()
    
    def _start(s):
        if not _HAS_WS: return
        def _run():
            while True:
                try:
                    ws = _ws_lib.WebSocketApp(
                        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                        on_message=s._on_msg,
                        on_error=lambda ws, e: None,
                        on_close=lambda ws, c, m: setattr(s, '_alive', False),
                        on_open=s._on_open
                    )
                    s._ws = ws
                    ws.run_forever(ping_interval=20, ping_timeout=10)
                except: pass
                s._alive = False
                s._retries += 1
                time.sleep(min(3 * s._retries, 15))
        s._thread = threading.Thread(target=_run, daemon=True)
        s._thread.start()
    
    def _on_open(s, ws):
        s._alive = True; s._retries = 0
        if s._asset_ids:
            sub = {"assets_ids": s._asset_ids, "type": "market"}
            ws.send(json.dumps(sub))
            log.info(f"PolyWS subscribed to {len(s._asset_ids)} tokens")
    
    def _on_msg(s, ws, msg):
        try:
            data = json.loads(msg)
            etype = data.get("event_type", "")
            s._last_update = time.time()
            
            if etype == "price_change":
                for ch in data.get("changes", []):
                    price = float(ch.get("price", 0))
                    side = ch.get("side", "")
                    asset_id = data.get("asset_id", "")
                    if asset_id == s._asset_ids[0]:  # YES token
                        if side == "BUY": s.yes_bid = price
                        elif side == "SELL": s.yes_ask = price
                        s.yes_p = (s.yes_bid + s.yes_ask) / 2 if s.yes_bid and s.yes_ask else price
                    elif len(s._asset_ids) > 1 and asset_id == s._asset_ids[1]:  # NO token
                        if side == "BUY": s.no_bid = price
                        elif side == "SELL": s.no_ask = price
                        s.no_p = (s.no_bid + s.no_ask) / 2 if s.no_bid and s.no_ask else price
            
            elif etype == "last_trade_price":
                price = float(data.get("price", 0))
                asset_id = data.get("asset_id", "")
                if price > 0:
                    if asset_id == s._asset_ids[0]:
                        s.yes_p = price
                    elif len(s._asset_ids) > 1 and asset_id == s._asset_ids[1]:
                        s.no_p = price
        except: pass
    
    @property
    def is_live(s):
        return s._alive and s._last_update > 0 and (time.time() - s._last_update) < 10


class TokenFeed:
    """Tracks Polymarket YES/NO prices as a time series.
    Enables: divergence detection, token momentum, smart money signals.
    Updated every time prices are fetched (every 5 ticks = 10 seconds)."""
    def __init__(s):
        s.data = deque(maxlen=200)   # ~30 min of 10-sec samples
        s._current_slug = None

    def update(s, slug, yes_p, no_p):
        """Record a new price snapshot."""
        if slug != s._current_slug:
            s.data.clear()
            s._current_slug = slug
        s.data.append({"t": time.time(), "yes": yes_p, "no": no_p})

    @property
    def n(s): return len(s.data)

    def token_chg(s, side, sec=30):
        """Price change for YES or NO token over last N seconds."""
        if len(s.data) < 3: return 0
        key = "yes" if side == "YES" else "no"
        snap = list(s.data)
        if len(snap) < 3: return 0
        now = snap[-1]
        cut = now["t"] - sec
        old = snap[0]
        for p in snap:
            if p["t"] >= cut: old = p; break
        if old[key] <= 0: return 0
        return (now[key] - old[key]) / old[key]

    def token_velocity(s, side, sec=20):
        """Rate of price change (acceleration). Positive = price rising faster."""
        if len(s.data) < 6: return 0
        key = "yes" if side == "YES" else "no"
        snap = list(s.data)
        prices = [(d[key], d["t"]) for d in snap if d["t"] >= time.time() - sec]
        if len(prices) < 4: return 0
        mid = len(prices) // 2
        first_half = [p for p, _ in prices[:mid]]
        second_half = [p for p, _ in prices[mid:]]
        if not first_half or not second_half: return 0
        chg1 = (first_half[-1] - first_half[0]) if len(first_half) > 1 else 0
        chg2 = (second_half[-1] - second_half[0]) if len(second_half) > 1 else 0
        return chg2 - chg1  # positive = accelerating up, negative = accelerating down

    def divergence(s, btc_feed, sec=60):
        """Detect divergence between BTC direction and token movement.
        Returns: +1 if BTC up but YES dropping (bearish divergence)
                 -1 if BTC down but NO dropping (bullish divergence)
                  0 if no divergence"""
        if len(s.data) < 5 or btc_feed.n < 15: return 0
        btc_chg = btc_feed.chg(sec)
        yes_chg = s.token_chg("YES", sec)
        # BTC going up but YES not following = market doesn't believe the move
        if btc_chg > 0.0005 and yes_chg < -0.02: return 1   # bearish divergence
        # BTC going down but NO not following
        if btc_chg < -0.0005:
            no_chg = s.token_chg("NO", sec)
            if no_chg < -0.02: return -1  # bullish divergence
        return 0

    def smart_money(s, sec=30):
        """Detect if tokens are moving without BTC cause.
        Returns: ('YES', strength) or ('NO', strength) or (None, 0)"""
        if len(s.data) < 5: return None, 0
        yes_chg = abs(s.token_chg("YES", sec))
        no_chg = abs(s.token_chg("NO", sec))
        # If YES is moving a lot (>3%) without context, someone knows something
        if yes_chg > 0.03:
            return "YES", yes_chg
        if no_chg > 0.03:
            return "NO", no_chg
        return None, 0

    def display_str(s):
        if len(s.data) < 3: return "warming"
        yc = s.token_chg("YES", 30) * 100
        nc = s.token_chg("NO", 30) * 100
        return f"Y:{yc:+.1f}% N:{nc:+.1f}%"


# ═══════════════════════════════════════════════════════════════
# ─── v8: ORDER BOOK INTELLIGENCE ───
# ═══════════════════════════════════════════════════════════════
class OrderBookIntel:
    """Reads the Polymarket order book for actionable intelligence.
    Detects: whale bids, book imbalance, support/resistance walls.
    Updated every time we fetch order books (~every 10 seconds)."""

    def __init__(s):
        s.yes_bid_depth = 0.0    # total $ on YES bid side (top 5 levels)
        s.yes_ask_depth = 0.0    # total $ on YES ask side
        s.no_bid_depth = 0.0
        s.no_ask_depth = 0.0
        s.yes_imbalance = 0.0    # >1 = more buyers, <1 = more sellers
        s.no_imbalance = 0.0
        s.whale_side = None      # "YES" or "NO" or None
        s.whale_size = 0.0       # dollar amount of whale order
        s.whale_price = 0.0
        s._last_update = 0

    def update(s, executor, market):
        """Fetch and analyze both order books. Call every 10-20 seconds."""
        if time.time() - s._last_update < 8: return  # rate limit
        s._last_update = time.time()
        if not executor.client or not executor.authed: return

        try:
            ybook = executor.client.get_order_book(market.tok_yes)
            nbook = executor.client.get_order_book(market.tok_no)
            s._analyze_book(ybook, "YES")
            s._analyze_book(nbook, "NO")
        except:
            pass

    def _analyze_book(s, book, side):
        """Analyze one side's order book."""
        if not isinstance(book, dict): return
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        # Calculate depth (top 5 levels)
        bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids[:5])
        ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:5])

        if side == "YES":
            s.yes_bid_depth = bid_depth
            s.yes_ask_depth = ask_depth
            s.yes_imbalance = bid_depth / ask_depth if ask_depth > 0 else 2.0
        else:
            s.no_bid_depth = bid_depth
            s.no_ask_depth = ask_depth
            s.no_imbalance = bid_depth / ask_depth if ask_depth > 0 else 2.0

        # Whale detection: single order > $200 in top 3 levels
        for bid in bids[:3]:
            order_size = float(bid.get("size", 0)) * float(bid.get("price", 0))
            if order_size > 200:
                if s.whale_side is None or order_size > s.whale_size:
                    s.whale_side = side
                    s.whale_size = order_size
                    s.whale_price = float(bid.get("price", 0))

    def get_imbalance(s, side):
        """Returns imbalance ratio. >1.5 = strong buy pressure, <0.5 = sell pressure."""
        return s.yes_imbalance if side == "YES" else s.no_imbalance

    def has_support(s, side, min_depth=50.0):
        """Is there meaningful bid support for this side?"""
        depth = s.yes_bid_depth if side == "YES" else s.no_bid_depth
        return depth >= min_depth

    def selling_pressure(s, side):
        """Is there heavy selling on this side? (thick ask wall)"""
        ask = s.yes_ask_depth if side == "YES" else s.no_ask_depth
        bid = s.yes_bid_depth if side == "YES" else s.no_bid_depth
        if bid <= 0: return True
        return ask / bid > 2.5  # asks 2.5x bigger than bids = heavy selling

    def display_str(s):
        parts = []
        if s.yes_bid_depth > 0:
            parts.append(f"Y:{s.yes_imbalance:.1f}x")
        if s.no_bid_depth > 0:
            parts.append(f"N:{s.no_imbalance:.1f}x")
        if s.whale_side:
            parts.append(f"🐋{s.whale_side}${s.whale_size:.0f}")
        return " ".join(parts) if parts else "book:--"


# ═══════════════════════════════════════════════════════════════
# ─── v8: PER-MARKET LOSS TRACKER ───
# ═══════════════════════════════════════════════════════════════
class MarketLossTracker:
    """Tracks losses per 15-minute market window. If too many losses,
    stops trading that market. Fresh start every new market.
    
    Rules:
    - 1st loss on a market: reduce next trade size by 50%
    - 2nd loss on a market: STOP trading this market entirely
    - New market = clean slate"""

    def __init__(s):
        s._losses = {}     # {slug: loss_count}
        s._current_slug = None

    def record_loss(s, slug):
        """Record a loss on this market."""
        s._losses[slug] = s._losses.get(slug, 0) + 1

    def get_penalty(s, slug):
        """Returns size multiplier. 1.0 = normal, 0.5 = reduced, 0.0 = blocked."""
        losses = s._losses.get(slug, 0)
        if losses >= 2: return 0.0   # STOP — this market is bad
        if losses >= 1: return 0.5   # reduce — first loss, be cautious
        return 1.0                   # full size

    def is_blocked(s, slug):
        """Should we stop trading this market?"""
        return s._losses.get(slug, 0) >= 2

    def cleanup(s):
        """Remove old entries (keep last 10)."""
        if len(s._losses) > 10:
            keys = list(s._losses.keys())
            for k in keys[:-10]:
                del s._losses[k]

    def display_str(s, slug):
        losses = s._losses.get(slug, 0)
        if losses >= 2: return "⛔STOP"
        if losses >= 1: return "⚠️-50%"
        return "✓"


# ═══════════════════════════════════════════════════════════════
# ─── v9: THE CORTEX — Unified Intelligence ───
# ═══════════════════════════════════════════════════════════════
class Cortex:
    """The brain. Sees everything. Hunts profit.

    Unlike the v8 Manager (which only adjusted bet sizes from win/loss),
    the Cortex has direct access to ALL raw data and makes its own decisions:

    PERCEPTION — reads raw data directly:
      BTC price, token prices, order book, volatility, regime, time
    MEMORY — learns from experience:
      Trust scores, market outcomes, danger zones, regime-strategy performance
    DECISION — outputs a state dict every system reads:
      strategy_trust, macro_bias, danger_zone, max_entry, session_mult

    KEY PRINCIPLE: Optimize for EV, not win rate.
    Never reduce Latency/Squeeze below 1.0x. Boost, don't restrict."""

    STRATS = ["ARB", "LATENCY", "MEANREV", "FLASH", "SQUEEZE", "PAIR", "SPIKE"]
    # Which regimes favor which strategies (from real trade data)
    REGIME_AFFINITY = {
        "LATENCY":  {"TRENDING_UP": 1.3, "TRENDING_DOWN": 1.3, "BREAKOUT": 1.5, "FLAT": 0.8, "CHOPPY": 0.7},
        "FLASH":    {"TRENDING_UP": 0.7, "TRENDING_DOWN": 0.7, "BREAKOUT": 0.6, "FLAT": 1.3, "CHOPPY": 1.0},
        "SQUEEZE":  {"TRENDING_UP": 1.0, "TRENDING_DOWN": 1.0, "BREAKOUT": 1.3, "FLAT": 0.8, "CHOPPY": 1.2},
        "MEANREV":  {"TRENDING_UP": 0.5, "TRENDING_DOWN": 0.5, "BREAKOUT": 0.8, "FLAT": 1.2, "CHOPPY": 1.3},
        "ARB":      {"TRENDING_UP": 1.0, "TRENDING_DOWN": 1.0, "BREAKOUT": 1.0, "FLAT": 1.0, "CHOPPY": 1.0},
    }

    def __init__(s):
        # ── Trust scores (EV-based, replaces Manager) ──
        s._trades = {st: deque(maxlen=20) for st in s.STRATS}
        s._trust = {st: 1.0 for st in s.STRATS}
        # v9.5: Per-slot trust (strategy × asset × timeframe)
        # Keys like "FLASH:btc-15m", "LATENCY:eth-15m", "SQUEEZE:btc-5m"
        s._slot_trades = {}   # {"FLASH:btc-15m": deque(maxlen=15)}
        s._slot_trust = {}    # {"FLASH:btc-15m": 1.0}

        # ── Outcome memory (per-asset cross-market momentum) ──
        # v9.5: Track each asset individually, combine for overall view
        s._asset_outcomes = {
            "btc": deque(maxlen=12),
            "eth": deque(maxlen=12),
            "sol": deque(maxlen=12),
            "xrp": deque(maxlen=12),
        }
        s._asset_bias = {a: "NEUTRAL" for a in s._asset_outcomes}
        s._asset_strength = {a: 0.0 for a in s._asset_outcomes}
        # Combined overall
        s._outcomes = deque(maxlen=12)  # kept for backward compat
        s._macro_bias = "NEUTRAL"
        s._macro_strength = 0.0

        # ── Danger zones (BTC price levels where we lose) ──
        s._loss_zones = {}   # {zone_key: {"losses": int, "wins": int}}
        s._zone_size = 500   # $500 BTC price buckets
        s._in_danger = False

        # ── Session tracking ──
        s._session_pnl = 0.0
        s._session_trades = 0
        s._session_wins = 0
        s._session_start = time.time()
        s._session_mult = 1.0  # 0.7–1.5

        # ── v9.1: Directional session bias ──
        # Track YES vs NO performance separately this session.
        # If one side is 0W/3L+, reduce it to 0.5x. Catches "YES is dead today."
        s._side_wins = {"YES": 0, "NO": 0}
        s._side_losses = {"YES": 0, "NO": 0}
        s._side_mult = {"YES": 1.0, "NO": 1.0}

        # ── v9.1: Recovery detection ──
        # After bleeding, detect when the bot starts winning again.
        # 2 consecutive wins after a losing session → snap back to 1.0x faster.
        s._consec_wins = 0
        s._consec_losses = 0

        # ── Regime-strategy performance (learned) ──
        s._regime_perf = {}  # {(strat, regime): {"wins": int, "losses": int, "pnl": float}}

        # ── Pattern discovery ──
        s._patterns = {}  # discovered correlations

        # ── v9.1: Market Lifecycle Model ──
        # Snapshots YES price at key timestamps (min 2, 5, 8, 12) for each market.
        # After 100+ markets, builds conditional probability tables:
        # "YES at $0.30-0.40 at minute 5 → UP 62% of the time"
        # This is a REAL predictive model trained on the market's own behavior.
        s._lifecycle_data = []   # list of completed market profiles
        s._lifecycle_file = "lifecycle_data.json"
        s._current_snapshot = {}  # {minute: yes_price} for the current market
        s._lifecycle_probs = {}   # {(minute_bucket, price_bucket): {"up": n, "down": n}}
        s._load_lifecycle()

        # ── Raw data access (set by Bot after init) ──
        s.feed = None       # BTC price feed
        s.token_feed = None # Polymarket token prices
        s.book_intel = None # Order book
        s.trend = None      # Trend engine

        # Bootstrap from history
        s._load_history()

    def _load_history(s):
        """Bootstrap from trade_data.json — read real field names."""
        try:
            with open("trade_data.json", "r") as f:
                data = json.load(f)
            history = data.get("history", [])
            for t in history[-60:]:
                strat = t.get("strat", "")
                pnl = t.get("pnl", 0)
                won = t.get("won", False)
                regime = t.get("regime", "UNKNOWN")
                btc_price = t.get("btc_price", 0)
                size = abs(pnl) if not won else max(abs(pnl) * 0.3, 1)

                # Feed trust scores
                if strat in s._trades and size > 0:
                    s._trades[strat].append({"won": won, "pnl": pnl, "size": size})

                # v9.5: Feed per-slot trust from slug in history
                slug = t.get("slug", "")
                if slug and strat in s.STRATS:
                    _h_parts = slug.split("-")
                    _h_asset = _h_parts[0] if _h_parts else "btc"
                    _h_tf = "5m" if "5m" in slug else "15m"
                    _h_sk = f"{strat}:{_h_asset}-{_h_tf}"
                    if _h_sk not in s._slot_trades:
                        s._slot_trades[_h_sk] = deque(maxlen=15)
                    s._slot_trades[_h_sk].append({"won": won, "pnl": pnl, "size": size})

                # Feed regime-strategy performance
                if strat and regime != "UNKNOWN":
                    key = (strat, regime)
                    if key not in s._regime_perf:
                        s._regime_perf[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
                    if won: s._regime_perf[key]["wins"] += 1
                    else: s._regime_perf[key]["losses"] += 1
                    s._regime_perf[key]["pnl"] += pnl

                # Feed danger zones
                if btc_price > 0:
                    zone = int(btc_price / s._zone_size) * s._zone_size
                    if zone not in s._loss_zones:
                        s._loss_zones[zone] = {"losses": 0, "wins": 0}
                    if won: s._loss_zones[zone]["wins"] += 1
                    else: s._loss_zones[zone]["losses"] += 1

            # Recalc all trust scores
            for st in s.STRATS:
                s._recalc_trust(st)
                if s._trust[st] < 0.40:
                    s._trust[st] = 0.40

            # v9.5: Recalc per-slot trust
            for sk in s._slot_trades:
                strat_name = sk.split(":")[0]
                s._recalc_slot_trust(sk, strat_name)

            # Log slot trust if any exist
            slot_info = [f"{sk}={v:.2f}" for sk, v in s._slot_trust.items() if len(s._slot_trades.get(sk, [])) >= 3]
            log.info(f"Cortex loaded: {', '.join(f'{st}={s._trust[st]:.2f}' for st in s.STRATS)}")
            if slot_info:
                log.info(f"Slot trust: {', '.join(slot_info[:10])}")
        except Exception as e:
            log.debug(f"Cortex history load: {e}")

    # ═══════════════════════════════════════════════
    #  PERCEPTION — Direct data reading
    # ═══════════════════════════════════════════════

    def perceive(s):
        """Called every loop iteration. Reads raw data, updates state."""
        # Update danger zone from live BTC price
        if s.feed and s.feed.price > 0:
            zone = int(s.feed.price / s._zone_size) * s._zone_size
            z = s._loss_zones.get(zone, {"losses": 0, "wins": 0})
            total = z["losses"] + z["wins"]
            s._in_danger = total >= 3 and z["losses"] / total > 0.65

        # Update session multiplier based on session P&L
        s._update_session_mult()

        # Update macro bias from outcome memory
        s._update_macro_bias()

    def _update_session_mult(s):
        """Session P&L drives aggression. Up = push. Down = slight pullback.
        v9.1: Recovery detection — 2 consecutive wins after a losing session
        snaps multiplier back up faster."""
        if s._session_trades < 2:
            s._session_mult = 1.0
            return
        bal = 4000  # approximate, gets overridden by real balance
        if s.feed:
            # Try to get real balance from context
            pass
        pnl_pct = s._session_pnl / max(bal, 1000)

        # Base session mult from P&L
        if pnl_pct > 0.10:      # up 10%+
            base = 1.3  # push harder, playing with profit
        elif pnl_pct > 0.05:    # up 5%+
            base = 1.15
        elif pnl_pct > -0.05:   # within ±5%
            base = 1.0
        elif pnl_pct > -0.15:   # down 5-15%
            base = 0.90  # v9.4: gentle pullback (was 0.85)
        else:                    # down 15%+
            base = 0.85  # v9.4: floor raised (was 0.7) — 30% WR means losing streaks are NORMAL

        # v9.1: Recovery detection — if we're in CAREFUL mode but just won 2+ in a row,
        # the market turned. Snap back up. Don't stay scared when things are working.
        if base < 1.0 and s._consec_wins >= 2:
            base = min(1.0, base + 0.2)  # recover 0.2 per 2-win streak, max 1.0

        s._session_mult = base

    def _update_side_mult(s):
        """v9.4: Gentler side bias. At 30% WR, losing streaks per side are NORMAL.
        Only slight reduction, never crush a side."""
        for side in ("YES", "NO"):
            w = s._side_wins[side]
            l = s._side_losses[side]
            if l >= 3 and w == 0:
                s._side_mult[side] = 0.7   # v9.4: was 0.4 — too aggressive
            elif l >= 2 and w == 0:
                s._side_mult[side] = 0.8   # v9.4: was 0.6
            elif l > w + 2:
                s._side_mult[side] = 0.85  # v9.4: was 0.7
            else:
                s._side_mult[side] = 1.0   # fine

    def get_side_mult(s, side):
        """v9.1: Returns directional multiplier for this side."""
        return s._side_mult.get(side, 1.0)

    def get_lifecycle_mult(s, side, minute, yes_price):
        """v9.4: Simplified. Slight boost for aligned trades, NO reduction.
        Multiple stacking multipliers were killing trade size."""
        prob_up, confidence = s.get_lifecycle_edge(minute, yes_price)
        if prob_up is None or confidence < 0.15:
            return 1.0  # not enough data or too close to 50/50

        # Boost aligned trades, but never reduce below 0.85
        if side == "YES" and prob_up >= 0.60:
            return 1.0 + confidence * 0.3  # max 1.3x boost
        elif side == "NO" and prob_up <= 0.40:
            return 1.0 + confidence * 0.3
        return 0.85  # slight reduction for counter-lifecycle, not 0.6x

    def _update_macro_bias(s):
        """Overall cross-market momentum from ALL asset outcomes combined."""
        if len(s._outcomes) < 3:
            s._macro_bias = "NEUTRAL"
            s._macro_strength = 0.0
            return
        recent = list(s._outcomes)[-6:]  # last 6 markets
        ups = sum(1 for x in recent if x)
        downs = len(recent) - ups
        ratio = ups / len(recent)

        if ratio >= 0.70:  # 70%+ UP
            s._macro_bias = "YES"
            s._macro_strength = min(0.3, (ratio - 0.5) * 0.6)
        elif ratio <= 0.30:  # 70%+ DOWN
            s._macro_bias = "NO"
            s._macro_strength = min(0.3, (0.5 - ratio) * 0.6)
        else:
            s._macro_bias = "NEUTRAL"
            s._macro_strength = 0.0

    def _update_asset_bias(s, asset):
        """Per-asset momentum tracking."""
        outcomes = s._asset_outcomes.get(asset, deque())
        if len(outcomes) < 3:
            s._asset_bias[asset] = "NEUTRAL"
            s._asset_strength[asset] = 0.0
            return
        recent = list(outcomes)[-6:]
        ups = sum(1 for x in recent if x)
        ratio = ups / len(recent)
        if ratio >= 0.70:
            s._asset_bias[asset] = "YES"
            s._asset_strength[asset] = min(0.3, (ratio - 0.5) * 0.6)
        elif ratio <= 0.30:
            s._asset_bias[asset] = "NO"
            s._asset_strength[asset] = min(0.3, (0.5 - ratio) * 0.6)
        else:
            s._asset_bias[asset] = "NEUTRAL"
            s._asset_strength[asset] = 0.0

    # ═══════════════════════════════════════════════
    #  MEMORY — Learn from every trade
    # ═══════════════════════════════════════════════

    def record_trade(s, strat, won, pnl, size, regime="UNKNOWN", btc_price=0, side="", slot_key=""):
        """Record a trade result. Updates ALL memory systems."""
        # Trust scores (global per-strategy)
        if strat in s._trades:
            s._trades[strat].append({"won": won, "pnl": pnl, "size": size})
            s._recalc_trust(strat)

        # v9.5: Per-slot trust (strategy × asset × timeframe)
        if slot_key and strat in s.STRATS:
            sk = f"{strat}:{slot_key}"
            if sk not in s._slot_trades:
                s._slot_trades[sk] = deque(maxlen=15)
            s._slot_trades[sk].append({"won": won, "pnl": pnl, "size": size})
            s._recalc_slot_trust(sk, strat)

        # Session tracking
        s._session_pnl += pnl
        s._session_trades += 1
        if won: s._session_wins += 1

        # v9.1: Directional session tracking
        if side in ("YES", "NO"):
            if won:
                s._side_wins[side] += 1
            else:
                s._side_losses[side] += 1
            s._update_side_mult()

        # v9.1: Recovery detection — track consecutive wins/losses
        if won:
            s._consec_wins += 1
            s._consec_losses = 0
        else:
            s._consec_losses += 1
            s._consec_wins = 0

        # Regime-strategy performance
        if regime != "UNKNOWN":
            key = (strat, regime)
            if key not in s._regime_perf:
                s._regime_perf[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
            if won: s._regime_perf[key]["wins"] += 1
            else: s._regime_perf[key]["losses"] += 1
            s._regime_perf[key]["pnl"] += pnl

        # Danger zones
        if btc_price > 0:
            zone = int(btc_price / s._zone_size) * s._zone_size
            if zone not in s._loss_zones:
                s._loss_zones[zone] = {"losses": 0, "wins": 0}
            if won: s._loss_zones[zone]["wins"] += 1
            else: s._loss_zones[zone]["losses"] += 1

        # Pattern discovery (run every 10 trades)
        if s._session_trades % 10 == 0:
            s._discover_patterns()

    def record_outcome(s, went_up, asset="btc"):
        """Record a market outcome (UP/DOWN) for a specific asset."""
        # Per-asset tracking
        if asset in s._asset_outcomes:
            s._asset_outcomes[asset].append(went_up)
            s._update_asset_bias(asset)
        # Overall tracking
        s._outcomes.append(went_up)
        s._update_macro_bias()

    def _recalc_trust(s, strat):
        """EV-based trust calculation. Rewards expected value, not just win rate."""
        trades = list(s._trades.get(strat, []))
        n = len(trades)
        if n < 3:
            s._trust[strat] = 1.0
            return

        recent = trades[-10:]
        wins = sum(1 for t in recent if t["won"])
        total = len(recent)
        wr = wins / total

        total_pnl = sum(t["pnl"] for t in recent)
        total_wagered = sum(t["size"] for t in recent)
        roi = total_pnl / total_wagered if total_wagered > 0 else 0

        # EV-based: positive ROI = good regardless of win rate
        if roi > 0.30:
            trust = 1.8 + min(roi * 0.5, 0.7)  # 1.8–2.5 (crushing it)
        elif roi > 0.10:
            trust = 1.3 + roi * 2  # 1.3–1.8 (solidly profitable)
        elif roi > 0:
            trust = 1.0 + roi * 3  # 1.0–1.3 (marginally profitable)
        elif roi > -0.20:
            trust = 0.5 + max(roi + 0.20, 0) * 2.5  # 0.5–1.0 (slightly losing)
        else:
            trust = 0.25  # heavily negative EV

        # Momentum: last 3 trades
        last3 = trades[-3:]
        if all(t["won"] for t in last3):
            trust *= 1.2  # hot streak
        elif all(not t["won"] for t in last3):
            trust *= 0.7  # bleeding

        # SACRED RULE: Latency and Squeeze never go below 1.0
        if strat in ("LATENCY", "SQUEEZE"):
            trust = max(1.0, trust)

        # Strategy with 6+ trades and 0 wins = disabled
        if n >= 6 and wins == 0:
            trust = 0.0

        s._trust[strat] = round(max(0.0, min(2.5, trust)), 2)
        # v9.4: Floor at 0.40 ALWAYS — not just at startup
        # Flash went to 0.10x during live trading = $26 bets = useless
        # At 30% WR, losing streaks are NORMAL. Don't crush strategies for it.
        if strat not in ("LATENCY", "SQUEEZE") and s._trust[strat] < 0.40 and s._trust[strat] > 0.0:
            s._trust[strat] = 0.40

    def _recalc_slot_trust(s, sk, strat):
        """Per-slot trust. Same EV logic as global but per asset+timeframe.
        Falls back to global trust until 3+ trades on this slot."""
        trades = list(s._slot_trades.get(sk, []))
        n = len(trades)
        if n < 3:
            s._slot_trust[sk] = s._trust.get(strat, 1.0)
            return
        recent = trades[-10:]
        wins = sum(1 for t in recent if t["won"])
        total = len(recent)
        total_pnl = sum(t["pnl"] for t in recent)
        total_wagered = sum(t["size"] for t in recent)
        roi = total_pnl / total_wagered if total_wagered > 0 else 0
        if roi > 0.30:
            trust = 1.8 + min(roi * 0.5, 0.7)
        elif roi > 0.10:
            trust = 1.3 + roi * 2
        elif roi > 0:
            trust = 1.0 + roi * 3
        elif roi > -0.20:
            trust = 0.5 + max(roi + 0.20, 0) * 2.5
        else:
            trust = 0.25
        last3 = trades[-3:]
        if all(t["won"] for t in last3): trust *= 1.2
        elif all(not t["won"] for t in last3): trust *= 0.7
        if strat in ("LATENCY", "SQUEEZE"): trust = max(1.0, trust)
        if n >= 6 and wins == 0: trust = 0.0
        s._slot_trust[sk] = round(max(0.0, min(2.5, trust)), 2)
        if strat not in ("LATENCY", "SQUEEZE") and s._slot_trust[sk] < 0.40 and s._slot_trust[sk] > 0.0:
            s._slot_trust[sk] = 0.40

    def _discover_patterns(s):
        """Scan trade history for patterns nobody programmed.
        Looks for correlations between conditions and outcomes."""
        patterns = {}

        # Pattern 1: Do we lose more on first trade after restart?
        # (Can't check this without restart tracking — future)

        # Pattern 2: Which entry price ranges win most?
        for strat in s.STRATS:
            trades = list(s._trades.get(strat, []))
            if len(trades) < 5: continue
            # Check if cheap entries (<$0.15) have better EV than expensive ones
            cheap = [t for t in trades if t.get("size", 0) < 20]
            expensive = [t for t in trades if t.get("size", 0) >= 20]
            if cheap and expensive:
                cheap_wr = sum(1 for t in cheap if t["won"]) / len(cheap)
                exp_wr = sum(1 for t in expensive if t["won"]) / len(expensive)
                if cheap_wr > exp_wr + 0.15:
                    patterns[f"{strat}_prefers_small"] = True

        s._patterns = patterns

    # ═══════════════════════════════════════════════
    #  v9.1: MARKET LIFECYCLE MODEL
    # ═══════════════════════════════════════════════

    def _load_lifecycle(s):
        """Load historical lifecycle data and rebuild probability tables."""
        try:
            with open(s._lifecycle_file, "r") as f:
                s._lifecycle_data = json.load(f)
            s._rebuild_lifecycle_probs()
            log.info(f"Lifecycle loaded: {len(s._lifecycle_data)} markets, {len(s._lifecycle_probs)} probability buckets")
        except:
            s._lifecycle_data = []

    def _save_lifecycle(s):
        """Persist lifecycle data to disk."""
        try:
            # Keep last 500 markets
            data = s._lifecycle_data[-500:]
            with open(s._lifecycle_file, "w") as f:
                json.dump(data, f)
        except: pass

    def lifecycle_snapshot(s, minute, yes_price, btc_chg_from_open):
        """Take a snapshot of current market state at a key minute mark.
        Called from the main loop at minutes 2, 4, 6, 8, 10, 12."""
        s._current_snapshot[minute] = {
            "yes": round(yes_price, 3),
            "btc_chg": round(btc_chg_from_open * 10000, 1),  # basis points
        }

    def lifecycle_close(s, went_up, yes_final):
        """Market resolved. Store the complete lifecycle profile."""
        if not s._current_snapshot:
            return
        profile = {
            "snapshots": dict(s._current_snapshot),
            "outcome": "UP" if went_up else "DOWN",
            "yes_final": round(yes_final, 3),
        }
        s._lifecycle_data.append(profile)
        s._current_snapshot = {}

        # Keep memory bounded
        if len(s._lifecycle_data) > 500:
            s._lifecycle_data = s._lifecycle_data[-500:]

        # Rebuild probability tables every 10 markets
        if len(s._lifecycle_data) % 10 == 0:
            s._rebuild_lifecycle_probs()
            s._save_lifecycle()

    def _rebuild_lifecycle_probs(s):
        """Build conditional probability tables from all lifecycle data.
        Groups by (minute, price_bucket) → P(UP)."""
        probs = {}
        for prof in s._lifecycle_data:
            outcome_up = prof["outcome"] == "UP"
            for min_str, snap in prof.get("snapshots", {}).items():
                minute = int(min_str)
                yes_p = snap["yes"]
                # Price buckets: 0.00-0.10, 0.10-0.20, ..., 0.90-1.00
                price_bucket = min(9, int(yes_p * 10))  # 0-9
                # Minute buckets: early(2-4), mid(5-8), late(9-12)
                if minute <= 4: min_bucket = "early"
                elif minute <= 8: min_bucket = "mid"
                else: min_bucket = "late"

                key = f"{min_bucket}_{price_bucket}"
                if key not in probs:
                    probs[key] = {"up": 0, "down": 0}
                if outcome_up:
                    probs[key]["up"] += 1
                else:
                    probs[key]["down"] += 1
        s._lifecycle_probs = probs

    def get_lifecycle_edge(s, minute, yes_price):
        """Query the lifecycle model: what's the probability this market goes UP
        given the current minute and YES price?
        Returns (probability, confidence) or (None, 0) if not enough data."""
        price_bucket = min(9, int(yes_price * 10))
        if minute <= 4: min_bucket = "early"
        elif minute <= 8: min_bucket = "mid"
        else: min_bucket = "late"

        key = f"{min_bucket}_{price_bucket}"
        data = s._lifecycle_probs.get(key)
        if not data:
            return None, 0
        total = data["up"] + data["down"]
        if total < 10:  # need at least 10 samples
            return None, 0
        prob_up = data["up"] / total
        # Confidence: how far from 50/50. 0.5 = no edge, 0.7 = good edge
        confidence = abs(prob_up - 0.5) * 2  # 0.0-1.0
        return round(prob_up, 3), round(confidence, 3)

    # ═══════════════════════════════════════════════
    #  DECISION — What strategies read
    # ═══════════════════════════════════════════════

    def get_trust(s, strat, slot_key=""):
        """Trust score with per-slot + regime affinity applied.
        v9.5: Uses per-slot trust if available (3+ trades), else global."""
        # v9.5: Per-slot trust takes priority if we have data
        if slot_key:
            sk = f"{strat}:{slot_key}"
            if sk in s._slot_trust and len(s._slot_trades.get(sk, [])) >= 3:
                base_trust = s._slot_trust[sk]
            else:
                base_trust = s._trust.get(strat, 1.0)
        else:
            base_trust = s._trust.get(strat, 1.0)

        # Apply regime affinity if we know current regime
        regime_mult = 1.0
        if s.trend:
            regime = s.trend.regime
            regime_mult = s.REGIME_AFFINITY.get(strat, {}).get(regime, 1.0)

            # Override with learned data if we have enough
            key = (strat, regime)
            perf = s._regime_perf.get(key)
            if perf and (perf["wins"] + perf["losses"]) >= 5:
                learned_wr = perf["wins"] / (perf["wins"] + perf["losses"])
                if learned_wr > 0.55:
                    regime_mult = max(regime_mult, 1.3)
                elif learned_wr < 0.30:
                    regime_mult = min(regime_mult, 0.6)

        trust = base_trust * regime_mult

        # SACRED: Latency and Squeeze minimum 1.0
        if strat in ("LATENCY", "SQUEEZE"):
            trust = max(1.0, trust)

        return round(max(0.0, min(2.5, trust)), 2)

    def get_macro_mult(s, side, asset="btc"):
        """Macro bias multiplier. Uses per-asset bias if available, falls back to overall.
        Boosts trades aligned with that asset's momentum."""
        # v9.5: Use per-asset bias first
        bias = s._asset_bias.get(asset, s._macro_bias)
        strength = s._asset_strength.get(asset, s._macro_strength)
        # If per-asset is neutral but overall has signal, use overall at half strength
        if bias == "NEUTRAL" and s._macro_bias != "NEUTRAL":
            bias = s._macro_bias
            strength = s._macro_strength * 0.5
        if bias == "NEUTRAL" or strength < 0.05:
            return 1.0
        if bias == side:
            return 1.0 + strength
        else:
            return 1.0 - (strength * 0.5)

    def get_session_mult(s):
        """Session-level aggression multiplier."""
        return s._session_mult

    def get_danger_mult(s):
        """Danger zone reduction. Disabled in v9.3 — not enough data to justify blocking."""
        return 1.0

    def get_max_entry(s, strat):
        """Tighter entry requirements for low-trust strategies.
        PAIR/SPIKE have their own entry logic, trust can't override."""
        if strat == "PAIR": return 0.50  # Pair can go up to $0.40 to complete a pair
        if strat == "SPIKE": return 0.30  # Spike buys panic sells
        trust = s._trust.get(strat, 1.0)
        defaults = {"ARB": 0.50, "LATENCY": 0.50, "MEANREV": 0.40, "FLASH": 0.50, "SQUEEZE": 0.40, "PAIR": 0.50, "SPIKE": 0.30}
        base = defaults.get(strat, 0.25)
        if trust < 0.5:
            return base * 0.75  # tighten by 25% for untrusted
        return base

    def is_disabled(s, strat):
        """Fully disabled strategies (0.0 trust)."""
        return s._trust.get(strat, 1.0) <= 0.0

    # ═══════════════════════════════════════════════
    #  DISPLAY
    # ═══════════════════════════════════════════════

    def display_trust(s):
        """Compact trust display for dashboard."""
        parts = []
        for st in s.STRATS:
            trust = s.get_trust(st)
            n = len(s._trades.get(st, []))
            if trust <= 0.0:
                tag = f"{st[:3]}:☠"
            elif n < 3:
                tag = f"{st[:3]}:--"
            elif trust >= 1.5:
                tag = f"{st[:3]}:🔥{trust:.1f}"
            elif trust >= 1.0:
                tag = f"{st[:3]}:{trust:.1f}"
            elif trust >= 0.5:
                tag = f"{st[:3]}:⚠{trust:.1f}"
            else:
                tag = f"{st[:3]}:❄{trust:.1f}"
            parts.append(tag)
        return " ".join(parts)

    def display_state(s):
        """Full Cortex state for dashboard."""
        # Session info
        elapsed = int(time.time() - s._session_start)
        mins = elapsed // 60
        wr = (s._session_wins / s._session_trades * 100) if s._session_trades > 0 else 0

        lines = []
        lines.append(f"Session: {s._session_trades}t {s._session_wins}W {wr:.0f}% {pnl_c2(s._session_pnl)}")
        lines.append(f"Macro: {s._macro_bias} ({s._macro_strength:.0%})")
        if s._in_danger:
            lines.append(f"⚠ DANGER ZONE (BTC ${int(s.feed.price / s._zone_size) * s._zone_size if s.feed else 0})")
        # v9.1: Show side bias
        y_m, n_m = s._side_mult["YES"], s._side_mult["NO"]
        side_str = ""
        if y_m < 1.0: side_str += f" YES↓{y_m:.1f}x"
        if n_m < 1.0: side_str += f" NO↓{n_m:.1f}x"
        if side_str: lines.append(f"Side bias:{side_str}")
        # v9.1: Show lifecycle model
        lc_n = len(s._lifecycle_data)
        if lc_n > 0:
            lines.append(f"Lifecycle: {lc_n} mkts, {len(s._lifecycle_probs)} buckets")
        # v9.1: Show recovery
        mode = "PUSH" if s._session_mult >= 1.1 else "NORMAL" if s._session_mult >= 0.9 else "CAREFUL"
        recover_tag = f" ↑recovering" if s._session_mult < 1.0 and s._consec_wins >= 2 else ""
        lines.append(f"Mode: {mode} ({s._session_mult:.1f}x){recover_tag}")
        return lines


# ═══════════════════════════════════════════════════════════════
# ─── v7: DATA COLLECTOR ───
# ═══════════════════════════════════════════════════════════════
class DataCollector:
    """Comprehensive trade data collection to CSV for analysis.
    Records everything needed to optimize the bot."""
    TRADE_CSV = "trade_log.csv"
    MARKET_CSV = "market_log.csv"
    TICK_CSV = "tick_log.csv"

    def __init__(s):
        s._market_trades = {}  # {slug: [trade_dicts]}
        s._current_market = None
        s._tick_count = 0
        s._init_csvs()

    def _init_csvs(s):
        """Create CSV headers if files don't exist."""
        import csv
        if not os.path.exists(s.TRADE_CSV):
            with open(s.TRADE_CSV, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "timestamp", "market_slug", "strategy", "side", "entry_price",
                    "shares", "cost", "pnl", "won", "regime", "trend_dir",
                    "trend_strength", "volatility", "btc_price_entry", "btc_price_exit",
                    "btc_chg_1m", "btc_chg_5m", "market_time_left_sec",
                    "conviction_count", "conviction_bonus", "streak_mult",
                    "tod_mult", "hour_utc", "fill_time_ms", "order_type"
                ])
        if not os.path.exists(s.MARKET_CSV):
            with open(s.MARKET_CSV, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "timestamp", "market_slug", "total_trades", "wins", "losses",
                    "total_pnl", "total_wagered", "regime_at_start", "regime_at_end",
                    "btc_open", "btc_close", "btc_change_pct",
                    "strategies_used", "conviction_triggered", "best_trade_pnl",
                    "worst_trade_pnl", "duration_sec"
                ])

    def record_trade(s, pos, btc_entry, btc_exit, feed, trend, conviction_count=0,
                     conviction_bonus=1.0, streak_mult=1.0, tod_mult=1.0,
                     fill_time_ms=0, order_type="market", market_tl=0):
        """Record a completed trade to CSV."""
        import csv
        try:
            ts = pos.opened.strftime('%Y-%m-%d %H:%M:%S') if pos.opened else ""
            hour = pos.opened.hour if pos.opened else 0
            chg_1m = feed.chg(60) if feed else 0
            chg_5m = feed.chg(300) if feed else 0
            vol = feed.volatility() if feed else 0
            regime = trend.regime if trend else ""
            t_dir = trend.trend_dir if trend else 0
            t_str = trend.trend_strength if trend else 0

            with open(s.TRADE_CSV, "a", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    ts, pos.slug, pos.strat, pos.side, f"{pos.entry:.4f}",
                    f"{pos.shares:.2f}", f"{pos.cost:.2f}", f"{pos.pnl:.2f}",
                    1 if pos.pnl > 0 else 0, regime, t_dir,
                    f"{t_str:.3f}", f"{vol:.6f}", f"{btc_entry:.2f}", f"{btc_exit:.2f}",
                    f"{chg_1m*100:.4f}", f"{chg_5m*100:.4f}", f"{market_tl:.0f}",
                    conviction_count, f"{conviction_bonus:.2f}", f"{streak_mult:.2f}",
                    f"{tod_mult:.2f}", hour, fill_time_ms, order_type
                ])

            # Track per-market
            slug = pos.slug
            if slug not in s._market_trades:
                s._market_trades[slug] = []
            s._market_trades[slug].append({
                "strat": pos.strat, "side": pos.side, "pnl": pos.pnl,
                "cost": pos.cost, "won": pos.pnl > 0, "regime": regime
            })
        except Exception as e:
            log.debug(f"CSV write error: {e}")

    def close_market(s, slug, btc_open, btc_close, start_regime, end_regime, duration=900):
        """Record market-level summary when a 15-min market ends."""
        import csv
        trades = s._market_trades.get(slug, [])
        if not trades: return
        try:
            wins = sum(1 for t in trades if t["won"])
            losses = len(trades) - wins
            total_pnl = sum(t["pnl"] for t in trades)
            total_wagered = sum(t["cost"] for t in trades)
            strats_used = ",".join(sorted(set(t["strat"] for t in trades)))
            conviction = len(set(t["strat"] for t in trades)) >= 2
            best = max(t["pnl"] for t in trades) if trades else 0
            worst = min(t["pnl"] for t in trades) if trades else 0
            btc_chg = ((btc_close - btc_open) / btc_open * 100) if btc_open else 0

            with open(s.MARKET_CSV, "a", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                    slug, len(trades), wins, losses,
                    f"{total_pnl:.2f}", f"{total_wagered:.2f}",
                    start_regime, end_regime,
                    f"{btc_open:.2f}", f"{btc_close:.2f}", f"{btc_chg:.4f}",
                    strats_used, 1 if conviction else 0,
                    f"{best:.2f}", f"{worst:.2f}", f"{duration:.0f}"
                ])

            # Clean up
            if slug in s._market_trades:
                del s._market_trades[slug]
        except Exception as e:
            log.debug(f"Market CSV error: {e}")

    def get_stats(s):
        """Return quick stats from CSV for dashboard display."""
        try:
            if not os.path.exists(s.TRADE_CSV): return {}
            import csv
            total = wins = 0
            pnl_sum = 0.0
            with open(s.TRADE_CSV, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    total += 1
                    if row.get("won") == "1": wins += 1
                    pnl_sum += float(row.get("pnl", 0))
            return {"total": total, "wins": wins, "wr": (wins/total*100) if total else 0,
                    "pnl": pnl_sum}
        except: return {}


# ═══════════════════════════════════════════════════════════════
# ─── STRATEGIES (v7: Research-backed for Polymarket 15-min BTC) ───
# Sources: gabagool ($58/trade), $313→$414K latency bot (98% WR),
# "Efficient Coder" spike bot (86% ROI), Dutch book arbitrage,
# Polymarket taker fee analysis, QuantVPS HFT research
# ═══════════════════════════════════════════════════════════════

class S_Arb:
    """TRUE ARBITRAGE: Buy the cheaper side when YES+NO sum < $0.95.
    v10: Tighter threshold (was 0.96) — backtest showed ARB2_95 is best:
    288t, 42% WR, +$5,069 over 30 days. Bigger edge per trade."""
    def __init__(s, c): s.c = c; s.market_slug = None
    def reset(s, slug):
        if s.market_slug != slug: s.market_slug = slug
    def check(s, m, trend):
        if not s.c.arb_enabled: return None
        s.reset(m.slug)
        yp, np_ = m.yes_p, m.no_p
        pair = yp + np_
        # v10: Need sum < $0.95 → 5%+ edge (was 0.96)
        if pair >= 0.95: return None
        buy_yes = yp < np_
        price = yp if buy_yes else np_
        # Only buy cheap sides — max $0.38
        if price < 0.08 or price > 0.38: return None
        side = "YES" if buy_yes else "NO"
        return {"s": "ARB", "side": side, "yes": buy_yes, "price": price,
                "pair": pair, "profit": 1.0 - pair, "sz": 0}


class S_Latency:
    """LATENCY ARBITRAGE — buys when BTC moves but Polymarket hasn't repriced.
    
    v10.1: Fixed the $0.53 coin-flip problem.
    - Requires 0.15%+ BTC move from market OPEN (not 30s spike)
    - Capped at $0.45 (not $0.55 — above $0.45 is coin flip territory)
    - Confidence formula scaled down (was giving 70% on 0.1% moves)
    - Uses Chainlink settlement price as truth."""
    def __init__(s, c): s.c = c
    def check(s, m, f, trend):
        if not s.c.latency_enabled or f.n < 10: return None
        tl = (m.end - datetime.now(timezone.utc)).total_seconds() if m.end else 999
        min_tl = 120 if m.timeframe == 5 else 240
        if tl < min_tl: return None

        # v10.1: PRIMARY signal = move from market open (this is what settles)
        chg_open = 0
        if m.open_btc > 0:
            settle_price = f.settlement_price if hasattr(f, 'settlement_price') else f.price
            if settle_price > 0:
                chg_open = (settle_price - m.open_btc) / m.open_btc
        
        # v10.1: SECONDARY = recent momentum (confirms direction, not primary)
        binance_chg_30 = f.chg(30)
        binance_chg_60 = f.chg(60)
        
        # v10.1: chg_open is the MAIN signal since that's what settles
        # Only use 30s/60s as CONFIRMATION, not as the trigger
        chg = chg_open
        if abs(chg_open) < 0.0005:
            # Open change is tiny → no real signal
            return None
        
        # v10.1: Require 0.15% from open in normal, 0.10% in breakout
        # Old: 0.10%/0.07% → way too sensitive, entered on noise
        min_chg = 0.0010 if (trend and trend.regime == "BREAKOUT") else 0.0015
        if abs(chg) < min_chg: return None
        
        # Secondary momentum must AGREE (not contradict)
        if binance_chg_30 != 0:
            if (chg > 0 and binance_chg_30 < -0.0003) or (chg < 0 and binance_chg_30 > 0.0003):
                return None  # BTC reversing in last 30s — don't chase
        
        up = chg > 0
        
        # v9.5: Chainlink must agree
        cl_chg = f.cl_chg(120) if hasattr(f, 'cl_chg') else 0
        if cl_chg != 0:
            cl_up = cl_chg > 0
            if cl_up != up:
                return None  # Chainlink disagrees

        target_price = m.yes_p if up else m.no_p
        # v10.1: Hard cap $0.45 — above this is coin flip territory
        # At $0.45: risk $0.45 to win $0.55 = need 45% WR (achievable)
        # At $0.50: risk $0.50 to win $0.50 = need 50% WR (no edge)
        # At $0.53: risk $0.53 to win $0.47 = need 53% WR (negative edge)
        if target_price > 0.45 or target_price < 0.15: return None

        other_price = m.no_p if up else m.yes_p
        if other_price < 0.10: return None

        # v10.1: Realistic confidence — scaled to BTC move size
        # 0.15% move = 55% conf, 0.30% move = 65% conf, 0.50%+ = 75% conf
        # Old formula: 0.60 + abs(chg)*100 → gave 70% on 0.10% move (fantasy)
        confidence = min(0.85, 0.50 + abs(chg) * 50)
        if trend and ((up and trend.trend_dir > 0) or (not up and trend.trend_dir < 0)):
            confidence = min(0.88, confidence + 0.05)

        edge = confidence - target_price
        # v10.1: need real edge — 0.08 minimum
        # At $0.40 with 0.15% move: conf=0.575, edge=0.175 → PASS
        # At $0.45 with 0.15% move: conf=0.575, edge=0.125 → PASS
        # At $0.45 with 0.10% move: wouldn't reach here (min_chg blocks)
        if edge < 0.08: return None

        return {"s": "LATENCY", "dir": "YES" if up else "NO", "yes": up,
                "edge": edge, "pred": confidence, "p": target_price, "chg": chg, "sz": 0}


class S_MeanReversion:
    """v8: MEAN REVERSION — Detects overextended token moves and buys the bounce.
    
    Different from Flash: Flash buys cheap tokens. Mean Reversion buys the BOUNCE.
    It waits for the drop to STOP (deceleration) before entering.
    
    Logic:
    1. Token price dropped significantly in last 60-90 seconds (overextended)
    2. The drop is SLOWING DOWN (velocity turning positive = deceleration)
    3. BTC is not confirming the drop (divergence or flattening)
    4. Order book shows bid support forming (buyers stepping in)
    
    Entry: $0.10-$0.35 range (wider than Flash — catches mid-range bounces)
    R:R: 2-5x depending on entry
    Expected WR: 35-45% (higher than Flash because we wait for confirmation)"""
    
    def __init__(s, c):
        s.c = c
        s._last_signal = {}  # v9.5: per-slug cooldowns
        s.scores = {}
    
    def check(s, m, f, trend, token_feed, book_intel):
        """Check for mean reversion setup. Needs token_feed and book_intel."""
        if f.n < 20: return None
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        min_tl = 120 if m.timeframe == 5 else 240
        if tl < min_tl: return None
        
        # Cooldown: 60 seconds between signals per market
        if time.time() - s._last_signal.get(m.slug, 0) < 60: return None
        
        # Check both sides for overextension
        for side, is_yes, price in [("YES", True, m.yes_p), ("NO", False, m.no_p)]:
            signal = s._check_side(side, is_yes, price, m, f, trend, token_feed, book_intel)
            if signal:
                s._last_signal[m.slug] = time.time()
                return signal
        
        return None
    
    def _check_side(s, side, is_yes, price, m, f, trend, token_feed, book_intel):
        """Check if one side is overextended and ready to bounce.
        v10: TWO paths — simple BTC bounce (backtest-proven) + token velocity."""
        
        # ── PATH 1: BACKTEST-PROVEN SIMPLE ($0.30-$0.45, BTC bounce) ──
        # Strategy lab: MR1_current = 886t, 39.6% WR, +$2,613
        if 0.30 <= price <= 0.45:
            other_price = m.no_p if is_yes else m.yes_p
            if other_price >= 0.55:
                c30 = f.chg(30); c2m = f.chg(120)
                # YES side: BTC bouncing up (30s up, 2m was down)
                if is_yes and c30 > 0.0002 and c2m < -0.0003:
                    return {"s": "MEANREV", "dir": side, "yes": is_yes,
                            "price": price, "conf": 0.65, "drop": c2m,
                            "velocity": c30, "sz": 0}
                # NO side: BTC bouncing down (30s down, 2m was up)
                if not is_yes and c30 < -0.0002 and c2m > 0.0003:
                    return {"s": "MEANREV", "dir": side, "yes": is_yes,
                            "price": price, "conf": 0.65, "drop": c2m,
                            "velocity": c30, "sz": 0}
        
        # ── PATH 2: ORIGINAL TOKEN VELOCITY ($0.10-$0.22) ──
        if price < 0.10 or price > 0.22: return None
        
        # 2. Token must have DROPPED significantly recently
        if token_feed.n < 5: return None
        token_drop = token_feed.token_chg(side, 60)  # 1-min change
        
        # Need a REAL drop: > 15% decline (e.g. $0.20 → $0.17)
        if token_drop > -0.15: return None  # not overextended enough
        
        # 3. The drop must be DECELERATING (velocity turning positive)
        velocity = token_feed.token_velocity(side, 30)
        
        # Velocity > 0 means: price was dropping but is now dropping LESS (or rising)
        # This is the key signal — the selling is exhausting
        if velocity < 0.001: return None  # still accelerating down, don't catch falling knife
        
        # 4. BTC should NOT be confirming the drop
        btc_chg = f.chg(60)
        if is_yes and btc_chg < -0.002: return None   # BTC still crashing, YES drop is justified
        if not is_yes and btc_chg > 0.002: return None  # BTC still pumping, NO drop is justified
        
        # 5. Order book support check (if available)
        book_ok = True
        if book_intel and book_intel.yes_bid_depth > 0:  # book data available
            if book_intel.selling_pressure(side):
                book_ok = False  # heavy selling, don't buy the dip yet
            imbalance = book_intel.get_imbalance(side)
            if imbalance < 0.4:
                book_ok = False  # sellers dominating, wait
        
        if not book_ok: return None
        
        # 6. Regime check: don't mean-revert against strong trends
        regime = trend.regime if trend else ""
        if is_yes and regime == "TRENDING_DOWN" and trend.trend_strength < -0.40:
            return None  # strong downtrend, YES drop is real
        if not is_yes and regime == "TRENDING_UP" and trend.trend_strength > 0.40:
            return None  # strong uptrend, NO drop is real
        
        # Calculate confidence based on signal strength
        drop_strength = min(abs(token_drop) * 5, 0.3)    # bigger drop = more overextended
        velocity_bonus = min(velocity * 50, 0.2)          # faster deceleration = stronger bounce
        conf = 0.55 + drop_strength + velocity_bonus
        conf = min(conf, 0.90)
        
        s.scores = {
            "drop": token_drop, "velocity": velocity,
            "btc_chg": btc_chg, "conf": conf, "book_ok": book_ok
        }
        
        return {
            "s": "MEANREV", "dir": side, "yes": is_yes,
            "price": price, "conf": conf,
            "drop": token_drop, "velocity": velocity,
            "sz": 0
        }


class S_Flash:
    """v10: SETTLEMENT FOLLOWER — Buy mid-priced tokens where Chainlink 
    shows which side should win but market hasn't caught up yet.
    
    BACKTEST PROVEN (30 days, 5174 markets):
      FLA2_5m_mid: 448t, 55.4% WR, +$936  (consistent, high volume)
      Old cheap $0.15-$0.30: 25% WR, losing money
    
    Key changes from v9.5:
    1. Price range: $0.38-$0.55 (was $0.15-$0.30) — sweet spot for WR + payoff
    2. 5-minute markets ONLY (15m adds noise, -$408 isolated)
    3. Direction: simple open direction from Chainlink (complex filters = worse)
    4. No flat market logic (was buying blind, now needs clear direction)
    """
    def __init__(s, c): s.c = c
    def check(s, m, f, trend, token_feed=None, book_intel=None):
        if not s.c.flash_enabled or f.n < 10: return None
        
        # v10: 5-minute markets ONLY — backtest shows 15m drags down P&L
        if m.timeframe != 5: return None
        
        # v10: Mid-price range $0.38-$0.48 (not above — $0.50+ is a coin flip)
        yes_mid = 0.38 <= m.yes_p <= 0.48
        no_mid = 0.38 <= m.no_p <= 0.48
        if not yes_mid and not no_mid: return None
        
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 120: return None  # need 2+ min left
        
        duration = m.timeframe * 60  # 300s for 5m
        market_age = duration - tl
        if market_age < 60: return None  # wait 1 min for direction data
        
        # v10: Settlement direction — change from market open
        # Chainlink is the settlement source. If BTC is up from open on
        # Chainlink, YES should win. Simple. Don't overthink it.
        chg_open = 0
        if m.open_btc > 0:
            settle_price = f.settlement_price if hasattr(f, 'settlement_price') else f.price
            if settle_price > 0:
                chg_open = (settle_price - m.open_btc) / m.open_btc
        
        # v10.1: Need clear direction (> 0.12% move from open)
        # 0.08% was too sensitive — bot entered on 0.02% noise and lost
        if abs(chg_open) < 0.0012: return None
        
        # BTC up from open → YES should win → buy YES if mid-priced
        if chg_open > 0.0012 and yes_mid:
            book_ok = True
            if book_intel and book_intel.yes_ask_depth > 0:
                if book_intel.selling_pressure("YES"):
                    book_ok = False
            if book_ok:
                return {"s": "FLASH", "dir": "YES", "yes": True, "price": m.yes_p, "sz": 0}
        
        # BTC down from open → NO should win → buy NO if mid-priced
        if chg_open < -0.0012 and no_mid:
            book_ok = True
            if book_intel and book_intel.no_ask_depth > 0:
                if book_intel.selling_pressure("NO"):
                    book_ok = False
            if book_ok:
                return {"s": "FLASH", "dir": "NO", "yes": False, "price": m.no_p, "sz": 0}
        
        return None


class S_Squeeze:
    """LATE GAME — Two modes:
    
    Mode 1: LOTTERY (original) — Buy cheap losing side in final minutes.
    Tokens at $0.12-$0.22 that could spike on reversal. 15-20% WR, 5:1 R:R.
    
    Mode 2: SNIPE (new, 5m only) — Buy winning side in final 45 seconds.
    At T-30s, BTC direction is ~85% determined. Buy the winning side at
    $0.82-$0.94 as MAKER ORDER (zero fees + rebate). Nearly certain win,
    small profit per trade ($0.06-$0.18/share), but very high win rate.
    
    The snipe exploits the fact that Polymarket odds lag behind BTC price
    in the final seconds of 5-minute markets."""
    def __init__(s, c):
        s.c = c
        s.was_squeezing = False
        s.squeeze_count = 0
        s._last_signal_time = 0
        s._last_snipe_time = 0

    def check(s, m, f, trend):
        if f.n < 10: return None
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        duration = m.timeframe * 60

        # ── MODE 2: SNIPE (5m markets, final 45 seconds) ──
        if m.timeframe == 5 and 8 <= tl <= 45:
            # Cooldown: one snipe per market
            if time.time() - s._last_snipe_time < 60: return None
            
            # v9.5: Use SETTLEMENT price (Chainlink) for direction
            # This is what resolves the market — most reliable signal near end
            cl_2m = f.cl_chg(120) if hasattr(f, 'cl_chg') else 0
            binance_2m = f.chg(120)
            binance_30s = f.chg(30)
            
            # Use Chainlink if available, otherwise Binance
            chg_2m = cl_2m if cl_2m != 0 else binance_2m
            chg_30s = binance_30s  # Chainlink updates too slow for 30s
            
            # Also check change from market open (most important for settlement)
            chg_from_open = 0
            if m.open_btc > 0:
                settle_p = f.settlement_price if hasattr(f, 'settlement_price') else f.price
                if settle_p > 0:
                    chg_from_open = (settle_p - m.open_btc) / m.open_btc
            
            # BTC clearly going UP: 2m positive AND open change positive
            if chg_2m > 0.0003 and chg_from_open > 0.0001:
                if 0.82 <= m.yes_p <= 0.94:
                    s._last_snipe_time = time.time()
                    return {"s": "SQUEEZE", "dir": "YES", "yes": True,
                            "price": m.yes_p, "adx": 0, "di_plus": 0, "di_minus": 0,
                            "mom_value": chg_2m, "squeeze_count": int(tl),
                            "fired": True, "sz": 0, "mode": "SNIPE"}
            
            # BTC clearly going DOWN
            if chg_2m < -0.0003 and chg_from_open < -0.0001:
                if 0.82 <= m.no_p <= 0.94:
                    s._last_snipe_time = time.time()
                    return {"s": "SQUEEZE", "dir": "NO", "yes": False,
                            "price": m.no_p, "adx": 0, "di_plus": 0, "di_minus": 0,
                            "mom_value": chg_2m, "squeeze_count": int(tl),
                            "fired": True, "sz": 0, "mode": "SNIPE"}

        # ── MODE 1: LOTTERY — DISABLED (v9.5) ──
        # Research from 72M trade dataset: takers buying longshots at $0.01-$0.15
        # lose 57% more than implied. We were the dumb money. SNIPE only now.
        # Keeping the code commented for reference but returning None.
        return None


class S_PairAccum:
    """v9.4: PAIR ACCUMULATOR — The Gabagool Strategy.
    Buy BOTH sides cheap in the same market. Pair cost < $1.00 = guaranteed profit.
    
    Data proof: 6 accidental pairs in our history, ALL won. 100% WR.
    gabagool made millions with this exact approach.
    
    Logic:
    1. We already hold a position (e.g. NO at $0.22)
    2. BTC reverses, making the OTHER side cheap (YES drops to $0.28)
    3. Buy the other side → pair cost = $0.22 + $0.28 = $0.50
    4. One side ALWAYS pays $1.00 → guaranteed $0.50 profit per pair
    
    Also works standalone: buy whichever side is cheap first, then wait for the other.
    If the other side never gets cheap, it's just a normal directional trade."""
    
    def __init__(s, c):
        s.c = c
        s._last_signal = 0
        # Track what we've accumulated per market for pair tracking
        s._pairs = {}  # slug -> {"yes_shares": 0, "yes_cost": 0, "no_shares": 0, "no_cost": 0}
    
    def update_pair(s, slug, side, shares, cost):
        """Called when any strategy buys — track for pair completion."""
        if slug not in s._pairs:
            s._pairs[slug] = {"yes_shares": 0, "yes_cost": 0, "no_shares": 0, "no_cost": 0}
        p = s._pairs[slug]
        if side == "YES":
            p["yes_shares"] += shares
            p["yes_cost"] += cost
        else:
            p["no_shares"] += shares
            p["no_cost"] += cost
    
    def get_pair_status(s, slug):
        """Returns (pair_cost, min_paired_shares, is_profitable)."""
        if slug not in s._pairs:
            return None, 0, False
        p = s._pairs[slug]
        if p["yes_shares"] <= 0 or p["no_shares"] <= 0:
            return None, 0, False
        
        min_shares = min(p["yes_shares"], p["no_shares"])
        yes_avg = p["yes_cost"] / p["yes_shares"] if p["yes_shares"] > 0 else 0
        no_avg = p["no_cost"] / p["no_shares"] if p["no_shares"] > 0 else 0
        pair_cost = yes_avg + no_avg
        return pair_cost, min_shares, pair_cost < 0.98
    
    def clear_market(s, slug):
        """Called when market resolves."""
        s._pairs.pop(slug, None)
    
    def check(s, m, f, trend, open_positions=None):
        """Check if we should buy the OTHER side to complete a pair."""
        now = time.time()
        if now - s._last_signal < 5: return None  # cooldown
        
        tl = (m.end - datetime.now(timezone.utc)).total_seconds() if m.end else 999
        min_tl = 120 if m.timeframe == 5 else 240
        if tl < min_tl: return None
        
        # What do we currently hold in this market?
        if not open_positions: return None
        
        held_yes = sum(p.shares for p in open_positions if "YES" in p.side)
        held_no = sum(p.shares for p in open_positions if "NO" in p.side)
        held_yes_cost = sum(p.cost for p in open_positions if "YES" in p.side)
        held_no_cost = sum(p.cost for p in open_positions if "NO" in p.side)
        
        if held_yes <= 0 and held_no <= 0: return None  # nothing held
        
        # Already have both sides? Don't add more
        if held_yes > 0 and held_no > 0: return None
        
        # We hold one side. Is the OTHER side cheap enough to pair?
        if held_yes > 0 and held_no <= 0:
            # We hold YES. Check if NO is cheap.
            no_price = m.no_p
            yes_avg = held_yes_cost / held_yes if held_yes > 0 else 0
            pair_cost = yes_avg + no_price
            
            if no_price < 0.15 or no_price > 0.40: return None  # outside our range
            if pair_cost >= 0.95: return None  # not profitable enough after any slippage
            
            profit_per_pair = 1.0 - pair_cost
            s._last_signal = now
            return {
                "s": "PAIR", "dir": "NO", "yes": False, "price": no_price,
                "sz": 0, "pair_cost": pair_cost, "profit_pct": profit_per_pair * 100,
                "target_shares": held_yes  # match the YES side
            }
        
        elif held_no > 0 and held_yes <= 0:
            # We hold NO. Check if YES is cheap.
            yes_price = m.yes_p
            no_avg = held_no_cost / held_no if held_no > 0 else 0
            pair_cost = yes_price + no_avg
            
            if yes_price < 0.15 or yes_price > 0.40: return None
            if pair_cost >= 0.95: return None
            
            profit_per_pair = 1.0 - pair_cost
            s._last_signal = now
            return {
                "s": "PAIR", "dir": "YES", "yes": True, "price": yes_price,
                "sz": 0, "pair_cost": pair_cost, "profit_pct": profit_per_pair * 100,
                "target_shares": held_no
            }
        
        return None


class S_Spike:
    """v9.4: SPIKE DETECTOR — Buy panic-sold tokens.
    
    When other bots liquidate losing positions, they dump tokens at terrible prices.
    We detect these sudden sell-offs via order book changes and buy the discount.
    
    Logic:
    1. Track ask depth for each token over time
    2. If ask depth suddenly spikes 3x+ (someone dumped a big sell order)
    3. AND the token price dropped to $0.10-$0.25
    4. Buy the token — panic sellers are usually wrong at these prices
    
    Similar to Flash but triggered by ORDER BOOK events, not BTC moves.
    Catches opportunities Flash misses when BTC is flat but someone panics."""
    
    def __init__(s, c):
        s.c = c
        s._last_signal = 0
        s._prev_yes_ask = 0.0
        s._prev_no_ask = 0.0
        s._prev_update = 0
    
    def check(s, m, f, trend, book_intel=None):
        """Check for spike (sudden sell pressure)."""
        now = time.time()
        if now - s._last_signal < 10: return None  # 10s cooldown
        if not book_intel: return None
        
        tl = (m.end - datetime.now(timezone.utc)).total_seconds() if m.end else 999
        min_tl = 120 if m.timeframe == 5 else 240
        if tl < min_tl: return None
        
        # Track ask depth changes
        yes_ask = book_intel.yes_ask_depth
        no_ask = book_intel.no_ask_depth
        
        # Need previous data to detect spike
        if s._prev_update == 0 or now - s._prev_update > 30:
            s._prev_yes_ask = yes_ask
            s._prev_no_ask = no_ask
            s._prev_update = now
            return None
        
        # Detect spike: ask depth jumped 3x+ (someone dumped tokens)
        yes_spike = yes_ask > s._prev_yes_ask * 3.0 and yes_ask > 100 if s._prev_yes_ask > 10 else False
        no_spike = no_ask > s._prev_no_ask * 3.0 and no_ask > 100 if s._prev_no_ask > 10 else False
        
        # Update prev values
        s._prev_yes_ask = yes_ask
        s._prev_no_ask = no_ask
        s._prev_update = now
        
        # YES spike: someone dumped YES tokens → YES is cheap → buy YES
        if yes_spike and 0.10 <= m.yes_p <= 0.28:
            # Confirm it's not a justified dump (BTC crashing)
            btc_1m = f.chg(60) if f else 0
            if btc_1m < -0.003: return None  # BTC actually crashing, dump is justified
            
            s._last_signal = now
            return {"s": "SPIKE", "dir": "YES", "yes": True, "price": m.yes_p, "sz": 0,
                    "spike_size": yes_ask}
        
        # NO spike: someone dumped NO tokens → NO is cheap → buy NO
        if no_spike and 0.10 <= m.no_p <= 0.28:
            btc_1m = f.chg(60) if f else 0
            if btc_1m > 0.003: return None  # BTC pumping, NO dump is justified
            
            s._last_signal = now
            return {"s": "SPIKE", "dir": "NO", "yes": False, "price": m.no_p, "sz": 0,
                    "spike_size": no_ask}
        
        return None



# ─── MARKET FINDER ───
class Finder:
    def __init__(s, c):
        s.c = c; s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/10.1"; s.cache = {}
    def test(s):
        try:
            r = s.s.get(f"{s.c.gamma_host}/markets", params={"limit": 1}, timeout=10)
            return r.status_code == 200
        except: return False
    def find(s, asset="btc", timeframe=15):
        now = datetime.now(timezone.utc)
        interval = timeframe  # 5 or 15
        cache_key = f"{asset}-{timeframe}m"
        # v9.5: Check cache FIRST — avoid unnecessary API calls
        cached = s.cache.get(cache_key)
        if cached:
            tl = (cached.end - now).total_seconds()
            if tl > 30 and cached.active: return cached
            # v10.1: Cache is stale — clear it so we search fresh
            if tl <= 30:
                s.cache.pop(cache_key, None)
        mb = (now.minute // interval) * interval
        base = now.replace(minute=mb, second=0, microsecond=0)
        # v10.1: More offsets — try harder to find markets
        # Old: [0, -interval, interval, -2*interval] → missed markets in transition
        offsets = [0, -interval, interval, -interval*2, interval*2]
        for off in offsets:
            ts = int((base + timedelta(minutes=off)).timestamp())
            slug = f"{asset}-updown-{timeframe}m-{ts}"
            m = s._get(slug, asset, timeframe)
            if m and m.active:
                tl = (m.end - now).total_seconds()
                if tl > 30: s.cache[cache_key] = m; return m
        return None
    def _get(s, slug, asset="btc", timeframe=15):
        try:
            r = s.s.get(f"{s.c.gamma_host}/markets", params={"slug": slug}, timeout=8)
            if r.status_code != 200: return None
            d = r.json()
            if isinstance(d, list): d = d[0] if d else None
            if not d or not (d.get("condition_id") or d.get("conditionId")): return None
            return s._parse(d, asset, timeframe)
        except: return None
    def _parse(s, d, asset="btc", timeframe=15):
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
            except: et = datetime.now(timezone.utc) + timedelta(minutes=timeframe)
            return Market(slug=d.get("slug", ""), cid=d.get("condition_id") or d.get("conditionId", ""),
                question=d.get("question", ""), tok_yes=tok[0].strip().strip('"'),
                tok_no=tok[1].strip().strip('"'), end=et,
                yes_p=float(pr[0]) if pr else 0.5, no_p=float(pr[1]) if len(pr) > 1 else 0.5,
                active=not d.get("closed", False), asset=asset, timeframe=timeframe)
        except: return None

# ─── EXECUTOR (same as v5) ───
class Executor:
    def __init__(s, c): s.c = c; s.client = None; s.authed = False; s._signer_addr = None; s._pending_makers = {}
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
    def order(s, market, is_yes, price, size, mode="maker"):
        """Place an order. Modes: 'maker' (GTC limit, ZERO fees), 'taker' (FOK, pays fees).
        v9.5: Default changed from taker to maker. Research: makers profit at 80/99 price levels.
        Returns (order_id, actual_shares) or (None, None) on failure."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        label = "YES" if is_yes else "NO"
        if price < 0.01 or price > 0.95: return None, None
        dollar_amount = round(price * size, 2)
        if dollar_amount < 0.50: dollar_amount = 0.50
        if s.c.dry_run:
            oid = f"DRY-{int(time.time()*1000)%99999}"
            log.info(f"DRY [{mode.upper()}]: ${dollar_amount:.2f} {label}")
            return oid, None
        if not s.authed: return None, None
        tid = market.tok_yes if is_yes else market.tok_no

        if mode == "maker":
            return s._order_maker(tid, label, price, size, dollar_amount, timeout=45, retries=3)
        elif mode == "hybrid":
            return s._order_hybrid(tid, label, price, size, dollar_amount)
        else:  # taker (explicit only — never default)
            return s._order_taker(tid, label, price, size, dollar_amount)

    def _order_taker(s, tid, label, price, size, dollar_amount):
        """FOK taker order — instant fill or cancel. Pays taker fee."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        try:
            market_order = MarketOrderArgs(token_id=tid, amount=dollar_amount, side=BUY)
            signed = s.client.create_market_order(market_order)
            resp = s.client.post_order(signed, OrderType.FOK)
            oid, shares = s._parse_resp(resp, label, dollar_amount, "TAKER-FOK")
            if oid: return oid, shares
        except Exception as e:
            log.warning(f"Taker FOK fail: {e}")
        # v9.5: Removed GTC fallback — it creates phantom orders that sit forever
        # If FOK fails, the liquidity isn't there. Just skip.
        return None, None

    def _order_maker(s, tid, label, price, size, dollar_amount, timeout=45, retries=3):
        """SEMI-BLOCKING maker order — posts GTC limit and waits up to 8 seconds.
        v9.5: Short wait prevents phantom positions (position created only on confirmed fill).
        If not filled in 8s, cancels and queues an async retry with better price.
        Zero fees + rebate on every fill."""
        from py_clob_client.clob_types import OrderArgs, OrderType, OpenOrderParams
        from py_clob_client.order_builder.constants import BUY
        # Get best bid to post just above it (top of book, still maker)
        maker_price = round(max(0.01, min(price - 0.01, 0.99)), 2)
        try:
            book = s.client.get_order_book(tid)
            if isinstance(book, dict):
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids:
                    best_bid = float(bids[0].get("price", 0))
                    maker_price = round(min(best_bid + 0.01, price - 0.01), 2)
                if asks:
                    best_ask = float(asks[0].get("price", 0))
                    maker_price = round(min(maker_price, best_ask - 0.01), 2)
        except:
            pass
        maker_price = round(max(0.01, min(maker_price, 0.99)), 2)
        limit_size = max(size, 5.0)

        try:
            signed = s.client.create_order(OrderArgs(
                price=maker_price, size=round(limit_size, 2), side=BUY, token_id=tid))
            resp = s.client.post_order(signed, OrderType.GTC)
            oid, _ = s._parse_resp(resp, label, maker_price * limit_size, "MAKER-GTC")
            if not oid:
                return None, None

            # Short blocking wait — 8 seconds max (not 45s!)
            # Most maker fills happen in 2-5 seconds if price is right
            for _ in range(4):  # 4 checks × 2s = 8 seconds
                time.sleep(2)
                try:
                    orders = s.client.get_orders(OpenOrderParams())
                    still_open = any(
                        (o.get("id") == oid or o.get("orderID") == oid)
                        for o in (orders if isinstance(orders, list) else [])
                    )
                    if not still_open:
                        log.info(f"MAKER FILLED: {label} @ ${maker_price} id={oid} (ZERO FEE)")
                        return oid, limit_size
                except:
                    pass

            # Not filled in 8s — cancel and move on
            # v9.5: No async retries. If price was right, it would've filled.
            # Async retries create phantom fills (positions without tracking).
            # Better to wait for next signal than retry with stale info.
            try:
                s.client.cancel(order_id=oid)
                log.info(f"MAKER UNFILLED (8s): {label} @ ${maker_price} — cancelled, moving on")
            except:
                pass

        except Exception as e:
            log.error(f"Maker order fail: {e}")

        return None, None

    def check_pending_fills(s):
        """Called every tick. Cleans up any stale pending state."""
        # v9.5: Simplified — no async retries, just cleanup
        s._pending_makers.clear()

    def _order_hybrid(s, tid, label, price, size, dollar_amount):
        """Try maker first (5s), fall back to taker if not filled. Best of both worlds."""
        from py_clob_client.clob_types import OrderArgs, OrderType, OpenOrderParams, MarketOrderArgs
        from py_clob_client.order_builder.constants import BUY
        # Step 1: Try maker with short timeout
        maker_price = round(max(0.01, min(price - 0.01, 0.99)), 2)
        try:
            book = s.client.get_order_book(tid)
            if isinstance(book, dict):
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids:
                    best_bid = float(bids[0].get("price", 0))
                    maker_price = round(min(best_bid + 0.01, price - 0.01), 2)
                if asks:
                    best_ask = float(asks[0].get("price", 0))
                    maker_price = round(min(maker_price, best_ask - 0.01), 2)
        except:
            pass
        maker_price = round(max(0.01, min(maker_price, 0.99)), 2)
        limit_size = max(size, 5.0)

        try:
            signed = s.client.create_order(OrderArgs(
                price=maker_price, size=round(limit_size, 2), side=BUY, token_id=tid))
            resp = s.client.post_order(signed, OrderType.GTC)
            oid, _ = s._parse_resp(resp, label, maker_price * limit_size, "HYBRID-MAKER")

            if oid:
                # Wait 5 seconds for fill
                fill_deadline = time.time() + 5
                while time.time() < fill_deadline:
                    time.sleep(1)
                    try:
                        orders = s.client.get_orders(OpenOrderParams())
                        still_open = any(
                            (o.get("id") == oid or o.get("orderID") == oid)
                            for o in (orders if isinstance(orders, list) else [])
                        )
                        if not still_open:
                            log.info(f"HYBRID MAKER FILLED: {label} @ ${maker_price} id={oid} (ZERO FEE)")
                            return oid, limit_size
                    except:
                        pass

                # Not filled — cancel maker and give up (no taker fallback)
                # v9.5: Research proves taker = losing side. Be patient or walk away.
                try:
                    s.client.cancel(order_id=oid)
                    log.info(f"HYBRID: maker unfilled after 5s, cancelled. No taker fallback.")
                except:
                    pass
        except Exception as e:
            log.error(f"Hybrid maker phase fail: {e}")

        # v9.5: No taker fallback. Return None.
        return None, None

    def _parse_resp(s, resp, label, amount, tag):
        """Parse order response, return (order_id, actual_shares) or (None, None)."""
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
            log.info(f"{tag}: ${amount:.2f} {label} id={oid} st={status} shares={actual_shares}")
            if oid != "?": return oid, actual_shares
        elif isinstance(resp, str) and len(resp) > 5:
            log.info(f"{tag}: ${amount:.2f} {label} resp={resp[:60]}")
            return resp, None
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
        s._pending_makers.clear()  # v9.5: clear async retries too
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
    def open(s, t, market_end=None, actual_shares=None, entry_regime="UNKNOWN"):
        shares = actual_shares if actual_shares else (t.size / t.price if t.price > 0 else 0)
        # Sanity check: if actual_shares gives a crazy entry price, fall back to t.price
        if actual_shares and shares > 0:
            computed_entry = t.size / shares
            if 0.01 <= computed_entry <= 0.99:
                actual_entry = computed_entry
            else:
                actual_entry = t.price  # API returned garbage, use intended price
                shares = t.size / t.price if t.price > 0 else 0  # recalc shares too
        else:
            actual_entry = t.price
        # If actual_shares is 0, this is an unfilled limit order
        # Still track it so we can cancel/clean it up, but mark it
        p = Pos(id=t.oid, strat=t.strat, slug=t.slug, side=t.side,
            entry=round(actual_entry, 4), shares=shares, cost=t.size if shares > 0 else 0,
            opened=t.ts, market_end=market_end, entry_regime=entry_regime)
        s.positions.append(p); s.trades.append(t)
        if shares > 0: s.total_bet += t.size  # only count filled orders
        return p
    def resolve(s, pos, won):
        # v8.1: Skip cancelled/unfilled orders
        if pos.shares <= 0 or pos.cost <= 0:
            pos.pnl = 0.0; pos.status = "CANCELLED"
            for t in s.trades:
                if t.oid == pos.id: t.pnl = 0.0
            return
        if won:
            gross_payout = pos.shares * 1.0
            fee = gross_payout * 0.02
            net_payout = gross_payout - fee
            pnl = net_payout - pos.cost
        else:
            pnl = -pos.cost
        # v8.1 SANITY CHECK: if "won" but pnl is less than -cost/2, 
        # something went wrong (shares data was bad). Fall back to -cost.
        if won and pnl < -(pos.cost * 0.5):
            log.warning(f"Resolution sanity fail: won=True but pnl={pnl:.2f}, cost={pos.cost:.2f}, shares={pos.shares:.2f}. Forcing loss.")
            pnl = -pos.cost
            won = False
        pos.pnl = round(pnl, 2); pos.status = "WON" if won else "LOST"
        s.dpnl += pnl
        for t in s.trades:
            if t.oid == pos.id: t.pnl = pnl
    def check_exp(s, f, feeds=None):
        now = datetime.now(timezone.utc)
        resolved_list = []
        for p in s.positions:
            if p.status != "OPEN" or not p.opened: continue
            # v9.4: Use correct asset feed for resolution
            _pos_asset = p.slug.split("-")[0] if p.slug else "btc"
            pf = feeds.get(_pos_asset, f) if feeds else f
            if p.market_end:
                past_end = (now - p.market_end).total_seconds()
                if past_end < 60: continue
            else:
                age = (now - p.opened).total_seconds()
                if age < 960: continue
            # Step 1: Always try Gamma API first (most accurate)
            resolved = s._check_resolution(p)
            if resolved is not None:
                # v9: Cross-check with BTC direction if we have data
                btc_agrees = None
                if p.market_end:
                    # v9.4: Detect timeframe from slug
                    _tf_secs = 300 if "5m" in p.slug else 900
                    ms = p.market_end - timedelta(seconds=_tf_secs)
                    bop = bcp = None
                    for x in list(pf.data):
                        if x["t"] >= ms.timestamp() and bop is None: bop = x["p"]
                        bcp = x["p"]
                    if bop and bcp:
                        btc_up = bcp > bop
                        btc_says_won = (btc_up and "YES" in p.side) or (not btc_up and "NO" in p.side)
                        btc_agrees = (btc_says_won == resolved)
                if btc_agrees is False:
                    log.warning(f"GAMMA vs BTC DISAGREE on {p.slug}! Gamma={resolved}, BTC={'up' if btc_up else 'down'}, side={p.side}. Using GAMMA (official settlement).")
                    # NEVER override Gamma — Polymarket pays based on Gamma resolution, not our BTC feed
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
            # v9.5: Wait 15 min for Gamma before using BTC fallback.
            # BTC fallback has been wrong multiple times (bot says LOST, Polymarket says WON).
            # Gamma is the ONLY authoritative source. BTC fallback is emergency-only.
            if p.market_end and (now - p.market_end).total_seconds() > 900:
                _tf_secs2 = 300 if "5m" in p.slug else 900
                market_start = p.market_end - timedelta(seconds=_tf_secs2)
                start_ts = market_start.timestamp()
                op = cp = None
                for x in list(pf.data):
                    if x["t"] >= start_ts and op is None: op = x["p"]
                    cp = x["p"]
                if op and cp:
                    up = cp > op
                    won = (up and "YES" in p.side) or (not up and "NO" in p.side)
                    log.info(f"BTC fallback {p.slug}: open=${op:.2f} close=${cp:.2f} {'UP' if up else 'DOWN'} side={p.side} → {'WON' if won else 'LOST'}")
                    s.resolve(p, won)
                else:
                    log.warning(f"BTC fallback {p.slug}: no price data (op={op} cp={cp}), defaulting to LOSS")
                    s.resolve(p, False)
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
                        won = (up and "YES" in p.side) or (not up and "NO" in p.side)
                        log.info(f"Age fallback {p.slug}: open=${op:.2f} close=${cp:.2f} {'UP' if up else 'DOWN'} side={p.side} → {'WON' if won else 'LOST'}")
                        s.resolve(p, won)
                    else:
                        log.warning(f"Age fallback {p.slug}: no price data, defaulting to LOSS")
                        s.resolve(p, False)
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
                    # v9: Verify slug matches — Gamma might return wrong market
                    returned_slug = m.get("slug", "")
                    if returned_slug and p.slug and returned_slug != p.slug:
                        log.warning(f"Gamma slug mismatch: asked={p.slug} got={returned_slug}")
                        return None  # don't trust it
                    if m.get("closed", False):
                        pr = m.get("outcomePrices") or m.get("outcome_prices") or ""
                        if isinstance(pr, str):
                            try: pr = json.loads(pr)
                            except: return None
                        if len(pr) >= 2:
                            yes_final = float(pr[0])
                            if yes_final > 0.9:
                                won = "YES" in p.side
                                log.info(f"Gamma resolved {p.slug}: YES won → {'WON' if won else 'LOST'} (side={p.side})")
                                return won
                            elif yes_final < 0.1:
                                won = "NO" in p.side
                                log.info(f"Gamma resolved {p.slug}: NO won → {'WON' if won else 'LOST'} (side={p.side})")
                                return won
                            else:
                                log.debug(f"Gamma {p.slug}: yes_final={yes_final} (not decisive)")
        except Exception as e:
            log.debug(f"Gamma check error: {e}")
        return None
    def stats(s):
        w = s._lifetime_wins + sum(1 for t in s.trades if t.pnl > 0)
        l = s._lifetime_losses + sum(1 for t in s.trades if t.pnl < 0)
        return w, l, (w / (w + l) * 100 if w + l else 0)

# ─── DASHBOARD v10 — Backtest-Optimized ───
class Dash:
    def __init__(s): s.evts = deque(maxlen=10)
    def ev(s, e): s.evts.append(f"{datetime.now().strftime('%H:%M:%S')} {e}")
    def render(s, c, conn, f, risk, mkt, strats, scores, orders, poly_pos,
               start_time=None, past_trades=None, trend=None, sizer=None,
               cortex=None, poly_ws=None, slot_markets=None, feeds=None, active_slot=""):
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.now().strftime("%H:%M:%S")
        rt = ""
        if start_time:
            elapsed = int(time.time() - start_time)
            hrs, rem = divmod(elapsed, 3600)
            mins, secs = divmod(rem, 60)
            rt = f"{hrs}h {mins}m {secs}s"

        # ╔══════════════════════════════════════════════════════════════╗
        #  HEADER — Title bar with mode
        # ╚══════════════════════════════════════════════════════════════╝
        mode = f"{OK}LIVE{R}" if not c.dry_run else f"{WARN}DRY{R}"
        cx_mode = "---"
        if cortex:
            sm = cortex.get_session_mult()
            if sm >= 1.1: cx_mode = f"{OK}◆ PUSH{R}"
            elif sm >= 0.9: cx_mode = f"{VAL}● NORMAL{R}"
            else: cx_mode = f"{WARN}▽ CAREFUL{R}"

        print(f"\n  {H1}╔{'═'*62}╗{R}")
        print(f"  {H1}║{R}  {H2}⬡ POLYMARKET BOT v10.1{R}           {DIM}{now}{R}  {DIM}⏱ {rt}{R}  {H1}║{R}")
        print(f"  {H1}║{R}  {DIM}₿ BTC 5m+15m{R}  {DIM}Backtest-Optimized (30d/5174mkt){R}        {H1}║{R}")
        print(f"  {H1}║{R}  {DIM}Mode:{R} {mode}  {DIM}│{R}  {DIM}Cortex:{R} {cx_mode}  {DIM}│{R}  ", end="")

        # Connection dots — one per feed
        gc = f"{OK}●{R}" if conn.gamma == "OK" else f"{ERR}●{R}"
        cc = f"{OK}●{R}" if conn.clob == "OK" else f"{ERR}●{R}"
        ac = f"{OK}●{R}" if conn.can_trade else f"{ERR}●{R}"
        # v10: BTC-only feed status
        feed_dots = ""
        if feeds:
            af = feeds.get("btc")
            if af and af.n > 0 and af.ws_status == "WS":
                feed_dots = f"{OK}●{R}"
            elif af and af.n > 0:
                feed_dots = f"{WARN}●{R}"
            else:
                feed_dots = f"{ERR}●{R}"
        else:
            feed_dots = f"{DIM}-{R}"
        print(f"{DIM}G{gc} C{cc} A{ac}{R} {DIM}F{feed_dots}{R}   {H1}║{R}")
        print(f"  {H1}╚{'═'*62}╝{R}")

        # ╔══════════════════════════════════════════════════════════════╗
        #  BALANCE + P&L — The money line
        # ╚══════════════════════════════════════════════════════════════╝
        pnl_str = pnl_c2(risk.tpnl)
        w, l, wr = risk.stats()
        sess_pnl = cortex._session_pnl if cortex else 0
        sess_str = pnl_c2(sess_pnl)

        print(f"\n  {H1}┌{'─'*62}┐{R}")
        recovery_tag = f"  {WARN}⚠ RECOVERY (½ size until ${c.recovery_target:,.0f}){R}" if risk.show_bal < c.recovery_target else ""
        print(f"  {H1}│{R}  {LBL}Balance{R}  {OK}${risk.show_bal:,.2f}{R}   {LBL}Available{R} {VAL}${risk.available:,.2f}{R}   {LBL}P&L{R} {pnl_str}       {H1}│{R}")
        print(f"  {H1}│{R}  {LBL}Record{R}   {OK}{w}W{R}/{ERR}{l}L{R} ({VAL}{wr:.0f}%{R})      {LBL}At Risk{R}  {WARN}${risk.open_risk:.2f}{R}      {LBL}Session{R} {sess_str}  {H1}│{R}")
        if recovery_tag:
            print(f"  {H1}│{R}{recovery_tag}                  {H1}│{R}")
        print(f"  {H1}└{'─'*62}┘{R}")

        # ╔══════════════════════════════════════════════════════════════╗
        #  THE CORTEX — Brain state
        # ╚══════════════════════════════════════════════════════════════╝
        if cortex:
            print(f"\n  {H2}┌{'─'*62}┐{R}")
            print(f"  {H2}│{R}  {H2}⬡ CORTEX{R}                                                     {H2}│{R}")
            print(f"  {H2}├{'─'*62}┤{R}")

            # Trust scores with visual bars
            for st in cortex.STRATS:
                trust = cortex.get_trust(st)
                base = cortex._trust.get(st, 1.0)
                n = len(cortex._trades.get(st, []))
                
                # Visual bar (0.0 to 2.5 scale, bar width 12)
                bar_fill = int(min(trust / 2.5, 1.0) * 12)
                if trust <= 0:
                    bar = f"{ERR}{'░'*12}{R}"
                    ic = "☠"
                elif trust >= 1.5:
                    bar = f"{OK}{'█'*bar_fill}{DIM}{'░'*(12-bar_fill)}{R}"
                    ic = "🔥"
                elif trust >= 1.0:
                    bar = f"{VAL}{'█'*bar_fill}{DIM}{'░'*(12-bar_fill)}{R}"
                    ic = "●"
                elif trust >= 0.5:
                    bar = f"{WARN}{'█'*bar_fill}{DIM}{'░'*(12-bar_fill)}{R}"
                    ic = "⚠"
                else:
                    bar = f"{ERR}{'█'*bar_fill}{DIM}{'░'*(12-bar_fill)}{R}"
                    ic = "❄"
                
                # v9.5: Show per-slot trust if different from global
                slot_info = ""
                slot_data = []
                for sk, st_val in cortex._slot_trust.items():
                    if sk.startswith(f"{st}:") and len(cortex._slot_trades.get(sk, [])) >= 3:
                        slot_name = sk.split(":")[1].upper()
                        slot_data.append(f"{slot_name}={st_val:.1f}")
                if slot_data:
                    slot_info = f" {DIM}[{' '.join(slot_data[:3])}]{R}"
                print(f"  {H2}│{R}  {ic} {st:8} {bar} {VAL}{trust:.2f}x{R} {DIM}({n}t){R}{slot_info}          {H2}│{R}")

            # Macro bias — per-asset + overall
            bias = cortex._macro_bias
            strength = cortex._macro_strength
            if bias == "YES":
                bias_str = f"{OK}▲ YES ({strength:.0%}){R}"
            elif bias == "NO":
                bias_str = f"{ERR}▼ NO ({strength:.0%}){R}"
            else:
                bias_str = f"{DIM}─ NEUTRAL{R}"

            # v10: BTC-only bias indicator
            asset_parts = []
            for a in ["btc"]:
                ab = cortex._asset_bias.get(a, "NEUTRAL")
                ao = list(cortex._asset_outcomes.get(a, []))[-6:]
                arrows = "".join([f"{OK}↑{R}" if x else f"{ERR}↓{R}" for x in ao]) if ao else ""
                if ab == "YES":
                    asset_parts.append(f"{OK}{a.upper()}↑{R}{arrows}")
                elif ab == "NO":
                    asset_parts.append(f"{ERR}{a.upper()}↓{R}{arrows}")
                else:
                    asset_parts.append(f"{DIM}{a.upper()}{R}{arrows}")
            asset_str = " ".join(asset_parts)

            print(f"  {H2}├{'─'*62}┤{R}")
            print(f"  {H2}│{R}  {LBL}Macro{R}  {bias_str}   {asset_str}              {H2}│{R}")

            # Danger zone + session
            sm = cortex.get_session_mult()
            sm_str = f"{OK}{sm:.1f}x{R}" if sm >= 1.0 else f"{WARN}{sm:.1f}x{R}"
            
            print(f"  {H2}│{R}  {LBL}Session{R} {sm_str}                                                {H2}│{R}")
            
            # v9.5: Chainlink status
            if hasattr(f, 'cl_price') and f.cl_price > 0:
                cl_age = f.cl_age
                cl_p = f.cl_price
                div = f.price_divergence * 100  # as %
                age_str = f"{int(cl_age)}s" if cl_age < 120 else f"{int(cl_age/60)}m"
                if abs(div) < 0.1:
                    cl_status = f"{OK}CL ${cl_p:,.0f} ≈ Binance (Δ{div:+.2f}%) {age_str}{R}"
                elif abs(div) < 0.3:
                    cl_status = f"{WARN}CL ${cl_p:,.0f} ≠ Binance (Δ{div:+.2f}%) {age_str}{R}"
                else:
                    cl_status = f"{ERR}CL ${cl_p:,.0f} ≠≠ Binance (Δ{div:+.2f}%) {age_str} ⚠SKIP{R}"
                print(f"  {H2}│{R}  {LBL}Oracle{R} {cl_status}  {H2}│{R}")
            
            # v9.1: Side bias
            y_m, n_m = cortex._side_mult["YES"], cortex._side_mult["NO"]
            side_parts = []
            if y_m < 1.0: side_parts.append(f"{ERR}YES↓{y_m:.1f}x{R}")
            if n_m < 1.0: side_parts.append(f"{ERR}NO↓{n_m:.1f}x{R}")
            if side_parts:
                side_str = " ".join(side_parts)
                print(f"  {H2}│{R}  {LBL}Side{R}   {side_str}                                          {H2}│{R}")

            # v9.1: Lifecycle model
            lc_n = len(cortex._lifecycle_data)
            lc_b = len(cortex._lifecycle_probs)
            if lc_n > 0:
                print(f"  {H2}│{R}  {LBL}Lifecycle{R} {OK}{lc_n}{R} mkts  {DIM}{lc_b} buckets{R}                            {H2}│{R}")

            # v9.1: Recovery indicator
            if sm < 1.0 and cortex._consec_wins >= 2:
                print(f"  {H2}│{R}  {OK}↑ RECOVERING{R} ({cortex._consec_wins}W streak)                              {H2}│{R}")

            # v9.1: Data collection summary
            cortex_total = sum(len(v) for v in cortex._trades.values())
            regime_combos = len(cortex._regime_perf)
            data_parts = [f"{cortex_total}t"]
            if regime_combos > 0: data_parts.append(f"{regime_combos}rgm")
            if lc_n > 0: data_parts.append(f"{lc_n}lc")
            data_str = " ".join(data_parts)
            print(f"  {H2}│{R}  {LBL}Data{R}   {DIM}{data_str}{R}                                          {H2}│{R}")

            # Pattern discoveries
            if cortex._patterns:
                pats = ", ".join(cortex._patterns.keys())[:40]
                print(f"  {H2}│{R}  {LBL}Patterns{R} {DIM}{pats}{R}                                    {H2}│{R}")

            print(f"  {H2}└{'─'*62}┘{R}")

        # ╔══════════════════════════════════════════════════════════════╗
        #  ACTIVE MARKETS — All assets being tracked
        # ╚══════════════════════════════════════════════════════════════╝
        if slot_markets and feeds:
            active_count = sum(1 for sm in slot_markets.values() if sm)
            print(f"\n  {H1}┌{'─'*62}┐{R}")
            print(f"  {H1}│{R}  {LBL}MARKETS{R}  ({active_count} active)                                        {H1}│{R}")
            print(f"  {H1}├{'─'*62}┤{R}")
            asset_icons = {"btc": "₿", "eth": "Ξ", "sol": "◎", "xrp": "✕"}
            for slot_key, sm in sorted(slot_markets.items()):
                if not sm: continue
                parts = slot_key.split("-")
                asset = parts[0]
                tf = parts[1] if len(parts) > 1 else "15m"
                af = feeds.get(asset)
                if not af or af.n == 0: continue
                tl = (sm.end - datetime.now(timezone.utc)).total_seconds()
                if tl < 0: continue
                mins_left = int(tl // 60)
                secs_left = int(tl % 60)
                duration = sm.timeframe * 60
                progress = max(0, min(1.0 - tl / duration, 1.0))
                bar_len = int(progress * 10)
                time_bar = f"{OK}{'━' * bar_len}{DIM}{'─' * (10 - bar_len)}{R}"
                ic = asset_icons.get(asset, "•")
                ws_tag = f"{OK}⚡{R}" if af.ws_status == "WS" else f"{WARN}H{R}"
                is_active = slot_key == active_slot
                marker = f"{OK}▶{R}" if is_active else " "
                chg1 = af.chg(60)*100
                c1 = OK if chg1 > 0.02 else ERR if chg1 < -0.02 else DIM
                print(f"  {H1}│{R}{marker}{ic} {LBL}{asset.upper():3}{R}{DIM}{tf:3}{R} {BTC}${af.price:>10,.2f}{R} {c1}{chg1:+.2f}%{R} {OK}Y{sm.yes_p:.2f}{R}/{ERR}N{sm.no_p:.2f}{R} {time_bar}{VAL}{mins_left}:{secs_left:02d}{R}{ws_tag}{H1}│{R}")
            print(f"  {H1}└{'─'*62}┘{R}")
        elif mkt:
            tl = (mkt.end - datetime.now(timezone.utc)).total_seconds()
            mins_left = int(tl // 60)
            secs_left = int(tl % 60)
            progress = max(0, min(1.0 - tl / (mkt.timeframe * 60), 1.0))
            bar_len = int(progress * 24)
            time_bar = f"{OK}{'━' * bar_len}{DIM}{'─' * (24 - bar_len)}{R}"
            ws_tag = f"{OK}⚡WS{R}" if f.ws_status == "WS" else f"{WARN}HTTP{R}"
            print(f"\n  {LBL}BTC{R}  {BTC}${f.price:,.2f}{R}  {ws_tag}")
            print(f"  {LBL}MKT{R}  {OK}Y ${mkt.yes_p:.2f}{R}  {ERR}N ${mkt.no_p:.2f}{R}  {time_bar} {VAL}{mins_left}:{secs_left:02d}{R}")

        # Trend + AI line
        if trend:
            regime = trend.regime
            if "UP" in regime: rc = OK; arrow = "▲"
            elif "DOWN" in regime: rc = ERR; arrow = "▼"
            elif regime == "BREAKOUT": rc = H2; arrow = "◆"
            elif regime == "CHOPPY": rc = WARN; arrow = "~"
            else: rc = DIM; arrow = "─"
            print(f"  {LBL}AI{R}   {rc}{arrow} {regime}{R}  {DIM}⏱ {rt}{R}" if rt else f"  {LBL}AI{R}   {rc}{arrow} {regime}{R}")

        # ╔══════════════════════════════════════════════════════════════╗
        #  STRATEGIES — What each is doing
        # ╚══════════════════════════════════════════════════════════════╝
        print(f"\n  {H1}┌{'─'*62}┐{R}")
        print(f"  {H1}│{R}  {LBL}STRATEGIES{R}                                                       {H1}│{R}")
        print(f"  {H1}├{'─'*62}┤{R}")
        icons = {"ARB": "♦", "LATENCY": "⚡", "MEANREV": "↩", "FLASH": "⚡", "SQUEEZE": "◈", "PAIR": "⬡", "SPIKE": "△"}
        for k, v in strats.items():
            ic = icons.get(k, "•")
            paused = sizer and sizer.is_paused(k)
            base_sz = c.get_base_size(k, risk.show_bal)
            # Get cortex-adjusted size for display
            cx_trust = cortex.get_trust(k) if cortex else 1.0
            adj_sz = base_sz * cx_trust
            if paused:
                pr = sizer.pause_remaining(k)
                line = f"  {ERR}⏸ {ic} {k:10}{R}  {ERR}PAUSED ({pr}s){R}"
            elif "disabled" in str(v):
                line = f"  {ERR}☠ {ic} {k:10}{R}  {ERR}{v}{R}"
            elif "ACTIVE" in str(v):
                line = f"  {OK}● {ic} {k:10}{R}  {OK}{v}{R}"
            elif "lock" in str(v) or "blocked" in str(v) or "bad hour" in str(v):
                line = f"  {ERR}○ {ic} {k:10}{R}  {ERR}{v}{R}"
            elif "confirming" in str(v):
                line = f"  {WARN}◎ {ic} {k:10}{R}  {WARN}{v}{R}"
            else:
                line = f"  {DIM}○ {ic} {k:10}{R}  {DIM}{v}{R}"
            sz_str = f"{DIM}${adj_sz:.0f}{R}"
            print(f"  {H1}│{R}{line}  {sz_str}  {H1}│{R}")
        print(f"  {H1}└{'─'*62}┘{R}")

        # ╔══════════════════════════════════════════════════════════════╗
        #  OPEN POSITIONS
        # ╚══════════════════════════════════════════════════════════════╝
        open_pos = [p for p in risk.positions if p.status == "OPEN"]
        if open_pos:
            print(f"\n  {H1}┌{'─'*62}┐{R}")
            print(f"  {H1}│{R}  {LBL}OPEN POSITIONS{R}  ({len(open_pos)})  {WARN}${risk.open_risk:.2f} at risk{R}                   {H1}│{R}")
            print(f"  {H1}├{'─'*62}┤{R}")
            for p in open_pos[-5:]:
                _pos_tf = 300 if "5m" in p.slug else 900
                if p.market_end:
                    remaining = max(0, (p.market_end - datetime.now(timezone.utc)).total_seconds())
                    bar_pct = max(0, min(1.0 - remaining / _pos_tf, 1.0))
                    time_str = f"{int(remaining//60)}:{int(remaining%60):02d}"
                else:
                    age = (datetime.now(timezone.utc) - p.opened).total_seconds() if p.opened else 0
                    bar_pct = min(age / _pos_tf, 1.0)
                    time_str = f"{int(age//60)}:{int(age%60):02d}"
                bar_len = int(bar_pct * 10)
                bar = f"{OK}{'█' * bar_len}{DIM}{'░' * (10 - bar_len)}{R}"
                side_col = OK if p.side == "YES" else ERR
                # v9.4: Asset label from slug
                _pos_asset = p.slug.split("-")[0].upper() if p.slug else "?"
                _pos_tfm = "5m" if "5m" in p.slug else "15m"
                print(f"  {H1}│{R}  {side_col}{p.side:3}{R} {DIM}[{p.strat[:5]:5}]{R} ${p.cost:.2f} @${p.entry:.2f}  {bar} {VAL}{time_str}{R} {DIM}{_pos_asset}{_pos_tfm}{R}    {H1}│{R}")
            print(f"  {H1}└{'─'*62}┘{R}")

        # ╔══════════════════════════════════════════════════════════════╗
        #  EVENTS — Recent activity
        # ╚══════════════════════════════════════════════════════════════╝
        evts = list(s.evts)[-6:]
        if evts:
            print(f"\n  {LBL}EVENTS{R}")
            for e in evts:
                if "WON" in e or "REDEEMED" in e or "ACTIVE" in e:
                    print(f"    {OK}{e}{R}")
                elif "LOST" in e or "Err" in e or "CANCELLED" in e:
                    print(f"    {ERR}{e}{R}")
                elif "LAT" in e or "FLASH" in e:
                    print(f"    {OK}{e}{R}")
                else:
                    print(f"    {DIM}{e}{R}")

        # ╔══════════════════════════════════════════════════════════════╗
        #  TRADE HISTORY — Last 10 trades
        # ╚══════════════════════════════════════════════════════════════╝
        all_ended = []
        if past_trades:
            for t in past_trades: all_ended.append(t)
        # Add closed positions NOT already in past_trades (avoids duplicates)
        past_slugs_costs = set()
        for t in all_ended:
            past_slugs_costs.add((t.get("slug",""), round(t.get("cost",0),2), t.get("side",""), round(t.get("entry",0),4)))
        closed = [p for p in risk.positions if p.status != "OPEN"]
        for p in closed:
            key = (p.slug, round(p.cost,2), p.side, round(p.entry,4))
            if key in past_slugs_costs: continue  # already in past_trades
            local_ts = "?"
            if p.opened:
                local_time = p.opened.astimezone() if p.opened.tzinfo else p.opened
                local_ts = local_time.strftime('%H:%M %m/%d')
            all_ended.append({
                "ts": local_ts,
                "status": "WIN" if p.pnl > 0 else "LOSS",
                "strat": p.strat, "side": p.side, "cost": p.cost,
                "entry": p.entry, "pnl": p.pnl, "slug": p.slug,
            })
        if all_ended:
            recent = all_ended[-10:]
            recent.reverse()
            w_count = sum(1 for t in all_ended if t["pnl"] > 0)
            l_count = sum(1 for t in all_ended if t["pnl"] <= 0)
            total_pnl = sum(t["pnl"] for t in all_ended)
            print(f"\n  {H1}┌{'─'*62}┐{R}")
            print(f"  {H1}│{R}  {LBL}TRADE HISTORY{R}  {OK}{w_count}W{R} {ERR}{l_count}L{R}  Total: {pnl_c2(total_pnl)}                    {H1}│{R}")
            print(f"  {H1}├{'─'*62}┤{R}")
            for t in recent:
                if t["pnl"] > 0:
                    icon = f"{OK}✓{R}"; col = OK
                else:
                    icon = f"{ERR}✗{R}"; col = ERR
                ts = t.get("ts", "?")
                try:
                    if len(str(ts)) >= 16:
                        parts = str(ts).split(" ")
                        date_p = parts[0].split("-")
                        time_p = parts[1][:5]
                        ts = f"{time_p} {date_p[1]}/{date_p[2]}"
                except: pass
                # v9.4: Extract asset+timeframe from slug
                slug = t.get("slug", "")
                asset_label = ""
                if slug:
                    slug_parts = slug.split("-")
                    if len(slug_parts) >= 3:
                        asset_label = f"{slug_parts[0].upper()}{slug_parts[2]}"  # e.g. BTC15m, ETH15m
                    else:
                        asset_label = slug_parts[0].upper() if slug_parts else ""
                print(f"  {H1}│{R}  {icon} {DIM}{ts:11}{R} {col}{t['side']:3}{R} [{t['strat'][:5]:5}] ${t['cost']:.2f}@${t['entry']:.2f} {pnl_c2(t['pnl'])} {DIM}{asset_label}{R} {H1}│{R}")
            print(f"  {H1}└{'─'*62}┘{R}")

        # ── FOOTER ──
        print(f"\n  {DIM}{'─'*62}")
        print(f"  Ctrl+C to stop{R}")

# ─── MAIN BOT v10 ───
class Bot:
    HISTORY_FILE = "trade_history.txt"

    def __init__(s):
        s.c = Config.from_env(); s.conn = Conn()
        # v9.4: Multi-asset price feeds
        s.feeds = {}
        s._asset_list = list(set(a for a, t in s.c.slots))
        for asset in s._asset_list:
            s.feeds[asset] = Feed(asset)
        s.feed = s.feeds.get("btc", Feed("btc"))  # default context
        s.finder = Finder(s.c); s.ex = Executor(s.c); s.risk = Risk(s.c)
        s.dash = Dash()
        # v9.4: Per-asset intelligence
        s.trends = {a: TrendEngine() for a in s._asset_list}
        s.trend = s.trends.get("btc", TrendEngine())
        s.momentum_guards = {a: MomentumGuard() for a in s._asset_list}
        s.momentum_guard = s.momentum_guards.get("btc", MomentumGuard())
        # v6: Intelligence engines (shared)
        s.sizer = AdaptiveSizer(s.c)
        s.confirm = ConfirmationEngine()
        s.conviction = ConvictionEngine()
        s.win_streak = WinStreakSizer()
        s.market_losses = MarketLossTracker()
        # v9: The Cortex — unified intelligence
        s.cortex = Cortex()
        s.cortex.feed = s.feed
        # v9.4: Per-slot state (each slot = asset + timeframe)
        s.slot_state = {}
        for asset, tf in s.c.slots:
            key = f"{asset}-{tf}m"
            s.slot_state[key] = {
                "asset": asset, "tf": tf, "key": key,
                "market": None,
                "poly_ws": PolyWebSocket(),
                "token_feed": TokenFeed(),
                "book_intel": OrderBookIntel(),
            }
        s.data = DataCollector()
        # Strategies (shared — they're stateless check functions)
        s.s1 = S_Arb(s.c); s.s2 = S_Latency(s.c); s.s3 = S_MeanReversion(s.c); s.s4 = S_Flash(s.c); s.s5 = S_Squeeze(s.c); s.s6 = S_PairAccum(s.c); s.s7 = S_Spike(s.c)
        s.mkt = None; s.strats = {"ARB": "...", "LATENCY": "...", "MEANREV": "...", "FLASH": "...", "SQUEEZE": "...", "PAIR": "...", "SPIKE": "..."}
        s.cd = {}; s._traded_cids = set()
        # v9.5: Cross-slot direction limiter — prevents correlated losses
        # Tracks recent entries: [(timestamp, side, slot_key), ...]
        s._recent_entries = []
        s.start_time = time.time()
        s._logged_positions = set()
        s._past_trades = []
        s._last_win_market = None
        s._active_slot = ""  # v9.4: which slot is currently being traded
        s._slot_markets = {}  # v9.4: quick ref for dashboard
        # Backward compat aliases
        s.token_feed = s.slot_state.get("btc-15m", {}).get("token_feed", TokenFeed())
        s.book_intel = s.slot_state.get("btc-15m", {}).get("book_intel", OrderBookIntel())
        s.poly_ws = s.slot_state.get("btc-15m", {}).get("poly_ws", PolyWebSocket())
        s.cortex.token_feed = s.token_feed
        s.cortex.book_intel = s.book_intel
        s.cortex.trend = s.trend
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
            f.write(f"  BOT v10.1 STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
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
        # v9.1: Skip logging SYNCED/UNKNOWN trades — their P&L is unreliable
        # They show as fake losses and confuse the trade history
        if pos.strat in ("SYNCED", "UNKNOWN") or pos.status == "CANCELLED":
            return
        # v9 fix: Add to _past_trades so it survives cleanup and stays in dashboard
        if pos.opened:
            local_time = pos.opened.astimezone() if pos.opened.tzinfo else pos.opened
            ts = local_time.strftime('%Y-%m-%d %H:%M:%S')
        else:
            ts = "?"
        s._past_trades.append({
            "ts": ts, "status": "WIN" if pos.pnl > 0 else "LOSS",
            "strat": pos.strat, "side": pos.side, "cost": pos.cost,
            "entry": pos.entry, "pnl": pos.pnl, "slug": pos.slug,
        })
        try:
            with open(s.HISTORY_FILE, "a") as f:
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
                f.write(f"  BOT v10.1 STOPPED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
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
                if not any(a in title_lower for a in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple"]): continue
                market_end = None
                if slug:
                    try:
                        parts = slug.split("-")
                        # v9.4: Detect timeframe from slug (e.g., "btc-updown-15m-...")
                        tf_secs = 900  # default 15m
                        for part in parts:
                            if part == "5m": tf_secs = 300
                            elif part == "15m": tf_secs = 900
                        for part in parts:
                            if part.isdigit() and len(part) >= 10:
                                market_end = datetime.fromtimestamp(int(part) + tf_secs, tz=timezone.utc); break
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
        print(f"\n  {H1}{'='*55}\n  |  POLYMARKET BOT v10.1 — BTC FOCUSED\n  |  BTC 5m + 15m | Backtest-Optimized\n  {'='*55}{R}\n")
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
        print(f"  {H2}[4/4]{R} Price feeds...")
        for asset in s._asset_list:
            feed_ok = False
            for _ in range(3):
                p = s.feeds[asset].poll()
                if p: print(f"        {OK}{asset.upper()}: ${p:,.2f}{R}"); feed_ok = True; break
                time.sleep(1)
            if not feed_ok: print(f"        {ERR}{asset.upper()}: Failed{R}")
        s.conn.binance = "OK"
        # v7: Show intelligence status
        print(f"\n  {H2}Intelligence:{R}")
        print(f"    Trend Engine: {OK}Active{R}")
        print(f"    Adaptive Sizing: {OK}Active{R} ({len(s.sizer.history)} historical trades)")
        print(f"    Confirmation: {OK}Active{R}")
        print(f"    Loss Streak Protection: {OK}Active{R} (pause after {s.c.streak_pause_count} losses)")
        print(f"    Conviction Engine: {OK}Active{R} (1.5x when 2+ strategies agree)")
        print(f"    Momentum Guard: {OK}Active{R} (blocks counter-trend after 3min)")
        print(f"    Win Streak Sizer: {OK}Active{R} (1.2-1.4x boost on 3-5+ wins)")
        print(f"    Time-of-Day Sizing: {OK}Active{R} (adjust by hourly performance)")
        print(f"    {H2}v10 STRATEGY SYSTEMS:{R}")
        print(f"    Token Price Feed: {OK}Active{R} (tracks Polymarket prices for divergence)")
        print(f"    Order Book Intel: {OK}Active{R} (whale detection + imbalance)")
        print(f"    Mean Reversion: {OK}Active{R} (replaces Momentum — buys the bounce)")
        print(f"    Smart Flash: {OK}Active{R} (v10: 5m only, $0.38-$0.55, Chainlink open direction)")
        print(f"    Market Loss Limit: {OK}Active{R} (stop after 2 losses per market)")
        print(f"    {H2}v10 BACKTEST-PROVEN CHANGES:{R}")
        print(f"    ARB Threshold: {OK}<$0.95{R} (was $0.96 — tighter = +$467 more profit)")
        print(f"    Latency Range: {OK}$0.15-$0.55{R} (was $0.40 — catches +866 more trades)")
        print(f"    Flash Rewrite: {OK}5m mid-price{R} (was cheap $0.15-$0.30 — 55% WR vs 25%)")
        print(f"    MeanRev: {OK}Unchanged{R} (already best variant at +$2,613)")
        print(f"    {H2}v10 THE CORTEX:{R}")
        print(f"    Unified Brain: {OK}Active{R} (replaces Manager — EV-based trust, not win rate)")
        print(f"    Macro Bias: {OK}Active{R} (cross-market momentum from last 12 outcomes)")
        print(f"    Session P&L: {OK}Active{R} (adapts aggression based on session performance)")
        print(f"    Regime Matching: {OK}Active{R} (learns which strat+regime combos work)")
        print(f"    Pattern Discovery: {OK}Active{R} (scans for correlations every 10 trades)")
        print(f"    {H2}v10 SYSTEMS:{R}")
        print(f"    5-Min Hard Block: {OK}Active{R} (blocks counter-trend when 5m trend is clear)")
        print(f"    Directional Bias: {OK}Active{R} (reduces side that keeps losing in session)")
        print(f"    Recovery Detection: {OK}Active{R} (snaps back sizing after 2 consecutive wins)")
        print(f"    Lifecycle Model: {OK}Active{R} (learns price patterns at min 2/4/6/8/10/12)")
        print(f"    Grinder: {OK}Active{R} (near-certain outcome buying, last 3 min, $0.82-$0.92)")
        print(f"    Max 3 Trades/Market: {OK}Active{R} (hard cap on total trades per 15m window)")
        # v9.1: Show actual data loaded
        cortex_trades = sum(len(v) for v in s.cortex._trades.values())
        regime_combos = len(s.cortex._regime_perf)
        lc_markets = len(s.cortex._lifecycle_data)
        print(f"\n  {H2}Intelligence Data:{R}")
        print(f"    Trade history: {OK}{len(s.sizer.history)}{R} trades → Cortex: {OK}{cortex_trades}{R} scored")
        print(f"    Regime map: {OK}{regime_combos}{R} strat×regime combos learned")
        print(f"    Lifecycle model: {OK}{lc_markets}{R} markets profiled{' (empty — collecting)' if lc_markets == 0 else ''}")
        print(f"    Trust: " + "  ".join(f"{st[:3]}={s.cortex._trust[st]:.2f}x" for st in s.cortex.STRATS))
        print(f"\n  {H1}{'='*55}{R}")
        print(f"  {'LIVE TRADING v10.1' if not s.c.dry_run else 'DRY RUN v10'}")
        print(f"  {H1}{'='*55}{R}")
        time.sleep(3); s._init_history(); s._sync_existing_positions(); s.dash.ev("Bot v10.1 started"); s._loop()

    def _set_slot_context(s, slot):
        """Switch the bot's context to trade a specific asset/timeframe slot.
        This sets s.feed, s.trend, etc. so _trade() works without changes."""
        asset = slot["asset"]
        s.feed = s.feeds[asset]
        s.trend = s.trends[asset]
        s.momentum_guard = s.momentum_guards[asset]
        s.token_feed = slot["token_feed"]
        s.book_intel = slot["book_intel"]
        s.poly_ws = slot["poly_ws"]
        s.mkt = slot["market"]
        s._active_slot = slot["key"]
        # Update Cortex references
        s.cortex.feed = s.feed
        s.cortex.token_feed = s.token_feed
        s.cortex.book_intel = s.book_intel
        s.cortex.trend = s.trend

    def _loop(s):
        ctr = 0; s._orders = []; s._poly_pos = []
        while True:
            try:
                # v9.4: Poll ALL asset feeds every tick
                for asset in s._asset_list:
                    s.feeds[asset].poll()
                ctr += 1
                # v9.4: Update ALL trend engines
                for asset in s._asset_list:
                    s.trends[asset].update(s.feeds[asset])
                    s.momentum_guards[asset].update(s.feeds[asset])
                # v9: Cortex perceives (uses current context)
                s.feed = s.feeds.get("btc", s.feeds[list(s.feeds.keys())[0]])
                s.trend = s.trends.get("btc", s.trends[list(s.trends.keys())[0]])
                s.cortex.feed = s.feed; s.cortex.trend = s.trend
                s.cortex.perceive()
                resolved = s.risk.check_exp(s.feed, feeds=s.feeds); s._cancel_exp()
                for p in resolved:
                    # v8.1: Skip cancelled/unfilled orders — don't corrupt learning
                    if p.status == "CANCELLED" or p.cost <= 0:
                        s.dash.ev(f"[{p.strat[:3]}] CANCELLED (unfilled)")
                        continue
                    s.dash.ev(f"[{p.strat[:3]}] {p.status} P&L:{p.pnl:+.2f}")
                    # v9.1: Skip ALL learning for SYNCED/UNKNOWN/CANCELLED trades
                    # Previously only sizer was skipped — Cortex, win_streak, market_losses
                    # all got poisoned by unreliable SYNCED P&L data.
                    if p.strat in ("SYNCED", "UNKNOWN") or p.status == "CANCELLED":
                        continue
                    # v6: Record in adaptive sizer
                    hour = p.opened.hour if p.opened else datetime.now(timezone.utc).hour
                    s.sizer.record(p.strat, p.side, p.pnl > 0, p.pnl, p.entry, hour, 
                                   p.entry_regime if p.entry_regime != "UNKNOWN" else (s.trend.regime if s.trend else "UNKNOWN"),
                                   btc_price=s.feed.price if s.feed else 0)
                    # v7: Track win streak
                    s.win_streak.record(p.pnl > 0)
                    # v8: Track per-market losses
                    if p.pnl <= 0:
                        s.market_losses.record_loss(p.slug)
                    # v9: Feed the Cortex
                    btc_p = s.feed.price if s.feed else 0
                    # v9.5: Extract slot_key from position slug
                    _pos_parts = p.slug.split("-") if p.slug else []
                    _pos_asset = _pos_parts[0] if _pos_parts else "btc"
                    _pos_tf = "5m" if "5m" in p.slug else "15m"
                    _pos_slot = f"{_pos_asset}-{_pos_tf}"
                    s.cortex.record_trade(p.strat, p.pnl > 0, p.pnl, p.cost,
                        regime=s.trend.regime if s.trend else "UNKNOWN",
                        btc_price=btc_p, side=p.side, slot_key=_pos_slot)
                    # v7: Smart redeem — trigger after wins
                    if p.pnl > 0:
                        s._last_win_market = time.time()
                    # v7: Record to CSV data collector
                    btc_now = s.feed.price if s.feed.price else 0
                    btc_at_open = 0
                    if p.opened:
                        for tick in s.feed.data:
                            if tick["t"] >= p.opened.timestamp():
                                btc_at_open = tick["p"]; break
                    conv_count = len(s.conviction._signals.get(p.slug, {}).get(p.side, []))
                    conv_bonus = s.conviction.get_bonus(p.slug, p.side)
                    market_tl = 0
                    if p.market_end:
                        market_tl = (p.market_end - datetime.now(timezone.utc)).total_seconds()
                    s.data.record_trade(
                        p, btc_entry=btc_at_open, btc_exit=btc_now,
                        feed=s.feed, trend=s.trend,
                        conviction_count=conv_count, conviction_bonus=conv_bonus,
                        streak_mult=s.win_streak.get_multiplier(),
                        tod_mult=1.0, market_tl=market_tl
                    )
                for p in s.risk.positions:
                    if p.status != "OPEN": s._log_trade(p)
                # v9.4: Update ALL slot markets every tick via their WebSockets
                for slot_key, slot in s.slot_state.items():
                    sm = slot["market"]
                    if not sm: continue
                    pws = slot["poly_ws"]
                    try:
                        if pws.is_live and pws.yes_p > 0 and pws.no_p > 0:
                            sm.yes_p, sm.no_p = pws.yes_p, pws.no_p
                            slot["token_feed"].update(sm.slug, sm.yes_p, sm.no_p)
                    except: pass
                # v9.5: MARKET DISCOVERY — every 5 ticks, find/update markets via Gamma API
                if ctr % 5 == 0:
                    for slot_key, slot in s.slot_state.items():
                        asset, tf = slot["asset"], slot["tf"]
                        m = s.finder.find(asset, tf)
                        if not m:
                            # v10.1: Log when market discovery FAILS
                            if ctr % 25 == 0:
                                log.warning(f"v10FIND {slot_key}: finder returned None — no active {asset}-{tf}m market")
                            continue
                        if m:
                            s.conn.gamma = "OK"
                            try:
                                yp, np_ = s.ex.prices(m); m.yes_p, m.no_p = yp, np_
                            except: pass
                            old_market = slot["market"]
                            new_market = (old_market is None or old_market.slug != m.slug)
                            if new_market:
                                if old_market and old_market.slug:
                                    asset_feed = s.feeds[asset]
                                    btc_close = asset_feed.price if asset_feed.price else 0
                                    s.data.close_market(
                                        old_market.slug, old_market.open_btc, btc_close,
                                        start_regime=s.trends[asset].regime,
                                        end_regime=s.trends[asset].regime
                                    )
                                    if old_market.open_btc and btc_close:
                                        went_up = btc_close >= old_market.open_btc
                                        s.cortex.record_outcome(went_up, asset=asset)
                                        s.cortex.lifecycle_close(went_up, old_market.yes_p)
                            slot["market"] = m
                            s._slot_markets[slot_key] = m
                            if new_market:
                                asset_feed = s.feeds[asset]
                                m.open_btc = asset_feed.price if asset_feed.price else 0
                                tf_label = f"{asset.upper()}-{tf}m"
                                s.dash.ev(f"New market: {tf_label} {m.slug[-15:]}")
                                s.s1.reset(m.slug)
                                if m.tok_yes and m.tok_no:
                                    slot["poly_ws"].subscribe(m.tok_yes, m.tok_no, m.slug)
                            # Update book intel + token feed
                            try:
                                slot["book_intel"].update(s.ex, m)
                                slot["token_feed"].update(m.slug, m.yes_p, m.no_p)
                            except: pass

                # v9.5: Check pending maker orders for fills (non-blocking)
                try:
                    s.ex.check_pending_fills()
                except Exception as e:
                    log.error(f"Pending fills check error: {e}")

                # v9.5: TRADE EXECUTION — every tick, check all slots with valid markets
                # This is the key fix: strategies like Flash/Latency need to see price
                # changes every second, not every 5 seconds after a Gamma API call.
                _any_traded = False
                for slot_key, slot in s.slot_state.items():
                    sm = slot.get("market")
                    if not sm:
                        if ctr % 50 == 0:
                            log.warning(f"v10STALL {slot_key}: no market found")
                        continue
                    tl = (sm.end - datetime.now(timezone.utc)).total_seconds()
                    if tl < 10:
                        if ctr % 50 == 0:
                            log.warning(f"v10STALL {slot_key}: market expired (tl={tl:.0f}s) slug={sm.slug[-20:]}")
                        continue  # market about to expire
                    _any_traded = True
                    s._set_slot_context(slot)
                    try:
                        if s.conn.can_trade or s.c.dry_run: s._trade(sm)
                    except Exception as e:
                        log.error(f"Trade error on {slot_key}: {e}")
                        s.dash.ev(f"Trade err: {slot_key}")
                
                # v10.1: STALL DETECTOR — if no slots had tradeable markets, log it
                if not _any_traded and ctr % 15 == 0:
                    slot_status = {}
                    for sk, sl in s.slot_state.items():
                        sm = sl.get("market")
                        if sm:
                            tl = (sm.end - datetime.now(timezone.utc)).total_seconds()
                            slot_status[sk] = f"tl={tl:.0f}s slug={sm.slug[-15:]}"
                        else:
                            slot_status[sk] = "NO MARKET"
                    log.warning(f"v10STALL NO TRADEABLE MARKETS: {slot_status}")
                if ctr % 30 == 0 and s.ex.authed:
                    rb = s.ex.get_balance()
                    if rb: s.risk.set_real(rb)
                    s._orders = s.ex.get_open_orders()
                    s._poly_pos = s.ex.get_positions()
                # v7: Smart redeem — after wins or every 3 min (was every 90 ticks)
                should_redeem = (
                    not s.c.dry_run and s._traded_cids and (
                        ctr % 90 == 0 or  # periodic fallback
                        (s._last_win_market and time.time() - s._last_win_market < 30 and ctr % 10 == 0)  # quick after win
                    )
                )
                if should_redeem:
                    s._auto_redeem()
                    if s._last_win_market and time.time() - s._last_win_market < 30:
                        s._last_win_market = None  # only trigger once per win
                # Periodic cleanup — keeps memory stable for long runs
                if ctr % 500 == 0:
                    s._cleanup()
                # v9.4: Long-running stability checks
                if ctr % 100 == 0:
                    # v9.4: Health check ALL feeds
                    for asset, feed in s.feeds.items():
                        if feed.n > 0:
                            last_data_age = time.time() - feed.data[-1]["t"]
                            if last_data_age > 120:
                                log.warning(f"{asset.upper()} feed stale ({last_data_age:.0f}s). Reconnecting...")
                                s.dash.ev(f"{asset.upper()} feed stale — reconnecting")
                                try:
                                    feed._start_ws()
                                except:
                                    feed.poll()
                    import gc
                    gc.collect()
                try:
                    s.dash.render(s.c, s.conn, s.feeds.get("btc", s.feed), s.risk, s.mkt, s.strats, s.s3.scores,
                        s._orders, s._poly_pos, s.start_time, s._past_trades,
                        s.trends.get("btc", s.trend), s.sizer,
                        cortex=s.cortex, poly_ws=s.poly_ws,
                        slot_markets=s._slot_markets, feeds=s.feeds, active_slot=s._active_slot)
                except Exception as e:
                    log.debug(f"Dashboard render error: {e}")
                time.sleep(s.c.poll_sec)
            except KeyboardInterrupt:
                try: s.ex.cancel_all()
                except: pass
                try: s._auto_redeem()
                except: pass
                try: s._close_history()
                except: pass
                try: s.cortex._save_lifecycle()
                except: pass
                try: s._summary()
                except: pass
                break
            except Exception as e:
                log.error(f"Loop: {e}\n{traceback.format_exc()}")
                s.dash.ev(f"Err: {str(e)[:40]}")
                # v9.4: Track consecutive errors for backoff
                if not hasattr(s, '_consec_errors'): s._consec_errors = 0
                s._consec_errors += 1
                wait = min(3 * s._consec_errors, 30)  # backoff: 3s, 6s, 9s... max 30s
                if s._consec_errors >= 10:
                    log.error(f"10 consecutive errors — restarting bot")
                    s.dash.ev("10 errors — restarting")
                    raise  # let the outer auto-restart loop handle it
                time.sleep(wait)
            else:
                # Reset error counter on successful tick
                if hasattr(s, '_consec_errors'): s._consec_errors = 0

    def _cancel_exp(s):
        now = datetime.now(timezone.utc)
        # v9.5: Check ALL slot markets for expiring orders
        any_expiring = False
        for slot_key, slot in s.slot_state.items():
            sm = slot.get("market")
            if sm:
                tl = (sm.end - now).total_seconds()
                if 0 < tl < 120:
                    any_expiring = True
                    break
        if any_expiring and s._orders:
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

        # 0. v9.5: PHANTOM POSITION CLEANUP — detect unfilled GTC orders
        # If bot has an OPEN position but the market has ended AND there's no
        # matching position on Polymarket, the order never filled → remove it.
        if s._poly_pos is not None:
            poly_slugs = set()
            for pp in (s._poly_pos or []):
                ps = pp.get("slug") or pp.get("market", {}).get("slug", "")
                if ps: poly_slugs.add(ps)
            phantoms = []
            for p in s.risk.positions:
                if p.status != "OPEN": continue
                if p.market_end and (now - p.market_end).total_seconds() > 120:
                    # Market ended 2+ min ago — check if Polymarket knows about it
                    if p.slug not in poly_slugs:
                        phantoms.append(p)
            for p in phantoms:
                log.info(f"PHANTOM CLEANUP: {p.strat} {p.side} ${p.cost:.2f} on {p.slug} — order never filled")
                s.dash.ev(f"Phantom removed: {p.strat} ${p.cost:.2f} (unfilled)")
                p.status = "CANCELLED"
                p.pnl = 0.0
                # Return the risk
                s.risk.open_risk = max(0, s.risk.open_risk - p.cost)

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

        # 5. Trim _past_trades (dashboard display only)
        if len(s._past_trades) > 100:
            s._past_trades = s._past_trades[-100:]

        # 6. Clean old _market_trades entries (markets that resolved long ago)
        if hasattr(s.cortex, '_market_trades') and len(s.cortex._market_trades) > 20:
            slugs = list(s.cortex._market_trades.keys())
            for slug in slugs[:-20]:
                del s.cortex._market_trades[slug]

        # 7. Clean PairAccum tracker
        if hasattr(s, 's6') and hasattr(s.s6, '_pairs') and len(s.s6._pairs) > 20:
            keys = list(s.s6._pairs.keys())
            for k in keys[:-20]:
                del s.s6._pairs[k]

        if removed > 0:
            log.debug(f"Cleanup: removed {removed} old positions, {len(stale_keys)} stale confirms")

    
    def _cancel_exp(s):
        now = datetime.now(timezone.utc)
        # v9.5: Check ALL slot markets for expiring orders
        any_expiring = False
        for slot_key, slot in s.slot_state.items():
            sm = slot.get("market")
            if sm:
                tl = (sm.end - now).total_seconds()
                if 0 < tl < 120:
                    any_expiring = True
                    break
        if any_expiring and s._orders:
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

        # 0. v9.5: PHANTOM POSITION CLEANUP — detect unfilled GTC orders
        # If bot has an OPEN position but the market has ended AND there's no
        # matching position on Polymarket, the order never filled → remove it.
        if s._poly_pos is not None:
            poly_slugs = set()
            for pp in (s._poly_pos or []):
                ps = pp.get("slug") or pp.get("market", {}).get("slug", "")
                if ps: poly_slugs.add(ps)
            phantoms = []
            for p in s.risk.positions:
                if p.status != "OPEN": continue
                if p.market_end and (now - p.market_end).total_seconds() > 120:
                    # Market ended 2+ min ago — check if Polymarket knows about it
                    if p.slug not in poly_slugs:
                        phantoms.append(p)
            for p in phantoms:
                log.info(f"PHANTOM CLEANUP: {p.strat} {p.side} ${p.cost:.2f} on {p.slug} — order never filled")
                s.dash.ev(f"Phantom removed: {p.strat} ${p.cost:.2f} (unfilled)")
                p.status = "CANCELLED"
                p.pnl = 0.0
                # Return the risk
                s.risk.open_risk = max(0, s.risk.open_risk - p.cost)

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

        # 5. Trim _past_trades (dashboard display only)
        if len(s._past_trades) > 100:
            s._past_trades = s._past_trades[-100:]

        # 6. Clean old _market_trades entries (markets that resolved long ago)
        if hasattr(s.cortex, '_market_trades') and len(s.cortex._market_trades) > 20:
            slugs = list(s.cortex._market_trades.keys())
            for slug in slugs[:-20]:
                del s.cortex._market_trades[slug]

        # 7. Clean PairAccum tracker
        if hasattr(s, 's6') and hasattr(s.s6, '_pairs') and len(s.s6._pairs) > 20:
            keys = list(s.s6._pairs.keys())
            for k in keys[:-20]:
                del s.s6._pairs[k]

        if removed > 0:
            log.debug(f"Cleanup: removed {removed} old positions, {len(stale_keys)} stale confirms")

    
    def _cancel_exp(s):
        now = datetime.now(timezone.utc)
        # v9.5: Check ALL slot markets for expiring orders
        any_expiring = False
        for slot_key, slot in s.slot_state.items():
            sm = slot.get("market")
            if sm:
                tl = (sm.end - now).total_seconds()
                if 0 < tl < 120:
                    any_expiring = True
                    break
        if any_expiring and s._orders:
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

        # 0. v9.5: PHANTOM POSITION CLEANUP — detect unfilled GTC orders
        # If bot has an OPEN position but the market has ended AND there's no
        # matching position on Polymarket, the order never filled → remove it.
        if s._poly_pos is not None:
            poly_slugs = set()
            for pp in (s._poly_pos or []):
                ps = pp.get("slug") or pp.get("market", {}).get("slug", "")
                if ps: poly_slugs.add(ps)
            phantoms = []
            for p in s.risk.positions:
                if p.status != "OPEN": continue
                if p.market_end and (now - p.market_end).total_seconds() > 120:
                    # Market ended 2+ min ago — check if Polymarket knows about it
                    if p.slug not in poly_slugs:
                        phantoms.append(p)
            for p in phantoms:
                log.info(f"PHANTOM CLEANUP: {p.strat} {p.side} ${p.cost:.2f} on {p.slug} — order never filled")
                s.dash.ev(f"Phantom removed: {p.strat} ${p.cost:.2f} (unfilled)")
                p.status = "CANCELLED"
                p.pnl = 0.0
                # Return the risk
                s.risk.open_risk = max(0, s.risk.open_risk - p.cost)

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

        # 5. Trim _past_trades (dashboard display only)
        if len(s._past_trades) > 100:
            s._past_trades = s._past_trades[-100:]

        # 6. Clean old _market_trades entries (markets that resolved long ago)
        if hasattr(s.cortex, '_market_trades') and len(s.cortex._market_trades) > 20:
            slugs = list(s.cortex._market_trades.keys())
            for slug in slugs[:-20]:
                del s.cortex._market_trades[slug]

        # 7. Clean PairAccum tracker
        if hasattr(s, 's6') and hasattr(s.s6, '_pairs') and len(s.s6._pairs) > 20:
            keys = list(s.s6._pairs.keys())
            for k in keys[:-20]:
                del s.s6._pairs[k]

        if removed > 0:
            log.debug(f"Cleanup: removed {removed} old positions, {len(stale_keys)} stale confirms")

    
    def _cancel_exp(s):
        now = datetime.now(timezone.utc)
        # v9.5: Check ALL slot markets for expiring orders
        any_expiring = False
        for slot_key, slot in s.slot_state.items():
            sm = slot.get("market")
            if sm:
                tl = (sm.end - now).total_seconds()
                if 0 < tl < 120:
                    any_expiring = True
                    break
        if any_expiring and s._orders:
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

        # 0. v9.5: PHANTOM POSITION CLEANUP — detect unfilled GTC orders
        # If bot has an OPEN position but the market has ended AND there's no
        # matching position on Polymarket, the order never filled → remove it.
        if s._poly_pos is not None:
            poly_slugs = set()
            for pp in (s._poly_pos or []):
                ps = pp.get("slug") or pp.get("market", {}).get("slug", "")
                if ps: poly_slugs.add(ps)
            phantoms = []
            for p in s.risk.positions:
                if p.status != "OPEN": continue
                if p.market_end and (now - p.market_end).total_seconds() > 120:
                    # Market ended 2+ min ago — check if Polymarket knows about it
                    if p.slug not in poly_slugs:
                        phantoms.append(p)
            for p in phantoms:
                log.info(f"PHANTOM CLEANUP: {p.strat} {p.side} ${p.cost:.2f} on {p.slug} — order never filled")
                s.dash.ev(f"Phantom removed: {p.strat} ${p.cost:.2f} (unfilled)")
                p.status = "CANCELLED"
                p.pnl = 0.0
                # Return the risk
                s.risk.open_risk = max(0, s.risk.open_risk - p.cost)

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

        # 5. Trim _past_trades (dashboard display only)
        if len(s._past_trades) > 100:
            s._past_trades = s._past_trades[-100:]

        # 6. Clean old _market_trades entries (markets that resolved long ago)
        if hasattr(s.cortex, '_market_trades') and len(s.cortex._market_trades) > 20:
            slugs = list(s.cortex._market_trades.keys())
            for slug in slugs[:-20]:
                del s.cortex._market_trades[slug]

        # 7. Clean PairAccum tracker
        if hasattr(s, 's6') and hasattr(s.s6, '_pairs') and len(s.s6._pairs) > 20:
            keys = list(s.s6._pairs.keys())
            for k in keys[:-20]:
                del s.s6._pairs[k]

        if removed > 0:
            log.debug(f"Cleanup: removed {removed} old positions, {len(stale_keys)} stale confirms")

    def _trade(s, m):
        if not s.risk.ok(): return
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        duration = m.timeframe * 60  # 300 for 5m, 900 for 15m
        min_tl = 30 if m.timeframe == 5 else 90  # 30s for 5m, 90s for 15m
        if tl < min_tl: return
        av = s.risk.available
        if av < 1.0: return

        # v10: DEBUG — log strategy state every 60 seconds
        _debug_key = f"_last_debug_{m.slug}"
        if not hasattr(s, '_debug_times'): s._debug_times = {}
        if time.time() - s._debug_times.get(_debug_key, 0) > 60:
            s._debug_times[_debug_key] = time.time()
            cl_div = abs(s.feed.price_divergence) if s.feed.cl_price > 0 else -1
            cl_age = s.feed.cl_age if hasattr(s.feed, 'cl_age') else -1
            btc_chg = s.feed.chg(60) * 100 if s.feed.n > 10 else 0
            chg_open = ((s.feed.price - m.open_btc) / m.open_btc * 100) if m.open_btc > 0 and s.feed.price > 0 else 0
            log.info(f"v10DBG {m.asset}-{m.timeframe}m Y={m.yes_p:.2f} N={m.no_p:.2f} "
                     f"sum={m.yes_p+m.no_p:.3f} tl={tl:.0f}s av=${av:.0f} "
                     f"BTC=${s.feed.price:,.0f} chg1m={btc_chg:+.2f}% open={chg_open:+.3f}% "
                     f"cl_div={cl_div:.4f} cl_age={cl_age:.0f}s")
        
        # v10: ALIVE LOG — every 5 min, show comprehensive status
        _alive_key = "_last_alive"
        if not hasattr(s, '_alive_time'): s._alive_time = 0
        if time.time() - s._alive_time > 300:
            s._alive_time = time.time()
            total_trades = sum(1 for p in s.risk.positions)
            open_trades = sum(1 for p in s.risk.positions if p.status == "OPEN")
            wins = sum(1 for p in s.risk.positions if getattr(p, 'result', '') == "WIN")
            losses = sum(1 for p in s.risk.positions if getattr(p, 'result', '') == "LOSS")
            log.info(f"v10ALIVE bal=${s.risk.show_bal:.0f} open={open_trades} "
                     f"total={total_trades} W={wins} L={losses} "
                     f"BTC=${s.feed.price:,.0f} session_pnl=${s.cortex._session_pnl:+.0f}")

        # v10: Chainlink divergence — reduce size but DON'T zero out
        # Stale Chainlink (30+ min old) shouldn't prevent ALL trading
        _cl_mult = 1.0
        if s.feed.cl_price > 0:
            divergence = abs(s.feed.price_divergence)
            cl_age = s.feed.cl_age if hasattr(s.feed, 'cl_age') else 0
            if divergence > 0.003 and cl_age < 300:
                # Only skip if Chainlink is FRESH and disagreeing (real divergence)
                _cl_mult = 0.3  # v10: reduce, don't zero
            elif divergence > 0.001:
                _cl_mult = 0.7  # v10: gentle reduction

        # v9.4: Asset/timeframe label for events
        slot_label = f"{m.asset.upper()}-{m.timeframe}m"
        slot_key = f"{m.asset}-{m.timeframe}m"  # v9.5: for per-slot trust lookup

        # v9.1: Market Lifecycle snapshot at key minute marks
        market_minute = int((duration - tl) / 60)
        if market_minute in (2, 4, 6, 8, 10, 12) and m.open_btc > 0:
            btc_chg = (s.feed.price - m.open_btc) / m.open_btc if s.feed.price else 0
            s.cortex.lifecycle_snapshot(market_minute, m.yes_p, btc_chg)

        # v9.1: Pre-compute lifecycle mult for this market state
        lc_yes = s.cortex.get_lifecycle_mult("YES", market_minute, m.yes_p)
        lc_no = s.cortex.get_lifecycle_mult("NO", market_minute, m.yes_p)

        # ── POSITION AWARENESS ──
        open_here = [p for p in s.risk.positions if p.status == "OPEN" and p.slug == m.slug]
        open_count = len(open_here)
        # v9.1: Count ALL trades on this market (open + resolved) for hard cap
        resolved_count = len([p for p in s.risk.positions if p.status != "OPEN" and p.slug == m.slug])
        total_market_trades = open_count + resolved_count
        if total_market_trades >= 5: return  # v10: raised from 3 to 5 — backtest allows more

        # v7.1: 30-second minimum gap between entries on same market
        # Checks ALL trades (not just same strategy) to prevent pile-in
        if open_here:
            newest_open = max(p.opened.timestamp() for p in open_here if p.opened)
            seconds_since_last = time.time() - newest_open
            if seconds_since_last < 30:
                return  # wait for previous trade to prove itself
            
            # v8.1: Only stack if first position is WINNING
            # If the first trade's token price has dropped, don't add more
            for p in open_here:
                if p.entry > 0:
                    current_price = m.yes_p if "YES" in p.side else m.no_p
                    if current_price < p.entry * 0.95:  # token dropped 5%+ since entry
                        return  # first position is losing — don't pile on

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

        # Total risk on this market (25% balance cap per market)
        market_risk = sum(p.cost for p in open_here)
        max_market_risk = s.risk.show_bal * 0.25  # v9.3: restored from 0.15 to v8.1's 0.25
        if market_risk >= max_market_risk: return

        # v9.4: RULE 3 — Max 2 orders per market. Data shows:
        # 1 buy: +$4,035, 2 buys: +$2,693, 5+buys: -$2,968
        # Averaging into losers destroys profits.
        # Exception: PAIR strategy can be the 3rd entry to complete a guaranteed pair.
        pair_eligible = (len(open_here) >= 1 and len(open_here) <= 2 and
                         len(set(p.side for p in open_here)) == 1)  # only have one side
        if len(open_here) >= 2 and not pair_eligible: return

        # Same-strategy stacking requires: 5+ min left, TRENDING/BREAKOUT regime
        can_same_stack = (tl >= 300 and s.trend and
                          s.trend.regime in ("TRENDING_UP", "TRENDING_DOWN", "BREAKOUT"))

        # ── v9.5: CROSS-SLOT DIRECTION LIMITER ──
        # Crypto assets are 80%+ correlated. Buying NO on BTC, ETH, SOL, XRP
        # simultaneously is really 1 bet copied 4 times. Limit to 2 same-direction
        # entries within 90 seconds across all slots.
        now_ts = time.time()
        s._recent_entries = [(t, side, sk) for t, side, sk in s._recent_entries if now_ts - t < 90]
        def _cross_slot_ok(side):
            same_dir = sum(1 for t, sd, sk in s._recent_entries if sd == side and sk != slot_key)
            return same_dir < 2  # max 2 other slots in same direction within 90s

        # ── SPREAD CHECK ──
        # v10: Only block on CONFIRMED wide spread, not API failures
        spread = s.ex.check_spread(m.tok_yes)
        if spread is not None and spread < 0.90 and spread > 0.08:
            # Real wide spread (not API failure returning 0.99)
            s.strats["ARB"] = f"wide spread ${spread:.2f}"
            s.strats["LATENCY"] = "wide spread"; s.strats["MEANREV"] = "wide spread"
            s.strats["FLASH"] = "wide spread"
            return
        # v10: If spread is None (API error) or 0.99 (empty book), CONTINUE trading
        # The backtest doesn't have this check and made +$8K

        # ── BAD HOUR — v10: advisory only, never blocks ──
        # Backtest made +$8K without hour filtering. Removing hard block.
        # Tod_mult below already handles hour-based sizing adjustments.
        bad_hour = False  # v10: disabled — was preventing ALL trading

        # v7: Time-of-day sizing multiplier
        hour_str = str(datetime.now(timezone.utc).hour)
        hour_stats = s.sizer.hourly_stats.get(hour_str, {})
        hour_total = hour_stats.get("wins", 0) + hour_stats.get("losses", 0)
        if hour_total >= 8:
            hour_wr = hour_stats["wins"] / hour_total
            if hour_wr >= 0.55: tod_mult = 1.2    # hot hour
            elif hour_wr <= 0.30: tod_mult = 0.7   # cold hour
            else: tod_mult = 1.0
        else:
            tod_mult = 1.0

        # v7: Win streak multiplier
        streak_mult = s.win_streak.get_multiplier()

        # v7: Reset conviction for new market
        s.conviction.reset(m.slug)

        # v7.2: Per-strategy minimum time left
        # v9.5: Timeframe-aware — 5m markets get shorter windows
        # Each strategy's own check() also validates, this is a safety net
        if m.timeframe == 5:
            lat_ok = tl >= 90    # 1.5 min for 5m
            flash_ok = tl >= 90  # 1.5 min for 5m
            mom_ok = tl >= 90    # 1.5 min for 5m
        else:
            lat_ok = tl >= 180   # 3 minutes for 15m
            flash_ok = tl >= 180
            mom_ok = tl >= 180

        # v7.2: Hard max size cap — no single trade exceeds 10% of balance
        # This catches multiplier stacking (trend * conviction * streak * tod)
        hard_max = min(s.risk.show_bal * 0.10, 400)  # v9.4: $400 hard cap. Data: $150-400 = +$3,121, $400+ = -$41
        # v9.5: Recovery mode halves the cap too — prevents multiplier chain from overriding recovery
        if s.risk.show_bal < s.c.recovery_target:
            hard_max = min(hard_max, 200)

        # v10: Per-market loss limit — REDUCE, don't block
        # Backtest didn't have this and made +$8K. Blocking kills trading.
        market_penalty = s.market_losses.get_penalty(m.slug)
        if market_penalty <= 0.0:
            market_penalty = 0.3  # v10: 30% size instead of blocking entirely
        
        # v10.1: FULL STATE DEBUG — every 30s, show why strategies aren't firing
        _dbg2_key = f"_dbg2_{m.slug}"
        if time.time() - s._debug_times.get(_dbg2_key, 0) > 30:
            s._debug_times[_dbg2_key] = time.time()
            chg_from_open = ((s.feed.price - m.open_btc) / m.open_btc * 100) if m.open_btc > 0 and s.feed.price > 0 else 0
            lo_p = min(m.yes_p, m.no_p)
            hi_p = max(m.yes_p, m.no_p)
            log.info(f"v10STATE {m.asset}-{m.timeframe}m "
                     f"Y={m.yes_p:.2f} N={m.no_p:.2f} lo=${lo_p:.2f} "
                     f"tl={tl:.0f}s open={chg_from_open:+.3f}% "
                     f"sum={m.yes_p+m.no_p:.3f} pen={market_penalty:.1f} "
                     f"open_pos={len(open_here)} resolved={resolved_count} "
                     f"paused={[k for k in ['ARB','LATENCY','MEANREV','FLASH','SQUEEZE'] if s.sizer.is_paused(k)]}")

        # v8.1: FAST TREND SAFETY — catches trends before the regime engine does
        # If BTC moved 0.15%+ in last 2 min, block buying the opposite side
        # This would have prevented 4 of 7 losses on Feb 16 night session
        btc_2m = s.feed.chg(120) if s.feed else 0
        fast_trend_up = btc_2m > 0.0015    # BTC up 0.15%+ in 2 min
        fast_trend_down = btc_2m < -0.0015  # BTC down 0.15%+ in 2 min

        # ── HELPER: check if a trade is allowed ──
        _counter_trend_mult = 1.0  # v9.5: set by allowed(), used in sizing
        def allowed(strat, side, price):
            """Returns (ok, reason, same_strat_count).
            Different strategy joining = always allowed at full size (same_strat_count=0).
            Same strategy stacking = diminishing size, needs cheaper price + trend."""

            # v9.5: Cross-slot direction limiter — prevent correlated bets
            if strat not in ("ARB", "PAIR") and not _cross_slot_ok(side):
                return False, f"cross-slot limit (2 {side} already)", 0

            # v7.1: Counter-trend caution — data shows lower win rate on these combos
            # v9.5: Changed from HARD BLOCK to SIZE REDUCTION (50%)
            # Hard block killed all trading in trending markets, which is most of the time.
            # The cheap side in trending markets IS where the edge is (mean reversion).
            # PAIR exempt — it deliberately buys the opposite side to lock in profit.
            nonlocal _counter_trend_mult
            _counter_trend_mult = 1.0
            if strat not in ("ARB", "PAIR"):
                if s.trend and s.trend.regime == "TRENDING_UP" and side == "NO":
                    _counter_trend_mult = 0.5  # half size, not blocked
                if s.trend and s.trend.regime == "TRENDING_DOWN" and side == "YES":
                    _counter_trend_mult = 0.5
                
                # v9.4: Removed v9.1 5-minute trend block — v8.1 didn't have this
                # and the regime block + fast trend block already cover it.
                # Having 3 layers of the same block = nothing ever trades.

                # v8.1: FAST TREND — v9.5: reduced from block to 30% size
                # BTC moved 0.15%+ in 2 min = strong momentum, trade cautiously not blocked
                if fast_trend_up and side == "NO":
                    _counter_trend_mult = min(_counter_trend_mult, 0.3)
                if fast_trend_down and side == "YES":
                    _counter_trend_mult = min(_counter_trend_mult, 0.3)

            # Side lock: must match existing direction
            # v9.4: PAIR exempt — its entire purpose is buying the OTHER side
            if locked_side and side != locked_side and strat != "PAIR":
                return False, f"side lock ({locked_side})", 0
            # v10.1: Hard cap 4 total trades per market
            # Was 3 in allowed() but 5 in outer check — contradictory
            if open_count + resolved_count >= 4:
                return False, "market cap (4 trades)", 0
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
                # v10 LEAN: Simple sizing like the backtest. No multiplier chain.
                sz = s.c.get_base_size("ARB", s.risk.show_bal)
                sz = sz * market_penalty * _cl_mult
                sz = min(sz, av, max_market_risk - market_risk, hard_max)
                if sz >= 1.0:
                    s.strats["ARB"] = f"ACTIVE {sig['side']} pair=${sig['pair']:.4f}"
                    s.dash.ev(f"[{slot_label}·ARB] {sig['side']} ${sz:.2f} pair=${sig['pair']:.3f}")
                    shares = sz / sig["price"]
                    oid, actual_shares = s.ex.order(m, sig["yes"], sig["price"], shares, mode="maker")
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "ARB", m.slug, sig["side"], sig["price"], sz, oid=oid)
                        s.risk.open(t, market_end=m.end, actual_shares=actual_shares, entry_regime=s.trend.regime if s.trend else "UNKNOWN")
                        s._recent_entries.append((time.time(), t.side, slot_key))
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
        if sig and lat_ok and time.time() - s.cd.get(f"lat:{slot_key}", 0) > 15 and not s.sizer.is_paused("LATENCY"):
            p = sig["p"]
            ok, reason, same_count = allowed("LATENCY", sig["dir"], p)
            if not ok:
                s.strats["LATENCY"] = reason
            else:
                sz = s.c.get_base_size("LATENCY", s.risk.show_bal)
                sz = sz * market_penalty * _cl_mult
                sz = min(sz, av, max_market_risk - market_risk, hard_max)
                if sz >= 1.0 and 0.08 <= p <= 0.88:
                    s.strats["LATENCY"] = f"ACTIVE {sig['dir']} edge={sig['edge']*100:.1f}%"
                    sh = max(sz / p, 5.0)
                    if sh * p > av: sh = av / p
                    s.dash.ev(f"[{slot_label}·LAT] {sig['dir']} ${sz:.2f} BTC{sig['chg']*100:+.2f}%")
                    oid, actual_shares = s.ex.order(m, sig["yes"], p, sh, mode="maker")
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "LATENCY", m.slug, sig["dir"], p, sz, oid=oid)
                        s.risk.open(t, market_end=m.end, actual_shares=actual_shares, entry_regime=s.trend.regime if s.trend else "UNKNOWN"); s.cd[f"lat:{slot_key}"] = time.time()
                        s._recent_entries.append((time.time(), t.side, slot_key))
                        if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                        s.conviction.record_signal(m.slug, "LATENCY", sig["dir"])
                        s.cd[f"lat_iso:{slot_key}"] = time.time()
                        s._beep()
                else: s.strats["LATENCY"] = f"signal! {sig['dir']} price=${p:.2f}"
        elif True:
            s.strats["LATENCY"] = f"btc {s.feed.chg(60)*100:+.2f}%"

        # ── S3: MEAN REVERSION (buys the bounce at $0.10-$0.45) ──
        latency_open = any(p.strat == "LATENCY" and p.status == "OPEN" for p in open_here)
        lat_isolate = time.time() - s.cd.get(f"lat_iso:{slot_key}", 0) < 8  # v10.1: was 20s, too long for 5m markets
        sig = s.s3.check(m, s.feed, s.trend, s.token_feed, s.book_intel)
        if sig and mom_ok and not latency_open and not lat_isolate and time.time() - s.cd.get(f"mrev:{slot_key}", 0) > 20 and not s.sizer.is_paused("MEANREV"):
            p = sig["price"]
            ok, reason, same_count = allowed("MEANREV", sig["dir"], p)
            if not ok:
                s.strats["MEANREV"] = reason
            else:
                sz = s.c.get_base_size("MOMENTUM", s.risk.show_bal)
                sz = sz * market_penalty * _cl_mult
                sz = min(sz, av, max_market_risk - market_risk, hard_max)
                if sz >= 1.0 and 0.10 <= p <= 0.48:
                    drop_pct = sig.get("drop", 0) * 100
                    s.strats["MEANREV"] = f"BOUNCE {sig['dir']} drop:{drop_pct:.0f}% vel:{sig.get('velocity',0):.3f}"
                    sh = max(sz / p, 5.0)
                    if sh * p > av: sh = av / p
                    s.dash.ev(f"[{slot_label}·MREV] {sig['dir']} ${sz:.2f} bounce drop:{drop_pct:.0f}%")
                    oid, actual_shares = s.ex.order(m, sig["yes"], p, sh)
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "MEANREV", m.slug, sig["dir"], p, sz, oid=oid)
                        s.risk.open(t, market_end=m.end, actual_shares=actual_shares, entry_regime=s.trend.regime if s.trend else "UNKNOWN"); s.cd[f"mrev:{slot_key}"] = time.time()
                        s._recent_entries.append((time.time(), t.side, slot_key))
                        if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                        s.conviction.record_signal(m.slug, "MEANREV", sig["dir"])
                        s._beep()
                else: s.strats["MEANREV"] = f"signal! {sig['dir']} p=${p:.2f}"
        else:
            s.strats["MEANREV"] = f"scanning"

        # ── S4: FLASH (settlement follower, 5m only, $0.38-$0.48) ──
        sig = s.s4.check(m, s.feed, s.trend, s.token_feed, s.book_intel)
        if sig and flash_ok and not lat_isolate and time.time() - s.cd.get(f"flash:{slot_key}", 0) > 30 and not s.sizer.is_paused("FLASH"):
            p = sig["price"]
            ok, reason, same_count = allowed("FLASH", sig["dir"], p)
            if not ok:
                s.strats["FLASH"] = reason
            else:
                sz = s.c.get_base_size("FLASH", s.risk.show_bal)
                sz = sz * market_penalty * _cl_mult
                sz = min(sz, av, max_market_risk - market_risk, hard_max, 250.0)
                if sz >= 1.0:
                    s.strats["FLASH"] = f"ACTIVE {sig['dir']} @ ${p:.4f}"
                    sh = max(sz / p, 5.0)
                    if sh * p > av: sh = av / p
                    s.dash.ev(f"[{slot_label}·FLASH] {sig['dir']} ${sz:.2f} @ ${p:.4f}")
                    oid, actual_shares = s.ex.order(m, sig["yes"], p, sh)
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "FLASH", m.slug, sig["dir"], p, sz, oid=oid)
                        s.risk.open(t, market_end=m.end, actual_shares=actual_shares, entry_regime=s.trend.regime if s.trend else "UNKNOWN"); s.cd[f"flash:{slot_key}"] = time.time()
                        s._recent_entries.append((time.time(), t.side, slot_key))
                        if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                        s.conviction.record_signal(m.slug, "FLASH", sig["dir"])
                        s._beep()
                else:
                    s.strats["FLASH"] = f"signal! {sig['dir']} sz too small"
        else:
            s.strats["FLASH"] = f"lo=${min(m.yes_p, m.no_p):.4f}"

        # ── S5: SQUEEZE → LATE GAME (lottery + snipe) ──
        if s.c.squeeze_enabled:
            sig = s.s5.check(m, s.feed, s.trend)
            if sig and not s.sizer.is_paused("SQUEEZE"):
                p = sig["price"]
                mode = sig.get("mode", "LOTTERY")
                
                if mode == "SNIPE" and m.timeframe == 5:
                    # SNIPE MODE: Buy winning side at $0.82-0.94, maker only
                    # Skip allowed() checks — snipe has its own rules
                    # Don't check counter-trend — we're buying WITH the trend
                    snipe_size = min(s.risk.show_bal * 0.04, 100, av)  # 4% or $100 max
                    if s.risk.show_bal < s.c.recovery_target:
                        snipe_size = min(snipe_size, 50)  # $50 max in recovery
                    if snipe_size >= 1.0 and 0.82 <= p <= 0.94:
                        # Profit per share: $1.00 - price = $0.06-$0.18
                        profit_per = 1.0 - p
                        sh = max(snipe_size / p, 2.0)
                        if sh * p > av: sh = av / p
                        s.strats["SQUEEZE"] = f"SNIPE {sig['dir']} ${p:.2f} {sig['squeeze_count']}s"
                        s.dash.ev(f"[{slot_label}·SNIPE] {sig['dir']} ${snipe_size:.2f} @{p:.2f} +${profit_per:.2f}/sh")
                        # MAKER ONLY — zero fees + rebate
                        oid, actual_shares = s.ex.order(m, sig["yes"], p, sh, mode="maker")
                        if oid:
                            t = Trd(datetime.now(timezone.utc), "SQUEEZE", m.slug, sig["dir"], p, snipe_size, oid=oid)
                            s.risk.open(t, market_end=m.end, actual_shares=actual_shares, entry_regime=s.trend.regime if s.trend else "UNKNOWN"); s.cd[f"sqz:{slot_key}"] = time.time()
                            s._recent_entries.append((time.time(), t.side, slot_key))
                            if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                            s._beep()
                else:
                    # LOTTERY MODE: Original cheap-side logic
                    ok, reason, same_count = allowed("SQUEEZE", sig["dir"], p)
                    if not ok:
                        s.strats["SQUEEZE"] = reason
                    elif s.sizer.is_side_cold(sig["dir"]):
                        s.strats["SQUEEZE"] = f"side cold"
                    else:
                        base = min(s.c.get_base_size("SQUEEZE", s.risk.show_bal), s.risk.show_bal * 0.03)
                        sz = s.sizer.get_size("SQUEEZE", base, s.risk.show_bal, same_strat_count=same_count)
                        sz = sz * market_penalty
                        sz = sz * s.cortex.get_trust("SQUEEZE", slot_key=slot_key) * s.cortex.get_macro_mult(sig.get("dir", sig.get("side", "YES")), asset=m.asset) * s.cortex.get_session_mult() * s.cortex.get_danger_mult() * s.cortex.get_side_mult(sig["dir"]) * (lc_yes if sig["dir"] == "YES" else lc_no)
                        squeeze_cap = s.risk.show_bal * 0.03
                        sz = min(sz, av, max_market_risk - market_risk, hard_max, squeeze_cap)
                        sz = sz * _counter_trend_mult * _cl_mult  # v9.5: counter-trend + chainlink
                        if sz >= 0.50 and p <= 0.22:
                            tl_left = sig["squeeze_count"]
                            s.strats["SQUEEZE"] = f"LOTTERY {sig['dir']} ${p:.2f} {tl_left}s left"
                            sh = max(sz / p, 5.0)
                            if sh * p > av: sh = av / p
                            s.dash.ev(f"[{slot_label}·SQZ] {sig['dir']} ${sz:.2f} @{p:.2f} lottery")
                            oid, actual_shares = s.ex.order(m, sig["yes"], p, sh)
                            if oid:
                                t = Trd(datetime.now(timezone.utc), "SQUEEZE", m.slug, sig["dir"], p, sz, oid=oid)
                                s.risk.open(t, market_end=m.end, actual_shares=actual_shares, entry_regime=s.trend.regime if s.trend else "UNKNOWN"); s.cd[f"sqz:{slot_key}"] = time.time()
                                s._recent_entries.append((time.time(), t.side, slot_key))
                                if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                                s.conviction.record_signal(m.slug, "SQUEEZE", sig["dir"])
                                s._beep()
                        else: s.strats["SQUEEZE"] = f"signal {sig['dir']} p=${p:.2f}"
            else:
                tl_now = (m.end - datetime.now(timezone.utc)).total_seconds() if m.end else 999
                if m.timeframe == 5 and tl_now <= 45:
                    s.strats["SQUEEZE"] = f"snipe zone ({int(tl_now)}s)"
                elif m.timeframe == 5 and tl_now <= 120:
                    s.strats["SQUEEZE"] = f"waiting snipe ({int(tl_now)}s)"
                else:
                    s.strats["SQUEEZE"] = f"waiting snipe"

        # ── S6: PAIR ACCUMULATOR → Buy other side to complete a guaranteed pair ──
        # Gabagool strategy: pair cost < $1.00 = guaranteed profit regardless of outcome.
        sig = s.s6.check(m, s.feed, s.trend, open_positions=open_here)
        if sig and not s.sizer.is_paused("PAIR"):
            p = sig["price"]
            ok, reason, same_count = allowed("PAIR", sig["dir"], p)
            if not ok:
                s.strats["PAIR"] = reason
            else:
                # Size: match the existing position to balance the pair
                target_shares = sig.get("target_shares", 0)
                sz = min(target_shares * p, av, max_market_risk - market_risk, hard_max)
                sz = min(sz, s.risk.show_bal * 0.08)  # 8% cap for pair completion
                if sz >= 1.0:
                    pair_cost = sig.get("pair_cost", 0)
                    profit_pct = sig.get("profit_pct", 0)
                    s.strats["PAIR"] = f"ACTIVE {sig['dir']} @ ${p:.2f} pair=${pair_cost:.2f} +{profit_pct:.0f}%"
                    sh = max(sz / p, 5.0)
                    if sh * p > av: sh = av / p
                    s.dash.ev(f"[{slot_label}·PAIR] {sig['dir']} ${sz:.2f} @ ${p:.2f} pair=${pair_cost:.2f} +{profit_pct:.0f}%")
                    oid, actual_shares = s.ex.order(m, sig["yes"], p, sh, mode="maker")
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "PAIR", m.slug, sig["dir"], p, sz, oid=oid)
                        s.risk.open(t, market_end=m.end, actual_shares=actual_shares, entry_regime=s.trend.regime if s.trend else "UNKNOWN")
                        s._recent_entries.append((time.time(), t.side, slot_key))
                        # Update pair tracker
                        s.s6.update_pair(m.slug, sig["dir"], actual_shares or sh, sz)
                        if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                        s._beep()
                else:
                    s.strats["PAIR"] = f"signal {sig['dir']} sz too small"
        else:
            if open_here and sig is None:
                # Show what we're waiting for
                held_sides = set(p.side for p in open_here)
                if len(held_sides) == 1:
                    held = list(held_sides)[0]
                    other = "NO" if held == "YES" else "YES"
                    other_p = m.no_p if held == "YES" else m.yes_p
                    s.strats["PAIR"] = f"need {other} @${other_p:.2f}"
                else:
                    s.strats["PAIR"] = "both sides held"
            elif not open_here:
                s.strats["PAIR"] = "no position to pair"

        # ── S7: SPIKE DETECTOR → Buy panic-sold tokens ──
        sig = s.s7.check(m, s.feed, s.trend, book_intel=s.book_intel)
        if sig and not s.sizer.is_paused("SPIKE"):
            p = sig["price"]
            ok, reason, same_count = allowed("SPIKE", sig["dir"], p)
            if not ok:
                s.strats["SPIKE"] = reason
            else:
                base = s.c.get_base_size("SPIKE", s.risk.show_bal)
                sz = base * s.cortex.get_trust("SPIKE", slot_key=slot_key) * s.cortex.get_session_mult() * s.cortex.get_danger_mult()
                sz = min(sz, av, max_market_risk - market_risk, hard_max)
                sz = sz * _counter_trend_mult * _cl_mult  # v9.5: counter-trend + chainlink
                if sz >= 1.0:
                    s.strats["SPIKE"] = f"ACTIVE {sig['dir']} @ ${p:.2f} spike!"
                    sh = max(sz / p, 5.0)
                    if sh * p > av: sh = av / p
                    s.dash.ev(f"[{slot_label}·SPIKE] {sig['dir']} ${sz:.2f} @ ${p:.2f} spike detected!")
                    oid, actual_shares = s.ex.order(m, sig["yes"], p, sh, mode="maker")
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "SPIKE", m.slug, sig["dir"], p, sz, oid=oid)
                        s.risk.open(t, market_end=m.end, actual_shares=actual_shares, entry_regime=s.trend.regime if s.trend else "UNKNOWN")
                        s._recent_entries.append((time.time(), t.side, slot_key))
                        if m.cid: s._traded_cids.add(m.cid); s._save_traded_cids()
                        s._beep()
                else:
                    s.strats["SPIKE"] = f"signal {sig['dir']} sz too small"
        else:
            if sig is None:
                s.strats["SPIKE"] = "monitoring book"



    def _summary(s):
        os.system("cls" if os.name == "nt" else "clear")
        w, l, wr = s.risk.stats()
        print(f"\n{H1}{'═'*62}")
        print(f"  {LBL}SESSION SUMMARY — BOT v10.1 — BACKTEST-OPTIMIZED{R}")
        print(f"{H1}{'═'*62}{R}")
        print(f"  {LBL}Balance:{R}  {bal_c(s.risk.show_bal)} USDC")
        print(f"  {LBL}Real P&L:{R} {pnl_c2(s.risk.tpnl)}")
        print(f"  {LBL}Wagered:{R}  {VAL}${s.risk.total_bet:.2f}{R}  ({len(s.risk.trades)} trades)")
        print(f"  {LBL}Record:{R}   {OK}{w}W{R} / {ERR}{l}L{R} / {VAL}{wr:.0f}%{R}")
        # v9: Cortex state at shutdown
        print(f"\n  {H2}⬡ CORTEX FINAL STATE:{R}")
        for st in s.cortex.STRATS:
            trust = s.cortex.get_trust(st)
            n = len(s.cortex._trades.get(st, []))
            if trust <= 0: tag = f"{ERR}☠ DISABLED{R}"
            elif trust >= 1.5: tag = f"{OK}🔥 {trust:.2f}x{R}"
            elif trust >= 1.0: tag = f"{VAL}{trust:.2f}x{R}"
            else: tag = f"{WARN}{trust:.2f}x{R}"
            print(f"    {st:10} {tag} ({n} trades)")
        print(f"    Macro bias: {s.cortex._macro_bias} ({s.cortex._macro_strength:.0%})")
        print(f"    Session: {s.cortex._session_trades}t, {pnl_c2(s.cortex._session_pnl)}")
        print(f"  {LBL}Sizing:{R}   {s.sizer.display_str()}")
        csv_stats = s.data.get_stats()
        if csv_stats.get("total", 0) > 0:
            print(f"  {LBL}CSV Data:{R} {VAL}{csv_stats['total']}{R} trades logged  |  All-time WR: {VAL}{csv_stats['wr']:.0f}%{R}  P&L: {pnl_c2(csv_stats['pnl'])}")
        print(f"  {LBL}Files:{R}    {DIM}trade_log.csv  market_log.csv  trade_data.json{R}")
        print(f"{H1}{'─'*62}{R}")
        for t in s.risk.trades[-10:]:
            pn = pnl_c2(t.pnl) if t.pnl else f"{DIM}pending{R}"
            icon = f"{OK}✓{R}" if t.pnl and t.pnl > 0 else f"{ERR}✗{R}" if t.pnl and t.pnl < 0 else f"{DIM}○{R}"
            print(f"  {icon} {t.ts.strftime('%H:%M:%S')} [{t.strat[:5]:5}] {t.side:6} ${t.size:.2f} @ ${t.price:.4f}  {pn}")
        print(f"{H1}{'═'*62}{R}\n")

if __name__ == "__main__":
    # v9.5: Ensure crashes are always logged to file
    import logging.handlers, signal
    crash_handler = logging.FileHandler("bot_crashes.log")
    crash_handler.setLevel(logging.WARNING)  # Catch warnings too
    crash_handler.setFormatter(logging.Formatter("%(asctime)s|%(levelname)s|%(message)s"))
    log.addHandler(crash_handler)
    
    # v9.5: Catch external signals (SSH disconnect, kill, etc.)
    def _signal_handler(signum, frame):
        sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
        msg = f"Bot received signal {sig_name} ({signum}) — shutting down"
        log.warning(msg)
        try:
            with open("bot_crashes.log", "a") as cf:
                cf.write(f"\n{'='*60}\n{datetime.now()}\n{msg}\n")
        except: pass
        raise SystemExit(0)
    
    for sig in [signal.SIGTERM, signal.SIGHUP]:
        try: signal.signal(sig, _signal_handler)
        except: pass  # Some signals not available on all platforms
    
    # v9.4: Auto-restart loop for long-running stability
    restart_count = 0
    max_restarts = 50  # safety limit per session
    while restart_count < max_restarts:
        try:
            Bot().run()
            break  # clean exit (Ctrl+C)
        except (KeyboardInterrupt, SystemExit):
            print("\n  Shutting down cleanly...")
            break
        except Exception as e:
            restart_count += 1
            crash_msg = f"Bot crashed (restart #{restart_count}): {e}\n{traceback.format_exc()}"
            log.error(crash_msg)
            # Also write to dedicated crash file in case main log fails
            try:
                with open("bot_crashes.log", "a") as cf:
                    cf.write(f"\n{'='*60}\n{datetime.now()}\n{crash_msg}\n")
            except: pass
            print(f"\n  ⚠ Bot crashed: {str(e)[:60]}")
            print(f"  ⟳ Auto-restarting in 10s... (restart #{restart_count}/{max_restarts})")
            time.sleep(10)
