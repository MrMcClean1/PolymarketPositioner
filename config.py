"""Configuration for Polymarket Positioner — 15-minute crypto market trader."""

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    """All positioner configuration. Override via .env file."""

    # ── Wallet / Auth ────────────────────────────────────────────────────────
    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
    PROXY_ADDRESS: str = os.getenv("PROXY_ADDRESS", "")
    CHAIN_ID: int = int(os.getenv("CHAIN_ID", "137"))
    SIGNATURE_TYPE: int = int(os.getenv("SIGNATURE_TYPE", "1"))

    # ── API Endpoints ────────────────────────────────────────────────────────
    CLOB_HOST: str = "https://clob.polymarket.com"
    GAMMA_API: str = "https://gamma-api.polymarket.com"
    DATA_API: str = "https://data-api.polymarket.com"

    # ── Target Assets ────────────────────────────────────────────────────────
    # Comma-separated: BTC,ETH,SOL,XRP
    TARGET_ASSETS: List[str] = field(
        default_factory=lambda: os.getenv("TARGET_ASSETS", "BTC,ETH,SOL").upper().split(",")
    )
    # Only trade 15-minute markets
    MARKET_INTERVAL: int = int(os.getenv("MARKET_INTERVAL", "15"))

    # ── Strategy ─────────────────────────────────────────────────────────────
    # Which strategy to run: "momentum", "arbitrage", "both"
    STRATEGY: str = os.getenv("STRATEGY", "both")

    # Momentum: RSI thresholds
    RSI_OVERBOUGHT: float = float(os.getenv("RSI_OVERBOUGHT", "60.0"))
    RSI_OVERSOLD: float = float(os.getenv("RSI_OVERSOLD", "40.0"))
    RSI_PERIOD: int = int(os.getenv("RSI_PERIOD", "14"))

    # Momentum: MACD settings
    MACD_FAST: int = int(os.getenv("MACD_FAST", "12"))
    MACD_SLOW: int = int(os.getenv("MACD_SLOW", "26"))
    MACD_SIGNAL: int = int(os.getenv("MACD_SIGNAL", "9"))

    # Momentum: Minimum signal strength (0.0–1.0)
    MIN_SIGNAL_STRENGTH: float = float(os.getenv("MIN_SIGNAL_STRENGTH", "0.6"))

    # Momentum: Only enter within N seconds of market open
    ENTRY_WINDOW_SEC: int = int(os.getenv("ENTRY_WINDOW_SEC", "120"))

    # Arbitrage: Maximum combined price to trigger arb (e.g. 0.98 means buy both if sum < 0.98)
    ARB_MAX_COMBINED: float = float(os.getenv("ARB_MAX_COMBINED", "0.98"))

    # ── Trade Sizing / Risk ──────────────────────────────────────────────────
    TRADE_SIZE_USDC: float = float(os.getenv("TRADE_SIZE_USDC", "10.0"))
    MAX_TRADE_SIZE_USDC: float = float(os.getenv("MAX_TRADE_SIZE_USDC", "50.0"))
    MIN_TRADE_SIZE_USDC: float = float(os.getenv("MIN_TRADE_SIZE_USDC", "2.0"))
    MAX_POSITION_PER_MARKET: float = float(os.getenv("MAX_POSITION_PER_MARKET", "100.0"))
    MAX_TOTAL_EXPOSURE: float = float(os.getenv("MAX_TOTAL_EXPOSURE", "300.0"))
    SLIPPAGE_TOLERANCE: float = float(os.getenv("SLIPPAGE_TOLERANCE", "0.03"))

    # ── Price Feed ───────────────────────────────────────────────────────────
    # Binance REST API for price data
    BINANCE_API: str = "https://api.binance.com"
    PRICE_POLL_INTERVAL: int = int(os.getenv("PRICE_POLL_INTERVAL", "5"))   # seconds
    PRICE_HISTORY_BARS: int = int(os.getenv("PRICE_HISTORY_BARS", "50"))    # candles kept

    # ── Polling ──────────────────────────────────────────────────────────────
    MARKET_POLL_INTERVAL: int = int(os.getenv("MARKET_POLL_INTERVAL", "30"))  # seconds
    MARKET_REFRESH_INTERVAL: int = int(os.getenv("MARKET_REFRESH_INTERVAL", "300"))

    # ── Misc ─────────────────────────────────────────────────────────────────
    DB_PATH: str = os.getenv("DB_PATH", "positioner.db")
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    def validate(self) -> List[str]:
        """Return list of config problems. Empty = all good."""
        problems: List[str] = []
        if not self.DRY_RUN:
            if not self.PRIVATE_KEY:
                problems.append("PRIVATE_KEY is required for live trading")
            if not self.PROXY_ADDRESS:
                problems.append("PROXY_ADDRESS is required for live trading")
        if self.STRATEGY not in ("momentum", "arbitrage", "both"):
            problems.append(f"STRATEGY must be 'momentum', 'arbitrage', or 'both', got: {self.STRATEGY}")
        if self.ARB_MAX_COMBINED >= 1.0:
            problems.append("ARB_MAX_COMBINED must be < 1.0")
        if self.MAX_TRADE_SIZE_USDC < self.MIN_TRADE_SIZE_USDC:
            problems.append("MAX_TRADE_SIZE_USDC must be >= MIN_TRADE_SIZE_USDC")
        return problems
