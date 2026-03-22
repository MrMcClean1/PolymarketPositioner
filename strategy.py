"""Strategy logic for Polymarket Positioner.

Two strategies:
  1. Momentum  — Use Binance price data (RSI + MACD + price change) to pick
                 the direction of a newly-opened 15-minute market.
  2. Arbitrage — Enter both UP and DOWN when their combined price < threshold,
                 locking in guaranteed profit regardless of outcome.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from config import Config
from market_data import Market
from price_feed import PriceFeed

logger = logging.getLogger(__name__)


class Direction(Enum):
    UP = "UP"
    DOWN = "DOWN"
    BOTH = "BOTH"   # for arbitrage
    NONE = "NONE"


@dataclass
class Signal:
    market_condition_id: str
    asset: str
    direction: Direction
    strategy: str           # "momentum" | "arbitrage"
    strength: float         # 0.0–1.0 confidence
    entry_price: float      # price for UP or DOWN token
    entry_price_down: float # price for DOWN token (arbitrage only)
    reason: str

    @property
    def is_actionable(self) -> bool:
        return self.direction != Direction.NONE and self.strength > 0


class StrategyEngine:
    """Generates trade signals for open 15-minute Polymarket markets."""

    def __init__(self, config: Config, price_feed: PriceFeed) -> None:
        self.config = config
        self.price_feed = price_feed

    # ── Momentum Strategy ─────────────────────────────────────────────────────

    def _momentum_signal(self, market: Market) -> Optional[Signal]:
        """
        Use RSI + MACD + recent price change to determine direction.

        Returns a Signal if confidence meets MIN_SIGNAL_STRENGTH threshold,
        otherwise returns None.
        """
        asset = market.asset

        # Need fresh price data
        if not self.price_feed.is_data_fresh(asset, max_age_sec=30):
            logger.debug("Stale price data for %s, skipping momentum", asset)
            return None

        # Only enter within the entry window (first N seconds of a market)
        elapsed = market.seconds_elapsed
        if elapsed > self.config.ENTRY_WINDOW_SEC:
            logger.debug(
                "Market %s elapsed %ds > entry window %ds",
                market.condition_id[:8], elapsed, self.config.ENTRY_WINDOW_SEC
            )
            return None

        rsi = self.price_feed.rsi(asset)
        macd_data = self.price_feed.macd(asset)
        pct_change = self.price_feed.price_change_pct(asset, lookback_bars=5)

        scores: List[float] = []
        reasons: List[str] = []

        # ── RSI score ─────────────────────────────────────────
        rsi_score = 0.0
        if rsi is not None:
            if rsi >= self.config.RSI_OVERBOUGHT:
                # Momentum up: RSI above overbought = bullish continuation
                rsi_score = min((rsi - self.config.RSI_OVERBOUGHT) / (100 - self.config.RSI_OVERBOUGHT), 1.0)
                reasons.append(f"RSI={rsi:.1f}↑")
            elif rsi <= self.config.RSI_OVERSOLD:
                # Momentum down
                rsi_score = -min((self.config.RSI_OVERSOLD - rsi) / self.config.RSI_OVERSOLD, 1.0)
                reasons.append(f"RSI={rsi:.1f}↓")
            else:
                reasons.append(f"RSI={rsi:.1f}~")
        scores.append(rsi_score)

        # ── MACD score ────────────────────────────────────────
        macd_score = 0.0
        if macd_data:
            hist = macd_data["histogram"]
            macd_line = macd_data["macd"]
            # Normalize: bullish if histogram > 0 AND MACD > 0
            if hist > 0:
                macd_score = min(hist / (abs(macd_line) + 1e-9) * 0.5, 1.0)
                reasons.append(f"MACD↑")
            elif hist < 0:
                macd_score = -min(abs(hist) / (abs(macd_line) + 1e-9) * 0.5, 1.0)
                reasons.append(f"MACD↓")
        scores.append(macd_score)

        # ── Price change score ─────────────────────────────────
        pct_score = 0.0
        if pct_change is not None:
            # Scale: ±0.5% = ±0.5, capped at ±1.0
            pct_score = max(-1.0, min(1.0, pct_change / 0.5))
            direction_str = "↑" if pct_change > 0 else "↓"
            reasons.append(f"Δ5m={pct_change:+.2f}%{direction_str}")
        scores.append(pct_score)

        # ── Aggregate ─────────────────────────────────────────
        if not scores:
            return None

        avg_score = sum(scores) / len(scores)
        strength = abs(avg_score)

        if strength < self.config.MIN_SIGNAL_STRENGTH:
            logger.debug(
                "%s momentum strength %.2f < threshold %.2f — skipping",
                asset, strength, self.config.MIN_SIGNAL_STRENGTH
            )
            return None

        direction = Direction.UP if avg_score > 0 else Direction.DOWN

        # Get entry price
        up_tok = market.up_token
        down_tok = market.down_token
        if direction == Direction.UP:
            if not up_tok:
                return None
            entry_price = up_tok.price
        else:
            if not down_tok:
                return None
            entry_price = down_tok.price

        return Signal(
            market_condition_id=market.condition_id,
            asset=asset,
            direction=direction,
            strategy="momentum",
            strength=strength,
            entry_price=entry_price,
            entry_price_down=down_tok.price if down_tok else 0.0,
            reason=" | ".join(reasons),
        )

    # ── Arbitrage Strategy ─────────────────────────────────────────────────────

    def _arbitrage_signal(self, market: Market) -> Optional[Signal]:
        """
        Detect arbitrage: if UP.price + DOWN.price < ARB_MAX_COMBINED,
        buying both guarantees a profit when one resolves to $1.

        Expected profit = 1.0 - (UP.price + DOWN.price)
        Less Polymarket fees (~1% taker).
        """
        up_tok = market.up_token
        down_tok = market.down_token

        if not up_tok or not down_tok:
            return None
        if up_tok.price <= 0 or down_tok.price <= 0:
            return None

        combined = up_tok.price + down_tok.price
        if combined >= self.config.ARB_MAX_COMBINED:
            return None

        expected_profit_pct = (1.0 - combined) * 100
        strength = min((self.config.ARB_MAX_COMBINED - combined) / 0.05, 1.0)

        return Signal(
            market_condition_id=market.condition_id,
            asset=market.asset,
            direction=Direction.BOTH,
            strategy="arbitrage",
            strength=strength,
            entry_price=up_tok.price,
            entry_price_down=down_tok.price,
            reason=f"UP={up_tok.price:.3f} + DOWN={down_tok.price:.3f} = {combined:.3f} < {self.config.ARB_MAX_COMBINED} (edge={expected_profit_pct:.1f}%)",
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def evaluate(self, market: Market) -> List[Signal]:
        """
        Evaluate a market and return any actionable signals.
        May return 0, 1, or 2 signals (one per strategy).
        """
        signals: List[Signal] = []

        if self.config.STRATEGY in ("momentum", "both"):
            try:
                sig = self._momentum_signal(market)
                if sig and sig.is_actionable:
                    signals.append(sig)
                    logger.info(
                        "MOMENTUM signal: %s %s @ %.3f (strength=%.2f) — %s",
                        market.asset, sig.direction.value, sig.entry_price, sig.strength, sig.reason
                    )
            except Exception as e:
                logger.error("Momentum evaluation error: %s", e)

        if self.config.STRATEGY in ("arbitrage", "both"):
            try:
                sig = self._arbitrage_signal(market)
                if sig and sig.is_actionable:
                    signals.append(sig)
                    logger.info(
                        "ARBITRAGE signal: %s BOTH (strength=%.2f) — %s",
                        market.asset, sig.strength, sig.reason
                    )
            except Exception as e:
                logger.error("Arbitrage evaluation error: %s", e)

        return signals
