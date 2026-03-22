"""Binance price feed — polls REST API for real-time OHLCV data.

Uses a background thread to keep a rolling window of 1-minute klines
for each configured asset (BTC, ETH, SOL, XRP).
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Tick:
    timestamp: datetime
    asset: str
    price: float
    bid: float
    ask: float


# Binance symbol map
BINANCE_SYMBOLS: Dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


# ── Price Feed ────────────────────────────────────────────────────────────────

class PriceFeed:
    """
    Background thread that polls Binance for price data.

    Provides:
      - Latest tick (current price, bid, ask)
      - Rolling window of 1-minute OHLCV candles
      - RSI and MACD indicators computed on demand
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "PolymarketPositioner/1.0"})

        self._ticks: Dict[str, Tick] = {}
        self._candles: Dict[str, Deque[Candle]] = {
            asset: deque(maxlen=config.PRICE_HISTORY_BARS)
            for asset in config.TARGET_ASSETS
        }

        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── Thread Control ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start background polling thread."""
        if self._running:
            return
        # Seed initial data
        for asset in self.config.TARGET_ASSETS:
            self._fetch_klines(asset)
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="PriceFeed")
        self._thread.start()
        logger.info("Price feed started for assets: %s", self.config.TARGET_ASSETS)

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        while self._running:
            for asset in self.config.TARGET_ASSETS:
                try:
                    self._fetch_ticker(asset)
                except Exception as e:
                    logger.debug("Ticker fetch error for %s: %s", asset, e)
            # Refresh klines every 60 seconds
            time.sleep(self.config.PRICE_POLL_INTERVAL)

    # ── Data Fetching ─────────────────────────────────────────────────────────

    def _fetch_ticker(self, asset: str) -> None:
        """Fetch best bid/ask and last price from Binance."""
        symbol = BINANCE_SYMBOLS.get(asset)
        if not symbol:
            return

        resp = self._session.get(
            f"{self.config.BINANCE_API}/api/v3/ticker/bookTicker",
            params={"symbol": symbol},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        tick = Tick(
            timestamp=datetime.now(timezone.utc),
            asset=asset,
            price=(float(data["bidPrice"]) + float(data["askPrice"])) / 2,
            bid=float(data["bidPrice"]),
            ask=float(data["askPrice"]),
        )

        with self._lock:
            self._ticks[asset] = tick

    def _fetch_klines(self, asset: str) -> None:
        """Fetch 1-minute OHLCV candles from Binance."""
        symbol = BINANCE_SYMBOLS.get(asset)
        if not symbol:
            return

        resp = self._session.get(
            f"{self.config.BINANCE_API}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": "1m",
                "limit": self.config.PRICE_HISTORY_BARS,
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()  # list of OHLCV arrays

        candles = []
        for k in raw:
            # [open_time, O, H, L, C, V, ...]
            candles.append(Candle(
                timestamp=datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
            ))

        with self._lock:
            self._candles[asset].clear()
            for c in candles:
                self._candles[asset].append(c)

    # ── Public Accessors ─────────────────────────────────────────────────────

    def get_tick(self, asset: str) -> Optional[Tick]:
        with self._lock:
            return self._ticks.get(asset)

    def get_price(self, asset: str) -> Optional[float]:
        tick = self.get_tick(asset)
        return tick.price if tick else None

    def get_candles(self, asset: str) -> List[Candle]:
        with self._lock:
            return list(self._candles.get(asset, []))

    # ── Technical Indicators ─────────────────────────────────────────────────

    def rsi(self, asset: str, period: Optional[int] = None) -> Optional[float]:
        """Calculate RSI from most recent candles. Returns 0–100 or None."""
        if period is None:
            period = self.config.RSI_PERIOD

        candles = self.get_candles(asset)
        if len(candles) < period + 1:
            return None

        closes = [c.close for c in candles[-(period + 1):]]
        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            if delta > 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(delta))

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def macd(self, asset: str) -> Optional[Dict[str, float]]:
        """Calculate MACD line, signal, and histogram. Returns None if insufficient data."""
        candles = self.get_candles(asset)
        min_bars = self.config.MACD_SLOW + self.config.MACD_SIGNAL
        if len(candles) < min_bars:
            return None

        closes = [c.close for c in candles]

        def ema(values: List[float], period: int) -> List[float]:
            k = 2.0 / (period + 1)
            result = [values[0]]
            for v in values[1:]:
                result.append(v * k + result[-1] * (1 - k))
            return result

        ema_fast = ema(closes, self.config.MACD_FAST)
        ema_slow = ema(closes, self.config.MACD_SLOW)

        # MACD line starts valid from MACD_SLOW index
        macd_line = [f - s for f, s in zip(ema_fast[self.config.MACD_SLOW:], ema_slow[self.config.MACD_SLOW:])]
        if len(macd_line) < self.config.MACD_SIGNAL:
            return None

        signal_line = ema(macd_line, self.config.MACD_SIGNAL)
        histogram = macd_line[-1] - signal_line[-1]

        return {
            "macd": macd_line[-1],
            "signal": signal_line[-1],
            "histogram": histogram,
        }

    def price_change_pct(self, asset: str, lookback_bars: int = 5) -> Optional[float]:
        """Return % price change over last N 1-minute bars."""
        candles = self.get_candles(asset)
        if len(candles) < lookback_bars + 1:
            return None
        old_price = candles[-(lookback_bars + 1)].close
        new_price = candles[-1].close
        if old_price == 0:
            return None
        return (new_price - old_price) / old_price * 100.0

    def is_data_fresh(self, asset: str, max_age_sec: int = 30) -> bool:
        """True if we have recent tick data for the asset."""
        tick = self.get_tick(asset)
        if not tick:
            return False
        age = (datetime.now(timezone.utc) - tick.timestamp).total_seconds()
        return age <= max_age_sec
