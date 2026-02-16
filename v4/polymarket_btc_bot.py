"""
POLYMARKET BTC 15-MIN BOT v4 — PRO STRATEGIES
Based on research into gabagool, $313-to-$414k bot, and professional MM strategies.

KEY CHANGES FROM v3:
1. ALL MAKER ORDERS (0% fee + daily USDC rebates) — taker fees up to 3.15%
2. Gabagool-style asymmetric arb — buy YES when cheap, NO when cheap
3. Improved latency — only when Binance moved AND Polymarket lags
4. Enhanced momentum — VWAP confirmation + higher thresholds
5. Flash crash detection — buy extreme dips for mean reversion
6. One trade per market, proper position resolution, balance protection

pip install py-clob-client python-dotenv requests numpy colorama
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

def pnl_c(v):
    if v > 0: return f"{OK}+${v:.6f}{R}"
    if v < 0: return f"{ERR}-${abs(v):.6f}{R}"
    return f"${v:.6f}"
def pnl_c2(v):
    if v > 0: return f"{OK}+${v:.2f}{R}"
    if v < 0: return f"{ERR}-${abs(v):.2f}{R}"
    return f"${v:.2f}"
def bal_c(v): return f"{OK}${v:.6f}{R}"

@dataclass
class Config:
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 0
    dry_run: bool = False
    starting_balance: float = 50.0
    arb_size: float = 5.0
    latency_size: float = 5.0
    momentum_size: float = 5.0
    flash_size: float = 3.0
    arb_enabled: bool = True
    arb_max_pair_cost: float = 0.985
    latency_enabled: bool = True
    latency_threshold: float = 0.004
    latency_min_edge: float = 0.12
    momentum_enabled: bool = True
    momentum_conf: float = 0.72
    flash_enabled: bool = True
    flash_threshold: float = 0.20
    max_daily_loss: float = 15.0
    max_positions: int = 5
    poll_sec: int = 2
    assets: list = field(default_factory=lambda: ["btc"])

    @classmethod
    def from_env(cls):
        pk = os.getenv("PRIVATE_KEY", "")
        clean = pk[2:] if pk.startswith("0x") else pk
        return cls(
            private_key=clean,
            funder_address=os.getenv("FUNDER_ADDRESS", ""),
            signature_type=int(os.getenv("SIGNATURE_TYPE", "0")),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            starting_balance=float(os.getenv("STARTING_BALANCE", "50.0")),
            arb_size=float(os.getenv("ARB_SIZE", "5.0")),
            latency_size=float(os.getenv("LATENCY_SIZE", "5.0")),
            momentum_size=float(os.getenv("MOMENTUM_SIZE", "5.0")),
            flash_size=float(os.getenv("FLASH_SIZE", "3.0")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "15.0")),
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

class Feed:
    def __init__(s):
        s.data = deque(maxlen=500)
        s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/4"
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

class S_Arb:
    def __init__(s, c): s.c = c; s.yes_qty = 0; s.no_qty = 0; s.yes_cost = 0; s.no_cost = 0; s.market_slug = None
    def reset(s, slug):
        if s.market_slug != slug:
            s.yes_qty = 0; s.no_qty = 0; s.yes_cost = 0; s.no_cost = 0; s.market_slug = slug
    def check(s, m):
        if not s.c.arb_enabled: return None
        s.reset(m.slug)
        yp, np_ = m.yes_p, m.no_p
        pair = yp + np_
        if pair >= s.c.arb_max_pair_cost: return None
        buy_yes = yp < np_
        price = yp if buy_yes else np_
        if price < 0.10 or price > 0.55: return None
        shares = s.c.arb_size / price
        side = "YES" if buy_yes else "NO"
        return {"s": "ARB", "side": side, "yes": buy_yes, "price": price,
                "pair": pair, "profit": 1.0 - pair, "sz": s.c.arb_size, "shares": shares}
    def record_fill(s, is_yes, shares, cost):
        if is_yes: s.yes_qty += shares; s.yes_cost += cost
        else: s.no_qty += shares; s.no_cost += cost

class S_Latency:
    def __init__(s, c): s.c = c
    def check(s, m, f):
        if not s.c.latency_enabled or f.n < 10 or m.open_btc <= 0: return None
        chg = (f.price - m.open_btc) / m.open_btc
        if abs(chg) < s.c.latency_threshold: return None
        up = chg > 0
        pred = min(0.92, 0.50 + abs(chg) * 40)
        mp = m.yes_p if up else m.no_p
        edge = pred - mp
        if edge < s.c.latency_min_edge: return None
        if mp > 0.70: return None
        other = m.no_p if up else m.yes_p
        if other < 0.25: return None
        return {"s": "LATENCY", "dir": "YES" if up else "NO", "yes": up,
                "edge": edge, "pred": pred, "p": mp, "chg": chg, "sz": s.c.latency_size}

class S_Momentum:
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
        if not vwap_agrees and abs(vwap) > 0.001: return None
        if conf < s.c.momentum_conf or abs(comp) < 0.15: return None
        up = comp > 0
        return {"s": "MOMENTUM", "dir": "YES" if up else "NO", "yes": up,
                "conf": conf, "comp": comp, "sig": sig, "rsi": rsi, "sz": s.c.momentum_size}
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
    def __init__(s, c): s.c = c
    def check(s, m, f):
        if not s.c.flash_enabled or f.n < 10: return None
        yes_cheap = m.yes_p <= s.c.flash_threshold
        no_cheap = m.no_p <= s.c.flash_threshold
        if not yes_cheap and not no_cheap: return None
        if abs(f.chg(120)) > 0.015: return None
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 180: return None
        if yes_cheap:
            return {"s": "FLASH", "dir": "YES", "yes": True, "price": m.yes_p, "sz": s.c.flash_size}
        return {"s": "FLASH", "dir": "NO", "yes": False, "price": m.no_p, "sz": s.c.flash_size}

class Finder:
    def __init__(s, c):
        s.c = c; s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/4"; s.cache = {}
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
                # Don't return markets about to expire
                tl = (m.end - now).total_seconds()
                if tl > 30:
                    s.cache[asset] = m; return m
        # Check cache — but only if still active
        cached = s.cache.get(asset)
        if cached:
            tl = (cached.end - now).total_seconds()
            if tl > 30 and cached.active:
                return cached
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
            # Use temp client — don't overwrite s.client if already authed
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
                if s.c.funder_address and st in [1, 2]: kw["funder"] = s.c.funder_address
                s.client = ClobClient(**kw)
                creds = s.client.derive_api_key()
                s.client.set_api_creds(creds)
                s.client.get_ok()
                s.authed = True; conn.auth = f"OK (type={st})"; conn.can_trade = True
                return True
            except Exception as e:
                conn.err(f"type={st}: {str(e)[:40]}")
        conn.auth = "FAILED"; return False
    def get_balance(s):
        addr = s.c.funder_address or s._get_signer_addr()
        if addr:
            bal = s._check_usdc(addr)
            if bal and bal > 0: return bal
        if s.authed:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                r = s.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                if isinstance(r, dict) and "balance" in r:
                    b = int(r["balance"]) / 1e6
                    if b > 0: return b
            except: pass
        signer = s._get_signer_addr()
        if signer and signer.lower() != (addr or "").lower():
            bal = s._check_usdc(signer)
            if bal and bal > 0: return bal
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
                    if r.status_code == 200: return int(r.json().get("result", "0x0"), 16) / 1e6
                except: continue
        except: pass
        return None
    def order(s, market, is_yes, price, size):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        label = "YES" if is_yes else "NO"
        if price < 0.10 or price > 0.90: return None
        maker_price = round(max(0.01, min(price - 0.01, 0.99)), 2)
        size = max(size, 5.0); bet = round(maker_price * size, 4)
        if s.c.dry_run:
            oid = f"DRY-{int(time.time()*1000)%99999}"
            log.info(f"DRY: ${bet:.4f} {label} ({size:.2f}sh @ ${maker_price:.4f})")
            return oid
        if not s.authed: return None
        try:
            tid = market.tok_yes if is_yes else market.tok_no
            signed = s.client.create_order(OrderArgs(price=maker_price, size=round(size, 2), side=BUY, token_id=tid))
            resp = s.client.post_order(signed, OrderType.GTC)
            if isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("id") or resp.get("order_id") or "?"
                status = resp.get("status", "")
                if status == "matched" or status == "live" or oid != "?":
                    log.info(f"MAKER: ${bet:.4f} {label} ({size:.2f}sh @ ${maker_price:.4f}) id={oid} status={status}")
                    return oid
                else:
                    log.warning(f"Order rejected: {resp}")
                    return None
            elif isinstance(resp, str):
                log.info(f"MAKER: ${bet:.4f} {label} resp={resp[:60]}")
                return resp if len(resp) > 5 else None
            else:
                log.warning(f"Order unexpected resp type: {type(resp)}")
                return None
        except Exception as e:
            log.error(f"Order fail: {e}"); return None
    def prices(s, m):
        """Get live YES/NO prices. Try midpoint first, then order book, then Gamma."""
        if not s.client or not s.authed:
            return m.yes_p, m.no_p
        try:
            ymid = s.client.get_midpoint(m.tok_yes)
            nmid = s.client.get_midpoint(m.tok_no)
            yp = float(ymid["mid"]) if isinstance(ymid, dict) else float(ymid)
            np_ = float(nmid["mid"]) if isinstance(nmid, dict) else float(nmid)
            # Sanity check — prices should be between 0 and 1
            if 0 < yp < 1 and 0 < np_ < 1:
                return yp, np_
            log.debug(f"Midpoint sanity fail: YES={yp} NO={np_}")
        except Exception as e:
            log.debug(f"Midpoint fail: {e}")
        # Fallback: try order book best bid/ask
        try:
            ybook = s.client.get_order_book(m.tok_yes)
            nbook = s.client.get_order_book(m.tok_no)
            yp = s._book_mid(ybook)
            np_ = s._book_mid(nbook)
            if yp and np_:
                return yp, np_
        except Exception as e:
            log.debug(f"Book fail: {e}")
        return m.yes_p, m.no_p

    def _book_mid(s, book):
        """Calculate midpoint from order book bids/asks."""
        if not isinstance(book, dict): return None
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            if best_bid > 0 and best_ask > 0:
                return (best_bid + best_ask) / 2
        elif bids:
            return float(bids[0].get("price", 0))
        elif asks:
            return float(asks[0].get("price", 0))
        return None
    def get_positions(s):
        """Fetch positions — try signer address first, then proxy."""
        addrs = []
        signer = s._get_signer_addr()
        funder = s.c.funder_address
        if signer: addrs.append(signer)
        if funder and funder.lower() != (signer or "").lower(): addrs.append(funder)
        for addr in addrs:
            try:
                r = requests.get("https://data-api.polymarket.com/positions",
                    params={"user": addr}, timeout=8)
                if r.status_code == 200:
                    data = r.json() if isinstance(r.json(), list) else []
                    if data: return data
            except: continue
        # Also try CLOB API
        if s.authed:
            try:
                # Some positions may only show via the proxy wallet
                r = requests.get("https://clob.polymarket.com/positions",
                    params={"user": signer}, timeout=8,
                    headers={"Authorization": f"Bearer {s.client.creds.api_key}" if hasattr(s.client, 'creds') and s.client.creds else ""})
                if r.status_code == 200:
                    data = r.json() if isinstance(r.json(), list) else []
                    if data: return data
            except: pass
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

class Risk:
    def __init__(s, c):
        s.c = c; s.bal = c.starting_balance; s.real_bal = None
        s.dpnl = 0.0; s.tpnl = 0.0; s.total_bet = 0.0
        s.trades = []; s.positions = []
    def set_real(s, b):
        if b is not None and b > 0: s.real_bal = b; s.bal = b
    @property
    def show_bal(s): return s.real_bal if s.real_bal is not None else s.bal
    @property
    def open_risk(s): return sum(p.cost for p in s.positions if p.status == "OPEN")
    @property
    def available(s): return s.show_bal - s.open_risk
    def ok(s):
        if s.dpnl <= -s.c.max_daily_loss: return False
        if len([p for p in s.positions if p.status == "OPEN"]) >= s.c.max_positions: return False
        return s.available >= 1.0
    def open(s, t):
        p = Pos(id=t.oid, strat=t.strat, slug=t.slug, side=t.side,
            entry=t.price, shares=t.size / t.price, cost=t.size, opened=t.ts)
        s.positions.append(p); s.trades.append(t); s.total_bet += t.size; return p
    def resolve(s, pos, won):
        pnl = (pos.shares * 1.0 - pos.cost) if won else -pos.cost
        if pnl > 0: pnl *= 0.98
        pos.pnl = pnl; pos.status = "WON" if pnl > 0 else "LOST"
        s.bal += pos.cost + pnl; s.dpnl += pnl; s.tpnl += pnl
        for t in s.trades:
            if t.oid == pos.id: t.pnl = pnl
    def check_exp(s, f):
        now = datetime.now(timezone.utc)
        for p in s.positions:
            if p.status != "OPEN" or not p.opened: continue
            age = (now - p.opened).total_seconds()
            if age > 960:
                op = cp = None
                for x in f.data:
                    if x["t"] >= p.opened.timestamp() and op is None: op = x["p"]
                    cp = x["p"]
                if op and cp:
                    up = cp > op
                    if p.strat == "ARB": s.resolve(p, True)
                    else: s.resolve(p, (up and "YES" in p.side) or (not up and "NO" in p.side))
                elif age > 1200: s.resolve(p, False)
    def stats(s):
        w = sum(1 for t in s.trades if t.pnl > 0)
        l = sum(1 for t in s.trades if t.pnl < 0)
        return w, l, (w / (w + l) * 100 if w + l else 0)

class Dash:
    def __init__(s): s.evts = deque(maxlen=8)
    def ev(s, e): s.evts.append(f"{datetime.now().strftime('%H:%M:%S')} {e}")
    def render(s, c, conn, f, risk, mkt, strats, scores, orders, poly_pos):
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.now().strftime("%H:%M:%S")
        mode = f"{ERR}LIVE{R}" if not c.dry_run else f"{WARN}DRY RUN{R}"
        # Header
        print(f"  {H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550")
        print(f"  \u2551  POLYMARKET BTC 15-MIN BOT v4   [{mode}]   {now}")
        print(f"  \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550{R}")
        # Connection
        g = f"{OK}\u2713{R}"; x = f"{ERR}\u2717{R}"
        print(f"    {LBL}CONNECTION STATUS{R}")
        print(f"    {g if conn.gamma != 'FAILED' else x} Gamma API (markets):  {conn.gamma}")
        print(f"    {g if conn.clob != 'FAILED' else x} CLOB API  (orders):  {conn.clob}")
        print(f"    {g if conn.can_trade else x} CLOB Auth (trading): {conn.auth}")
        print(f"    {g if conn.binance != 'FAILED' else x} Binance   (price):  {conn.binance}")
        if conn.can_trade and not c.dry_run:
            print(f"    {OK}>>> READY TO TRADE <<<{R}")
        elif c.dry_run:
            print(f"    {WARN}\u25cb DRY RUN MODE \u2014 simulating trades{R}")
        # Account
        print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        print(f"    {LBL}ACCOUNT{R}")
        src = "LIVE" if risk.real_bal is not None else "est"
        print(f"    Polymarket Balance: {bal_c(risk.show_bal)} USDC  ({src})")
        print(f"    Available: {bal_c(risk.available)}   At Risk: {VAL}${risk.open_risk:.4f}{R}")
        w, l, wr = risk.stats()
        print(f"    Daily P&L: {pnl_c2(risk.dpnl)}   Total P&L: {pnl_c2(risk.tpnl)}")
        print(f"    Total Wagered: {VAL}${risk.total_bet:.4f}{R}   Bet Size: ${c.momentum_size:.2f}/trade")
        print(f"    Trades: {VAL}{len(risk.trades)}{R}   W:{OK}{w}{R} L:{ERR}{l}{R} WR:{VAL}{wr:.0f}%{R}   Loss Limit: {ERR}-${c.max_daily_loss:.2f}{R}")
        # Market
        if mkt:
            tl = (mkt.end - datetime.now(timezone.utc)).total_seconds()
            print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
            print(f"    {LBL}MARKET DATA{R}")
            chg1m = f.chg(60) * 100
            chg_c = OK if chg1m >= 0 else ERR
            print(f"    BTC: {BTC}${f.price:,.2f}{R}   Samples:{VAL}{f.n}{R}   1min:{chg_c}{chg1m:+.3f}%{R}   Vol:{VAL}{f.volatility()*100:.2f}%{R}")
            print(f"    Market: {MKT}{mkt.slug}{R}")
            sm = mkt.yes_p + mkt.no_p
            sm_c = OK if sm < 0.99 else VAL
            tl_c = ERR if tl < 120 else WARN if tl < 300 else VAL
            print(f"    YES:{OK}${mkt.yes_p:.4f}{R}  NO:{ERR}${mkt.no_p:.4f}{R}  SUM:{sm_c}${sm:.4f}{R}  Exp:{tl_c}{int(tl//60)}:{int(tl%60):02d}{R}")
        # Strategies
        print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        print(f"    {LBL}STRATEGIES{R}")
        icons = {"ARB": "\u2666", "LATENCY": "\u26a1", "MOMENTUM": "\u2191", "FLASH": "\u26a0"}
        for k, v in strats.items():
            ic = icons.get(k, "\u25cb")
            if "ACTIVE" in str(v):
                print(f"    {OK}\u25cf {ic} {k:12}{R} {OK}{v}{R}")
            else:
                print(f"    {DIM}\u25cb {ic} {k:12}{R} {v}")
        if scores:
            parts = []
            for k, v in scores.items():
                c2 = OK if v > 0.1 else ERR if v < -0.1 else DIM
                parts.append(f"{k}:{c2}{v:+.2f}{R}")
            print(f"    Signals: {'  '.join(parts)}")
        # Positions
        open_pos = [p for p in risk.positions if p.status == "OPEN"]
        closed = [p for p in risk.positions if p.status != "OPEN"][-5:]
        print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        print(f"    {LBL}POSITIONS ({len(open_pos)} open, ${risk.open_risk:.4f} at risk){R}")
        for p in open_pos[-5:]:
            age = (datetime.now(timezone.utc) - p.opened).total_seconds() if p.opened else 0
            bar_pct = min(age / 900, 1.0)
            bar_len = int(bar_pct * 10)
            bar = f"{OK}{'█' * bar_len}{DIM}{'░' * (10 - bar_len)}{R}"
            print(f"    {POS}OPEN{R}  [{p.strat[:5]:5}] {p.side:6}  BET:${p.cost:.4f} @ ${p.entry:.4f}  {bar} {int(age//60)}:{int(age%60):02d}")
        for p in closed:
            icon = f"{OK}\u2713{R}" if p.pnl > 0 else f"{ERR}\u2717{R}"
            print(f"    {icon} {OK if p.pnl > 0 else ERR}{p.status:5}{R} [{p.strat[:5]:5}] {p.side:6}  P&L:{pnl_c2(p.pnl)}")
        # Trade Log
        recent_trades = risk.trades[-5:]
        if recent_trades:
            print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
            print(f"    {LBL}TRADE LOG (bet = ${c.momentum_size:.2f} per trade){R}")
            for t in recent_trades:
                pn = pnl_c2(t.pnl) if t.pnl else f"{DIM}pending{R}"
                print(f"    {t.ts.strftime('%H:%M:%S')} [{t.strat[:5]:5}] {t.side:6}  BET:${t.size:.4f} @${t.price:.4f}  {pn}")
        # Polymarket positions
        if poly_pos:
            recent = [p for p in poly_pos if s._is_recent(p)][:3]
            if recent:
                print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
                print(f"    {LBL}POLYMARKET POSITIONS ({len(recent)} recent){R}")
                for p in recent:
                    title = str(p.get("title") or p.get("question") or "?")[:42]
                    sz = p.get("size", 0); avg = p.get("avgPrice", 0)
                    print(f"    {MKT}{title}{R}  Size:{VAL}{sz}{R} @${avg}")
        # Events
        print(f"  {H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        print(f"    {LBL}EVENTS{R}")
        for e in list(s.evts)[-6:]:
            print(f"    {EVT}{e}{R}")
        print(f"  {H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550{R}")
        print(f"  {DIM}Ctrl+C to stop{R}")

    def _is_recent(s, p):
        for key in ["createdAt", "timestamp", "created_at"]:
            ts = p.get(key)
            if ts:
                try:
                    if isinstance(ts, (int, float)):
                        dt = datetime.fromtimestamp(ts if ts < 1e11 else ts/1e3, tz=timezone.utc)
                    else:
                        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    return (datetime.now(timezone.utc) - dt).total_seconds() < 86400
                except: pass
        return True

class Bot:
    def __init__(s):
        s.c = Config.from_env(); s.conn = Conn(); s.feed = Feed()
        s.finder = Finder(s.c); s.ex = Executor(s.c); s.risk = Risk(s.c)
        s.dash = Dash()
        s.s1 = S_Arb(s.c); s.s2 = S_Latency(s.c); s.s3 = S_Momentum(s.c); s.s4 = S_Flash(s.c)
        s.mkt = None; s.strats = {"ARB": "...", "LATENCY": "...", "MOMENTUM": "...", "FLASH": "..."}; s.cd = {}

    def run(s):
        os.system("cls" if os.name == "nt" else "clear")
        print(f"\n  {H1}{'='*55}\n  |  POLYMARKET BTC BOT v4 — PRO\n  {'='*55}{R}\n")
        print(f"  {H2}[1/4]{R} Gamma..."); s.conn.gamma = "OK" if s.finder.test() else "FAILED"
        print(f"        {'OK' if s.conn.gamma == 'OK' else 'FAILED'}")
        print(f"  {H2}[2/4]{R} CLOB..."); s.conn.clob = "OK" if s.ex.test_public() else "FAILED"
        print(f"        {'OK' if s.conn.clob == 'OK' else 'FAILED'}")
        print(f"  {H2}[3/4]{R} Auth...")
        if s.c.private_key:
            if s.ex.connect(s.conn):
                print(f"        {OK}Authenticated!{R}")
                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                    s.ex.client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                except: pass
                rb = s.ex.get_balance()
                if rb: s.risk.set_real(rb); print(f"        Balance: ${rb:.6f}")
            else:
                print(f"        {ERR}Auth failed{R}")
                if not s.c.dry_run: input("  Press Enter for dry-run..."); s.c.dry_run = True
        else: s.c.dry_run = True
        print(f"  {H2}[4/4]{R} Binance...")
        binance_ok = False
        for _ in range(5):
            p = s.feed.poll()
            if p: s.conn.binance = f"OK — ${p:,.2f}"; print(f"        BTC: ${p:,.2f}"); binance_ok = True; break
            time.sleep(1)
        if not binance_ok:
            s.conn.binance = "FAILED"
            print(f"        {ERR}Binance failed — retrying in background{R}")
        print(f"\n  {H1}{'='*55}{R}")
        print(f"  {'LIVE TRADING' if not s.c.dry_run else 'DRY RUN'}")
        print(f"  {H1}{'='*55}{R}")
        time.sleep(3); s.dash.ev("Bot v4 started"); s._loop()

    def _loop(s):
        ctr = 0; s._orders = []; s._poly_pos = []
        while True:
            try:
                s.feed.poll(); ctr += 1
                s.risk.check_exp(s.feed); s._cancel_exp()
                if s.mkt:
                    try: yp, np_ = s.ex.prices(s.mkt); s.mkt.yes_p, s.mkt.no_p = yp, np_
                    except: pass
                if ctr % 5 == 0:
                    for asset in s.c.assets:
                        m = s.finder.find(asset)
                        if m:
                            # Force live price update from CLOB immediately
                            try: yp, np_ = s.ex.prices(m); m.yes_p, m.no_p = yp, np_
                            except: pass
                            # Track if this is a new market window
                            new_market = (s.mkt is None or s.mkt.slug != m.slug)
                            s.mkt = m
                            if new_market:
                                m.open_btc = s.feed.price if s.feed.price else 0
                                s.dash.ev(f"New market: {m.slug[-20:]}")
                                s.s1.reset(m.slug)  # Reset arb tracking
                            if s.conn.can_trade or s.c.dry_run: s._trade(m)
                if ctr % 30 == 0 and s.ex.authed:
                    rb = s.ex.get_balance()
                    if rb: s.risk.set_real(rb)
                    s._orders = s.ex.get_open_orders()
                    s._poly_pos = s.ex.get_positions()
                s.dash.render(s.c, s.conn, s.feed, s.risk, s.mkt, s.strats, s.s3.scores, s._orders, s._poly_pos)
                time.sleep(s.c.poll_sec)
            except KeyboardInterrupt:
                s.ex.cancel_all(); s._summary(); break
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
        for p in s.risk.positions:
            if p.status != "OPEN" or not p.opened: continue
            age = (now - p.opened).total_seconds()
            if age > 960:
                op = cp = None
                for x in s.feed.data:
                    if x["t"] >= p.opened.timestamp() and op is None: op = x["p"]
                    cp = x["p"]
                if op and cp:
                    up = cp > op
                    if p.strat == "ARB": s.risk.resolve(p, True)
                    else: s.risk.resolve(p, (up and "YES" in p.side) or (not up and "NO" in p.side))
                    s.dash.ev(f"[{p.strat[:3]}] {p.status} P&L:{p.pnl:+.2f}")
                elif age > 1200: s.risk.resolve(p, False); s.dash.ev(f"[{p.strat[:3]}] EXPIRED")

    def _trade(s, m):
        if not s.risk.ok(): return
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 90: return
        av = s.risk.available
        if av < 1.0: return
        open_slugs = set(p.slug for p in s.risk.positions if p.status == "OPEN")
        if m.slug in open_slugs: return

        # S1: Arb
        sig = s.s1.check(m)
        if sig and av >= sig["sz"]:
            s.strats["ARB"] = f"ACTIVE {sig['side']} pair=${sig['pair']:.4f}"
            s.dash.ev(f"[ARB] {sig['side']} ${sig['sz']:.2f} pair=${sig['pair']:.3f}")
            oid = s.ex.order(m, sig["yes"], sig["price"], sig["shares"])
            if oid:
                t = Trd(datetime.now(timezone.utc), "ARB", m.slug, sig["side"], sig["price"], sig["sz"], oid=oid)
                s.risk.open(t); s.s1.record_fill(sig["yes"], sig["shares"], sig["sz"])
            return
        s.strats["ARB"] = f"sum=${m.yes_p + m.no_p:.4f}"

        # S2: Latency
        sig = s.s2.check(m, s.feed)
        if sig and time.time() - s.cd.get("lat", 0) > 30:
            p = sig["p"]; sz = min(sig["sz"], av)
            if sz >= 1.0 and 0.15 <= p <= 0.70:
                s.strats["LATENCY"] = f"ACTIVE {sig['dir']} edge={sig['edge']*100:.1f}%"
                sh = max(sz / p, 5.0)
                if sh * p > av: sh = av / p
                s.dash.ev(f"[LAT] {sig['dir']} ${sz:.2f} BTC{sig['chg']*100:+.2f}%")
                oid = s.ex.order(m, sig["yes"], p, sh)
                if oid:
                    t = Trd(datetime.now(timezone.utc), "LATENCY", m.slug, sig["dir"], p, sz, oid=oid)
                    s.risk.open(t); s.cd["lat"] = time.time()
                return
            else:
                s.strats["LATENCY"] = f"signal! {sig['dir']} but price=${p:.2f}"
        else:
            s.strats["LATENCY"] = f"btc {s.feed.chg(60)*100:+.2f}%"

        # S3: Momentum
        sig = s.s3.check(m, s.feed)
        if sig and time.time() - s.cd.get("mom", 0) > 60:
            p = m.yes_p if sig["yes"] else m.no_p
            sz = min(sig["sz"], av)
            if sz >= 1.0 and 0.20 <= p <= 0.75:
                s.strats["MOMENTUM"] = f"ACTIVE {sig['dir']} {sig['conf']:.0%}"
                sh = max(sz / p, 5.0)
                if sh * p > av: sh = av / p
                s.dash.ev(f"[MOM] {sig['dir']} ${sz:.2f} conf={sig['conf']:.0%}")
                oid = s.ex.order(m, sig["yes"], p, sh)
                if oid:
                    t = Trd(datetime.now(timezone.utc), "MOMENTUM", m.slug, sig["dir"], p, sz, oid=oid)
                    s.risk.open(t); s.cd["mom"] = time.time()
                return
            else:
                s.strats["MOMENTUM"] = f"signal! {sig['dir']} {sig['conf']:.0%} price=${p:.2f}"
        else:
            s.strats["MOMENTUM"] = f"samples:{s.feed.n}"

        # S4: Flash
        sig = s.s4.check(m, s.feed)
        if sig and time.time() - s.cd.get("flash", 0) > 120:
            p = sig["price"]; sz = min(sig["sz"], av)
            if sz >= 1.0:
                s.strats["FLASH"] = f"ACTIVE {sig['dir']} @ ${p:.4f}"
                sh = max(sz / p, 5.0)
                if sh * p > av: sh = av / p
                s.dash.ev(f"[FLASH] {sig['dir']} ${sz:.2f} @ ${p:.4f}")
                oid = s.ex.order(m, sig["yes"], p, sh)
                if oid:
                    t = Trd(datetime.now(timezone.utc), "FLASH", m.slug, sig["dir"], p, sz, oid=oid)
                    s.risk.open(t); s.cd["flash"] = time.time()
                return
        s.strats["FLASH"] = f"lo=${min(m.yes_p, m.no_p):.4f}"

    def _summary(s):
        os.system("cls" if os.name == "nt" else "clear")
        w, l, wr = s.risk.stats()
        print(f"\n{H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550")
        print(f"  {LBL}SESSION SUMMARY \u2014 BOT v4 PRO{R}")
        print(f"{H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550{R}")
        print(f"  {LBL}Polymarket Balance:{R} {bal_c(s.risk.show_bal)} USDC")
        print(f"  {LBL}Session P&L:{R}        {pnl_c2(s.risk.tpnl)}")
        print(f"  {LBL}Total Wagered:{R}      {VAL}${s.risk.total_bet:.4f}{R}  ({len(s.risk.trades)} trades)")
        print(f"  {LBL}Record:{R}             {OK}{w}W{R} / {ERR}{l}L{R} / {VAL}{wr:.0f}%{R}")
        print(f"{H1}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{R}")
        for t in s.risk.trades[-10:]:
            pn = pnl_c2(t.pnl) if t.pnl else f"{DIM}pending{R}"
            icon = f"{OK}\u2713{R}" if t.pnl and t.pnl > 0 else f"{ERR}\u2717{R}" if t.pnl and t.pnl < 0 else f"{DIM}\u25cb{R}"
            print(f"  {icon} {t.ts.strftime('%H:%M:%S')} [{t.strat[:5]:5}] {t.side:6} ${t.size:.2f} @ ${t.price:.4f}  {pn}")
        print(f"{H1}\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550{R}\n")

if __name__ == "__main__":
    Bot().run()
