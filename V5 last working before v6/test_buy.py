"""
REAL TEST: Place a $1 market buy on the current BTC 15-min market
This will spend $1 of real money.
"""
import os, json, requests
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY
from datetime import datetime, timedelta, timezone

pk = os.getenv("PRIVATE_KEY", "")
funder = os.getenv("FUNDER_ADDRESS", "")

print("Step 1: Connect")
client = ClobClient(
    "https://clob.polymarket.com",
    key=pk,
    chain_id=137,
    signature_type=1,
    funder=funder
)
creds = client.derive_api_key()
client.set_api_creds(creds)

bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
balance = int(bal["balance"]) / 1e6
print(f"  Balance: ${balance:.2f}")

print("\nStep 2: Find market")
now = datetime.now(timezone.utc)
mb = (now.minute // 15) * 15
base = now.replace(minute=mb, second=0, microsecond=0)

for off in [0, -15, 15]:
    ts = int((base + timedelta(minutes=off)).timestamp())
    slug = f"btc-updown-15m-{ts}"
    r = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=8)
    if r.status_code == 200:
        d = r.json()
        if isinstance(d, list) and d and not d[0].get("closed", False):
            tok = json.loads(d[0].get("clobTokenIds", "[]"))
            if len(tok) >= 2:
                yes_tid = tok[0].strip().strip('"')
                no_tid = tok[1].strip().strip('"')
                print(f"  {d[0]['question']}")

                # Get prices
                mid_yes = client.get_midpoint(yes_tid)
                mid_no = client.get_midpoint(no_tid)
                yp = float(mid_yes["mid"])
                np_ = float(mid_no["mid"])
                print(f"  YES: ${yp:.3f}  NO: ${np_:.3f}")

                # Buy whichever is cheaper (better value)
                if yp <= np_:
                    buy_tid = yes_tid
                    side_label = "YES"
                    price = yp
                else:
                    buy_tid = no_tid
                    side_label = "NO"
                    price = np_

                print(f"\nStep 3: Buy $1 of {side_label} @ ~${price:.3f}")
                order = MarketOrderArgs(
                    token_id=buy_tid,
                    amount=1.0,
                    side=BUY,
                )
                signed = client.create_market_order(order)
                resp = client.post_order(signed, OrderType.FOK)
                print(f"  Response: {resp}")

                if isinstance(resp, dict) and resp.get("success"):
                    print(f"\n  ✓ TRADE PLACED!")
                    oid = resp.get("orderID", "?")
                    print(f"  Order ID: {oid}")
                else:
                    print(f"\n  ✗ Trade may have failed")

                # Check new balance
                bal2 = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                new_bal = int(bal2["balance"]) / 1e6
                print(f"\n  Balance before: ${balance:.2f}")
                print(f"  Balance after:  ${new_bal:.2f}")
                print(f"  Spent: ${balance - new_bal:.2f}")
                break

print("\nDone!")
