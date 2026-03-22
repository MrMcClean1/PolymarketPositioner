#!/usr/bin/env python3
"""
Polymarket Positioner — 15-minute crypto market trader.

Strategies:
  momentum  — RSI + MACD + price change → directional bet at market open
  arbitrage — Buy both UP+DOWN when combined price < 1 for guaranteed profit
  both      — Run both strategies simultaneously

Usage:
  python main.py                  # Run with .env settings
  python main.py --dry-run        # Force dry-run (no real orders)
  python main.py --live           # Force live trading
  python main.py --no-dashboard   # Headless (log output only)
  python main.py --reset-db       # Reset database
  python main.py --strategy arb   # Override strategy (momentum|arbitrage|both)
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import requests

from config import Config
from dashboard import Dashboard
from database import Database
from executor import TradeExecutor
from market_data import MarketDataClient
from price_feed import PriceFeed
from strategy import StrategyEngine


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = RotatingFileHandler("positioner.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)


# ── Main Positioner ───────────────────────────────────────────────────────────

class Positioner:
    """
    Main orchestrator for the Polymarket Positioner.

    Main loop every MARKET_POLL_INTERVAL seconds:
      1. Refresh Polymarket market list (every MARKET_REFRESH_INTERVAL)
      2. Update market prices
      3. Evaluate each open market for signals
      4. Execute any actionable signals
      5. Render dashboard
    """

    def __init__(self, config: Config, no_dashboard: bool = False) -> None:
        self.config = config
        self.running = True
        self.no_dashboard = no_dashboard

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "PolymarketPositioner/1.0"})

        # Core modules
        self.db = Database(config.DB_PATH)
        self.price_feed = PriceFeed(config)
        self.market_client = MarketDataClient(config, self._session)
        self.executor = TradeExecutor(config, self.db)
        self.strategy = StrategyEngine(config, self.price_feed)

        self.dashboard = None if no_dashboard else Dashboard(config, self.db, self.price_feed)

        # Deduplification: track which markets we've already traded this cycle
        self._positioned_markets: set = set()

        # Timing
        self._last_market_poll = 0.0
        self._cycle = 0

    def _setup_signals(self) -> None:
        def _handler(sig, frame):
            logging.info("Received %s — shutting down…", sig)
            self.running = False

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def _init(self) -> None:
        """Initialize all modules."""
        logging.info("=" * 60)
        logging.info("Polymarket Positioner")
        logging.info("Strategy : %s", self.config.STRATEGY.upper())
        logging.info("Assets   : %s", ", ".join(self.config.TARGET_ASSETS))
        logging.info("DRY RUN  : %s", self.config.DRY_RUN)
        logging.info("=" * 60)

        # Initialize executor (CLOB client for live trading)
        self.executor.initialize()

        # Seed initial market data
        logging.info("Fetching initial market list…")
        count = self.market_client.refresh()
        logging.info("Found %d active 15-minute markets", count)

        # Seed initial price data (blocking)
        logging.info("Seeding price feed…")
        for asset in self.config.TARGET_ASSETS:
            try:
                self.price_feed._fetch_klines(asset)
                self.price_feed._fetch_ticker(asset)
                logging.info("  %s: $%.2f", asset, self.price_feed.get_price(asset) or 0)
            except Exception as e:
                logging.warning("  %s: price seed failed — %s", asset, e)

        # Start background price polling
        self.price_feed.start()

    def _should_poll_markets(self) -> bool:
        return (time.time() - self._last_market_poll) >= self.config.MARKET_POLL_INTERVAL

    def _poll_and_trade(self) -> None:
        """Core trading loop: poll markets → generate signals → execute."""
        # Refresh full market list periodically
        if self.market_client.should_refresh():
            count = self.market_client.refresh()
            logging.debug("Market refresh: %d active markets", count)

        # Update prices for open markets
        self.market_client.update_prices()

        # Get all currently open markets
        open_markets = self.market_client.get_open_markets()

        if self.dashboard:
            self.dashboard.open_markets = open_markets

        for market in open_markets:
            # Skip if we already entered this market in this positioner session
            if market.condition_id in self._positioned_markets:
                continue

            # Skip if already positioned in DB (prevents double-entry across restarts)
            if self.db.has_any_position(market.condition_id):
                self._positioned_markets.add(market.condition_id)
                continue

            # Evaluate signals
            signals = self.strategy.evaluate(market)

            for sig in signals:
                if not sig.is_actionable:
                    continue
                try:
                    success = self.executor.execute(sig, market)
                    if success:
                        self._positioned_markets.add(market.condition_id)
                        logging.info(
                            "Positioned %s %s via %s strategy",
                            market.asset, sig.direction.value, sig.strategy
                        )
                except Exception as e:
                    logging.error("Execution error for %s: %s", market.condition_id[:8], e)

        self._last_market_poll = time.time()

    def _render(self) -> None:
        if not self.dashboard:
            return
        try:
            stats = self.db.get_stats()
            self.dashboard.cycle_count = self._cycle
            self.dashboard.status = "RUNNING" if self.running else "STOPPING"
            self.dashboard.render(stats)
        except Exception as e:
            logging.error("Dashboard error: %s", e)

    def run(self) -> None:
        """Main event loop."""
        self._setup_signals()
        self._init()

        while self.running:
            cycle_start = time.time()
            self._cycle += 1

            try:
                if self._should_poll_markets():
                    self._poll_and_trade()
            except Exception as e:
                logging.error("Main loop error (continuing): %s", e)

            self._render()

            elapsed = time.time() - cycle_start
            sleep_time = max(0, 5.0 - elapsed)   # render every ~5 seconds
            if sleep_time > 0 and self.running:
                time.sleep(sleep_time)

        logging.info("Positioner stopped")
        self.price_feed.stop()
        self._session.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Positioner — 15-minute crypto trader")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode (no real orders)")
    parser.add_argument("--live", action="store_true", help="Force live trading (REAL MONEY)")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable terminal dashboard")
    parser.add_argument("--reset-db", action="store_true", help="Reset database before start")
    parser.add_argument("--strategy", choices=["momentum", "arbitrage", "both"], help="Override strategy")
    parser.add_argument("--assets", help="Override target assets (comma-separated, e.g. BTC,ETH)")
    parser.add_argument("--config", help="Path to .env config file")
    args = parser.parse_args()

    if args.config:
        from dotenv import load_dotenv
        load_dotenv(args.config, override=True)

    config = Config()

    if args.dry_run:
        config.DRY_RUN = True
    if args.live:
        config.DRY_RUN = False
    if args.strategy:
        config.STRATEGY = args.strategy
    if args.assets:
        config.TARGET_ASSETS = [a.strip().upper() for a in args.assets.split(",")]

    setup_logging(config.LOG_LEVEL)

    # Validate
    problems = config.validate()
    if problems:
        for p in problems:
            if config.DRY_RUN:
                logging.warning("Config: %s", p)
            else:
                logging.error("Config: %s", p)
        if not config.DRY_RUN:
            sys.exit(1)

    if args.reset_db:
        Database(config.DB_PATH).reset()
        logging.info("Database reset")

    # Run
    positioner = Positioner(config, no_dashboard=args.no_dashboard)
    positioner.run()


if __name__ == "__main__":
    main()
