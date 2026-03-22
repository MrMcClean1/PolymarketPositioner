"""Polymarket market discovery and live price polling for 15-minute crypto markets."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class MarketToken:
    token_id: str
    outcome: str          # "UP" or "DOWN"
    price: float = 0.5


@dataclass
class Market:
    condition_id: str
    question: str
    asset: str            # BTC, ETH, SOL, XRP
    interval_minutes: int
    start_time: datetime
    end_time: datetime
    tokens: List[MarketToken] = field(default_factory=list)
    active: bool = True

    @property
    def up_token(self) -> Optional[MarketToken]:
        for t in self.tokens:
            if t.outcome.upper() in ("UP", "YES"):
                return t
        return self.tokens[0] if self.tokens else None

    @property
    def down_token(self) -> Optional[MarketToken]:
        for t in self.tokens:
            if t.outcome.upper() in ("DOWN", "NO"):
                return t
        return self.tokens[1] if len(self.tokens) > 1 else None

    @property
    def combined_price(self) -> float:
        up = self.up_token
        down = self.down_token
        if up and down:
            return up.price + down.price
        return 1.0

    @property
    def seconds_to_open(self) -> float:
        now = datetime.now(timezone.utc)
        delta = (self.start_time - now).total_seconds()
        return max(0.0, delta)

    @property
    def seconds_elapsed(self) -> float:
        now = datetime.now(timezone.utc)
        delta = (now - self.start_time).total_seconds()
        return max(0.0, delta)

    @property
    def seconds_remaining(self) -> float:
        now = datetime.now(timezone.utc)
        delta = (self.end_time - now).total_seconds()
        return max(0.0, delta)

    @property
    def is_open(self) -> bool:
        now = datetime.now(timezone.utc)
        return self.start_time <= now <= self.end_time


# ── Market Data Client ────────────────────────────────────────────────────────

class MarketDataClient:
    """Fetches and tracks active 15-minute Polymarket crypto markets."""

    ASSET_KEYWORDS = {
        "BTC": ["bitcoin", "btc"],
        "ETH": ["ethereum", "eth"],
        "SOL": ["solana", "sol"],
        "XRP": ["xrp", "ripple"],
    }

    def __init__(self, config: Config, session: requests.Session) -> None:
        self.config = config
        self.session = session
        self._markets: Dict[str, Market] = {}   # condition_id → Market
        self._last_refresh = 0.0

    # ── Fetching ─────────────────────────────────────────────────────────────

    def _fetch_active_markets(self) -> List[dict]:
        """Fetch raw active market data from the Gamma API."""
        try:
            resp = self.session.get(
                f"{self.config.GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "tag_slug": "crypto",
                    "limit": 200,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data.get("markets", [])
            if isinstance(data, list):
                return data
            return []
        except Exception as e:
            logger.error("Failed to fetch markets from Gamma API: %s", e)
            return []

    def _detect_asset(self, question: str) -> Optional[str]:
        """Detect which crypto asset a market is about."""
        q_lower = question.lower()
        for asset, keywords in self.ASSET_KEYWORDS.items():
            if any(kw in q_lower for kw in keywords):
                return asset
        return None

    def _is_interval_market(self, question: str) -> bool:
        """Check if market is a 15-minute interval market."""
        q_lower = question.lower()
        markers = ["15", "15m", "15-min", "15 min", "fifteen"]
        return any(m in q_lower for m in markers)

    def _parse_market(self, raw: dict) -> Optional[Market]:
        """Parse a raw market dict into a Market object."""
        try:
            question = raw.get("question", "") or raw.get("title", "")
            if not question:
                return None

            # Only target configured assets
            asset = self._detect_asset(question)
            if not asset or asset not in self.config.TARGET_ASSETS:
                return None

            # Only target 15-minute interval markets
            if not self._is_interval_market(question):
                return None

            condition_id = raw.get("conditionId") or raw.get("condition_id") or raw.get("id", "")
            if not condition_id:
                return None

            # Parse times
            start_iso = raw.get("startDate") or raw.get("start_date") or raw.get("startDateIso", "")
            end_iso = raw.get("endDate") or raw.get("end_date") or raw.get("endDateIso", "")

            if not start_iso or not end_iso:
                return None

            start_time = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            end_time = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))

            # Parse tokens
            tokens: List[MarketToken] = []
            raw_tokens = raw.get("tokens", raw.get("outcomes", []))
            for tok in raw_tokens:
                if isinstance(tok, dict):
                    token_id = tok.get("token_id", tok.get("tokenId", tok.get("id", "")))
                    outcome = tok.get("outcome", tok.get("name", ""))
                    price = float(tok.get("price", 0.5))
                    if token_id:
                        tokens.append(MarketToken(
                            token_id=token_id,
                            outcome=outcome,
                            price=price,
                        ))

            return Market(
                condition_id=condition_id,
                question=question,
                asset=asset,
                interval_minutes=self.config.MARKET_INTERVAL,
                start_time=start_time,
                end_time=end_time,
                tokens=tokens,
                active=True,
            )
        except Exception as e:
            logger.debug("Failed to parse market: %s — %s", raw.get("question", "?"), e)
            return None

    # ── Price Update ─────────────────────────────────────────────────────────

    def _update_market_prices(self, market: Market) -> None:
        """Refresh token prices from CLOB midpoint endpoint."""
        try:
            token_ids = [t.token_id for t in market.tokens if t.token_id]
            if not token_ids:
                return

            # Batch midpoint request
            params = [("token_id", tid) for tid in token_ids]
            resp = self.session.get(
                f"{self.config.CLOB_HOST}/midpoints",
                params=params,
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()

            # data can be a dict of {token_id: price} or list
            if isinstance(data, dict):
                for tok in market.tokens:
                    if tok.token_id in data:
                        tok.price = float(data[tok.token_id])
            elif isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        tid = entry.get("asset_id", entry.get("token_id", ""))
                        price = entry.get("mid", entry.get("price", 0.5))
                        for tok in market.tokens:
                            if tok.token_id == tid:
                                tok.price = float(price)
        except Exception as e:
            logger.debug("Price update failed for %s: %s", market.asset, e)

    # ── Public API ────────────────────────────────────────────────────────────

    def refresh(self) -> int:
        """Refresh the full market list. Returns count of active markets found."""
        raw_markets = self._fetch_active_markets()
        found = 0

        new_markets: Dict[str, Market] = {}
        for raw in raw_markets:
            market = self._parse_market(raw)
            if market:
                new_markets[market.condition_id] = market
                found += 1

        self._markets = new_markets
        self._last_refresh = time.time()

        if found:
            logger.info("Found %d active 15-minute markets for assets: %s", found, self.config.TARGET_ASSETS)
        else:
            logger.warning("No 15-minute markets found. Will retry on next refresh.")

        return found

    def get_open_markets(self) -> List[Market]:
        """Return currently open (within trading window) markets."""
        return [m for m in self._markets.values() if m.is_open and m.active]

    def get_upcoming_markets(self, within_seconds: int = 60) -> List[Market]:
        """Return markets opening within N seconds."""
        return [
            m for m in self._markets.values()
            if 0 < m.seconds_to_open <= within_seconds
        ]

    def update_prices(self) -> None:
        """Update prices for all tracked markets."""
        for market in list(self._markets.values()):
            if market.is_open:
                self._update_market_prices(market)

    def get_market(self, condition_id: str) -> Optional[Market]:
        return self._markets.get(condition_id)

    def should_refresh(self) -> bool:
        return (time.time() - self._last_refresh) >= self.config.MARKET_REFRESH_INTERVAL

    @property
    def market_count(self) -> int:
        return len(self._markets)
