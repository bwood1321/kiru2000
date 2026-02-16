"""
Try every possible combo of signer/funder/type with your key
"""
import os, json, requests
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY
from eth_account import Account
from datetime import datetime, timedelta, timezone

pk = "0xf34656da3eb79f15133b133dc16095010a088c3d4b2618f6de0ff083d8f5319d"
pk_clean = pk[2:]

key_addr = Account.from_key(pk).address
profile_addr = "0xF9b8c919b97CbD5eb1d71b23B9915Df711860694"
proxy_addr = "0xe2446Ade31f46a8B3BA3c92B7ef18c0879Bb9043"

print(f"Key produces:    {key_addr}")
print(f"Profile shows:   {profile_addr}")
print(f"Proxy wallet:    {proxy_addr}")

# Find market
now = datetime.now(timezone.utc)
mb = (now.minute // 15) * 15
base = now.replace(minute=mb, second=0, microsecond=0)
tid = None
for off in [0, -15, 15]:
    ts = int((base + timedelta(minutes=off)).timestamp())
    slug = f"btc-updown-15m-{ts}"
    r = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=8)
    if r.status_code == 200:
        d = r.json()
        if isinstance(d, list) and d and not d[0].get("closed", False):
            tok = json.loads(d[0].get("clobTokenIds", "[]"))
            if len(tok) >= 2:
                tid = tok[1].strip().strip('"')
                print(f"Market: {slug}")
                break

if not tid:
    print("No market!"); exit()

# Try ALL combos
combos = [
    {"label": "type=1, funder=profile", "key": pk, "sig": 1, "funder": profile_addr},
    {"label": "type=1, funder=proxy",   "key": pk, "sig": 1, "funder": proxy_addr},
    {"label": "type=1, funder=key_addr", "key": pk, "sig": 1, "funder": key_addr},
    {"label": "type=0, no funder",       "key": pk, "sig": 0, "funder": None},
    {"label": "type=0, funder=profile",  "key": pk, "sig": 0, "funder": profile_addr},
    {"label": "type=2, funder=profile",  "key": pk, "sig": 2, "funder": profile_addr},
    {"label": "type=2, funder=proxy",    "key": pk, "sig": 2, "funder": proxy_addr},
    # Also try with clean key (no 0x)
    {"label": "type=1, funder=profile, no0x", "key": pk_clean, "sig": 1, "funder": profile_addr},
    {"label": "type=0, funder=profile, no0x", "key": pk_clean, "sig": 0, "funder": profile_addr},
]

print(f"\n{'='*60}")

for c in combos:
    print(f"\n--- {c['label']} ---")
    try:
        kw = {"host": "https://clob.polymarket.com", "key": c["key"], "chain_id": 137, "signature_type": c["sig"]}
        if c["funder"]:
            kw["funder"] = c["funder"]
        
        client = ClobClient(**kw)
        creds = client.derive_api_key()
        client.set_api_creds(creds)
        
        # Check balance
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        b = int(bal.get("balance", 0)) / 1e6
        print(f"  Balance: ${b:.2f}")
        
        # Try limit order
        order = OrderArgs(token_id=tid, price=0.01, size=10.0, side=BUY)
        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.GTC)
        print(f"  ✓ ORDER WORKED: {resp}")
        client.cancel_all()
        print(f"  ✓✓✓ THIS CONFIG WORKS ✓✓✓")
        break
        
    except Exception as e:
        err = str(e)
        if "invalid signature" in err.lower():
            print(f"  ✗ Invalid signature")
        elif "not enough balance" in err.lower():
            print(f"  ✓ SIGNATURE VALID! (just no balance on this address)")
        else:
            print(f"  ✗ {err[:80]}")

print(f"\n{'='*60}")
print("DONE")
