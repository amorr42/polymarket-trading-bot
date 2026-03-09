# Polymarket Trading Bot

An automated trading system for Polymarket's binary Up/Down prediction markets.
The bot trades 5-minute, 15-minute, and 30-minute BTC/ETH/SOL/XRP Up/Down markets
using zone-based price analysis, Kelly criterion position sizing, and oracle-aware
exit logic.

---

## What the Bot Does

Polymarket's Up/Down markets resolve at 1.00 (win) or 0.00 (loss) when a settlement
oracle publishes the final price. The bot exploits mean-reversion opportunities when
market probabilities deviate significantly from fair value, particularly during
flash-crash events where one side's price drops sharply within a short window.

**Core logic:**

1. Monitors live orderbook data via WebSocket.
2. Classifies each outcome token into one of five price zones (DEAD, LOW, MID, HIGH,
   PREMIUM) and gates entries accordingly.
3. Sizes each position using the Kelly criterion — a mathematically optimal bet-sizing
   formula that balances growth rate against ruin risk.
4. Protects against oracle settlement risk by blocking new entries and force-closing
   open positions as the market approaches its resolution time.
5. Paper-trades by default (no real orders) to allow strategy validation without risk.

---

## Architecture

```
polymarket-combined/
├── apps/                         # Runnable strategy applications
│   ├── paper_trader.py           # Paper trading simulator (no real orders)
│   ├── compounder.py             # Live compounding strategy with capital protection
│   ├── flash_crash_strategy.py   # Flash crash detection strategy (base)
│   ├── flash_crash_runner.py     # Runner for flash_crash_strategy
│   ├── base_strategy.py          # Abstract base class for all strategies
│   ├── db_alert_watcher.py       # Database-backed price alert watcher
│   ├── orderbook_viewer.py       # Real-time orderbook display
│   └── event_orderbook_viewer.py # Event-specific orderbook viewer
│
├── lib/                          # Reusable trading library
│   ├── market_manager.py         # WebSocket market connection + orderbook management
│   ├── market_selector.py        # Market selection (by coin, interval, or DB prefix)
│   ├── price_tracker.py          # Price history + flash crash detection
│   ├── position_manager.py       # Position state tracking
│   ├── btc_oracle.py             # BTC spot price feed (used for value arbitrage)
│   ├── db.py                     # PostgreSQL database helpers
│   ├── terminal_utils.py         # ANSI color output utilities
│   └── alerts/
│       ├── swing_detector.py     # Swing high/low reversal detector
│       ├── momentum_detector.py  # Momentum signal detector
│       └── pump_detector.py      # Pump-and-dump pattern detector
│
├── src/                          # Polymarket API client layer
│   ├── websocket_client.py       # CLOB WebSocket client (orderbook snapshots)
│   ├── gamma_client.py           # Gamma API client (market metadata)
│   ├── bot.py                    # TradingBot core (order placement)
│   ├── client.py                 # CLOB REST API client
│   ├── signer.py                 # EIP-712 / Gnosis Safe signing
│   ├── crypto.py                 # Cryptographic utilities
│   ├── http.py                   # HTTP helpers
│   ├── config.py                 # Configuration loader
│   └── utils.py                  # Shared utilities
│
├── config.example.yaml           # Configuration template
├── requirements.txt              # Python dependencies
└── setup.bat / run.bat           # Windows helper scripts
```

---

## Features

### Zone System

Each market token is assigned to a zone based on its current mid price:

| Zone    | Price Range | Action | Notes |
|---------|-------------|--------|-------|
| DEAD    | < 0.10      | Block  | Outcome nearly certain — no edge |
| LOW     | 0.10–0.30   | Block (paper) / Small (compounder) | Unfavorable odds |
| MID     | 0.30–0.70   | Normal entry | Mean reversion target |
| HIGH    | 0.70–0.90   | Good entry | Flash crash opportunity |
| PREMIUM | 0.90–0.99   | Best entry | Hold until settlement |

### Kelly Criterion Position Sizing

Position sizes are calculated using the fractional Kelly formula:

```
f* = (W * R - (1 - W)) / R  * kelly_fraction
```

Where W is the estimated win rate and R is the payoff ratio for each zone.
Fractional Kelly (0.15x–0.50x depending on zone) is applied for risk control.

### Oracle Protection

Polymarket markets settle when an external oracle publishes the final price. The bot
enforces strict time-based guards:

- **No new entries** within the last 30–45 seconds before settlement.
- **Force-close** all open positions within the last 25–30 seconds.
- **Neutral zone block**: when the market is close to 50/50 and near expiry, entries
  are suppressed to avoid getting caught by random noise at settlement.

### Paper Trading (`apps/paper_trader.py`)

Simulates trades against live Polymarket data without sending any real orders.
Tracks balance, fees, unrealized P&L, zone statistics, and win rates across a
configurable session duration.

Additional logic:
- **Momentum reversal**: after a stop loss, immediately evaluates a reversal trade
  in the opposite direction if the price is still in a valid zone.
- **Death spiral protection**: if the same direction loses twice in a row, a 2-minute
  reversal cooldown is enforced.
- **Volatility filter**: minimum price volatility required before entering.

### Compounder (`apps/compounder.py`)

A live compounding strategy with multi-layered capital protection:

- **Protected base**: a configurable floor (e.g. $10) that is never risked regardless
  of market conditions.
- **Trading capital**: only the amount above the protected base is used for trades.
- **Target multiplier**: the session ends automatically when the trading capital
  reaches the configured multiple (e.g. 1.5x, 2.0x).

Entry signals (in priority order):

1. **Dynamic Order Flow Event** (T+60s): analyses 60 seconds of live orderbook data
   for bid/ask imbalance and price velocity before committing to an entry.
2. **Market Open Rush** (first 45s): aggressive entries at market open when prices
   are volatile and mid-zone.
3. **Swing Bounce/Reject**: enters on detected swing lows (bounce) or swing highs
   (reject) using a configurable window and minimum move threshold.
4. **Oracle Value**: BTC spot price vs. market implied probability arbitrage.

### Flash Crash Strategy (`apps/flash_crash_strategy.py`)

Detects when one side's probability drops by a configurable threshold (default 30pp)
within a short lookback window, then buys the crashed side expecting mean reversion.
Uses configurable take-profit and stop-loss dollar amounts.

---

## Installation

### Prerequisites

- Python 3.10+
- PostgreSQL (optional, only required for `db_alert_watcher.py` and DB-prefix market
  selection)
- A Polymarket account with a funded Gnosis Safe wallet (required only for live
  trading; paper trading works without credentials)

### Steps

```bash
# 1. Clone the repository
git clone <repo-url>
cd polymarket-combined

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Linux/macOS
venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials (live trading only)
cp config.example.yaml config.yaml
# Edit config.yaml and fill in your safe_address, RPC URL, and builder API keys

# 5. Set environment variables (or use a .env file)
# POLYMARKET_PRIVATE_KEY=<your-private-key>
```

---

## Usage

### Paper Trading

Run a 30-minute simulation with a $100 starting balance on BTC 5-minute markets:

```bash
python apps/paper_trader.py --db-prefix btc-updown-5m --balance 100 --duration 30
```

Run using the Gamma API to auto-discover the current BTC market:

```bash
python apps/paper_trader.py --coin BTC --balance 100 --duration 30
```

Customise entry thresholds:

```bash
python apps/paper_trader.py \
  --db-prefix btc-updown-5m \
  --balance 200 \
  --duration 60 \
  --drop 0.05 \       # 5% drop triggers entry (default: 7%)
  --lookback 20 \     # 20s detection window (default: 15s)
  --tp 0.25 \         # 25% take profit (default: 20%)
  --sl 0.08           # 8% stop loss (default: 10%)
```

### Compounder

Start with $12 total balance, protect $10, target 1.5x on the $2 trading capital:

```bash
python apps/compounder.py --balance 12 --protected 10 --target-mult 1.5 --db-prefix btc-updown-5m
```

More aggressive: $15 total, $10 protected, target 2x, write a session log:

```bash
python apps/compounder.py \
  --balance 15 \
  --protected 10 \
  --target-mult 2.0 \
  --db-prefix btc-updown-5m \
  --log auto
```

Use the Gamma API with a specific coin and interval instead of a DB prefix:

```bash
python apps/compounder.py --coin ETH --interval 15m --balance 12 --protected 10
```

### CLI Reference

**paper_trader.py:**

| Flag | Default | Description |
|------|---------|-------------|
| `--coin` | BTC | Coin (BTC, ETH, SOL, XRP) — used if `--db-prefix` not set |
| `--db-prefix` | btc-updown-5m | Market slug prefix; overrides `--coin` |
| `--balance` | 100 | Starting balance in USD |
| `--duration` | 30 | Simulation duration in minutes |
| `--drop` | 0.07 | Flash crash drop threshold (7%) |
| `--lookback` | 15 | Drop detection window in seconds |
| `--tp` | 0.20 | Take profit (20%) |
| `--sl` | 0.10 | Stop loss (10%) |
| `--no-trade-secs` | 45 | Block new entries this many seconds before settlement |
| `--force-exit-secs` | 30 | Force-close this many seconds before settlement |
| `--min-vol` | 0.03 | Minimum volatility required to enter |

**compounder.py:**

| Flag | Default | Description |
|------|---------|-------------|
| `--balance` | 12 | Total starting balance in USD |
| `--protected` | 10 | Protected base amount (never risked) |
| `--target-mult` | 1.5 | Exit multiplier (1.5 = 50% gain on trading capital) |
| `--coin` | BTC | Coin symbol |
| `--interval` | 5m | Market interval (5m, 15m, 30m) |
| `--db-prefix` | — | DB slug prefix; overrides `--coin`/`--interval` |
| `--tp` | 0.10 | Take profit (10%) |
| `--sl` | 0.05 | Stop loss (5%) |
| `--duration` | 60 | Max session duration in minutes |
| `--log` | — | Log file path (`auto` for timestamped name) |

---

## How It Works — Strategy Detail

### Flash Crash Detection

The `PriceTracker` maintains a rolling price history per token. A flash crash event is
triggered when the price drops by at least `drop_threshold` within the last
`lookback_secs` seconds. On detection, the bot evaluates a long entry on the crashed
token.

### BTC Oracle Value Signal

The compounder fetches the current BTC spot price via `BTCOracle` and compares it to
the "price to beat" captured at market open. If BTC is above the opening price, the
market should resolve UP — if the UP token is trading below fair value (edge >= 3pp),
the bot enters long UP. The reverse applies for BTC below opening price.

### Settlement-Aware Exit Logic

For PREMIUM zone positions (probability > 0.90), the bot holds through settlement
rather than taking profit early, since the expected value of a $0.92 token reaching
$1.00 at settlement exceeds the benefit of an early exit. For HIGH zone positions,
profit-taking is suspended during the final 90 seconds if the trade is in profit.

---

## Risk Warning

This software is experimental. Prediction markets are highly volatile and can move
to zero. The strategies implemented here do not guarantee profit. Always:

- Test with paper trading before using real funds.
- Keep protected capital well above the minimum threshold.
- Monitor sessions actively, especially near settlement.
- Never risk funds you cannot afford to lose.

---

## License

See [LICENSE](LICENSE) for details.
