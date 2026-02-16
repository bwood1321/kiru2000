"""
POLYMARKET BOT v5 — FULL SETUP
Run this FIRST on any new PC: python setup.py
"""
import subprocess, sys, os

print("="*50)
print("  POLYMARKET BOT v5 SETUP")
print("="*50)

# Step 1: Install dependencies
deps = [
    "py-clob-client",
    "python-dotenv",
    "requests",
    "numpy",
    "colorama",
    "web3",
    "eth-account",
]

print("\n[1/2] Installing dependencies...\n")
for pkg in deps:
    print(f"  Installing {pkg}...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])
    print(f"  Done")

# Step 2: Create .env if missing
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
print(f"\n[2/2] Setting up .env...")

if os.path.exists(env_path):
    print(f"  .env already exists — skipping")
else:
    with open(env_path, "w") as f:
        f.write("""PRIVATE_KEY=0xf34656da3eb79f15133b133dc16095010a088c3d4b2618f6de0ff083d8f5319d
FUNDER_ADDRESS=0xF9b8c919b97CbD5eb1d71b23B9915Df711860694
SIGNATURE_TYPE=1
DRY_RUN=false
STARTING_BALANCE=23.0
ARB_SIZE=3.0
LATENCY_SIZE=3.0
MOMENTUM_SIZE=3.0
FLASH_SIZE=2.0
MAX_DAILY_LOSS=10.0
""")
    print(f"  .env created")

print(f"\n{'='*50}")
print(f"  SETUP COMPLETE!")
print(f"  Run: python polymarket_btc_bot.py")
print(f"{'='*50}")
