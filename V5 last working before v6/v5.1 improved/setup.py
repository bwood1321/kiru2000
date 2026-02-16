"""
POLYMARKET BOT — SETUP WIZARD
Run this first to set up your .env file
"""
import os, sys

def main():
    print("""
  ╔═══════════════════════════════════════════════════════════╗
  ║         POLYMARKET BOT — SETUP WIZARD                    ║
  ╚═══════════════════════════════════════════════════════════╝

  STEP 1: GET YOUR PRIVATE KEY FROM POLYMARKET
  ─────────────────────────────────────────────
  1. Go to https://polymarket.com
  2. Log in to your account
  3. Click on "Cash" (top right, your balance)
  4. Click the 3 dots ⋮ menu
  5. Click "Export Private Key"
  6. Copy the key (64 hex characters)

  If you signed up with EMAIL/GOOGLE:
  → Go to: https://reveal.magic.link/polymarket
  → Log in with the same email
  → It shows your private key

  STEP 2: GET YOUR FUNDER ADDRESS
  ────────────────────────────────
  1. Go to https://polymarket.com/settings
  2. Find your wallet/proxy address (starts with 0x)
  3. Copy it
""")

    pk = input("  Paste your PRIVATE KEY here: ").strip()

    # Clean up the key
    if pk.startswith("0x"):
        pk = pk[2:]

    # Validate
    if len(pk) != 64:
        print(f"\n  ⚠ Your key is {len(pk)} characters. It should be 64.")
        if " " in pk:
            print("  It looks like you pasted words (seed phrase).")
            print("  You need the HEX key, not seed phrase.")
            print("  Go to: Cash → ⋮ → Export Private Key")
            return
        else:
            print("  Continuing anyway...\n")

    # Check if hex
    try:
        int(pk, 16)
    except ValueError:
        print("\n  ⚠ Key contains non-hex characters.")
        print("  A valid key only has: 0-9 and a-f")
        print("  Go to: Cash → ⋮ → Export Private Key")
        return

    funder = input("  Paste your FUNDER ADDRESS (0x...): ").strip()
    if not funder.startswith("0x"):
        funder = "0x" + funder

    print(f"""
  ─────────────────────────────────────────────
  How did you sign up for Polymarket?
    0 = MetaMask / wallet extension
    1 = Email or Google (Magic Link)
    2 = Trust Wallet / other mobile wallet
""")
    sig = input("  Enter 0, 1, or 2: ").strip()
    if sig not in ["0", "1", "2"]:
        sig = "1"
        print("  Defaulting to 1 (email/Google)")

    # Write .env
    env_content = f"""PRIVATE_KEY={pk}
FUNDER_ADDRESS={funder}
SIGNATURE_TYPE={sig}
DRY_RUN=false
STARTING_BALANCE=7.0
ARB_SIZE=1.0
LATENCY_SIZE=1.0
MOMENTUM_SIZE=1.0
MAX_DAILY_LOSS=3.0
"""

    with open(".env", "w") as f:
        f.write(env_content)

    print(f"""
  ✓ .env file created!
  ─────────────────────────────────────────────
  Private Key: {pk[:6]}...{pk[-4:]}
  Funder: {funder}
  Signature Type: {sig}
  ─────────────────────────────────────────────

  Now run the bot:
    python polymarket_btc_bot.py
""")

if __name__ == "__main__":
    main()
