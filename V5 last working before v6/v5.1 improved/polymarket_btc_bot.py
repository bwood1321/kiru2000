"""
POLYMARKET BTC 15-MIN BOT v5.1 — IMPROVED STRATEGIES + SMART SIZING
Complete rebuild for email/proxy wallet (signature_type=1).

KEY CHANGES FROM v4:
1. Proxy wallet (signature_type=1) — how Polymarket expects email accounts
2. Auto-redeem — claims winning tokens back to USDC.e every 5 minutes
3. Honest P&L — tracks real on-chain balance changes, not simulated outcomes
4. Smarter strategies — higher confidence thresholds, fewer bad bets
5. Rate-limit friendly — delays between RPC calls

pip install py-clob-client python-dotenv requests numpy colorama web3
"""
import os, sys, time, json, logging, traceback
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
def bal_c(v): return f"{OK}${v:.2f}{R}"

@dataclass
class Config:
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 1  # v5: proxy wallet
    dry_run: bool = False
    starting_balance: float = 24.0
    # Dynamic sizing — % of balance per trade
    arb_pct: float = 0.05       # 5% of balance
    latency_pct: float = 0.06   # 6% of balance
    momentum_pct: float = 0.05  # 5% of balance
    flash_pct: float = 0.05     # 5% of balance
    # Strategy config
    arb_enabled: bool = True
    arb_max_pair_cost: float = 0.99   # v5.1: realistic — markets often sum to 0.98-0.99
    latency_enabled: bool = True
    latency_threshold: float = 0.003  # v5.1: 0.3% BTC move triggers check
    latency_min_edge: float = 0.08    # v5.1: 8% edge minimum (was 15%)
    momentum_enabled: bool = True
    momentum_conf: float = 0.65       # v5.1: 65% confidence (was 78%)
    flash_enabled: bool = True
    flash_threshold: float = 0.27     # v5.1: tightened from 30¢ — CSV data shows losses at $0.28-$0.30
    max_daily_loss: float = 10.0
    max_positions: int = 7
    def get_size(s, strat, balance):
        """Dynamic sizing: returns dollar amount based on % of current balance"""
        pcts = {"ARB": s.arb_pct, "LATENCY": s.latency_pct, "MOMENTUM": s.momentum_pct, "FLASH": s.flash_pct}
        pct = pcts.get(strat, 0.05)
        sz = round(balance * pct, 2)
        return max(sz, 1.0)  # minimum $1
    poll_sec: int = 2
    assets: list = field(default_factory=lambda: ["btc"])

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
            flash_pct=float(os.getenv("FLASH_PCT", "0.05")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "10.0")),
        )

log = logging.getLogger("Bot"); log.setLevel(logging.DEBUG)
_fh = logging.FileHandler("polybot.log")
_fh.setFormatter(logging.Formatter("%(asctime)s|%(levelname)s|%(message)s"))
log.addHandler(_fh)

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
    market_end: datetime = None  # when the 15-min market expires
    _recorded: bool = False

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
        s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/5"
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

# ─── STRATEGIES ───
class S_Arb:
    """Gabagool-style: buy whichever side is cheaper when YES+NO < threshold."""
    def __init__(s, c): s.c = c; s.market_slug = None
    def reset(s, slug):
        if s.market_slug != slug: s.market_slug = slug
    def check(s, m):
        if not s.c.arb_enabled: return None
        s.reset(m.slug)
        yp, np_ = m.yes_p, m.no_p
        pair = yp + np_
        if pair >= s.c.arb_max_pair_cost: return None
        buy_yes = yp < np_
        price = yp if buy_yes else np_
        if price < 0.10 or price > 0.55: return None
        shares = 1  # placeholder, will be set dynamically in _trade
        side = "YES" if buy_yes else "NO"
        return {"s": "ARB", "side": side, "yes": buy_yes, "price": price,
                "pair": pair, "profit": 1.0 - pair, "sz": 0, "shares": shares}

class S_Latency:
    """Buy when BTC moved on Binance but Polymarket hasn't caught up."""
    def __init__(s, c): s.c = c
    def check(s, m, f):
        if not s.c.latency_enabled or f.n < 10: return None
        # Check both: vs market open price AND recent 60s movement
        chg = 0
        if m.open_btc > 0:
            chg = (f.price - m.open_btc) / m.open_btc
        recent = f.chg(60)
        # Use whichever is larger (more signal)
        if abs(recent) > abs(chg): chg = recent
        if abs(chg) < s.c.latency_threshold: return None
        up = chg > 0
        pred = min(0.90, 0.50 + abs(chg) * 40)
        mp = m.yes_p if up else m.no_p
        edge = pred - mp
        if edge < s.c.latency_min_edge: return None
        # Only block if market FULLY priced it in
        favored = m.yes_p if up else m.no_p
        if favored > 0.70: return None
        other = m.no_p if up else m.yes_p
        if other < 0.20: return None
        return {"s": "LATENCY", "dir": "YES" if up else "NO", "yes": up,
                "edge": edge, "pred": pred, "p": mp, "chg": chg, "sz": 0}
    def check_with_trend(s, m, f, trend):
        sig = s.check(m, f)
        if sig and trend:
            # Boost confidence when trend agrees
            up = sig["yes"]
            if (up and trend.trend_dir > 0) or (not up and trend.trend_dir < 0):
                sig["edge"] = min(0.40, sig["edge"] + 0.03)
            # Reduce confidence when trend disagrees
            elif (up and trend.trend_dir < 0) or (not up and trend.trend_dir > 0):
                sig["edge"] -= 0.03
                if sig["edge"] < s.c.latency_min_edge: return None
        return sig

class S_Momentum:
    """5-indicator momentum with VWAP confirmation."""
    def __init__(s, c): s.c = c; s.scores = {}
    def check(s, m, f):
        if not s.c.momentum_enabled: return None
        p = f.arr(60)
        if len(p) < 30: return None
        sig = {}
        bb = p[-20:]; mu, sd = np.mean(bb), np.std(bb)
        if sd == 0: return None
        cur = p[-1]; z = (cur - mu) / sd
        sig["bb"] = float(np.clip(z * 0.4, -1, 1))
        if len(p) >= 21:
            ef, es = s._ema(p, 8), s._ema(p, 21)
            sig["ema"] = float(np.clip((ef - es) / es * 150, -1, 1))
        else: sig["ema"] = 0
        rsi = s._rsi(p, 14)
        if rsi > 70: sig["rsi"] = -0.8
        elif rsi > 60: sig["rsi"] = -0.3
        elif rsi < 30: sig["rsi"] = 0.8
        elif rsi < 40: sig["rsi"] = 0.3
        else: sig["rsi"] = 0
        sig["roc"] = float(np.clip(f.chg(120) * 80, -1, 1))
        vwap = f.vwap_trend(120)
        sig["vwap"] = float(np.clip(vwap * 200, -1, 1))
        s.scores = sig
        w = {"bb": .30, "ema": .20, "rsi": .20, "roc": .15, "vwap": .15}
        comp = sum(sig[k] * w[k] for k in sig)
        conf = min(1.0, 0.5 + abs(comp) * 0.5)
        vwap_agrees = (comp > 0 and vwap > 0) or (comp < 0 and vwap < 0)
        if not vwap_agrees and abs(vwap) > 0.005: return None  # v5.1: looser vwap check
        if conf < s.c.momentum_conf or abs(comp) < 0.12: return None  # v5.1: 0.12 (was 0.18)
        up = comp > 0
        return {"s": "MOMENTUM", "dir": "YES" if up else "NO", "yes": up,
                "conf": conf, "comp": comp, "sig": sig, "rsi": rsi, "sz": 0}
    def _ema(s, p, n):
        k = 2 / (n + 1); e = p[0]
        for x in p[1:]: e = x * k + e * (1 - k)
        return e
    def _rsi(s, p, n):
        if len(p) < n + 1: return 50
        d = np.diff(p[-n-1:])
        g = np.mean(np.maximum(d, 0)); l = np.mean(np.maximum(-d, 0))
        return 100 if l == 0 else 100 - 100 / (1 + g / l)

class S_Flash:
    """v5.1 IMPROVED: Buy cheap sides with smarter entry timing.
    CSV data analysis: best wins at $0.10-$0.25, losses at $0.28+.
    Improvements:
    - Prefer entries mid-market (5-10 min) where direction is clearer
    - Stronger momentum confirmation for entries > $0.22
    - Very cheap entries ($0.05-$0.15) need minimal confirmation
    - Size boost for very cheap entries (better R:R)"""
    def __init__(s, c): s.c = c
    def check(s, m, f, trend=None):
        if not s.c.flash_enabled or f.n < 10: return None
        yes_cheap = 0.05 <= m.yes_p <= s.c.flash_threshold
        no_cheap = 0.05 <= m.no_p <= s.c.flash_threshold
        if not yes_cheap and not no_cheap: return None
        btc_2m = f.chg(120)
        btc_30s = f.chg(30)
        btc_10s = f.chg(10)
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 120: return None  # need 2+ min
        # Don't trade in extreme moves — market already decided
        if abs(btc_2m) > 0.012: return None
        # Determine cheap tier for entry strictness
        # Tier 1: $0.05-$0.15 (amazing R:R, loose entry)
        # Tier 2: $0.15-$0.22 (good R:R, normal entry)
        # Tier 3: $0.22-$0.27 (ok R:R, strict entry)
        
        if yes_cheap:
            price = m.yes_p
            tier = 1 if price <= 0.15 else (2 if price <= 0.22 else 3)
            # Tier 1: any sign of life
            if tier == 1 and (btc_10s > -0.0005 or btc_30s > -0.001):
                return {"s": "FLASH", "dir": "YES", "yes": True, "price": price, "sz": 0, "tier": tier}
            # Tier 2: need mild recovery
            if tier == 2 and (btc_30s > 0 or btc_10s > 0.0001):
                if trend is None or trend.trend_dir >= 0:
                    return {"s": "FLASH", "dir": "YES", "yes": True, "price": price, "sz": 0, "tier": tier}
            # Tier 3: need clear recovery + trend support
            if tier == 3 and btc_30s > 0.0001 and btc_10s > 0:
                if trend is not None and trend.trend_dir >= 0:
                    # Extra: prefer mid-market timing (5-10 min in)
                    if tl <= 600:  # within first 10 min
                        return {"s": "FLASH", "dir": "YES", "yes": True, "price": price, "sz": 0, "tier": tier}
        
        if no_cheap:
            price = m.no_p
            tier = 1 if price <= 0.15 else (2 if price <= 0.22 else 3)
            if tier == 1 and (btc_10s < 0.0005 or btc_30s < 0.001):
                return {"s": "FLASH", "dir": "NO", "yes": False, "price": price, "sz": 0, "tier": tier}
            if tier == 2 and (btc_30s < 0 or btc_10s < -0.0001):
                if trend is None or trend.trend_dir <= 0:
                    return {"s": "FLASH", "dir": "NO", "yes": False, "price": price, "sz": 0, "tier": tier}
            if tier == 3 and btc_30s < -0.0001 and btc_10s < 0:
                if trend is not None and trend.trend_dir <= 0:
                    if tl <= 600:
                        return {"s": "FLASH", "dir": "NO", "yes": False, "price": price, "sz": 0, "tier": tier}
        return None


# ─── TREND ENGINE (from v6) ───
class TrendEngine:
    """Lightweight trend detection — helps strategies pick the right side."""
    def __init__(s):
        s.trend_dir = 0  # -1=down, 0=flat, 1=up
        s.trend_str = "FLAT"
        s.btc_changes = deque(maxlen=60)
        s.market_results = deque(maxlen=20)
        s.hourly_stats = {}
        s._last_update = 0
    def update(s, feed):
        if time.time() - s._last_update < 5: return
        s._last_update = time.time()
        if feed.n < 10: return
        chg_30 = feed.chg(30)
        chg_120 = feed.chg(120)
        s.btc_changes.append(chg_30)
        # Simple trend: weight recent more
        if abs(chg_120) < 0.0003:
            s.trend_dir = 0; s.trend_str = "FLAT"
        elif chg_120 > 0.0008:
            s.trend_dir = 1; s.trend_str = "UP"
        elif chg_120 < -0.0008:
            s.trend_dir = -1; s.trend_str = "DOWN"
        elif chg_30 > 0.0003:
            s.trend_dir = 1; s.trend_str = "DRIFT_UP"
        elif chg_30 < -0.0003:
            s.trend_dir = -1; s.trend_str = "DRIFT_DN"
        else:
            s.trend_dir = 0; s.trend_str = "FLAT"
    def record_result(s, won, hour):
        s.market_results.append(won)
        h = str(hour)
        if h not in s.hourly_stats: s.hourly_stats[h] = {"w": 0, "l": 0}
        if won: s.hourly_stats[h]["w"] += 1
        else: s.hourly_stats[h]["l"] += 1
    def is_bad_hour(s):
        h = str(datetime.now(timezone.utc).hour)
        st = s.hourly_stats.get(h)
        if not st: return False
        total = st["w"] + st["l"]
        if total < 3: return False
        wr = st["w"] / total
        return wr < 0.25  # hour is bad if < 25% win rate with 3+ trades
    def recent_streak(s):
        """Returns negative number for losing streak, positive for winning."""
        if not s.market_results: return 0
        streak = 0
        last = list(s.market_results)[-1]
        for r in reversed(list(s.market_results)):
            if r == last: streak += 1
            else: break
        return streak if last else -streak
    def should_pause(s):
        """Pause if 4+ losses in a row."""
        return s.recent_streak() <= -4

class SideLock:
    """Prevent conflicting YES/NO positions in the same market."""
    def __init__(s):
        s.locked_side = {}  # slug -> "YES" or "NO"
    def lock(s, slug, side):
        s.locked_side[slug] = side
    def is_locked(s, slug, side):
        if slug not in s.locked_side: return False
        return s.locked_side[slug] != side  # blocked if trying opposite
    def reset(s, slug):
        s.locked_side.pop(slug, None)

# ─── MARKET FINDER ───
class Finder:
    def __init__(s, c):
        s.c = c; s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/5"; s.cache = {}
    def test(s):
        try:
            r = s.s.get(f"{s.c.gamma_host}/markets", params={"slug": "test", "limit": 1}, timeout=8)
            return r.status_code in [200, 404]
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

# ─── EXECUTOR ───
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
        # Match reference: signature_type=1 with funder for proxy wallets
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
        """Get balance — CLOB API first (proxy wallets hold funds internally), then on-chain."""
        # v5: For proxy wallets, CLOB API is the source of truth
        if s.authed:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                r = s.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                if isinstance(r, dict) and "balance" in r:
                    b = int(r["balance"]) / 1e6
                    if b > 0: return b
            except: pass
        # Fallback: check proxy on-chain
        if s.c.funder_address:
            bal = s._check_usdc(s.c.funder_address)
            if bal is not None and bal > 0: return bal
        # Fallback: check signer on-chain
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
        """Place order using MarketOrderArgs (market order) with FOK fallback to limit."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        label = "YES" if is_yes else "NO"
        if price < 0.10 or price > 0.90: return None, None
        # Calculate dollar amount to spend
        dollar_amount = round(price * size, 2)
        if dollar_amount < 0.50: dollar_amount = 0.50
        if s.c.dry_run:
            oid = f"DRY-{int(time.time()*1000)%99999}"
            log.info(f"DRY: ${dollar_amount:.2f} {label}")
            return oid, None
        if not s.authed: return None, None
        tid = market.tok_yes if is_yes else market.tok_no
        # Method 1: Market order (FOK)
        try:
            market_order = MarketOrderArgs(
                token_id=tid,
                amount=dollar_amount,
                side=BUY,
            )
            signed = s.client.create_market_order(market_order)
            resp = s.client.post_order(signed, OrderType.FOK)
            if isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("id") or resp.get("order_id") or "?"
                status = resp.get("status", "")
                # Get actual shares from response
                # takingAmount = shares received, makingAmount = dollars spent (in USDC units)
                actual_shares = None
                taking = resp.get("takingAmount")
                if taking:
                    try:
                        val = float(taking)
                        # If > 1000, it's in micro-units (divide by 1e6)
                        # If < 1000, it's already in normal units
                        actual_shares = val / 1e6 if val > 1000 else val
                    except: pass
                log.info(f"MARKET ORDER: ${dollar_amount:.2f} {label} id={oid} st={status} shares={actual_shares}")
                if oid != "?": return oid, actual_shares
            elif isinstance(resp, str) and len(resp) > 5:
                log.info(f"MARKET ORDER: ${dollar_amount:.2f} {label} resp={resp[:60]}")
                return resp, None
        except Exception as e:
            log.error(f"Market order fail: {e}")
        # Method 2: Limit order fallback (GTC)
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
        """Get live YES/NO prices from CLOB midpoint, fallback to order book."""
        if not s.client or not s.authed: return m.yes_p, m.no_p
        try:
            ymid = s.client.get_midpoint(m.tok_yes)
            nmid = s.client.get_midpoint(m.tok_no)
            yp = float(ymid["mid"]) if isinstance(ymid, dict) else float(ymid)
            np_ = float(nmid["mid"]) if isinstance(nmid, dict) else float(nmid)
            if 0 < yp < 1 and 0 < np_ < 1: return yp, np_
        except: pass
        try:
            ybook = s.client.get_order_book(m.tok_yes)
            nbook = s.client.get_order_book(m.tok_no)
            yp = s._book_mid(ybook); np_ = s._book_mid(nbook)
            if yp and np_: return yp, np_
        except: pass
        return m.yes_p, m.no_p
    def _book_mid(s, book):
        if not isinstance(book, dict): return None
        bids = book.get("bids", []); asks = book.get("asks", [])
        if bids and asks:
            bb = float(bids[0].get("price", 0)); ba = float(asks[0].get("price", 0))
            if bb > 0 and ba > 0: return (bb + ba) / 2
        elif bids: return float(bids[0].get("price", 0))
        elif asks: return float(asks[0].get("price", 0))
        return None
    def get_positions(s):
        """Fetch positions from both proxy and signer addresses."""
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
        """v5: Redeem resolved conditional tokens back to USDC.e."""
        if s.c.dry_run or not s.c.private_key: return []
        try:
            from web3 import Web3
            from eth_account import Account
            pk = s.c.private_key
            if not pk.startswith("0x"): pk = "0x" + pk
            # v5: Must send redeem TX from SIGNER, not proxy (proxy is a contract)
            signer = Account.from_key(pk).address
            w3 = None
            for rpc in ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc))
                    if w3.is_connected(): break
                except: continue
            if not w3 or not w3.is_connected(): return []
            ctf_addr = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
            usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            abi = json.loads('[{"constant":false,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"name":"conditionId","type":"bytes32"}],"name":"payoutDenominator","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
            ctf = w3.eth.contract(address=Web3.to_checksum_address(ctf_addr), abi=abi)
            redeemed = []
            for cid in condition_ids:
                try:
                    cid_bytes = bytes.fromhex(cid.replace("0x", ""))
                    time.sleep(1)  # Rate limit
                    payout = ctf.functions.payoutDenominator(cid_bytes).call()
                    if payout == 0: continue
                    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(signer))
                    txn = ctf.functions.redeemPositions(
                        Web3.to_checksum_address(usdc_addr),
                        b'\x00' * 32, cid_bytes, [1, 2]
                    ).build_transaction({
                        'from': Web3.to_checksum_address(signer),
                        'nonce': nonce, 'gas': 200000,
                        'gasPrice': w3.eth.gas_price, 'chainId': 137,
                    })
                    signed = w3.eth.account.sign_transaction(txn, pk)
                    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                    if receipt.status == 1:
                        redeemed.append(cid)
                        log.info(f"REDEEMED {cid[:16]}... tx={tx_hash.hex()[:16]}")
                    time.sleep(2)
                except Exception as e:
                    log.debug(f"Redeem fail: {e}")
            return redeemed
        except Exception as e:
            log.debug(f"Redeem setup fail: {e}")
            return []

# ─── RISK MANAGER ───
class Risk:
    def __init__(s, c):
        s.c = c; s.bal = c.starting_balance; s.real_bal = None
        s.start_bal = None  # v5: track starting balance for honest P&L
        s.dpnl = 0.0; s.tpnl = 0.0; s.total_bet = 0.0
        s.trades = []; s.positions = []
    def set_real(s, b):
        if b is not None and b > 0:
            # v5: Honest P&L — track actual balance change
            if s.start_bal is None: s.start_bal = b
            s.real_bal = b; s.bal = b
            s.tpnl = b - s.start_bal  # Real P&L from chain
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
            # ACCURATE P&L: payout = shares × $1.00 × 0.98 (2% fee)
            # profit = payout - cost (what we actually spent)
            gross_payout = pos.shares * 1.0
            fee = gross_payout * 0.02
            net_payout = gross_payout - fee
            pnl = net_payout - pos.cost  # profit = what we got back - what we spent
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
            # Use market_end if available, otherwise fall back to age
            if p.market_end:
                past_end = (now - p.market_end).total_seconds()
                if past_end < 60: continue  # wait 60s after market end
            else:
                age = (now - p.opened).total_seconds()
                if age < 960: continue
            # Try to get actual resolution from Gamma API
            resolved = s._check_resolution(p)
            if resolved is not None:
                s.resolve(p, resolved)
                resolved_list.append(p)
            elif p.market_end and (now - p.market_end).total_seconds() > 300:
                # 5 min past market end and still no resolution — use BTC direction
                op = cp = None
                for x in f.data:
                    if x["t"] >= p.opened.timestamp() and op is None: op = x["p"]
                    cp = x["p"]
                if op and cp:
                    up = cp > op
                    s.resolve(p, (up and "YES" in p.side) or (not up and "NO" in p.side))
                    resolved_list.append(p)
                else:
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
                        s.resolve(p, (up and "YES" in p.side) or (not up and "NO" in p.side))
                    else: s.resolve(p, False)
                    resolved_list.append(p)
        return resolved_list

    def _check_resolution(s, p):
        """Check if market resolved and which side won via Gamma API."""
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
                            # If YES resolved to ~1.0, YES won. If ~0.0, NO won.
                            if yes_final > 0.9:
                                return "YES" in p.side
                            elif yes_final < 0.1:
                                return "NO" in p.side
        except: pass
        return None
    def stats(s):
        w = sum(1 for t in s.trades if t.pnl > 0)
        l = sum(1 for t in s.trades if t.pnl < 0)
        return w, l, (w / (w + l) * 100 if w + l else 0)

# ─── DASHBOARD ───
class Dash:
    def __init__(s): s.evts = deque(maxlen=8)
    def ev(s, e): s.evts.append(f"{datetime.now().strftime('%H:%M:%S')} {e}")
    def render(s, c, conn, f, risk, mkt, strats, scores, orders, poly_pos, start_time=None, past_trades=None, trend=None, sidelock=None):
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.now().strftime("%H:%M:%S")
        # Runtime display
        rt = ""
        if start_time:
            elapsed = int(time.time() - start_time)
            hrs, rem = divmod(elapsed, 3600)
            mins, secs = divmod(rem, 60)
            if hrs > 0: rt = f"  {VAL}⏱ {hrs}h{mins:02d}m{R}"
            else: rt = f"  {VAL}⏱ {mins}m{secs:02d}s{R}"
        mode = f"{ERR}LIVE{R}" if not c.dry_run else f"{WARN}DRY RUN{R}"
        print(f"  {H1}╔{'═'*60}╗{R}")
        print(f"  {H1}║  POLYMARKET BTC BOT v5.1       {DIM}{now}{R}{rt}  {H1}║{R}")
        print(f"  {H1}╚{'═'*60}╝{R}")
        gc = lambda ok: f"{OK}●{R}" if ok else f"{ERR}●{R}"
        connstr = f"Gamma{gc(conn.gamma != 'FAILED')} CLOB{gc(conn.clob != 'FAILED')} Auth{gc(conn.can_trade)} Binance{gc(conn.binance != 'FAILED')}"
        print(f"  {connstr}    Mode: {mode}")
        print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        print(f"  {H1}┌{'─'*60}┐{R}")
        w, l, wr = risk.stats()
        print(f"  {H1}│{R}  Balance  {VAL}${risk.show_bal:.2f}{R}     Available  {VAL}${risk.available:.2f}{R}      P&L  {pnl_c2(risk.tpnl)}  {H1}│{R}")
        print(f"  {H1}│{R}  Record   {OK}{w}W{R}/{ERR}{l}L{R} ({VAL}{wr:.0f}%{R})    At Risk  {VAL}${risk.open_risk:.2f}{R}                    {H1}│{R}")
        print(f"  {H1}└{'─'*60}┘{R}")
        if mkt:
            tl = (mkt.end - datetime.now(timezone.utc)).total_seconds()
            print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
            print(f"    {LBL}MARKET{R}")
            chg1m = f.chg(60) * 100; chg_c = OK if chg1m >= 0 else ERR
            print(f"    BTC: {BTC}${f.price:,.2f}{R}  1m:{chg_c}{chg1m:+.3f}%{R}  Vol:{VAL}{f.volatility()*100:.2f}%{R}")
            sm = mkt.yes_p + mkt.no_p; sm_c = OK if sm < 0.99 else VAL
            tl_c = ERR if tl < 120 else WARN if tl < 300 else VAL
            print(f"    YES:{OK}${mkt.yes_p:.4f}{R}  NO:{ERR}${mkt.no_p:.4f}{R}  SUM:{sm_c}${sm:.4f}{R}  Exp:{tl_c}{int(tl//60)}:{int(tl%60):02d}{R}")
            # Show trend + side lock
            if trend:
                td = trend
                tc = OK if td.trend_dir > 0 else ERR if td.trend_dir < 0 else DIM
                streak = td.recent_streak()
                streak_str = f"{OK}+{streak}{R}" if streak > 0 else f"{ERR}{streak}{R}" if streak < 0 else "0"
                bad = f"  {ERR}⚠BAD HOUR{R}" if td.is_bad_hour() else ""
                pause = f"  {ERR}⏸PAUSED{R}" if td.should_pause() else ""
                lock = ""
                if sidelock and mkt and mkt.slug in sidelock.locked_side:
                    lock = f"  {WARN}🔒{sidelock.locked_side[mkt.slug]}{R}"
                print(f"    AI  {tc}▸ {td.trend_str}{R}  Streak:{streak_str}{bad}{pause}{lock}")
        print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        print(f"    {LBL}STRATEGIES{R}")
        icons = {"ARB": "\u25c6", "LATENCY": "\u26a1", "MOMENTUM": "\u2197", "FLASH": "\u26a1"}
        for k, v in strats.items():
            ic = icons.get(k, "\u25cb")
            sz = c.get_size(k, risk.show_bal)
            if "ACTIVE" in str(v): print(f"    {OK}\u25cf {ic} {k:12}{R} {OK}{v}{R}  {DIM}${sz:.2f}{R}")
            elif "PAUSED" in str(v) or "bad" in str(v): print(f"    {ERR}\u25cb {ic} {k:12}{R} {ERR}{v}{R}  {DIM}${sz:.2f}{R}")
            else: print(f"    {DIM}\u25cb {ic} {k:12}{R} {v}  {DIM}${sz:.2f}{R}")
        if scores:
            parts = []
            for k, v in scores.items():
                c2 = OK if v > 0.1 else ERR if v < -0.1 else DIM
                parts.append(f"{k}:{c2}{v:+.2f}{R}")
            print(f"    Signals: {'  '.join(parts)}")
        open_pos = [p for p in risk.positions if p.status == "OPEN"]
        closed = [p for p in risk.positions if p.status != "OPEN"][-8:]
        print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        print(f"    {LBL}POSITIONS ({len(open_pos)} open, ${risk.open_risk:.2f} at risk){R}")
        if open_pos:
            for p in open_pos[-5:]:
                # Show time remaining until market ends, not age since opened
                if p.market_end:
                    remaining = (p.market_end - datetime.now(timezone.utc)).total_seconds()
                    remaining = max(0, remaining)
                    total = 900  # 15 min market
                    bar_pct = min(1.0 - remaining / total, 1.0)
                    bar_pct = max(0, bar_pct)
                    mins_left = int(remaining // 60)
                    secs_left = int(remaining % 60)
                    time_str = f"{mins_left}:{secs_left:02d} left"
                else:
                    age = (datetime.now(timezone.utc) - p.opened).total_seconds() if p.opened else 0
                    bar_pct = min(age / 900, 1.0)
                    mins_left = int(age // 60)
                    secs_left = int(age % 60)
                    time_str = f"{mins_left}:{secs_left:02d}"
                bar_len = int(bar_pct * 10)
                bar = f"{OK}{'\u2588' * bar_len}{DIM}{'\u2591' * (10 - bar_len)}{R}"
                print(f"    {POS}OPEN{R}  [{p.strat[:5]:5}] {p.side:6}  ${p.cost:.2f} @ ${p.entry:.4f}  {bar} {time_str}")
        else:
            print(f"    {DIM}No open positions{R}")
        print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        print(f"    {LBL}EVENTS{R}")
        for e in list(s.evts)[-6:]: print(f"    {EVT}{e}{R}")
        # Combine past trades (from file) + current session closed positions
        all_ended = []
        if past_trades:
            for t in past_trades:
                all_ended.append(t)
        for p in closed:
            all_ended.append({
                "ts": p.opened.strftime('%H:%M %m/%d') if p.opened else "?",
                "status": "WIN" if p.pnl > 0 else "LOSS",
                "strat": p.strat,
                "side": p.side,
                "cost": p.cost,
                "entry": p.entry,
                "pnl": p.pnl,
                "slug": p.slug,
            })
        if all_ended:
            recent = all_ended[-10:]
            recent.reverse()  # newest on top
            w_count = sum(1 for t in all_ended if t["pnl"] > 0)
            l_count = sum(1 for t in all_ended if t["pnl"] <= 0)
            total_pnl = sum(t["pnl"] for t in all_ended)
            print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
            print(f"    {LBL}TRADE HISTORY  {OK}{w_count}W{R} {ERR}{l_count}L{R}  Total:{pnl_c2(total_pnl)}{R}")
            for t in recent:
                icon = f"{OK}\u2713{R}" if t["pnl"] > 0 else f"{ERR}\u2717{R}"
                col = OK if t["pnl"] > 0 else ERR
                ts = t.get("ts", "?")
                try:
                    if len(str(ts)) >= 16:
                        parts = str(ts).split(" ")
                        date_p = parts[0].split("-")
                        time_p = parts[1][:5]
                        ts = f"{time_p} {date_p[1]}/{date_p[2]}"
                except: pass
                print(f"    {icon} {DIM}{ts:11}{R} {col}{t['side']:3}{R} [{t['strat'][:5]:5}] ${t['cost']:.2f}@${t['entry']:.2f} {pnl_c2(t['pnl'])}")
        print(f"  {H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550{R}")
        print(f"  {DIM}Ctrl+C to stop{R}")
    def _is_recent(s, p):
        for key in ["createdAt", "timestamp", "created_at"]:
            ts = p.get(key)
            if ts:
                try:
                    if isinstance(ts, (int, float)):
                        dt = datetime.fromtimestamp(ts if ts < 1e11 else ts/1e3, tz=timezone.utc)
                    else: dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    return (datetime.now(timezone.utc) - dt).total_seconds() < 86400
                except: pass
        return True

# ─── MAIN BOT ───
class Bot:
    HISTORY_FILE = "trade_history.txt"

    def __init__(s):
        s.c = Config.from_env(); s.conn = Conn(); s.feed = Feed()
        s.finder = Finder(s.c); s.ex = Executor(s.c); s.risk = Risk(s.c)
        s.dash = Dash()
        s.s1 = S_Arb(s.c); s.s2 = S_Latency(s.c); s.s3 = S_Momentum(s.c); s.s4 = S_Flash(s.c)
        s.trend = TrendEngine(); s.sidelock = SideLock()
        s.dash._trend = s.trend  # give dashboard access
        s.mkt = None; s.strats = {"ARB": "...", "LATENCY": "...", "MOMENTUM": "...", "FLASH": "..."}
        s.cd = {}; s._traded_cids = set()
        s.start_time = time.time()
        s._logged_positions = set()
        s._past_trades = []  # loaded from trade_history.txt on startup

    def _init_history(s):
        """Write session start header to trade history file."""
        # Load past ended trades from file first
        s._past_trades = s._load_history()
        with open(s.HISTORY_FILE, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"  BOT STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"  Balance: ${s.risk.show_bal:.2f}\n")
            mode = "LIVE" if not s.c.dry_run else "DRY RUN"
            f.write(f"  Mode: {mode}\n")
            f.write(f"{'='*60}\n")
        if s._past_trades:
            s.dash.ev(f"Loaded {len(s._past_trades)} past trades from history")

    def _load_history(s):
        """Parse trade_history.txt and return list of past ended trades."""
        trades = []
        try:
            if not os.path.exists(s.HISTORY_FILE): return []
            with open(s.HISTORY_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("["): continue
                    # Format: [2026-02-14 19:32:55] WIN   FLASH      NO    $2.00 @ $0.2950  P&L: +$1.75  (slug)
                    try:
                        # Extract timestamp
                        ts_end = line.index("]")
                        ts_str = line[1:ts_end]
                        rest = line[ts_end+1:].strip()
                        # Extract WIN/LOSS
                        parts = rest.split()
                        if len(parts) < 6: continue
                        status = parts[0]  # WIN or LOSS
                        strat = parts[1]   # strategy name
                        side = parts[2]    # YES or NO
                        # Extract cost
                        cost_str = parts[3].replace("$", "")
                        cost = float(cost_str)
                        # Extract entry price (after @)
                        entry = 0.0
                        for i, p in enumerate(parts):
                            if p == "@" and i+1 < len(parts):
                                entry = float(parts[i+1].replace("$", ""))
                                break
                        # Extract P&L
                        pnl = 0.0
                        for i, p in enumerate(parts):
                            if p == "P&L:":
                                if i+1 < len(parts):
                                    pnl_str = parts[i+1].replace("+$", "").replace("-$", "-").replace("$", "")
                                    pnl = float(pnl_str)
                                    if parts[i+1].startswith("-"): pnl = -abs(pnl)
                                break
                        # Extract slug
                        slug = ""
                        if "(" in line and ")" in line:
                            slug = line[line.rindex("(")+1:line.rindex(")")]
                        trades.append({
                            "ts": ts_str,
                            "status": status,
                            "strat": strat,
                            "side": side,
                            "cost": cost,
                            "entry": entry,
                            "pnl": pnl,
                            "slug": slug,
                        })
                    except: continue
        except: pass
        return trades

    def _log_trade(s, pos):
        """Append an ended trade to the history file."""
        if pos.id in s._logged_positions: return
        s._logged_positions.add(pos.id)
        try:
            with open(s.HISTORY_FILE, "a") as f:
                ts = pos.opened.strftime('%Y-%m-%d %H:%M:%S') if pos.opened else "?"
                icon = "WIN " if pos.pnl > 0 else "LOSS"
                pnl = f"+${pos.pnl:.2f}" if pos.pnl > 0 else f"-${abs(pos.pnl):.2f}"
                f.write(f"  [{ts}] {icon}  {pos.strat:10} {pos.side:4}  ${pos.cost:.2f} @ ${pos.entry:.4f}  P&L: {pnl}  ({pos.slug})\n")
        except: pass

    def _close_history(s):
        """Write session end footer to trade history file."""
        try:
            w, l, wr = s.risk.stats()
            with open(s.HISTORY_FILE, "a") as f:
                f.write(f"{'─'*60}\n")
                f.write(f"  BOT STOPPED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                elapsed = int(time.time() - s.start_time)
                hrs, rem = divmod(elapsed, 3600)
                mins, secs = divmod(rem, 60)
                f.write(f"  Runtime: {hrs}h {mins}m {secs}s\n")
                f.write(f"  Balance: ${s.risk.show_bal:.2f}  |  Real P&L: {'+'if s.risk.tpnl>=0 else ''}{s.risk.tpnl:.2f}\n")
                f.write(f"  Trades: {len(s.risk.trades)}  |  W: {w}  L: {l}  WR: {wr:.0f}%\n")
                f.write(f"  Wagered: ${s.risk.total_bet:.2f}\n")
                f.write(f"{'='*60}\n\n")
        except: pass

    @staticmethod
    def _beep():
        """Play alert sound when new position opens."""
        try:
            if sys.platform == "win32":
                import winsound
                # 3 ascending beeps
                winsound.Beep(800, 150)
                winsound.Beep(1000, 150)
                winsound.Beep(1200, 200)
            else:
                # Linux/Mac terminal bell
                print("\a", end="", flush=True)
        except:
            print("\a", end="", flush=True)

    def run(s):
        os.system("cls" if os.name == "nt" else "clear")
        print(f"\n  {H1}{'='*55}\n  |  POLYMARKET BTC BOT v5 \u2014 PROXY WALLET\n  {'='*55}{R}\n")
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
                    s.c.starting_balance = rb  # auto-set starting balance
                    print(f"        Balance: ${rb:.2f} (auto-detected)")
                else: print(f"        {WARN}Balance check failed — using estimate{R}")
            else:
                print(f"        {ERR}Auth failed{R}")
                for e in s.conn.errors[-3:]: print(f"        {DIM}{e}{R}")
                if not s.c.dry_run: input("  Press Enter for dry-run..."); s.c.dry_run = True
        else: s.c.dry_run = True
        print(f"  {H2}[4/4]{R} Binance...")
        binance_ok = False
        for _ in range(5):
            p = s.feed.poll()
            if p: s.conn.binance = f"OK \u2014 ${p:,.2f}"; print(f"        BTC: ${p:,.2f}"); binance_ok = True; break
            time.sleep(1)
        if not binance_ok: s.conn.binance = "FAILED"; print(f"        {ERR}Failed{R}")
        print(f"\n  {H1}{'='*55}{R}")
        print(f"  {'LIVE TRADING (proxy)' if not s.c.dry_run else 'DRY RUN'}")
        print(f"  {H1}{'='*55}{R}")
        time.sleep(3); s._init_history(); s._sync_existing_positions(); s.dash.ev("Bot v5 started"); s._loop()

    def _sync_existing_positions(s):
        """Load existing OPEN positions from Polymarket into the bot's tracker.
        Only syncs positions where the market hasn't ended yet."""
        try:
            positions = s.ex.get_positions()
            if not positions: return
            synced = 0
            now = datetime.now(timezone.utc)
            for p in positions:
                title = p.get("title") or p.get("question") or p.get("market", {}).get("question", "")
                slug = p.get("slug") or p.get("market", {}).get("slug", "")
                if not title and not slug: continue

                title_lower = (title or "").lower()
                if "bitcoin" not in title_lower and "btc" not in title_lower: continue

                # Get market end time - skip if already ended
                market_end = None
                if slug:
                    try:
                        parts = slug.split("-")
                        for part in parts:
                            if part.isdigit() and len(part) >= 10:
                                ts = int(part)
                                market_end = datetime.fromtimestamp(ts + 900, tz=timezone.utc)
                                break
                    except: pass
                
                # Skip if market already ended - don't add old resolved positions
                if market_end and now > market_end:
                    continue

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

                already_tracked = any(
                    pos.slug == slug and pos.status == "OPEN"
                    for pos in s.risk.positions
                )
                if already_tracked: continue

                pos = Pos(
                    id=f"SYNC-{slug[:20]}-{int(time.time())}",
                    strat="SYNCED",
                    slug=slug or title[:30],
                    side=side,
                    entry=avg_price if avg_price > 0 else 0.50,
                    shares=size_val,
                    cost=cost if cost > 0 else size_val * (avg_price if avg_price else 0.50),
                    opened=datetime.now(timezone.utc),
                    market_end=market_end,
                )
                s.risk.positions.append(pos)
                synced += 1
                s.dash.ev(f"Synced: {side} {size_val:.1f}sh @ ${avg_price:.2f}")
                log.info(f"Synced existing position: {side} {size_val:.1f} shares @ ${avg_price:.4f} ({slug})")

            if synced: s.dash.ev(f"Loaded {synced} existing position(s)")
        except Exception as e:
            log.debug(f"Position sync error: {e}")

    def _loop(s):
        ctr = 0; s._orders = []; s._poly_pos = []
        while True:
            try:
                s.feed.poll(); ctr += 1
                resolved = s.risk.check_exp(s.feed); s._cancel_exp()
                for p in resolved:
                    s.dash.ev(f"[{p.strat[:3]}] {p.status} P&L:{p.pnl:+.2f}")
                    # Record result to trend engine
                    s.trend.record_result(p.status == "WON", datetime.now(timezone.utc).hour)
                # Log any newly ended positions to trade_history.txt
                for p in s.risk.positions:
                    if p.status != "OPEN": s._log_trade(p)
                if s.mkt:
                    try: yp, np_ = s.ex.prices(s.mkt); s.mkt.yes_p, s.mkt.no_p = yp, np_
                    except: pass
                if ctr % 5 == 0:
                    for asset in s.c.assets:
                        m = s.finder.find(asset)
                        if m:
                            try: yp, np_ = s.ex.prices(m); m.yes_p, m.no_p = yp, np_
                            except: pass
                            new_market = (s.mkt is None or s.mkt.slug != m.slug)
                            s.mkt = m
                            if new_market:
                                m.open_btc = s.feed.price if s.feed.price else 0
                                s.dash.ev(f"New market: {m.slug[-20:]}")
                                s.s1.reset(m.slug)
                                # v5.1: Reset side lock for new market
                                if s.mkt: s.sidelock.reset(m.slug)
                                # v5.1: Record results from resolved positions
                                for p in s.risk.positions:
                                    if p.status in ("WON", "LOST") and hasattr(p, '_recorded') and not p._recorded:
                                        s.trend.record_result(p.status == "WON", datetime.now(timezone.utc).hour)
                                        p._recorded = True
                            if s.conn.can_trade or s.c.dry_run: s._trade(m)
                if ctr % 30 == 0 and s.ex.authed:
                    rb = s.ex.get_balance()
                    if rb: s.risk.set_real(rb)
                    s._orders = s.ex.get_open_orders()
                    s._poly_pos = s.ex.get_positions()
                # v5: Auto-redeem every 5 minutes
                if ctr % 150 == 0 and not s.c.dry_run and s._traded_cids:
                    s._auto_redeem()
                s.dash.render(s.c, s.conn, s.feed, s.risk, s.mkt, s.strats, s.s3.scores, s._orders, s._poly_pos, s.start_time, s._past_trades, s.trend, s.sidelock)
                time.sleep(s.c.poll_sec)
            except KeyboardInterrupt:
                s.ex.cancel_all(); s._auto_redeem(); s._close_history(); s._summary(); break
            except Exception as e:
                log.error(f"Loop: {e}\n{traceback.format_exc()}")
                s.dash.ev(f"Err: {str(e)[:40]}"); time.sleep(3)

    def _cancel_exp(s):
        """Cancel open orders near market expiry. Resolution is handled by risk.check_exp()."""
        now = datetime.now(timezone.utc)
        if s.mkt:
            tl = (s.mkt.end - now).total_seconds()
            if 0 < tl < 120 and s._orders:
                try: s.ex.cancel_all(); s._orders = []; s.dash.ev("Cancelled \u2014 expiring")
                except: pass

    def _auto_redeem(s):
        """v5: Redeem resolved markets."""
        if not s._traded_cids: return
        try:
            redeemed = s.ex.redeem_positions(list(s._traded_cids))
            for cid in redeemed:
                s.dash.ev(f"REDEEMED {cid[:12]}...")
                s._traded_cids.discard(cid)
            if redeemed:
                rb = s.ex.get_balance()
                if rb: s.risk.set_real(rb)
        except Exception as e:
            log.debug(f"Auto-redeem error: {e}")

    def _trade(s, m):
        if not s.risk.ok(): return
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 90: return
        av = s.risk.available
        bal = s.risk.show_bal
        if av < 1.0: return
        open_in_market = sum(1 for p in s.risk.positions if p.status == "OPEN" and p.slug == m.slug)
        if open_in_market >= 2: return
        # v5.1: Min volatility check — don't trade when BTC is dead flat
        btc_vol = abs(s.feed.chg(120))
        if btc_vol < 0.0003 and not sig:  # 0.03% in 2 min = too flat
            pass  # allow ARB (it doesn't need direction) but others need vol
        # v5.1: Update trend
        s.trend.update(s.feed)
        # v5.1: Check for bad conditions
        if s.trend.should_pause():
            for k in s.strats: s.strats[k] = "PAUSED (streak)"
            return
        bad_hour = s.trend.is_bad_hour()

        # S1: Arb
        sig = s.s1.check(m)
        if sig and not bad_hour:
            sz = min(s.c.get_size("ARB", bal), av)
            if sz >= 1.0 and av >= sz and not s.sidelock.is_locked(m.slug, sig["side"]):
                sig["sz"] = sz
                sig["shares"] = sz / sig["price"]
                s.strats["ARB"] = f"ACTIVE {sig['side']} pair=${sig['pair']:.4f}"
                s.dash.ev(f"[ARB] {sig['side']} ${sz:.2f} pair=${sig['pair']:.3f}")
            oid, shares = s.ex.order(m, sig["yes"], sig["price"], sig["shares"])
            if oid:
                t = Trd(datetime.now(timezone.utc), "ARB", m.slug, sig["side"], sig["price"], sig["sz"], oid=oid)
                s.risk.open(t, market_end=m.end, actual_shares=shares)
                s.sidelock.lock(m.slug, sig["side"])
                if m.cid: s._traded_cids.add(m.cid)
                s._beep()
            return
        s.strats["ARB"] = f"sum=${m.yes_p + m.no_p:.4f}"

        # S2: Latency (with trend)
        sig = s.s2.check_with_trend(m, s.feed, s.trend)
        if sig and time.time() - s.cd.get("lat", 0) > 30 and not bad_hour:
            p = sig["p"]; sz = min(s.c.get_size("LATENCY", bal), av)
            if sz >= 1.0 and 0.12 <= p <= 0.70 and not s.sidelock.is_locked(m.slug, sig["dir"]):
                s.strats["LATENCY"] = f"ACTIVE {sig['dir']} edge={sig['edge']*100:.1f}%"
                sh = max(sz / p, 5.0)
                if sh * p > av: sh = av / p
                s.dash.ev(f"[LAT] {sig['dir']} ${sz:.2f} BTC{sig['chg']*100:+.2f}%")
                oid, shares = s.ex.order(m, sig["yes"], p, sh)
                if oid:
                    t = Trd(datetime.now(timezone.utc), "LATENCY", m.slug, sig["dir"], p, sz, oid=oid)
                    s.risk.open(t, market_end=m.end, actual_shares=shares); s.cd["lat"] = time.time()
                    s.sidelock.lock(m.slug, sig["dir"])
                    if m.cid: s._traded_cids.add(m.cid)
                    s._beep()
                return
            else: s.strats["LATENCY"] = f"signal! {sig['dir']} price=${p:.2f}"
        else: s.strats["LATENCY"] = f"btc {s.feed.chg(60)*100:+.2f}%"

        # S3: Momentum
        sig = s.s3.check(m, s.feed)
        if sig and time.time() - s.cd.get("mom", 0) > 60 and not bad_hour:
            p = m.yes_p if sig["yes"] else m.no_p
            sz = min(s.c.get_size("MOMENTUM", bal), av)
            side = "YES" if sig["yes"] else "NO"
            if sz >= 1.0 and 0.15 <= p <= 0.75 and not s.sidelock.is_locked(m.slug, side):
                s.strats["MOMENTUM"] = f"ACTIVE {sig['dir']} {sig['conf']:.0%}"
                sh = max(sz / p, 5.0)
                if sh * p > av: sh = av / p
                s.dash.ev(f"[MOM] {sig['dir']} ${sz:.2f} conf={sig['conf']:.0%}")
                oid, shares = s.ex.order(m, sig["yes"], p, sh)
                if oid:
                    t = Trd(datetime.now(timezone.utc), "MOMENTUM", m.slug, sig["dir"], p, sz, oid=oid)
                    s.risk.open(t, market_end=m.end, actual_shares=shares); s.cd["mom"] = time.time()
                    s.sidelock.lock(m.slug, sig["dir"])
                    if m.cid: s._traded_cids.add(m.cid)
                    s._beep()
                return
            else: s.strats["MOMENTUM"] = f"signal! {sig['dir']} {sig['conf']:.0%} p=${p:.2f}"
        else: s.strats["MOMENTUM"] = f"samples:{s.feed.n}"

        # S4: Flash (with trend + tier sizing)
        sig = s.s4.check(m, s.feed, s.trend)
        if sig and time.time() - s.cd.get("flash", 0) > 90:  # 90s cooldown (was 120)
            p = sig["price"]
            tier = sig.get("tier", 2)
            # Tier-based sizing: cheaper = more confident = bigger bet
            tier_mult = {1: 1.3, 2: 1.0, 3: 0.8}
            sz = min(s.c.get_size("FLASH", bal) * tier_mult.get(tier, 1.0), av)
            if sz >= 1.0 and not s.sidelock.is_locked(m.slug, sig["dir"]):
                s.strats["FLASH"] = f"ACTIVE {sig['dir']} @ ${p:.4f} T{tier}"
                sh = max(sz / p, 5.0)
                if sh * p > av: sh = av / p
                s.dash.ev(f"[FLASH] {sig['dir']} ${sz:.2f} @ ${p:.4f}")
                oid, shares = s.ex.order(m, sig["yes"], p, sh)
                if oid:
                    t = Trd(datetime.now(timezone.utc), "FLASH", m.slug, sig["dir"], p, sz, oid=oid)
                    s.risk.open(t, market_end=m.end, actual_shares=shares); s.cd["flash"] = time.time()
                    s.sidelock.lock(m.slug, sig["dir"])
                    if m.cid: s._traded_cids.add(m.cid)
                    s._beep()
                return
        s.strats["FLASH"] = f"lo=${min(m.yes_p, m.no_p):.4f}"

    def _summary(s):
        os.system("cls" if os.name == "nt" else "clear")
        w, l, wr = s.risk.stats()
        print(f"\n{H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550")
        print(f"  {LBL}SESSION SUMMARY \u2014 BOT v5{R}")
        print(f"{H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550{R}")
        print(f"  {LBL}Balance:{R}  {bal_c(s.risk.show_bal)} USDC")
        print(f"  {LBL}Real P&L:{R} {pnl_c2(s.risk.tpnl)}")
        print(f"  {LBL}Wagered:{R}  {VAL}${s.risk.total_bet:.2f}{R}  ({len(s.risk.trades)} trades)")
        print(f"  {LBL}Record:{R}   {OK}{w}W{R} / {ERR}{l}L{R} / {VAL}{wr:.0f}%{R}")
        print(f"{H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        for t in s.risk.trades[-10:]:
            pn = pnl_c2(t.pnl) if t.pnl else f"{DIM}pending{R}"
            icon = f"{OK}\u2713{R}" if t.pnl and t.pnl > 0 else f"{ERR}\u2717{R}" if t.pnl and t.pnl < 0 else f"{DIM}\u25cb{R}"
            print(f"  {icon} {t.ts.strftime('%H:%M:%S')} [{t.strat[:5]:5}] {t.side:6} ${t.size:.2f} @ ${t.price:.4f}  {pn}")
        print(f"{H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550{R}\n")

if __name__ == "__main__":
    Bot().run()
