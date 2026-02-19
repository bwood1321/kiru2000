"""
POLYMARKET BTC BOT v10 — THE MONEY PRINTER
One strategy. Maker orders. Every market. Built to make money.

The $438K bot proved it: simplicity wins.
- Watch BTC on Binance (fast feed)
- When BTC confirms a direction → buy the winning side
- Post maker orders (0% fee)
- Collect $1.00 at expiry
- Repeat 50+ times per day

pip install py-clob-client python-dotenv requests numpy websocket-client web3 colorama
"""
import os, sys, time, json, logging, traceback, math, threading, gc
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from collections import deque
from typing import Optional
import requests, numpy as np
from dotenv import load_dotenv
load_dotenv()

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "colorama", "-q"])
    from colorama import init, Fore, Style
    init(autoreset=True)

try:
    import websocket as _ws_lib
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

# ─── COLORS ───
C  = Fore.CYAN + Style.BRIGHT     # headers
M  = Fore.MAGENTA + Style.BRIGHT  # accent
W  = Fore.WHITE + Style.BRIGHT    # labels
Y  = Fore.YELLOW + Style.BRIGHT   # values/BTC
G  = Fore.GREEN + Style.BRIGHT    # positive
R  = Fore.RED + Style.BRIGHT      # negative
D  = Fore.WHITE + Style.DIM       # dim
X  = Style.RESET_ALL              # reset

def pnl_c(v):
    if v > 0: return f"{G}+${v:.2f}{X}"
    if v < 0: return f"{R}-${abs(v):.2f}{X}"
    return f"${v:.2f}"

# ─── LOGGING ───
log = logging.getLogger("v10")
log.setLevel(logging.DEBUG)
from logging.handlers import RotatingFileHandler
_fh = RotatingFileHandler("v10.log", maxBytes=5*1024*1024, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s|%(levelname)s|%(message)s"))
log.addHandler(_fh)


# ═══════════════════════════════════════════════════════════════
# ─── CONFIG ───
# ═══════════════════════════════════════════════════════════════
@dataclass
class Config:
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 1
    dry_run: bool = False

    # ─── STRATEGY PARAMS ───
    # Trend Rider: buy confirmed direction
    min_move: float = 0.0012       # 0.12% BTC move to trigger (from open)
    max_price: float = 0.88        # max price to pay for winning side
    min_time: int = 60             # earliest entry: 60s into market (speed edge is early)
    max_time: int = 780            # latest entry: 13 min (120s before expiry)

    # Scalp: near-certain outcomes in final minutes
    scalp_min_move: float = 0.002  # 0.20% BTC move for scalp
    scalp_price_lo: float = 0.78   # winning side must be $0.78-$0.93
    scalp_price_hi: float = 0.93
    scalp_window: int = 240        # last 4 minutes

    # Sizing
    trade_size_pct: float = 0.04   # 4% of balance per trade
    max_trade_pct: float = 0.05    # hard cap: 5% of balance (scales with growth)
    min_trade_usd: float = 5.0     # minimum to bother
    max_open_trades: int = 2       # max simultaneous positions

    # Risk
    cooldown_losses: int = 4       # reduce size after 4 consecutive losses
    cooldown_markets: int = 3      # stay reduced for 3 markets, then restore
    max_daily_loss: float = 2000.0 # stop if down $2000 in session
    profit_protect_pct: float = 0.50  # if up 50%+ of daily profit, tighten size

    # Polling
    poll_sec: int = 2

    @classmethod
    def from_env(cls):
        pk = os.getenv("PRIVATE_KEY", "")
        clean = pk[2:] if pk.startswith("0x") else pk
        return cls(
            private_key=clean,
            funder_address=os.getenv("FUNDER_ADDRESS", ""),
            signature_type=int(os.getenv("SIGNATURE_TYPE", "1")),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            trade_size_pct=float(os.getenv("TRADE_SIZE_PCT", "0.04")),
            max_trade_pct=float(os.getenv("MAX_TRADE_PCT", "0.05")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "2000")),
        )


# ═══════════════════════════════════════════════════════════════
# ─── DATA TYPES ───
# ═══════════════════════════════════════════════════════════════
@dataclass
class Market:
    slug: str; cid: str; question: str
    tok_yes: str; tok_no: str; end: datetime
    yes_p: float = 0.5; no_p: float = 0.5
    active: bool = True; open_btc: float = 0.0

@dataclass
class Trade:
    ts: datetime; side: str; price: float; cost: float
    mode: str; slug: str; oid: str = ""
    pnl: float = 0.0; status: str = "OPEN"
    market_end: datetime = None; shares: float = 0.0


# ═══════════════════════════════════════════════════════════════
# ─── BTC PRICE FEED (Binance WebSocket) ───
# ═══════════════════════════════════════════════════════════════
class Feed:
    def __init__(s):
        s.data = deque(maxlen=1000)
        s._ws_alive = False; s._ws_last = 0; s._ws_retries = 0
        s._http = requests.Session()
        s._http.headers["User-Agent"] = "PolyBot/10"
        if _HAS_WS:
            threading.Thread(target=s._ws_loop, daemon=True).start()

    def _ws_loop(s):
        while True:
            try:
                ws = _ws_lib.WebSocketApp(
                    "wss://stream.binance.com:9443/ws/btcusdt@trade",
                    on_message=s._on_msg,
                    on_error=lambda ws, e: None,
                    on_close=lambda ws, c, m: setattr(s, '_ws_alive', False),
                    on_open=lambda ws: setattr(s, '_ws_alive', True))
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except: pass
            s._ws_alive = False
            s._ws_retries += 1
            time.sleep(min(5 * s._ws_retries, 30))

    def _on_msg(s, ws, msg):
        try:
            p = float(json.loads(msg).get("p", 0))
            if p > 0:
                now = time.time()
                if now - s._ws_last >= 0.2:
                    s.data.append({"t": now, "p": p})
                    s._ws_last = now
                    s._ws_retries = 0
        except: pass

    def poll(s):
        if s._ws_alive and s._ws_last > 0 and (time.time() - s._ws_last) < 5:
            return
        # Try multiple HTTP sources
        sources = [
            ("Binance", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
             lambda r: float(r.json()["price"])),
            ("Coinbase", "https://api.coinbase.com/v2/prices/BTC-USD/spot",
             lambda r: float(r.json()["data"]["amount"])),
            ("Bybit", "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT",
             lambda r: float(r.json()["result"]["list"][0]["lastPrice"])),
            ("Kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
             lambda r: float(list(r.json()["result"].values())[0]["c"][0])),
        ]
        for name, url, parser in sources:
            try:
                r = s._http.get(url, timeout=3)
                p = parser(r)
                if p and p > 1000:
                    s.data.append({"t": time.time(), "p": p})
                    return
            except: continue

    @property
    def price(s):
        snap = list(s.data)
        return snap[-1]["p"] if snap else 0
    @property
    def n(s): return len(s.data)
    @property
    def source(s): return "WS" if s._ws_alive and (time.time() - s._ws_last) < 5 else "HTTP"

    def chg(s, sec=60):
        """BTC price change over last N seconds as a ratio."""
        snap = list(s.data)
        if len(snap) < 2: return 0
        now = snap[-1]; cut = now["t"] - sec
        old = snap[0]
        for p in snap:
            if p["t"] >= cut: old = p; break
        return (now["p"] - old["p"]) / old["p"] if old["p"] else 0

    def chg_from(s, ref_price):
        """BTC change from a reference price."""
        snap = list(s.data)
        if not snap or ref_price <= 0: return 0
        return (snap[-1]["p"] - ref_price) / ref_price


# ═══════════════════════════════════════════════════════════════
# ─── POLYMARKET WEBSOCKET (token prices) ───
# ═══════════════════════════════════════════════════════════════
class PolyWS:
    def __init__(s):
        s.yes_p = 0.0; s.no_p = 0.0
        s._alive = False; s._thread = None
        s._ws = None; s._tokens = []; s._last = 0

    def subscribe(s, tok_yes, tok_no):
        s._tokens = [tok_yes, tok_no]
        s.yes_p = 0.0; s.no_p = 0.0
        if not _HAS_WS: return  # no websocket library
        if s._ws:
            try: s._ws.close()
            except: pass
        if not s._thread or not s._thread.is_alive():
            s._thread = threading.Thread(target=s._loop, daemon=True)
            s._thread.start()

    def _loop(s):
        while True:
            try:
                ws = _ws_lib.WebSocketApp(
                    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                    on_message=s._on_msg,
                    on_error=lambda ws, e: None,
                    on_close=lambda ws, c, m: setattr(s, '_alive', False),
                    on_open=s._on_open)
                s._ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except: pass
            s._alive = False
            time.sleep(5)

    def _on_open(s, ws):
        s._alive = True
        if s._tokens:
            ws.send(json.dumps({"assets_ids": s._tokens, "type": "market"}))
            log.info(f"PolyWS subscribed to {len(s._tokens)} tokens")

    def _on_msg(s, ws, msg):
        try:
            d = json.loads(msg)
            s._last = time.time()
            et = d.get("event_type", "")
            if et == "price_change":
                for ch in d.get("changes", []):
                    price = float(ch.get("price", 0))
                    aid = d.get("asset_id", "")
                    if aid == s._tokens[0]: s.yes_p = price
                    elif len(s._tokens) > 1 and aid == s._tokens[1]: s.no_p = price
            elif et == "last_trade_price":
                price = float(d.get("price", 0))
                aid = d.get("asset_id", "")
                if price > 0:
                    if aid == s._tokens[0]: s.yes_p = price
                    elif len(s._tokens) > 1 and aid == s._tokens[1]: s.no_p = price
        except: pass

    @property
    def live(s): return s._alive and s._last > 0 and (time.time() - s._last) < 10


# ═══════════════════════════════════════════════════════════════
# ─── MARKET FINDER (from v9 — proven working) ───
# ═══════════════════════════════════════════════════════════════
class Finder:
    def __init__(s, host):
        s.host = host
        s._http = requests.Session()
        s._http.headers["User-Agent"] = "PolyBot/10"
        s._cache = {}
        s._last_market = None

    def find(s):
        """Find the current active BTC 15-min market."""
        now = datetime.now(timezone.utc)
        mb = (now.minute // 15) * 15
        base = now.replace(minute=mb, second=0, microsecond=0)
        for off in [0, -15, 15, -30]:
            ts = int((base + timedelta(minutes=off)).timestamp())
            slug = f"btc-updown-15m-{ts}"
            m = s._get(slug)
            if m and m.active:
                tl = (m.end - now).total_seconds()
                if tl > 30:
                    s._last_market = m
                    return m
        # Return cached if still valid
        if s._last_market:
            tl = (s._last_market.end - now).total_seconds()
            if tl > 30 and s._last_market.active:
                return s._last_market
        return None

    def _get(s, slug):
        """Fetch market by slug using query params (Gamma API format)."""
        if slug in s._cache:
            cached = s._cache[slug]
            tl = (cached.end - datetime.now(timezone.utc)).total_seconds()
            if tl < -60:
                s._cache.pop(slug, None)
                return None
            return cached
        try:
            r = s._http.get(f"{s.host}/markets", params={"slug": slug}, timeout=8)
            if r.status_code != 200: return None
            d = r.json()
            if isinstance(d, list): d = d[0] if d else None
            if not d or not (d.get("condition_id") or d.get("conditionId")): return None
            m = s._parse(d)
            if m:
                s._cache[slug] = m
                # Clean old cache
                if len(s._cache) > 20:
                    now = datetime.now(timezone.utc)
                    expired = [k for k, v in s._cache.items()
                               if (v.end - now).total_seconds() < -300]
                    for k in expired: s._cache.pop(k, None)
            return m
        except Exception as e:
            log.debug(f"Finder._get({slug}): {e}")
            return None

    def _parse(s, d):
        """Parse a Gamma API market dict into a Market object."""
        try:
            tok = d.get("clobTokenIds") or d.get("clob_token_ids") or ""
            if isinstance(tok, str):
                tok = json.loads(tok) if tok.startswith("[") else tok.split(",")
            if not tok or len(tok) < 2: return None
            pr = d.get("outcomePrices") or d.get("outcome_prices") or ""
            if isinstance(pr, str):
                try: pr = json.loads(pr)
                except: pr = [0.5, 0.5]
            ed = d.get("endDate") or d.get("end_date_iso") or ""
            try: et = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            except: et = datetime.now(timezone.utc) + timedelta(minutes=15)
            return Market(
                slug=d.get("slug", ""), cid=d.get("condition_id") or d.get("conditionId", ""),
                question=d.get("question", ""), tok_yes=tok[0].strip().strip('"'),
                tok_no=tok[1].strip().strip('"'), end=et,
                yes_p=float(pr[0]) if pr else 0.5, no_p=float(pr[1]) if len(pr) > 1 else 0.5,
                active=not d.get("closed", False))
        except:
            return None

    def check_resolution(s, slug):
        """Check if a market has resolved. Returns outcome prices or None."""
        try:
            r = s._http.get(f"{s.host}/markets", params={"slug": slug}, timeout=5)
            if r.status_code != 200: return None
            d = r.json()
            if isinstance(d, list): d = d[0] if d else None
            if not d: return None
            if d.get("closed") or d.get("resolved"):
                pr = d.get("outcomePrices") or d.get("outcome_prices") or ""
                if isinstance(pr, str):
                    try: pr = json.loads(pr)
                    except: return None
                if len(pr) >= 2:
                    return float(pr[0]) > 0.5  # True = YES won
            return None
        except:
            return None


# ═══════════════════════════════════════════════════════════════
# ─── EXECUTOR (ORDER PLACEMENT) ───
# ═══════════════════════════════════════════════════════════════
class Executor:
    def __init__(s, c):
        s.c = c; s.client = None; s.authed = False; s._signer = None

    def connect(s):
        from py_clob_client.client import ClobClient
        pk = s.c.private_key
        if not pk: return False
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
                s.client = client; s.authed = True
                log.info(f"Auth OK type={st}")
                return True
            except Exception as e:
                log.debug(f"Auth type={st} fail: {e}")
        return False

    def test(s):
        try:
            from py_clob_client.client import ClobClient
            return str(ClobClient(s.c.clob_host).get_ok()).upper() in ["OK", "TRUE"]
        except: return False

    def balance(s):
        if s.authed:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                r = s.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                if isinstance(r, dict) and "balance" in r:
                    b = int(r["balance"]) / 1e6
                    if b > 0: return b
            except: pass
        # Fallback: on-chain check
        for addr in [s.c.funder_address, s._get_signer()]:
            if not addr: continue
            try:
                usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                clean = addr.lower().replace("0x", "").zfill(64)
                data = "0x70a08231" + clean
                for rpc in ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]:
                    r = requests.post(rpc, json={"jsonrpc": "2.0", "method": "eth_call",
                        "params": [{"to": usdc, "data": data}, "latest"], "id": 1}, timeout=5)
                    if r.status_code == 200:
                        result = r.json().get("result", "0x0")
                        if result and result != "0x":
                            b = int(result, 16) / 1e6
                            if b > 0: return b
            except: continue
        return None

    def _get_signer(s):
        if s._signer: return s._signer
        try:
            from eth_account import Account
            pk = s.c.private_key
            if not pk.startswith("0x"): pk = "0x" + pk
            s._signer = Account.from_key(pk).address
        except: pass
        return s._signer

    def order(s, market, is_yes, price, shares, mode="hybrid"):
        """Place order. Returns (order_id, actual_shares) or (None, None)."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        label = "YES" if is_yes else "NO"
        tid = market.tok_yes if is_yes else market.tok_no
        dollar = round(price * shares, 2)

        if s.c.dry_run:
            oid = f"DRY-{int(time.time()*1000)%99999}"
            log.info(f"DRY [{mode}]: ${dollar:.2f} {label} @ {price}")
            return oid, shares

        if not s.authed: return None, None

        if mode == "hybrid":
            return s._hybrid(tid, label, price, shares, dollar)
        else:
            return s._taker(tid, label, price, shares, dollar)

    def _hybrid(s, tid, label, price, shares, dollar):
        """Try maker first (8s), fallback to taker."""
        from py_clob_client.clob_types import OrderArgs, OrderType, OpenOrderParams, MarketOrderArgs
        from py_clob_client.order_builder.constants import BUY

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
        except: pass
        maker_price = round(max(0.01, min(maker_price, 0.99)), 2)
        limit_shares = max(shares, 5.0)

        try:
            signed = s.client.create_order(OrderArgs(
                price=maker_price, size=round(limit_shares, 2), side=BUY, token_id=tid))
            resp = s.client.post_order(signed, OrderType.GTC)
            oid = s._parse_oid(resp)
            if oid:
                deadline = time.time() + 8
                while time.time() < deadline:
                    time.sleep(1.5)
                    try:
                        orders = s.client.get_orders(OpenOrderParams())
                        still_open = any(
                            (o.get("id") == oid or o.get("orderID") == oid)
                            for o in (orders if isinstance(orders, list) else []))
                        if not still_open:
                            log.info(f"MAKER FILLED: {label} @ ${maker_price} id={oid}")
                            return oid, limit_shares
                    except: pass
                try: s.client.cancel(order_id=oid)
                except: pass
                log.info(f"Maker unfilled → taker fallback")
        except Exception as e:
            log.error(f"Maker fail: {e}")

        return s._taker(tid, label, price, shares, dollar)

    def _taker(s, tid, label, price, shares, dollar):
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        try:
            mo = MarketOrderArgs(token_id=tid, amount=max(dollar, 0.50), side=BUY)
            signed = s.client.create_market_order(mo)
            resp = s.client.post_order(signed, OrderType.FOK)
            oid = s._parse_oid(resp)
            if oid:
                log.info(f"TAKER: ${dollar:.2f} {label} @ {price} id={oid}")
                return oid, shares
        except Exception as e:
            err = str(e)
            log.error(f"Taker fail: {err}")
            if "restricted" in err.lower() or "403" in err:
                log.error("⚠ GEOBLOCK DETECTED — trading restricted in this region!")
        return None, None

    def _parse_oid(s, resp):
        if isinstance(resp, dict):
            oid = resp.get("orderID") or resp.get("id") or resp.get("order_id")
            return oid if oid else None
        elif isinstance(resp, str) and len(resp) > 5:
            return resp
        return None

    def cancel_all(s):
        if s.c.dry_run or not s.authed: return
        try: s.client.cancel_all()
        except: pass

    def get_open_orders(s):
        if not s.authed: return []
        try:
            from py_clob_client.clob_types import OpenOrderParams
            r = s.client.get_orders(OpenOrderParams())
            return r if isinstance(r, list) else []
        except: return []

    def redeem(s, condition_ids):
        """Redeem resolved positions."""
        if s.c.dry_run or not condition_ids: return []
        redeemed = []
        # Method 1: CLOB client
        if s.authed and s.client:
            for cid in condition_ids:
                for method in ["redeem", "redeem_positions", "redeemPositions"]:
                    try:
                        fn = getattr(s.client, method, None)
                        if fn and fn(cid):
                            redeemed.append(cid); time.sleep(3); break
                    except: continue
        # Method 2: On-chain
        remaining = [c for c in condition_ids if c not in redeemed]
        if remaining:
            try:
                from web3 import Web3
                from eth_account import Account
                pk = s.c.private_key
                if not pk.startswith("0x"): pk = "0x" + pk
                signer = Account.from_key(pk).address
                w3 = None
                for rpc in ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]:
                    try:
                        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                        if w3.is_connected(): break
                    except: continue
                if not w3 or not w3.is_connected(): return redeemed
                ctf_addr = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
                usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                abi = json.loads('[{"constant":false,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"name":"conditionId","type":"bytes32"}],"name":"payoutDenominator","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
                ctf = w3.eth.contract(address=Web3.to_checksum_address(ctf_addr), abi=abi)
                for cid in remaining:
                    try:
                        cid_bytes = bytes.fromhex(cid.replace("0x", ""))
                        if ctf.functions.payoutDenominator(cid_bytes).call() == 0: continue
                        nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(signer))
                        txn = ctf.functions.redeemPositions(
                            Web3.to_checksum_address(usdc_addr),
                            b'\x00' * 32, cid_bytes, [1, 2]
                        ).build_transaction({
                            'from': Web3.to_checksum_address(signer), 'nonce': nonce,
                            'gas': 250000, 'gasPrice': w3.eth.gas_price, 'chainId': 137})
                        signed_tx = w3.eth.account.sign_transaction(txn, pk)
                        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                        if receipt.status == 1:
                            redeemed.append(cid)
                            log.info(f"REDEEMED on-chain {cid[:16]}")
                        time.sleep(5)
                    except Exception as e:
                        log.debug(f"Redeem fail: {e}")
            except Exception as e:
                log.debug(f"Redeem setup fail: {e}")
        return redeemed


# ═══════════════════════════════════════════════════════════════
# ─── DASHBOARD ───
# ═══════════════════════════════════════════════════════════════
class Dashboard:
    def __init__(s):
        s.events = deque(maxlen=8)

    def ev(s, msg):
        s.events.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        log.info(msg)

    def render(s, bot):
        os.system("clear" if os.name != "nt" else "cls")
        now = datetime.now().strftime("%H:%M:%S")
        rt = str(timedelta(seconds=int(time.time() - bot.start_time)))
        bal = bot.balance
        session_pnl = bot.balance - bot.start_bal if bot.start_bal else 0

        # Win/loss tracking
        wins = sum(1 for t in bot.trades if t.status == "WON")
        losses = sum(1 for t in bot.trades if t.status == "LOST")
        total = wins + losses
        wr = (wins / total * 100) if total > 0 else 0

        print(f"\n  {C}╔{'═'*60}╗{X}")
        print(f"  {C}║{X}  {M}◆ POLYMARKET v10{X} {D}— THE MONEY PRINTER{X}     {D}{now}  ⏱  {rt}{X}  {C}║{X}")
        print(f"  {C}╚{'═'*60}╝{X}")

        # Balance bar
        print(f"\n  {W}Balance{X}  {Y}${bal:,.2f}{X}     {W}Session{X}  {pnl_c(session_pnl)}     {W}Record{X}  {G}{wins}W{X}/{R}{losses}L{X} ({wr:.0f}%)")

        # Open risk
        open_risk = sum(t.cost for t in bot.trades if t.status == "OPEN")
        open_count = sum(1 for t in bot.trades if t.status == "OPEN")
        print(f"  {W}At Risk{X}  {Y}${open_risk:,.2f}{X} ({open_count} open)     {W}Available{X}  {Y}${max(0, bal - open_risk):,.2f}{X}")

        # BTC
        f = bot.feed
        if f.n > 0:
            chg1 = f.chg(60) * 100
            chg5 = f.chg(300) * 100
            src = f.source
            c1 = G if chg1 >= 0 else R
            c5 = G if chg5 >= 0 else R
            print(f"\n  {W}BTC{X}  {Y}${f.price:,.2f}{X}  {D}({src}){X}     {c1}{chg1:+.3f}%/1m{X}  {c5}{chg5:+.3f}%/5m{X}")

        # Market
        m = bot.market
        if m:
            tl = (m.end - datetime.now(timezone.utc)).total_seconds()
            chg_open = f.chg_from(m.open_btc) * 100 if m.open_btc else 0
            direction = f"{G}▲ UP{X}" if chg_open > 0.05 else f"{R}▼ DOWN{X}" if chg_open < -0.05 else f"{D}— FLAT{X}"
            poly_src = "WS" if bot.poly_ws.live else "API"

            print(f"\n  {W}Market{X}  {D}{m.slug[-25:]}{X}     {W}Expires{X}  {Y}{tl:.0f}s{X}")
            print(f"  {W}YES{X}  {Y}${m.yes_p:.2f}{X}     {W}NO{X}  {Y}${m.no_p:.2f}{X}     {D}({poly_src}){X}")
            print(f"  {W}BTC Move{X}  {Y}{chg_open:+.3f}%{X}  {direction}")

            # Signal status
            signal = bot.last_signal
            if signal:
                print(f"\n  {M}► SIGNAL{X}  {signal}")
            elif bot.traded_this_market:
                print(f"\n  {D}  Already traded this market{X}")
            elif tl < 30:
                print(f"\n  {D}  Market expiring...{X}")
            elif tl > 870:
                print(f"\n  {D}  Waiting for next market...{X}")
            else:
                why = bot.block_reason or "Scanning..."
                # Show edge info when available
                if bot.feed.n > 0 and m.open_btc > 0:
                    chg_abs = abs(chg_open) / 100  # chg_open is in %, convert to ratio
                    elapsed = 900 - tl
                    implied = min(0.93, 0.55 + chg_abs * 200)
                    if elapsed > 300: implied = min(0.93, implied + 0.03)
                    winning = m.no_p if chg_open < 0 else m.yes_p
                    edge_val = implied - winning
                    edge_color = G if edge_val >= 0.05 else Y if edge_val >= 0 else R
                    print(f"\n  {D}  {why}{X}")
                    print(f"  {D}  Implied: {implied:.0%}  Price: ${winning:.2f}  Edge: {edge_color}{edge_val*100:+.1f}%{X}")
                else:
                    print(f"\n  {D}  {why}{X}")
        else:
            print(f"\n  {D}  No active market{X}")

        # Cooldown / size reduction
        if bot._reduced_markets > 0:
            print(f"\n  {Y}⚠ REDUCED SIZE{X}  {Y}{bot._reduced_markets} markets remaining{X} (after {bot._consec_losses} losses)")
        elif bot._consec_losses >= 2:
            print(f"\n  {D}  Loss streak: {bot._consec_losses} (reduce at {bot.c.cooldown_losses}){X}")

        # Event log
        if s.events:
            print(f"\n  {D}{'─'*60}{X}")
            for e in list(s.events)[-6:]:
                print(f"  {D}{e}{X}")

        # Trade history
        closed = [t for t in bot.trades if t.status in ("WON", "LOST")]
        if closed:
            print(f"\n  {W}Recent Trades{X}")
            print(f"  {D}{'─'*60}{X}")
            for t in closed[-8:]:
                icon = f"{G}✓{X}" if t.status == "WON" else f"{R}✗{X}"
                print(f"  {icon} {t.ts.strftime('%H:%M')} {t.side:3} ${t.cost:.2f} @${t.price:.2f}  {pnl_c(t.pnl)}  {D}{t.mode}{X}")

        print(f"\n  {D}{'═'*60}{X}\n")


# ═══════════════════════════════════════════════════════════════
# ─── THE BOT ───
# ═══════════════════════════════════════════════════════════════
class Bot:
    def __init__(s):
        s.c = Config.from_env()
        s.feed = Feed()
        s.poly_ws = PolyWS()
        s.finder = Finder(s.c.gamma_host)
        s.ex = Executor(s.c)
        s.dash = Dashboard()

        s.market = None
        s.trades = []
        s.traded_cids = set()
        s.traded_this_market = False
        s.last_signal = ""
        s.block_reason = ""
        s.balance = 0.0
        s.start_bal = 0.0
        s.start_time = time.time()
        s.cooldown_until = 0.0
        s._consec_losses = 0
        s._reduced_markets = 0  # markets remaining at reduced size
        s._last_market_slug = ""
        s._tick = 0
        s._session_high = 0.0   # highest balance this session

        # Load saved CIDs
        try:
            with open("v10_cids.json") as f:
                s.traded_cids = set(json.load(f))
        except: pass

    def _save_cids(s):
        try:
            with open("v10_cids.json", "w") as f:
                json.dump(list(s.traded_cids), f)
        except: pass

    def run(s):
        print(f"\n  {C}{'='*55}{X}")
        print(f"  {M}◆ POLYMARKET v10 — THE MONEY PRINTER{X}")
        print(f"  {C}{'='*55}{X}\n")

        # Connect
        print(f"  {W}[1/3]{X} Binance feed...", end=" ")
        s.feed.poll(); time.sleep(1)
        print(f"{G}{'WS' if s.feed.source == 'WS' else 'HTTP'}{X} (${s.feed.price:,.2f})" if s.feed.price else f"{R}FAIL{X}")

        print(f"  {W}[2/3]{X} Polymarket API...", end=" ")
        print(f"{G}OK{X}" if s.ex.test() else f"{R}FAIL{X}")

        print(f"  {W}[3/3]{X} Auth...", end=" ")
        if s.ex.connect():
            print(f"{G}OK{X}")
        elif s.c.dry_run:
            print(f"{Y}DRY RUN{X}")
        else:
            print(f"{R}FAIL{X}"); return

        # Get balance
        bal = s.ex.balance()
        if bal:
            s.balance = bal; s.start_bal = bal
            print(f"\n  {W}Balance:{X} {G}${bal:,.2f}{X}")
        else:
            s.balance = s.c.trade_size_pct * 10000  # estimate
            s.start_bal = s.balance

        print(f"\n  {W}Config:{X}")
        print(f"    Trade size: {s.c.trade_size_pct*100:.0f}% (${s.balance * s.c.trade_size_pct:.2f}) cap {s.c.max_trade_pct*100:.0f}% (${s.balance * s.c.max_trade_pct:.2f})")
        print(f"    Min BTC move: {s.c.min_move*100:.2f}%")
        print(f"    Min edge: 5%")
        print(f"    Maker-first execution, compounding ON")

        print(f"\n  {G}Starting in 3s...{X}")
        time.sleep(3)
        s.dash.ev("v10 started")
        s._loop()

    def _loop(s):
        while True:
            try:
                s._tick += 1
                s.feed.poll()

                # Find market
                if s._tick % 5 == 0 or not s.market:
                    m = s.finder.find()
                    if m:
                        # New market?
                        if m.slug != s._last_market_slug:
                            # Resolve old positions first
                            if s._last_market_slug:
                                s._resolve_old()
                            s.market = m
                            # Only set open_btc if feed has data
                            if s.feed.price > 0:
                                s.market.open_btc = s.feed.price
                            else:
                                s.market.open_btc = 0  # will be set on next tick when feed loads
                            s.traded_this_market = False
                            s.last_signal = ""
                            s.block_reason = ""
                            s._last_market_slug = m.slug
                            s.dash.ev(f"New market: {m.slug[-25:]}")
                            if m.tok_yes and m.tok_no:
                                s.poly_ws.subscribe(m.tok_yes, m.tok_no)
                        else:
                            s.market = m  # update prices
                            # Fix open_btc if it was 0 (feed wasn't ready)
                            if s.market.open_btc == 0 and s.feed.price > 0:
                                s.market.open_btc = s.feed.price

                # Update token prices from WebSocket
                if s.market and s.poly_ws.live:
                    if s.poly_ws.yes_p > 0: s.market.yes_p = s.poly_ws.yes_p
                    if s.poly_ws.no_p > 0: s.market.no_p = s.poly_ws.no_p

                # Update prices from API periodically
                if s.market and s._tick % 10 == 0 and s.ex.authed:
                    try:
                        yb = s.ex.client.get_order_book(s.market.tok_yes)
                        nb = s.ex.client.get_order_book(s.market.tok_no)
                        ya = yb.get("asks", [{}]) if isinstance(yb, dict) else [{}]
                        na = nb.get("asks", [{}]) if isinstance(nb, dict) else [{}]
                        if ya: s.market.yes_p = float(ya[0].get("price", s.market.yes_p))
                        if na: s.market.no_p = float(na[0].get("price", s.market.no_p))
                    except: pass

                # Trade logic
                if s.market and s.feed.n > 5:
                    s._evaluate()

                # Cancel stale orders near market expiry
                if s.market:
                    tl = (s.market.end - datetime.now(timezone.utc)).total_seconds()
                    if tl < 20 and s.ex.authed:
                        # Market about to expire — cancel any unfilled orders
                        open_orders = s.ex.get_open_orders()
                        if open_orders:
                            s.ex.cancel_all()
                            s.dash.ev(f"Cancelled {len(open_orders)} orders (market expiring)")

                # Balance refresh
                if s._tick % 30 == 0 and s.ex.authed:
                    bal = s.ex.balance()
                    if bal and bal > 0: s.balance = bal

                # Redeem
                if s._tick % 60 == 0 and s.traded_cids:
                    batch = list(s.traded_cids)[:3]
                    redeemed = s.ex.redeem(batch)
                    for cid in redeemed:
                        s.traded_cids.discard(cid)
                        s.dash.ev(f"Redeemed {cid[:12]}...")
                    if redeemed:
                        s._save_cids()
                        time.sleep(3)
                        bal = s.ex.balance()
                        if bal: s.balance = bal

                # Periodic resolution check for stuck OPEN trades
                if s._tick % 15 == 0:
                    s._resolve_old()

                # Cleanup
                if s._tick % 300 == 0:
                    gc.collect()
                    # Trim old trades from memory (keep last 200)
                    if len(s.trades) > 200:
                        s.trades = s.trades[-200:]

                # Render
                s.dash.render(s)
                time.sleep(s.c.poll_sec)

            except KeyboardInterrupt:
                s.ex.cancel_all()
                s._summary()
                break
            except Exception as e:
                log.error(f"Loop: {e}\n{traceback.format_exc()}")
                s.dash.ev(f"Error: {str(e)[:40]}")
                if not hasattr(s, '_err_count'): s._err_count = 0
                s._err_count += 1
                if s._err_count >= 10:
                    raise  # let auto-restart handle it
                time.sleep(min(3 * s._err_count, 30))
            else:
                if hasattr(s, '_err_count'): s._err_count = 0

    def _evaluate(s):
        """The entire strategy in one function.
        
        EDGE: We see BTC move on Binance 0.5-2 seconds before Polymarket 
        reprices. We buy the winning side CHEAP while the market is slow.
        
        The key is the GAP between what BTC says should happen and what 
        the market is currently pricing. Bigger gap = bigger edge.
        """
        m = s.market
        f = s.feed
        c = s.c

        s.last_signal = ""
        s.block_reason = ""

        # ─── PRE-CHECKS ───
        if s.traded_this_market:
            return
        if s.balance <= 0:
            s.block_reason = "No balance"
            return

        open_trades = [t for t in s.trades if t.status == "OPEN"]
        open_risk = sum(t.cost for t in open_trades)
        if len(open_trades) >= c.max_open_trades:
            s.block_reason = f"Max open trades ({c.max_open_trades})"
            return
        # Max 10% of balance at risk at any time
        max_risk = s.balance * 0.10
        if open_risk >= max_risk:
            s.block_reason = f"Max risk (${open_risk:.0f}/${max_risk:.0f})"
            return

        session_loss = s.start_bal - s.balance if s.start_bal else 0
        if session_loss >= c.max_daily_loss:
            s.block_reason = f"Daily loss limit (${session_loss:.0f})"
            return

        tl = (m.end - datetime.now(timezone.utc)).total_seconds()
        if tl < 30 or tl > 870:
            s.block_reason = f"Outside window ({tl:.0f}s left)"
            return

        # ─── BTC DIRECTION ───
        chg_open = f.chg_from(m.open_btc)
        chg_1m = f.chg(60)
        chg_2m = f.chg(120)
        abs_move = abs(chg_open)

        btc_up = chg_open > 0
        side = "YES" if btc_up else "NO"
        is_yes = btc_up
        target_price = m.yes_p if btc_up else m.no_p

        # ─── COUNTER-TREND CHECK ───
        # 1-min trend must agree (no reversal in progress)
        if btc_up and chg_1m < -0.0005:
            s.block_reason = f"BTC reversing (1m: {chg_1m*100:+.3f}%)"
            return
        if not btc_up and chg_1m > 0.0005:
            s.block_reason = f"BTC reversing (1m: {chg_1m*100:+.3f}%)"
            return

        # ─── EDGE CALCULATION ───
        # What does BTC say the fair probability is?
        # Based on historical data and pro bot behavior:
        # 0.10% move → ~65% chance direction holds  
        # 0.15% move → ~72% chance direction holds
        # 0.25% move → ~82% chance direction holds
        # 0.40%+ move → ~90%+ chance direction holds
        # Formula: 0.55 + move * 200, capped at 0.93
        btc_implied_prob = min(0.93, 0.55 + abs_move * 200)
        # e.g. 0.12% → 0.79, 0.15% → 0.85, 0.20% → 0.95→capped 0.93
        
        # Time bonus: the longer BTC has been moving in one direction, 
        # the more likely it holds. Early in market = less certain.
        elapsed = 900 - tl  # seconds since market opened
        if elapsed > 300:  # 5+ minutes of sustained direction
            btc_implied_prob = min(0.93, btc_implied_prob + 0.03)
        
        # The edge is: what we think the probability is MINUS what the market charges
        edge = btc_implied_prob - target_price
        
        # ─── MODE SELECTION ───
        mode = None

        # EARLY RIDER (primary): Buy when market hasn't caught up
        # Enter in minutes 1-13, when there's a speed gap
        # Must have positive edge AND minimum BTC move
        if tl >= 60 and tl <= c.max_time:
            if abs_move >= c.min_move and edge >= 0.05:
                # Price sanity: don't buy too expensive (diminishing returns)
                # or too cheap (market might be right that it's a loser)
                if 0.10 <= target_price <= 0.82:
                    mode = "RIDER"

        # SCALP: Last 4 min, outcome nearly decided
        # Can pay higher prices because probability is very high
        if not mode and tl <= c.scalp_window and tl >= 30:
            if abs_move >= c.scalp_min_move:
                if c.scalp_price_lo <= target_price <= c.scalp_price_hi:
                    if (btc_up and chg_2m > 0) or (not btc_up and chg_2m < 0):
                        # Scalp edge: BTC has been going one direction for 10+ min
                        # Probability is very high, even at $0.85
                        if btc_implied_prob >= target_price:
                            mode = "SCALP"

        if not mode:
            if abs_move < c.min_move:
                s.block_reason = f"Move too small ({abs_move*100:.3f}% < {c.min_move*100:.2f}%)"
            elif edge < 0.05:
                s.block_reason = f"No edge ({edge*100:.1f}% = implied {btc_implied_prob:.0%} - price ${target_price:.2f})"
            elif target_price > 0.82:
                s.block_reason = f"Price too high (${target_price:.2f}) — wait for cheaper"
            elif target_price < 0.10:
                s.block_reason = f"Price too low (${target_price:.2f})"
            else:
                s.block_reason = f"No signal (move={abs_move*100:.3f}%, edge={edge*100:.1f}%, tl={tl:.0f}s)"
            return

        # ─── SIZING (compounding) ───
        # Base: 4% of current balance. Cap: 5% of balance.
        # This compounds naturally — as balance grows, bets grow.
        max_trade = s.balance * c.max_trade_pct
        base_size = min(s.balance * c.trade_size_pct, max_trade)
        
        # Kelly-inspired: scale with edge
        if edge >= 0.15:
            size = base_size * 1.5  # strong edge
        elif edge >= 0.10:
            size = base_size * 1.2  # good edge
        elif edge >= 0.05:
            size = base_size * 0.8  # thin edge
        else:
            size = base_size * 0.5  # marginal
        
        size = min(size, max_trade)
        if mode == "SCALP":
            size = min(size, base_size * 0.8)

        # Smart cooldown: reduce size after consecutive losses
        if s._reduced_markets > 0:
            size = size * 0.50  # half size while recovering
            s.block_reason = f"Reduced size ({s._reduced_markets} markets left)"

        # Session profit protection: if we're up big, slightly reduce
        # to protect gains. Don't give it all back.
        session_pnl = s.balance - s.start_bal if s.start_bal else 0
        if session_pnl > 500 and session_pnl > s.start_bal * 0.05:
            # Up more than $500 and 5% — tighten slightly
            size = size * 0.85

        size = max(size, c.min_trade_usd)
        available = s.balance - open_risk
        if size > available: size = available
        if size < c.min_trade_usd:
            s.block_reason = f"Insufficient balance (${available:.2f})"
            return

        # ─── EXECUTE ───
        shares = max(size / target_price, 5.0)
        s.last_signal = f"{mode} {side} ${size:.0f} @${target_price:.2f} edge={edge*100:.0f}% (BTC {chg_open*100:+.3f}%)"
        s.dash.ev(f"[{mode}] {side} ${size:.2f} @${target_price:.2f} edge={edge*100:.0f}% BTC{chg_open*100:+.3f}%")

        oid, actual_shares = s.ex.order(m, is_yes, target_price, shares, mode="hybrid")
        if oid:
            trade = Trade(
                ts=datetime.now(timezone.utc), side=side, price=target_price,
                cost=size, mode=mode, slug=m.slug, oid=oid,
                market_end=m.end, shares=actual_shares or shares)
            s.trades.append(trade)
            s.traded_this_market = True
            if s._reduced_markets > 0:
                s._reduced_markets -= 1
            if m.cid:
                s.traded_cids.add(m.cid)
                s._save_cids()
            s._log_trade(trade)
            s.dash.ev(f"FILLED {side} ${size:.2f}")
        else:
            s.dash.ev(f"Order FAILED")

    def _resolve_old(s):
        """Resolve trades from previous market."""
        for t in s.trades:
            if t.status != "OPEN": continue
            if not t.market_end: continue
            age = (datetime.now(timezone.utc) - t.market_end).total_seconds()
            if age < 30: continue  # too soon

            # Check Gamma API (using query params like v9)
            try:
                r = s.finder._http.get(f"{s.c.gamma_host}/markets",
                                       params={"slug": t.slug}, timeout=5)
                if r.status_code == 200:
                    d = r.json()
                    if isinstance(d, list): d = d[0] if d else None
                    if d and (d.get("closed") or d.get("resolved")):
                        pr = d.get("outcomePrices") or d.get("outcome_prices") or ""
                        if isinstance(pr, str):
                            try: pr = json.loads(pr)
                            except: continue
                        if len(pr) >= 2:
                            yes_won = float(pr[0]) > 0.5
                            won = (yes_won and t.side == "YES") or (not yes_won and t.side == "NO")
                            if won:
                                payout = t.shares * 1.0  # $1.00 per winning share
                                t.pnl = round(payout - t.cost, 2)
                                t.status = "WON"
                                s._consec_losses = 0
                                s._reduced_markets = 0  # winning resets reduction
                            else:
                                t.pnl = round(-t.cost, 2)
                                t.status = "LOST"
                                s._consec_losses += 1
                                if s._consec_losses >= s.c.cooldown_losses:
                                    s._reduced_markets = s.c.cooldown_markets
                                    s.dash.ev(f"⚠ Size reduced for {s.c.cooldown_markets} markets ({s._consec_losses} losses)")
                            # Track session high
                            if s.balance > s._session_high:
                                s._session_high = s.balance
                            s.dash.ev(f"{'✓ WON' if won else '✗ LOST'} {t.side} {pnl_c(t.pnl)}")
                            s._log_trade(t, resolved=True)
                            continue
            except: pass

            # BTC fallback (only if Gamma unavailable AND market is 5+ min past expiry)
            if age > 300 and t.market_end:
                try:
                    # Use our BTC data
                    market_start_ts = (t.market_end - timedelta(seconds=900)).timestamp()
                    snap = list(s.feed.data)
                    op = cp = None
                    for x in snap:
                        if x["t"] >= market_start_ts and op is None: op = x["p"]
                        cp = x["p"]
                    if op and cp:
                        btc_up = cp > op
                        won = (btc_up and t.side == "YES") or (not btc_up and t.side == "NO")
                        if won:
                            t.pnl = round(t.shares - t.cost, 2)
                            t.status = "WON"
                            s._consec_losses = 0
                            s._reduced_markets = 0
                        else:
                            t.pnl = round(-t.cost, 2)
                            t.status = "LOST"
                            s._consec_losses += 1
                            if s._consec_losses >= s.c.cooldown_losses:
                                s._reduced_markets = s.c.cooldown_markets
                        s.dash.ev(f"{'✓' if won else '✗'} {t.side} {pnl_c(t.pnl)} (BTC fallback)")
                        s._log_trade(t, resolved=True)
                except: pass

    def _log_trade(s, t, resolved=False):
        """Append trade to CSV log."""
        try:
            header = not os.path.exists("v10_trades.csv")
            with open("v10_trades.csv", "a") as f:
                if header:
                    f.write("time,side,price,cost,mode,slug,status,pnl,shares\n")
                f.write(f"{t.ts.isoformat()},{t.side},{t.price:.4f},{t.cost:.2f},"
                        f"{t.mode},{t.slug},{t.status},{t.pnl:.2f},{t.shares:.2f}\n")
        except: pass

    def _summary(s):
        print(f"\n  {C}{'═'*55}{X}")
        print(f"  {M}SESSION SUMMARY — v10{X}")
        print(f"  {C}{'═'*55}{X}")
        wins = [t for t in s.trades if t.status == "WON"]
        losses = [t for t in s.trades if t.status == "LOST"]
        total_pnl = sum(t.pnl for t in s.trades if t.status in ("WON", "LOST"))
        print(f"\n  {W}Trades:{X}  {len(wins) + len(losses)}")
        print(f"  {W}Wins:{X}    {G}{len(wins)}{X}")
        print(f"  {W}Losses:{X}  {R}{len(losses)}{X}")
        print(f"  {W}Win Rate:{X} {Y}{(len(wins)/(len(wins)+len(losses))*100) if wins or losses else 0:.0f}%{X}")
        print(f"  {W}P&L:{X}     {pnl_c(total_pnl)}")
        print(f"  {W}Balance:{X} {Y}${s.balance:,.2f}{X}")
        print(f"\n  {C}{'═'*55}{X}\n")


# ═══════════════════════════════════════════════════════════════
# ─── ENTRY POINT ───
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    restart_count = 0
    max_restarts = 50
    while restart_count < max_restarts:
        try:
            Bot().run()
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            restart_count += 1
            log.error(f"Crash #{restart_count}: {e}\n{traceback.format_exc()}")
            print(f"\n  {R}⚠ Crashed: {str(e)[:60]}{X}")
            print(f"  {Y}⟳ Restarting in 10s... ({restart_count}/{max_restarts}){X}")
            time.sleep(10)
