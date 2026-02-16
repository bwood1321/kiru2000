# Polymarket BTC Bot v5

Automated trading bot for Polymarket BTC 15-minute up/down markets.

## Files

| File | Purpose |
|------|---------|
| `setup.py` | Run FIRST — installs all dependencies and creates .env |
| `polymarket_btc_bot.py` | The main bot |
| `test_buy.py` | Test script — places a $1 trade to verify everything works |
| `README.md` | This file |

## Quick Start

```
1. Copy all files to a folder (e.g. C:\Polybot\)
2. python setup.py
3. python test_buy.py             (verify trading works — costs $1)
4. python polymarket_btc_bot.py   (start the bot)
```

## Strategies

- **ARB** — Buys when YES+NO sum < $0.99 (guaranteed profit)
- **LATENCY** — Buys when BTC moves on Binance before Polymarket updates
- **MOMENTUM** — 5-indicator system (Bollinger, EMA, RSI, ROC, VWAP)
- **FLASH** — Buys panic dips when a side drops below $0.30

## Controls

- `Ctrl+C` to stop (auto-redeems winning tokens and shows summary)

## Config (.env)

| Setting | Default | Description |
|---------|---------|-------------|
| DRY_RUN | false | Set true to test without real money |
| ARB_SIZE | 3.0 | Dollars per arb trade |
| LATENCY_SIZE | 3.0 | Dollars per latency trade |
| MOMENTUM_SIZE | 3.0 | Dollars per momentum trade |
| FLASH_SIZE | 2.0 | Dollars per flash trade |
| MAX_DAILY_LOSS | 10.0 | Stop trading after this much loss |
