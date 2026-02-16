"""
POLYMARKET BTC 15-MIN BOT v3 — TRIPLE STRATEGY
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

# ═══════════════════════════════════════════════════════
#  AUTO-INSTALL COLORAMA
# ═══════════════════════════════════════════════════════
try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "colorama", "-q"])
    from colorama import init, Fore, Back, Style
    init(autoreset=True)

# ═══════════════════════════════════════════════════════
#  COLOR THEME
# ═══════════════════════════════════════════════════════
H1    = Fore.CYAN + Style.BRIGHT       # Main headers / box borders
H2    = Fore.MAGENTA + Style.BRIGHT    # Section headers
LBL   = Fore.WHITE + Style.BRIGHT      # Labels
VAL   = Fore.YELLOW + Style.BRIGHT     # Values / numbers
OK    = Fore.GREEN + Style.BRIGHT      # Success / connected
ERR   = Fore.RED + Style.BRIGHT        # Errors / failures
WARN  = Fore.YELLOW                    # Warnings
BTC   = Fore.YELLOW + Style.BRIGHT     # BTC price
MKT   = Fore.CYAN                      # Market info
STRAT = Fore.MAGENTA                   # Strategy labels
TRAD  = Fore.BLUE + Style.BRIGHT       # Trade info
POS   = Fore.CYAN + Style.BRIGHT       # Position info
EVT   = Fore.WHITE + Style.DIM         # Event log
DIM   = Fore.WHITE + Style.DIM         # Dim text
R     = Style.RESET_ALL

def pnl_c(v):
    """Color a P&L value green/red."""
    if v > 0: return f"{Fore.GREEN + Style.BRIGHT}+${v:.6f}{R}"
    if v < 0: return f"{ERR}-${abs(v):.6f}{R}"
    return f"${v:.6f}"

def pnl_c2(v):
    """Short P&L color."""
    if v > 0: return f"{Fore.GREEN + Style.BRIGHT}+${v:.2f}{R}"
    if v < 0: return f"{ERR}-${abs(v):.2f}{R}"
    return f"${v:.2f}"

def bal_c(v):
    """Full decimal balance color."""
    return f"{Fore.GREEN + Style.BRIGHT}${v:.6f}{R}"


# ═══════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════
@dataclass
class Config:
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 1
    dry_run: bool = False
    starting_balance: float = 7.0
    arb_enabled: bool = True
    arb_max_pair_cost: float = 0.98
    arb_size: float = 1.0
    latency_enabled: bool = True
    latency_threshold: float = 0.005
    latency_min_edge: float = 0.15
    latency_size: float = 1.0
    momentum_enabled: bool = True
    momentum_size: float = 1.0
    momentum_conf: float = 0.75
    max_daily_loss: float = 3.0
    max_positions: int = 5
    poll_sec: int = 2
    assets: list = field(default_factory=lambda: ["btc"])

    @classmethod
    def from_env(cls):
        pk = os.getenv("PRIVATE_KEY", "")
        # Keep raw key — ClobClient handles format internally
        # Strip 0x for validation but store with it for ClobClient
        clean = pk[2:] if pk.startswith("0x") else pk
        return cls(
            private_key=clean,
            funder_address=os.getenv("FUNDER_ADDRESS", ""),
            signature_type=int(os.getenv("SIGNATURE_TYPE", "1")),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            starting_balance=float(os.getenv("STARTING_BALANCE", "7.0")),
            arb_size=float(os.getenv("ARB_SIZE", "1.0")),
            latency_size=float(os.getenv("LATENCY_SIZE", "1.0")),
            momentum_size=float(os.getenv("MOMENTUM_SIZE", "1.0")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "3.0")),
        )

log = logging.getLogger("Bot"); log.setLevel(logging.DEBUG)
_fh = logging.FileHandler("polybot.log")
_fh.setFormatter(logging.Formatter("%(asctime)s|%(levelname)s|%(message)s"))
log.addHandler(_fh)


# ═══════════════════════════════════════════════════════
#  DATA
# ═══════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════
#  PRICE FEED
# ═══════════════════════════════════════════════════════
class Feed:
    def __init__(s):
        s.data = deque(maxlen=500)
        s.s = requests.Session(); s.s.headers["User-Agent"] = "PolyBot/3"
    def poll(s):
        for fn in [s._b, s._c]:
            try:
                p = fn()
                if p: s.data.append({"t": time.time(), "p": p}); return p
            except: continue
        return None
    def _b(s):
        r = s.s.get("https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=3)
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


# ═══════════════════════════════════════════════════════
#  STRATEGIES
# ═══════════════════════════════════════════════════════
class S1:
    def __init__(s, c): s.c = c
    def check(s, m):
        if not s.c.arb_enabled: return None
        pair = m.yes_p + m.no_p; net = 1.0 - pair - 0.02
        if net <= 0 or pair >= s.c.arb_max_pair_cost: return None
        sh = s.c.arb_size / pair
        return {"s": "ARB", "pair": pair, "profit": net*sh, "yp": m.yes_p,
                "np": m.no_p, "sh": sh, "sz": s.c.arb_size}

class S2:
    def __init__(s, c): s.c = c
    def check(s, m, f):
        if not s.c.latency_enabled or f.n < 10 or m.open_btc <= 0: return None
        chg = (f.price - m.open_btc) / m.open_btc
        if abs(chg) < s.c.latency_threshold: return None
        up = chg > 0
        pred = min(0.95, 0.50 + abs(chg) * 50)
        mp = m.yes_p if up else m.no_p
        edge = pred - mp
        if edge < s.c.latency_min_edge: return None
        return {"s": "LATENCY", "dir": "YES" if up else "NO", "yes": up,
                "edge": edge, "p": mp, "chg": chg, "sz": s.c.latency_size}

class S3:
    def __init__(s, c): s.c = c; s.scores = {}
    def check(s, m, f):
        if not s.c.momentum_enabled: return None
        p = f.arr(60)
        if len(p) < 25: return None
        sig = {}
        bb = p[-20:]; mu, sd = np.mean(bb), np.std(bb)
        if sd == 0: return None
        cur = p[-1]
        sig["bb"] = 0.8 if cur > mu+2*sd else (-0.8 if cur < mu-2*sd else (0.3 if cur > mu else -0.3))
        if len(p) >= 21:
            ef, es = s._ema(p, 9), s._ema(p, 21)
            sig["ema"] = float(np.clip((ef-es)/es*200, -1, 1))
        else: sig["ema"] = 0
        rsi = s._rsi(p, 14)
        sig["rsi"] = (-0.7 if rsi > 70 else (0.7 if rsi < 30 else (0.3 if rsi > 55 else (-0.3 if rsi < 45 else 0))))
        sig["roc"] = float(np.clip(f.chg(120)*100, -1, 1))
        s.scores = sig
        w = {"bb": .30, "ema": .25, "rsi": .25, "roc": .20}
        comp = sum(sig[k]*w[k] for k in sig)
        conf = min(1.0, 0.5 + abs(comp)*0.5)
        if conf < s.c.momentum_conf or abs(comp) < 0.15: return None
        up = comp > 0
        return {"s": "MOMENTUM", "dir": "YES" if up else "NO", "yes": up,
                "conf": conf, "comp": comp, "sig": sig, "rsi": rsi, "sz": s.c.momentum_size}
    def _ema(s, p, n):
        k = 2/(n+1); e = p[0]
        for x in p[1:]: e = x*k + e*(1-k)
        return e
    def _rsi(s, p, n):
        if len(p) < n+1: return 50
        d = np.diff(p[-n-1:])
        g, l = np.mean(np.maximum(d, 0)), np.mean(np.maximum(-d, 0))
        return 100 if l == 0 else 100-100/(1+g/l)


# ═══════════════════════════════════════════════════════
#  MARKET FINDER (Gamma API)
# ═══════════════════════════════════════════════════════
class Finder:
    def __init__(s, c):
        s.c = c; s.s = requests.Session()
        s.s.headers["User-Agent"] = "PolyBot/3"; s.cache = {}
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
            if m and m.active: s.cache[asset] = m; return m
        return s.cache.get(asset)
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
            return Market(slug=d.get("slug", ""),
                cid=d.get("condition_id") or d.get("conditionId", ""),
                question=d.get("question", ""),
                tok_yes=tok[0].strip().strip('"'), tok_no=tok[1].strip().strip('"'),
                end=et, yes_p=float(pr[0]) if pr else 0.5,
                no_p=float(pr[1]) if len(pr)>1 else 0.5,
                active=not d.get("closed", False))
        except: return None


# ═══════════════════════════════════════════════════════
#  EXECUTOR (CLOB API — REAL BETTING)
#  Uses exact methods from working Polymarket API code
# ═══════════════════════════════════════════════════════
class Executor:
    def __init__(s, c):
        s.c = c; s.client = None; s.authed = False; s._signer_addr = None

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
            s.client = ClobClient(s.c.clob_host)
            return str(s.client.get_ok()).upper() in ["OK", "TRUE"]
        except: return False

    def connect(s, conn):
        from py_clob_client.client import ClobClient
        pk = s.c.private_key
        if not pk: conn.auth = "NO KEY"; return False
        types = [s.c.signature_type] + [t for t in [0, 1, 2] if t != s.c.signature_type]
        for st in types:
            try:
                kw = {"host": s.c.clob_host, "key": pk,
                      "chain_id": s.c.chain_id, "signature_type": st}
                if s.c.funder_address and st in [1, 2]:
                    kw["funder"] = s.c.funder_address
                conn.auth = f"Trying type={st}..."
                s.client = ClobClient(**kw)

                # Use derive_api_key() — matches working code
                creds = s.client.derive_api_key()
                s.client.set_api_creds(creds)

                s.client.get_ok()
                s.authed = True; conn.auth = f"OK (type={st})"; conn.can_trade = True
                log.info(f"Auth OK type={st}"); return True
            except Exception as e:
                log.debug(f"Auth type={st}: {e}"); conn.err(f"type={st}: {str(e)[:40]}")
        conn.auth = "FAILED"; return False

    def get_balance(s):
        """Fetch USDC balance using 3 methods:
        1. On-chain via raw Polygon RPC call (no web3 needed)
        2. CLOB API get_balance_allowance
        3. Fallback: return None
        """
        addr = s.c.funder_address
        if not addr: addr = s._get_signer_addr()

        # Method 1: Direct on-chain via raw JSON-RPC (no web3 install needed)
        if addr:
            bal = s._check_usdc_onchain(addr)
            if bal is not None and bal > 0:
                log.info(f"On-chain balance ({addr[:10]}...): ${bal:.6f}")
                return bal

        # Method 2: CLOB API
        if s.authed:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                r = s.client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                if isinstance(r, dict) and "balance" in r:
                    bal = int(r["balance"]) / 1e6
                    if bal > 0:
                        log.info(f"CLOB balance: ${bal:.6f}")
                        return bal
            except Exception as e:
                log.debug(f"CLOB balance fail: {e}")

        # Method 3: Check signer address on-chain too
        signer = s._get_signer_addr()
        if signer and signer.lower() != (addr or "").lower():
            bal = s._check_usdc_onchain(signer)
            if bal is not None and bal > 0:
                log.info(f"On-chain signer balance ({signer[:10]}...): ${bal:.6f}")
                return bal

        return None

    def _check_usdc_onchain(s, wallet_addr):
        """Check USDC.e balance on Polygon using raw JSON-RPC — no web3 needed."""
        try:
            # USDC.e contract on Polygon
            usdc_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            # balanceOf(address) function selector = 0x70a08231
            # Pad address to 32 bytes
            clean_addr = wallet_addr.lower().replace("0x", "").zfill(64)
            data = "0x70a08231" + clean_addr

            # Try multiple Polygon RPCs
            rpcs = [
                "https://polygon-rpc.com",
                "https://rpc.ankr.com/polygon",
                "https://polygon.llamarpc.com",
            ]
            for rpc in rpcs:
                try:
                    r = requests.post(rpc, json={
                        "jsonrpc": "2.0", "method": "eth_call",
                        "params": [{"to": usdc_e, "data": data}, "latest"],
                        "id": 1
                    }, timeout=5)
                    if r.status_code == 200:
                        result = r.json().get("result", "0x0")
                        raw = int(result, 16)
                        return raw / 1e6  # USDC has 6 decimals
                except:
                    continue
        except Exception as e:
            log.debug(f"On-chain RPC balance fail: {e}")
        return None

    def _get_signer_addr(s):
        """Derive the signer address from private key."""
        try:
            from eth_account import Account
            pk = s.c.private_key
            if not pk.startswith("0x"): pk = "0x" + pk
            acct = Account.from_key(pk)
            return acct.address
        except:
            return None

    def get_positions(s):
        """Fetch open positions from Data API."""
        addr = s.c.funder_address or s._get_signer_addr()
        if not addr: return []
        try:
            r = requests.get(f"https://data-api.polymarket.com/positions",
                params={"user": addr}, timeout=8)
            if r.status_code == 200:
                return r.json() if isinstance(r.json(), list) else []
        except Exception as e:
            log.debug(f"Positions fetch: {e}")
        return []

    def prices(s, m):
        """Get midpoint prices — returns dict with 'mid' key."""
        try:
            ymid = s.client.get_midpoint(m.tok_yes)
            nmid = s.client.get_midpoint(m.tok_no)
            # get_midpoint returns dict like {'mid': '0.51'}
            yp = float(ymid["mid"]) if isinstance(ymid, dict) else float(ymid)
            np_ = float(nmid["mid"]) if isinstance(nmid, dict) else float(nmid)
            return yp, np_
        except Exception as e:
            log.debug(f"Midpoint fail: {e}")
            return m.yes_p, m.no_p

    def get_spread(s, token_id):
        """Get spread for a token."""
        try:
            sp = s.client.get_spread(token_id)
            return float(sp["spread"]) if isinstance(sp, dict) else float(sp)
        except: return 0

    def get_best_prices(s, token_id):
        """Get best bid/ask prices."""
        try:
            buy_p = s.client.get_price(token_id, side="BUY")
            sell_p = s.client.get_price(token_id, side="SELL")
            bp = float(buy_p["price"]) if isinstance(buy_p, dict) else float(buy_p)
            sp = float(sell_p["price"]) if isinstance(sell_p, dict) else float(sell_p)
            return bp, sp
        except: return None, None

    def order(s, market, is_yes, price, size):
        """Place a GTC limit order as MAKER (0% fee + rebates).
        Price is set 1-2 cents BELOW midpoint so it rests on the book.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        label = "YES" if is_yes else "NO"

        # Price sanity check — only buy between $0.15 and $0.85
        if price < 0.15 or price > 0.85:
            log.info(f"SKIP: {label} price ${price:.4f} outside safe range $0.15-$0.85")
            return None

        # Set price 1 cent BELOW midpoint to be a MAKER order (0% fee)
        maker_price = round(price - 0.01, 2)
        maker_price = max(0.01, min(maker_price, 0.99))

        # Enforce Polymarket minimum of 5 shares
        size = max(size, 5.0)
        bet = round(maker_price * size, 4)

        if s.c.dry_run:
            oid = f"DRY-{int(time.time()*1000)%99999}"
            log.info(f"DRY: MAKER BET ${bet:.4f} on {label} ({size:.2f}sh @ ${maker_price:.4f})")
            return oid
        if not s.authed: return None
        try:
            tid = market.tok_yes if is_yes else market.tok_no
            signed = s.client.create_order(OrderArgs(
                price=maker_price, size=round(size, 2),
                side=BUY, token_id=tid))
            resp = s.client.post_order(signed, OrderType.GTC)
            oid = resp.get("orderID") or resp.get("id") or "?"
            log.info(f"MAKER ORDER: ${bet:.4f} on {label} ({size:.2f}sh @ ${maker_price:.4f}) id={oid}")
            return oid
        except Exception as e:
            log.error(f"Order fail: {e}"); return None

    def market_order(s, market, is_yes, amount):
        """Place a FOK market order — spends exact dollar amount."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        label = "YES" if is_yes else "NO"
        if s.c.dry_run:
            oid = f"DRY-{int(time.time()*1000)%99999}"
            log.info(f"DRY MARKET: BET ${amount:.4f} on {label}")
            return oid
        if not s.authed: return None
        try:
            tid = market.tok_yes if is_yes else market.tok_no
            mo = MarketOrderArgs(
                token_id=tid, amount=amount,
                side=BUY, order_type=OrderType.FOK)
            signed = s.client.create_market_order(mo)
            resp = s.client.post_order(signed, OrderType.FOK)
            oid = resp.get("orderID") or resp.get("id") or "?"
            log.info(f"MARKET ORDER: BET ${amount:.4f} on {label} id={oid}")
            return oid
        except Exception as e:
            log.error(f"Market order fail: {e}"); return None

    def get_open_orders(s):
        """Get current open orders."""
        if not s.authed: return []
        try:
            from py_clob_client.clob_types import OpenOrderParams
            orders = s.client.get_orders(OpenOrderParams())
            return orders if isinstance(orders, list) else []
        except: return []

    def cancel_all(s):
        if s.c.dry_run or not s.authed: return
        try: s.client.cancel_all()
        except: pass


# ═══════════════════════════════════════════════════════
#  RISK
# ═══════════════════════════════════════════════════════
class Risk:
    def __init__(s, c):
        s.c = c; s.bal = c.starting_balance; s.real_bal = None
        s.dpnl = 0.0; s.tpnl = 0.0; s.total_bet = 0.0
        s.trades = []; s.positions = []
    def set_real(s, b):
        if b is not None and b > 0: s.real_bal = b; s.bal = b; s.c.starting_balance = b
    @property
    def show_bal(s): return s.real_bal if s.real_bal is not None else s.bal
    def ok(s):
        if s.dpnl <= -s.c.max_daily_loss: return False
        if len([p for p in s.positions if p.status == "OPEN"]) >= s.c.max_positions: return False
        return s.bal >= 0.50
    def open(s, t):
        p = Pos(id=t.oid, strat=t.strat, slug=t.slug, side=t.side,
            entry=t.price, shares=t.size/t.price, cost=t.size, opened=t.ts)
        s.positions.append(p); s.trades.append(t)
        s.bal -= t.size; s.total_bet += t.size; return p
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
            if (now - p.opened).total_seconds() > 900:
                op = cp = None
                for x in f.data:
                    if x["t"] >= p.opened.timestamp() and op is None: op = x["p"]
                    cp = x["p"]
                if op and cp:
                    up = cp > op
                    if p.strat == "ARB": s.resolve(p, True)
                    else: s.resolve(p, (up and "YES" in p.side) or (not up and "NO" in p.side))
    def stats(s):
        w = sum(1 for t in s.trades if t.pnl > 0)
        l = sum(1 for t in s.trades if t.pnl < 0)
        return w, l, (w/(w+l)*100 if w+l else 0)


# ═══════════════════════════════════════════════════════
#  COLORED DASHBOARD
# ═══════════════════════════════════════════════════════
class Dash:
    def __init__(s): s.events = deque(maxlen=12)
    def ev(s, m): s.events.append(f"{datetime.now().strftime('%H:%M:%S')} {m}")

    def render(s, cfg, conn, feed, risk, mkt, strats, scores, open_orders=None, poly_positions=None):
        os.system("cls" if os.name == "nt" else "clear")
        w, l, wr = risk.stats()
        mode_c = f"{WARN}DRY RUN{R}" if cfg.dry_run else f"{OK}LIVE{R}"
        now = datetime.now().strftime("%H:%M:%S")

        # ── HEADER ──
        print(f"\n  {H1}{'═'*62}")
        print(f"  {H1}║  {LBL}POLYMARKET BTC 15-MIN BOT v3   {H1}[{mode_c}{H1}]   {DIM}{now}")
        print(f"  {H1}{'═'*62}{R}")

        # ── CONNECTION STATUS (green/red indicators) ──
        print(f"  {H2}  CONNECTION STATUS{R}")
        def ic(v):
            sv = str(v).upper()
            if "OK" in sv: return f"  {OK}✓{R}"
            if "FAIL" in sv: return f"  {ERR}✗{R}"
            return f"  {WARN}○{R}"
        print(f"  {ic(conn.gamma)} {DIM}Gamma API (markets):{R}  {conn.gamma}")
        print(f"  {ic(conn.clob)} {DIM}CLOB API  (orders):{R}  {conn.clob}")
        print(f"  {ic(conn.auth)} {DIM}CLOB Auth (trading):{R} {conn.auth}")
        print(f"  {ic(conn.binance)} {DIM}Binance   (price):{R}  {conn.binance}")
        if conn.can_trade:
            print(f"    {OK}>>> READY TO TRADE <<<{R}")
        else:
            print(f"    {ERR}>>> NOT READY — check auth <<<{R}")
        if conn.errors:
            print(f"    {ERR}Last: {conn.errors[-1]}{R}")

        # ── BALANCE (full decimals, green) ──
        print(f"  {H1}{'─'*62}{R}")
        print(f"  {H2}  ACCOUNT{R}")
        src = f"{OK}LIVE{R}" if risk.real_bal is not None else f"{WARN}estimated{R}"
        print(f"    {LBL}Polymarket Balance:{R} {bal_c(risk.show_bal)} USDC  ({src})")
        print(f"    {LBL}Available:{R} {bal_c(risk.bal)}   {LBL}Starting:{R} ${cfg.starting_balance:.6f}")
        print(f"    {LBL}Daily P&L:{R} {pnl_c2(risk.dpnl)}   {LBL}Total P&L:{R} {pnl_c2(risk.tpnl)}")
        print(f"    {LBL}Total Wagered:{R} {VAL}${risk.total_bet:.4f}{R}   {LBL}Bet Size:{R} {VAL}${cfg.arb_size:.2f}{R}/trade")
        print(f"    {LBL}Trades:{R} {VAL}{len(risk.trades)}{R}   {LBL}W:{OK}{w}{R} {LBL}L:{ERR}{l}{R} {LBL}WR:{VAL}{wr:.0f}%{R}   {LBL}Loss Limit:{R} {ERR}-${cfg.max_daily_loss:.2f}{R}")

        # ── BTC & MARKET (yellow/cyan) ──
        print(f"  {H1}{'─'*62}{R}")
        print(f"  {H2}  MARKET DATA{R}")
        chg1 = feed.chg(60) * 100
        chg_c = f"{OK}{chg1:+.3f}%{R}" if chg1 >= 0 else f"{ERR}{chg1:+.3f}%{R}"
        print(f"    {LBL}BTC:{R} {BTC}${feed.price:,.2f}{R}   {LBL}Samples:{R}{VAL}{feed.n}{R}   {LBL}1min:{R}{chg_c}")
        if mkt:
            tl = max(0, (mkt.end - datetime.now(timezone.utc)).total_seconds())
            pair = mkt.yes_p + mkt.no_p
            pair_c = f"{OK}${pair:.4f}{R}" if pair < 0.98 else f"{WARN}${pair:.4f}{R}"
            print(f"    {LBL}Market:{R} {MKT}{mkt.slug}{R}")
            print(f"    {LBL}YES:{R}{OK}${mkt.yes_p:.4f}{R}  {LBL}NO:{R}{ERR}${mkt.no_p:.4f}{R}  {LBL}SUM:{R}{pair_c}  {LBL}Exp:{R}{VAL}{int(tl//60)}:{int(tl%60):02d}{R}")
        else:
            print(f"    {WARN}Searching for active 15-min market...{R}")

        # ── STRATEGIES (magenta section) ──
        print(f"  {H1}{'─'*62}{R}")
        print(f"  {H2}  STRATEGIES{R}")
        for name, st in strats.items():
            if "ACTIVE" in st:
                print(f"    {OK}●{R} {STRAT}{name:<12s}{R} {OK}{st}{R}")
            else:
                print(f"    {DIM}○{R} {STRAT}{name:<12s}{R} {DIM}{st}{R}")
        if scores:
            sc = "  ".join(f"{k}:{Fore.YELLOW}{v:+.2f}{R}" for k, v in scores.items())
            print(f"    {DIM}Signals:{R} {sc}")

        # ── POSITIONS (blue section) ──
        print(f"  {H1}{'─'*62}{R}")
        op = [p for p in risk.positions if p.status == "OPEN"]
        cl = [p for p in risk.positions if p.status != "OPEN"][-4:]
        at_risk = sum(p.cost for p in op)
        print(f"  {H2}  POSITIONS{R} ({POS}{len(op)} open{R}, {VAL}${at_risk:.4f}{R} at risk)")
        for p in op:
            age = ""
            if p.opened:
                a = (datetime.now(timezone.utc) - p.opened).total_seconds()
                age = f"{int(a//60)}:{int(a%60):02d}"
            sc = OK if "YES" in p.side else ERR
            print(f"    {POS}OPEN{R}  [{STRAT}{p.strat[:6]:6s}{R}] {sc}{p.side:6s}{R} {LBL}BET:{VAL}${p.cost:.4f}{R} @ ${p.entry:.4f}  {DIM}age:{age}{R}")
        for p in cl:
            sc = OK if p.status == "WON" else ERR
            print(f"    {sc}{p.status:4s}{R}  [{STRAT}{p.strat[:6]:6s}{R}] {p.side:6s} {LBL}BET:{VAL}${p.cost:.4f}{R} → {pnl_c2(p.pnl)}")

        # ── TRADE LOG (cyan section) ──
        print(f"  {H1}{'─'*62}{R}")
        print(f"  {H2}  TRADE LOG{R} {DIM}(bet = ${cfg.arb_size:.2f} per trade){R}")
        if risk.trades:
            for t in risk.trades[-5:]:
                ps = pnl_c2(t.pnl) if t.pnl else f"{WARN}pending{R}"
                tt = t.ts.strftime("%H:%M:%S")
                sc = OK if "YES" in t.side else ERR
                print(f"    {DIM}{tt}{R} [{STRAT}{t.strat[:6]:6s}{R}] {sc}{t.side:6s}{R} {LBL}BET:{VAL}${t.size:.4f}{R} @${t.price:.4f}  {ps}")
        else:
            print(f"    {DIM}Waiting for signals...{R}")

        # ── POLYMARKET LIVE ORDERS (yellow section) ──
        if open_orders:
            print(f"  {H1}{'─'*62}{R}")
            print(f"  {H2}  POLYMARKET OPEN ORDERS{R} ({VAL}{len(open_orders)}{R})")
            for o in open_orders[:5]:
                side_c = OK if o.get("side") == "BUY" else ERR
                print(f"    {side_c}{o.get('side','?'):4s}{R} {VAL}${o.get('price','?')}{R} x{o.get('original_size','?')}  {DIM}id:{str(o.get('id',''))[:16]}...{R}")

        # ── POLYMARKET POSITIONS (from Data API — recent only) ──
        if poly_positions:
            # Filter to recent positions only (last 24h)
            recent = []
            now_ts = datetime.now(timezone.utc)
            for pp in poly_positions:
                try:
                    ts = pp.get("createdAt") or pp.get("timestamp") or pp.get("created_at") or ""
                    if ts:
                        from datetime import datetime as dt2
                        pt = dt2.fromisoformat(str(ts).replace("Z", "+00:00"))
                        if (now_ts - pt).total_seconds() < 86400:
                            recent.append(pp)
                    else:
                        recent.append(pp)  # No timestamp = show it
                except:
                    recent.append(pp)
            if recent:
                print(f"  {H1}{'─'*62}{R}")
                print(f"  {H2}  POLYMARKET POSITIONS{R} ({VAL}{len(recent)}{R} recent)")
                for pp in recent[:5]:
                    sz = pp.get("size", 0)
                    avgp = pp.get("avgPrice", 0)
                    side_c = OK if float(sz) > 0 else ERR
                    title = str(pp.get("title", pp.get("slug", "?")))[:35]
                    print(f"    {side_c}{title}{R}  {LBL}Size:{VAL}{sz}{R} @{VAL}${avgp}{R}")

        # ── EVENT LOG (dim section) ──
        print(f"  {H1}{'─'*62}{R}")
        print(f"  {H2}  EVENTS{R}")
        for ev in list(s.events)[-4:]:
            print(f"    {EVT}{ev}{R}")
        print(f"  {H1}{'═'*62}{R}")
        print(f"  {DIM}Ctrl+C to stop{R}\n")


# ═══════════════════════════════════════════════════════
#  MAIN BOT
# ═══════════════════════════════════════════════════════
class Bot:
    def __init__(s, c):
        s.c = c; s.feed = Feed(); s.finder = Finder(c)
        s.ex = Executor(c); s.risk = Risk(c); s.dash = Dash()
        s.conn = Conn(); s.s1 = S1(c); s.s2 = S2(c); s.s3 = S3(c)
        s.strats = {"ARB": "scanning", "LATENCY": "scanning", "MOMENTUM": "collecting"}
        s.cd = {}; s.mkt = None

    def start(s):
        os.system("cls" if os.name == "nt" else "clear")
        print(f"\n  {H1}╔═══════════════════════════════════════════════════╗")
        print(f"  {H1}║  {LBL}POLYMARKET BTC BOT v3 — INITIALIZING{H1}            ║")
        print(f"  {H1}╚═══════════════════════════════════════════════════╝{R}")
        m = f"{WARN}DRY RUN{R}" if s.c.dry_run else f"{OK}LIVE TRADING{R}"
        print(f"  Mode: {m}   Balance: {VAL}${s.c.starting_balance:.6f}{R}\n")

        # 1. Gamma
        print(f"  {H2}[1/4]{R} Testing Gamma API (market data)...")
        if s.finder.test():
            s.conn.gamma = "OK"; print(f"        {OK}✓ Gamma API connected{R}")
        else:
            s.conn.gamma = "FAILED"; print(f"        {ERR}✗ Gamma API failed{R}")

        # 2. CLOB
        print(f"  {H2}[2/4]{R} Testing CLOB API (public)...")
        if s.ex.test_public():
            s.conn.clob = "OK"; print(f"        {OK}✓ CLOB API connected{R}")
        else:
            s.conn.clob = "FAILED"; print(f"        {ERR}✗ CLOB API failed{R}")

        # 3. Auth
        print(f"  {H2}[3/4]{R} Authenticating...")
        if s.c.private_key:
            pk_p = s.c.private_key[:6] + "..." + s.c.private_key[-4:]
            print(f"        {DIM}Key: {pk_p}  Funder: {s.c.funder_address}{R}")
            if s.ex.connect(s.conn):
                print(f"        {OK}✓ Authenticated!{R}")
                # Sync allowances with API
                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                    s.ex.client.update_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                except: pass
                print(f"        {DIM}Fetching balance...{R}")
                rb = s.ex.get_balance()
                if rb is not None:
                    s.risk.set_real(rb)
                    print(f"        {OK}✓ Balance: {bal_c(rb)} USDC{R}")
                else:
                    print(f"        {WARN}⚠ Could not fetch balance, using ${s.c.starting_balance:.6f}{R}")
            else:
                print(f"        {ERR}✗ Auth failed{R}")
                for e in s.conn.errors[-3:]: print(f"          {ERR}{e}{R}")
                if not s.c.dry_run:
                    print(f"\n  {WARN}Get your key: polymarket.com → Cash → ⋮ → Export Private Key{R}")
                    print(f"  {WARN}Or: https://reveal.magic.link/polymarket{R}")
                    print(f"  {WARN}Then run: python setup.py{R}\n")
                    input(f"  {DIM}Press Enter for read-only mode...{R}")
                    s.c.dry_run = True
        else:
            s.conn.auth = "NO KEY"; s.c.dry_run = True
            print(f"        {WARN}⚠ No key — read-only mode{R}")

        # 4. Binance
        print(f"  {H2}[4/4]{R} Testing Binance price feed...")
        for _ in range(5):
            p = s.feed.poll()
            if p:
                s.conn.binance = f"OK — ${p:,.2f}"
                print(f"        {OK}✓ BTC: {BTC}${p:,.2f}{R}"); break
            time.sleep(1)
        else:
            s.conn.binance = "FAILED"; print(f"        {ERR}✗ Price feed failed{R}")

        print(f"\n  {H1}{'═'*55}{R}")
        if s.conn.can_trade and not s.c.dry_run:
            print(f"  {OK}✓ ALL SYSTEMS GO — LIVE TRADING ACTIVE{R}")
        elif s.c.dry_run:
            print(f"  {WARN}○ DRY RUN MODE — simulating trades{R}")
        else:
            print(f"  {ERR}✗ READ-ONLY — fix auth to trade{R}")
        print(f"  {H1}{'═'*55}{R}")
        print(f"  {DIM}Starting in 3 seconds...{R}"); time.sleep(3)
        s.dash.ev("Bot initialized"); s._loop()

    def _loop(s):
        ctr = 0; s._orders = []; s._poly_pos = []
        while True:
            try:
                s.feed.poll(); ctr += 1

                # Resolve expired positions in ALL modes (not just dry run)
                s.risk.check_exp(s.feed)

                # Cancel unfilled orders for expired markets every cycle
                s._cancel_expired_orders()

                # Update YES/NO prices EVERY cycle (2 sec)
                if s.mkt:
                    try: yp, np_ = s.ex.prices(s.mkt); s.mkt.yes_p, s.mkt.no_p = yp, np_
                    except: pass

                # Search for market + run strategies every 5th cycle (10 sec)
                if ctr % 5 == 0:
                    for asset in s.c.assets:
                        m = s.finder.find(asset)
                        if m:
                            s.mkt = m
                            if m.open_btc == 0 and s.feed.price: m.open_btc = s.feed.price
                            if s.conn.can_trade or s.c.dry_run: s._trade(m)

                # Refresh balance + orders + positions every 30 cycles (~1 min)
                if ctr % 30 == 0 and s.ex.authed:
                    rb = s.ex.get_balance()
                    if rb is not None: s.risk.set_real(rb)
                    s._orders = s.ex.get_open_orders()
                    s._poly_pos = s.ex.get_positions()
                    s._check_order_status()

                s.dash.render(s.c, s.conn, s.feed, s.risk, s.mkt, s.strats,
                    s.s3.scores if hasattr(s.s3, 'scores') else {},
                    s._orders, s._poly_pos)
                time.sleep(s.c.poll_sec)
            except KeyboardInterrupt:
                s.ex.cancel_all(); s._summary(); break
            except Exception as e:
                log.error(f"Loop: {e}\n{traceback.format_exc()}")
                s.dash.ev(f"Err: {str(e)[:40]}"); time.sleep(3)

    def _cancel_expired_orders(s):
        """Cancel unfilled orders before expiry and resolve expired positions."""
        now = datetime.now(timezone.utc)

        # Cancel all open orders if current market expires in < 2 min
        if s.mkt:
            tl = (s.mkt.end - now).total_seconds()
            if tl < 120 and tl > 0 and s._orders:
                try:
                    s.ex.cancel_all()
                    s.dash.ev(f"Cancelled orders — market expires in {int(tl)}s")
                    log.info(f"Cancelled orders, market expires in {tl:.0f}s")
                    s._orders = []
                except: pass

        # Resolve positions older than 16 min (market expired)
        for p in s.risk.positions:
            if p.status != "OPEN": continue
            if not p.opened: continue
            age = (now - p.opened).total_seconds()
            if age > 960:
                op = cp = None
                for x in s.feed.data:
                    if x["t"] >= p.opened.timestamp() and op is None: op = x["p"]
                    cp = x["p"]
                if op and cp:
                    up = cp > op
                    if p.strat == "ARB":
                        s.risk.resolve(p, True)
                    else:
                        won = (up and "YES" in p.side) or (not up and "NO" in p.side)
                        s.risk.resolve(p, won)
                    s.dash.ev(f"[{p.strat[:3]}] {p.status} {p.side} P&L:{p.pnl:+.2f}")
                    log.info(f"Resolved {p.strat} {p.side}: {p.status} P&L=${p.pnl:.4f}")
                else:
                    if age > 1200:
                        s.risk.resolve(p, False)
                        s.dash.ev(f"[{p.strat[:3]}] EXPIRED {p.side}")
                        log.info(f"Expired {p.strat} {p.side}: no price data")

    def _check_order_status(s):
        """Check if open orders were filled or need cancelling."""
        if not s.ex.authed: return
        try:
            open_oids = set()
            for o in s._orders:
                oid = o.get("id", "")
                if oid: open_oids.add(oid)

            # If a position's order is no longer in open orders,
            # it was either filled or cancelled
            for p in s.risk.positions:
                if p.status != "OPEN": continue
                if p.id and p.id.startswith("DRY"): continue
                # If order not in open orders and position is old enough
                if p.id not in open_oids and p.opened:
                    age = (datetime.now(timezone.utc) - p.opened).total_seconds()
                    if age > 60:
                        log.info(f"Order {p.id[:20]} no longer open — may be filled or cancelled")
        except Exception as e:
            log.debug(f"Order status check: {e}")

    def _trade(s, m):
        if not s.risk.ok(): return
        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 60: return

        # Check real available balance before trading
        available = s.risk.show_bal - sum(
            p.cost for p in s.risk.positions if p.status == "OPEN")
        if available < 1.0:
            return  # Not enough free balance

        # Only one trade per market — don't stack bets
        open_slugs = set(p.slug for p in s.risk.positions if p.status == "OPEN")
        if m.slug in open_slugs:
            return  # Already have a position in this market

        # S1: Parity Arb
        sig = s.s1.check(m)
        if sig:
            if available < sig["sz"]:
                s.strats["ARB"] = f"signal! but need ${sig['sz']:.2f} (have ${available:.2f})"
            else:
                s.strats["ARB"] = f"ACTIVE pair=${sig['pair']:.4f} +${sig['profit']:.4f}"
                s.dash.ev(f"[ARB] BETTING ${sig['sz']:.4f} on YES+NO (pair=${sig['pair']:.4f})")
                oy = s.ex.order(m, True, sig["yp"], sig["sh"]/2)
                on = s.ex.order(m, False, sig["np"], sig["sh"]/2)
                if oy:
                    t = Trd(datetime.now(timezone.utc), "ARB", m.slug, "YES+NO",
                        sig["pair"], sig["sz"], oid=oy)
                    s.risk.open(t)
                return
        s.strats["ARB"] = f"sum=${m.yes_p+m.no_p:.4f}"

        sig = s.s2.check(m, s.feed)
        if sig and time.time() - s.cd.get("lat", 0) > 30:
            price = m.yes_p if sig["yes"] else m.no_p
            if price < 0.15 or price > 0.85:
                s.strats["LATENCY"] = f"signal! price ${price:.2f} outside safe range"
            else:
                trade_sz = min(sig["sz"], available)
                if trade_sz < 0.50:
                    s.strats["LATENCY"] = f"signal! need ${sig['sz']:.2f} (have ${available:.2f})"
                else:
                    s.strats["LATENCY"] = f"ACTIVE {sig['dir']} edge={sig['edge']*100:.1f}%"
                    sh = max(trade_sz / price, 5.0)
                    actual_cost = sh * price
                    if actual_cost > available:
                        sh = available / price
                    s.dash.ev(f"[LAT] BETTING ${trade_sz:.4f} on {sig['dir']} (BTC {sig['chg']*100:+.2f}%)")
                    oid = s.ex.order(m, sig["yes"], price, sh)
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "LATENCY", m.slug,
                            sig["dir"], price, trade_sz, oid=oid)
                        s.risk.open(t); s.cd["lat"] = time.time()
                    return
        s.strats["LATENCY"] = f"btc {s.feed.chg(60)*100:+.2f}%"

        sig = s.s3.check(m, s.feed)
        if sig and time.time() - s.cd.get("mom", 0) > 60:
            price = m.yes_p if sig["yes"] else m.no_p
            if price < 0.15 or price > 0.85:
                s.strats["MOMENTUM"] = f"signal! price ${price:.2f} outside safe range"
            else:
                trade_sz = min(sig["sz"], available)
                if trade_sz < 0.50:
                    s.strats["MOMENTUM"] = f"signal! need ${sig['sz']:.2f} (have ${available:.2f})"
                else:
                    s.strats["MOMENTUM"] = f"ACTIVE {sig['dir']} {sig['conf']:.0%}"
                    sh = max(trade_sz / price, 5.0)
                    actual_cost = sh * price
                    if actual_cost > available:
                        sh = available / price
                    s.dash.ev(f"[MOM] BETTING ${trade_sz:.4f} on {sig['dir']} (conf={sig['conf']:.0%})")
                    oid = s.ex.order(m, sig["yes"], price, sh)
                    if oid:
                        t = Trd(datetime.now(timezone.utc), "MOMENTUM", m.slug,
                            sig["dir"], price, trade_sz, oid=oid)
                        s.risk.open(t); s.cd["mom"] = time.time()
                    return
        s.strats["MOMENTUM"] = f"samples:{s.feed.n}"

    def _summary(s):
        os.system("cls" if os.name == "nt" else "clear")
        w, l, wr = s.risk.stats()
        print(f"\n{H1}{'═'*60}")
        print(f"  {LBL}SESSION SUMMARY{R}")
        print(f"{H1}{'═'*60}{R}")
        print(f"  {LBL}Polymarket Balance:{R} {bal_c(s.risk.show_bal)} USDC")
        print(f"  {LBL}Session P&L:{R}  {pnl_c2(s.risk.tpnl)}")
        print(f"  {LBL}Total Wagered:{R} {VAL}${s.risk.total_bet:.4f}{R}  ({len(s.risk.trades)} bets @ ${s.c.arb_size:.2f})")
        print(f"  {LBL}Trades:{R} {len(s.risk.trades)}  {LBL}W:{OK}{w}{R} {LBL}L:{ERR}{l}{R} {LBL}WR:{VAL}{wr:.0f}%{R}")
        print(f"  {H1}{'─'*60}{R}")
        for st in ["ARB", "LATENCY", "MOMENTUM"]:
            tt = [t for t in s.risk.trades if t.strat == st]; tw = sum(t.size for t in tt)
            print(f"  {STRAT}{st:12s}{R}: {VAL}{len(tt)}{R} trades  Wagered:{VAL}${tw:.4f}{R}  P&L:{pnl_c2(sum(t.pnl for t in tt))}")
        if s.risk.trades:
            print(f"\n  {DIM}{'Time':<10} {'Strat':<8} {'Side':<6} {'Bet':>8} {'Price':>8} {'P&L':>10}{R}")
            print(f"  {DIM}{'─'*54}{R}")
            for t in s.risk.trades[-10:]:
                ps = pnl_c2(t.pnl) if t.pnl else f"{WARN}pend{R}"
                sc = OK if "YES" in t.side else ERR
                print(f"  {DIM}{t.ts.strftime('%H:%M:%S')}{R}  {STRAT}{t.strat[:7]:<8}{R}{sc}{t.side:<6}{R} {VAL}${t.size:.4f}{R}  ${t.price:.4f}  {ps}")
        print(f"{H1}{'═'*60}{R}")
        print(f"  {DIM}Log: polybot.log{R}\n")


# ═══════════════════════════════════════════════════════
def main():
    cfg = Config.from_env()
    pk = cfg.private_key
    if not pk:
        print(f"\n  {ERR}╔═══════════════════════════════════════════════════╗")
        print(f"  {ERR}║  {LBL}NO PRIVATE KEY FOUND{ERR}                            ║")
        print(f"  {ERR}╠═══════════════════════════════════════════════════╣")
        print(f"  {ERR}║{R}  Run: {OK}python setup.py{R}                            {ERR}║")
        print(f"  {ERR}║{R}  Or get key: Cash → ⋮ → Export Private Key       {ERR}║")
        print(f"  {ERR}║{R}  Or: {MKT}https://reveal.magic.link/polymarket{R}       {ERR}║")
        print(f"  {ERR}╚═══════════════════════════════════════════════════╝{R}\n")
        sys.exit(1)
    if " " in pk or "_" in pk:
        print(f"\n  {ERR}WRONG KEY FORMAT — you pasted a seed phrase (words).{R}")
        print(f"  {LBL}The bot needs a hex private key (only 0-9, a-f).{R}")
        print(f"  {LBL}Get it: Cash → ⋮ → Export Private Key{R}")
        print(f"  {LBL}Or run: python setup.py{R}\n"); sys.exit(1)
    try: int(pk, 16)
    except ValueError:
        print(f"\n  {ERR}Key has invalid characters. Only 0-9 and a-f allowed.{R}")
        print(f"  {LBL}Run: python setup.py{R}\n"); sys.exit(1)
    Bot(cfg).start()

if __name__ == "__main__":
    main()
