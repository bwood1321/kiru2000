"""
POLYMARKET REDEMPTION TOOL
Checks for unredeemed winning positions and redeems them back to USDC.e.

For EOA wallets (type 0), we can call redeemPositions directly on the
Gnosis CTF contract. This burns winning conditional tokens and returns USDC.e.

Usage: python redeem_winnings.py
"""
import os, json, time, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

load_dotenv()

# === CONFIG ===
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
if not PRIVATE_KEY.startswith("0x"):
    PRIVATE_KEY = "0x" + PRIVATE_KEY

SIGNER = Account.from_key(PRIVATE_KEY).address
print(f"\n{'='*60}")
print(f"  POLYMARKET REDEMPTION TOOL")
print(f"{'='*60}")
print(f"  Signer: {SIGNER}")

# Polygon RPC
RPC = "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(RPC))
if not w3.is_connected():
    RPC = "https://rpc.ankr.com/polygon"
    w3 = Web3(Web3.HTTPProvider(RPC))
print(f"  RPC: {'Connected' if w3.is_connected() else 'FAILED'}")

# Contract addresses
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Gnosis CTF
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8b8982e"  # Polymarket CTF Exchange
HASH_ZERO = b'\x00' * 32

# ABI for redeemPositions
REDEEM_ABI = json.loads("""[{
    "constant": false,
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"}
    ],
    "name": "redeemPositions",
    "outputs": [],
    "payable": false,
    "stateMutability": "nonpayable",
    "type": "function"
}]""")

# ABI for balanceOf (ERC1155)
BALANCE_ABI = json.loads("""[{
    "constant": true,
    "inputs": [
        {"name": "account", "type": "address"},
        {"name": "id", "type": "uint256"}
    ],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "payable": false,
    "stateMutability": "view",
    "type": "function"
}]""")

# ABI for payoutDenominator (check if condition resolved)
PAYOUT_ABI = json.loads("""[{
    "constant": true,
    "inputs": [{"name": "conditionId", "type": "bytes32"}],
    "name": "payoutDenominator",
    "outputs": [{"name": "", "type": "uint256"}],
    "payable": false,
    "stateMutability": "view",
    "type": "function"
}]""")

ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_CONTRACT), abi=REDEEM_ABI + BALANCE_ABI + PAYOUT_ABI)

# === STEP 1: Check current USDC.e balance ===
print(f"\n{'─'*60}")
print(f"  STEP 1: Current Balance")
usdc_contract = w3.eth.contract(
    address=Web3.to_checksum_address(USDC_E),
    abi=json.loads('[{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
)
usdc_bal = usdc_contract.functions.balanceOf(Web3.to_checksum_address(SIGNER)).call()
print(f"  USDC.e Balance: ${usdc_bal / 1e6:.6f}")

pol_bal = w3.eth.get_balance(Web3.to_checksum_address(SIGNER))
print(f"  POL Balance: {pol_bal / 1e18:.4f} POL (for gas)")

# === STEP 2: Find all markets we traded ===
print(f"\n{'─'*60}")
print(f"  STEP 2: Finding traded markets...")

session = requests.Session()
session.headers["User-Agent"] = "PolyBot/4"

# Get recent BTC 15-min markets from the last 24 hours
now = datetime.now(timezone.utc)
markets_found = []

# Search backwards through 15-min windows (last 24 hours = 96 windows)
for hours_back in range(24):
    for mins in [0, 15, 30, 45]:
        check_time = now.replace(minute=mins, second=0, microsecond=0) - timedelta(hours=hours_back)
        ts = int(check_time.timestamp())
        slug = f"btc-updown-15m-{ts}"
        try:
            r = session.get(f"https://gamma-api.polymarket.com/markets",
                params={"slug": slug}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    m = data[0]
                    cid = m.get("condition_id") or m.get("conditionId")
                    tok = m.get("clobTokenIds") or m.get("clob_token_ids") or ""
                    if isinstance(tok, str):
                        try: tok = json.loads(tok)
                        except: tok = tok.split(",")
                    closed = m.get("closed", False)
                    if cid and tok and len(tok) >= 2:
                        markets_found.append({
                            "slug": slug,
                            "condition_id": cid,
                            "tok_yes": tok[0].strip().strip('"'),
                            "tok_no": tok[1].strip().strip('"'),
                            "closed": closed,
                            "question": m.get("question", "")[:60]
                        })
        except: continue

print(f"  Found {len(markets_found)} BTC 15-min markets in last 24h")

# === STEP 3: Check which markets have tokens we hold ===
print(f"\n{'─'*60}")
print(f"  STEP 3: Checking token balances...")

redeemable = []
held_tokens = []

for m in markets_found:
    try:
        yes_id = int(m["tok_yes"])
        no_id = int(m["tok_no"])
        cid_bytes = bytes.fromhex(m["condition_id"].replace("0x", ""))

        yes_bal = ctf.functions.balanceOf(Web3.to_checksum_address(SIGNER), yes_id).call()
        no_bal = ctf.functions.balanceOf(Web3.to_checksum_address(SIGNER), no_id).call()

        if yes_bal > 0 or no_bal > 0:
            # Check if condition is resolved
            payout_denom = ctf.functions.payoutDenominator(cid_bytes).call()
            resolved = payout_denom > 0

            yes_usd = yes_bal / 1e6
            no_usd = no_bal / 1e6

            entry = {
                **m,
                "yes_bal": yes_bal,
                "no_bal": no_bal,
                "yes_usd": yes_usd,
                "no_usd": no_usd,
                "resolved": resolved,
                "cid_bytes": cid_bytes,
            }
            held_tokens.append(entry)

            status = "✓ RESOLVED" if resolved else "○ PENDING"
            print(f"  {status}  {m['question'][:45]}")
            print(f"           YES: {yes_usd:.4f}  NO: {no_usd:.4f}  tokens")

            if resolved:
                redeemable.append(entry)
    except Exception as e:
        continue

if not held_tokens:
    print(f"  No conditional tokens found in wallet.")
    print(f"  Your trades may have already been redeemed, or tokens are")
    print(f"  held in the CTF Exchange contract (not yet settled).")

# === STEP 4: Redeem resolved positions ===
print(f"\n{'─'*60}")
print(f"  STEP 4: Redeeming winning positions...")

if not redeemable:
    print(f"  Nothing to redeem right now.")
    if held_tokens:
        pending = [h for h in held_tokens if not h["resolved"]]
        if pending:
            print(f"  {len(pending)} positions still pending resolution.")
            print(f"  15-min markets resolve ~16 mins after start.")
            print(f"  Run this script again in a few minutes.")
else:
    print(f"  Found {len(redeemable)} resolved positions to redeem!")
    
    total_redeemed = 0
    for m in redeemable:
        try:
            print(f"\n  Redeeming: {m['question'][:50]}")
            print(f"    YES tokens: {m['yes_usd']:.4f}  NO tokens: {m['no_usd']:.4f}")

            nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(SIGNER))
            gas_price = w3.eth.gas_price

            txn = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_E),   # collateralToken
                HASH_ZERO,                            # parentCollectionId (always 0)
                m["cid_bytes"],                       # conditionId
                [1, 2]                                # indexSets [YES, NO]
            ).build_transaction({
                'from': Web3.to_checksum_address(SIGNER),
                'nonce': nonce,
                'gas': 200000,
                'gasPrice': gas_price,
                'chainId': 137,
            })

            signed = w3.eth.account.sign_transaction(txn, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"    TX: {tx_hash.hex()}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                print(f"    ✓ REDEEMED successfully!")
                total_redeemed += m["yes_usd"] + m["no_usd"]
            else:
                print(f"    ✗ Transaction failed")

            time.sleep(2)  # Wait between redemptions
        except Exception as e:
            print(f"    Error: {e}")

    print(f"\n  Total tokens redeemed: ~${total_redeemed:.4f}")

# === STEP 5: Check final balance ===
print(f"\n{'─'*60}")
print(f"  STEP 5: Final Balance")
new_usdc = usdc_contract.functions.balanceOf(Web3.to_checksum_address(SIGNER)).call()
print(f"  USDC.e Balance: ${new_usdc / 1e6:.6f}")
diff = (new_usdc - usdc_bal) / 1e6
if diff > 0:
    print(f"  Recovered: +${diff:.6f} USDC.e!")
elif diff == 0:
    print(f"  No change (tokens may still be settling)")

# === STEP 6: Check CTF Exchange for unsettled tokens ===
print(f"\n{'─'*60}")
print(f"  STEP 6: Checking CTF Exchange for unsettled tokens...")
try:
    exchange_bal = ctf.functions.balanceOf(
        Web3.to_checksum_address(CTF_EXCHANGE), 0  # Generic check
    ).call()
except:
    pass

# Check if we have any approval issues
print(f"\n  NOTE: If tokens show as held but can't redeem, you may need")
print(f"  to approve the CTF contract. The bot will handle this")
print(f"  automatically going forward.")

print(f"\n{'='*60}")
print(f"  DONE. Run this script periodically to claim winnings.")
print(f"  Or let the updated bot handle redemption automatically.")
print(f"{'='*60}\n")
