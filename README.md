# Polymarket Positioner

An automated trading bot for **Polymarket's 15-minute crypto markets** (BTC, ETH, SOL, XRP). Uses real-time Binance price data to generate directional signals and detect arbitrage opportunities.

## Strategies

### 1. Momentum
Reads live Binance price data and computes RSI, MACD, and 5-minute price change. When signals align strongly enough, it enters the predicted direction (UP or DOWN) at the start of each 15-minute window.

| Signal | Bullish (enter UP) | Bearish (enter DOWN) |
|--------|--------------------|----------------------|
| RSI | > 60 | < 40 |
| MACD histogram | positive | negative |
| 5-min price change | positive | negative |

All three signals are averaged into a combined score. If the score exceeds `MIN_SIGNAL_STRENGTH`, an order is placed.

### 2. Arbitrage
Scans every open 15-minute market for mispricings where:

```
UP.price + DOWN.price < ARB_MAX_COMBINED (default: 0.98)
```

Since one token always resolves to $1.00, buying both sides guarantees a profit equal to `1.0 - combined_price`, minus Polymarket's ~1% taker fee. No price prediction required.

### 3. Both (default)
Runs both strategies simultaneously on every open market.

---

## Project Structure

```
PolymarketPositioner/
├── main.py          # Entry point, main event loop, CLI
├── config.py        # All settings loaded from .env
├── market_data.py   # Discovers active 15-min markets via Polymarket Gamma API
├── price_feed.py    # Binance REST poller (background thread) + RSI/MACD
├── strategy.py      # Signal generation: momentum + arbitrage
├── executor.py      # CLOB order placement (dry-run + live)
├── database.py      # SQLite order/position/PnL tracking
├── dashboard.py     # Rich terminal dashboard
├── requirements.txt # Python dependencies
└── .env.example     # Configuration template
```

---

## Quick Start

### 1. Install dependencies

```bash
cd PolymarketPositioner
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
nano .env   # fill in PRIVATE_KEY and PROXY_ADDRESS for live trading
```

At minimum, set:
- `PRIVATE_KEY` — your Polymarket wallet's private key
- `PROXY_ADDRESS` — your Polymarket proxy wallet address

Leave `DRY_RUN=true` to test without placing real orders.

### 3. Run

```bash
# Dry run (safe — no real orders placed)
./venv/bin/python3 main.py --dry-run

# Arbitrage only
./venv/bin/python3 main.py --strategy arbitrage

# Momentum only, BTC and ETH
./venv/bin/python3 main.py --strategy momentum --assets BTC,ETH

# Live trading (REAL MONEY — start small)
./venv/bin/python3 main.py --live

# Headless (log output only, no dashboard)
./venv/bin/python3 main.py --no-dashboard

# Reset database
./venv/bin/python3 main.py --reset-db
```

---

## Configuration Reference

All settings live in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | *(required for live)* | Polymarket wallet private key |
| `PROXY_ADDRESS` | *(required for live)* | Polymarket proxy wallet address |
| `CHAIN_ID` | `137` | Polygon mainnet |
| `SIGNATURE_TYPE` | `1` | `1`=email/magic, `2`=MetaMask |
| `TARGET_ASSETS` | `BTC,ETH,SOL` | Which assets to trade |
| `STRATEGY` | `both` | `momentum` / `arbitrage` / `both` |
| `RSI_OVERBOUGHT` | `60.0` | RSI bullish threshold |
| `RSI_OVERSOLD` | `40.0` | RSI bearish threshold |
| `RSI_PERIOD` | `14` | RSI lookback period (bars) |
| `MACD_FAST` | `12` | MACD fast EMA period |
| `MACD_SLOW` | `26` | MACD slow EMA period |
| `MACD_SIGNAL` | `9` | MACD signal line period |
| `MIN_SIGNAL_STRENGTH` | `0.6` | Minimum combined signal (0–1) |
| `ENTRY_WINDOW_SEC` | `120` | Seconds after open to enter momentum trades |
| `ARB_MAX_COMBINED` | `0.98` | Max UP+DOWN sum for arbitrage |
| `TRADE_SIZE_USDC` | `10.0` | Base trade size in USDC |
| `MAX_TRADE_SIZE_USDC` | `50.0` | Hard cap per trade |
| `MIN_TRADE_SIZE_USDC` | `2.0` | Minimum trade size |
| `MAX_POSITION_PER_MARKET` | `100.0` | Max USDC exposure per market |
| `MAX_TOTAL_EXPOSURE` | `300.0` | Max total USDC across all positions |
| `SLIPPAGE_TOLERANCE` | `0.03` | Max acceptable slippage (3%) |
| `PRICE_POLL_INTERVAL` | `5` | Seconds between Binance polls |
| `PRICE_HISTORY_BARS` | `50` | 1-minute candles kept in memory |
| `MARKET_POLL_INTERVAL` | `10` | Seconds between strategy cycles |
| `MARKET_REFRESH_INTERVAL` | `300` | Seconds between full market list refresh |
| `DB_PATH` | `positioner.db` | SQLite database file |
| `DRY_RUN` | `true` | **Set to `false` for real trading** |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## How It Works

### Market Discovery
Every `MARKET_REFRESH_INTERVAL` seconds, the bot fetches active markets from Polymarket's Gamma API and filters for 15-minute BTC/ETH/SOL/XRP markets. Each market has two tokens:
- **UP token** — resolves to $1.00 if the asset price is higher at expiry
- **DOWN token** — resolves to $1.00 if the asset price is lower at expiry

### Price Feed
A background thread polls Binance every `PRICE_POLL_INTERVAL` seconds for live ticker data. It also maintains a rolling window of 1-minute OHLCV candles used to compute RSI and MACD.

### Strategy Loop
Every `MARKET_POLL_INTERVAL` seconds:
1. Refresh market prices from the Polymarket CLOB
2. Evaluate each open market for momentum and/or arbitrage signals
3. Skip markets already positioned in this session
4. Execute any actionable signals via the CLOB API

### Position Deduplication
The bot tracks which markets it has positioned in (in the SQLite database), so it never double-enters the same 15-minute window — even across restarts.

### Risk Management
- Trade size scales with signal strength (stronger signal → larger size, up to `MAX_TRADE_SIZE_USDC`)
- Hard exposure limits per market and total portfolio
- Slippage tolerance check before live orders

---

## Dashboard

The terminal dashboard (powered by [Rich](https://github.com/Textualize/rich)) shows:

- **Price Feed** — live Binance prices with RSI, MACD histogram, 5-min change, and data freshness
- **Open Markets** — all active 15-minute markets with UP/DOWN prices, combined price, and time elapsed/remaining
- **Performance** — total PnL, open exposure, win rate, and per-strategy breakdown
- **Recent Orders** — last 10 orders with asset, direction, price, USDC size, status, and PnL

---

## Requirements

- Python 3.9+
- Internet connection (Binance API + Polymarket API)
- Polymarket account with funded proxy wallet (for live trading)

### Dependencies

```
requests>=2.31.0       # HTTP client
python-dotenv>=1.0.0   # .env file loading
rich>=13.0.0           # Terminal dashboard
py-clob-client>=0.17.0 # Polymarket CLOB SDK
```

---

## Risk Warning

This bot trades real money on prediction markets. Always:

1. Start with `DRY_RUN=true` to verify behavior before going live
2. Use small position sizes (`TRADE_SIZE_USDC=5`) initially
3. Monitor the dashboard and logs
4. Set conservative exposure limits (`MAX_TOTAL_EXPOSURE`)
5. Understand that prediction markets carry inherent risk — past strategy performance does not guarantee future results

---

## API Reference

The bot uses three Polymarket APIs:

| API | Purpose | URL |
|-----|---------|-----|
| Gamma API | Market discovery | `https://gamma-api.polymarket.com` |
| CLOB API | Order book prices + order placement | `https://clob.polymarket.com` |
| Data API | Historical data (optional) | `https://data-api.polymarket.com` |

And Binance for price data:

| API | Purpose | URL |
|-----|---------|-----|
| Binance REST | 1-minute klines + book ticker | `https://api.binance.com` |
