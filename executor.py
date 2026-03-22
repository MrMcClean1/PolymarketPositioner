"""Trade executor — places orders on Polymarket via the CLOB SDK."""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from config import Config
from database import Database
from market_data import Market, MarketToken
from strategy import Direction, Signal

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Places orders on Polymarket for positioner signals."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self._client = None
        self._initialized = False

    # ── Initialization ────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Initialize CLOB client for live trading."""
        if self.config.DRY_RUN:
            logger.info("DRY RUN mode — CLOB client not initialized")
            return True

        if not self.config.PRIVATE_KEY or not self.config.PROXY_ADDRESS:
            logger.warning("Missing credentials — cannot initialize CLOB client")
            return False

        try:
            from py_clob_client.client import ClobClient

            self._client = ClobClient(
                self.config.CLOB_HOST,
                key=self.config.PRIVATE_KEY,
                chain_id=self.config.CHAIN_ID,
                signature_type=self.config.SIGNATURE_TYPE,
                funder=self.config.PROXY_ADDRESS,
            )
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            self._initialized = True
            logger.info("CLOB client initialized successfully")
            return True
        except Exception as e:
            logger.error("CLOB client initialization failed: %s", e)
            return False

    # ── Exposure Checks ───────────────────────────────────────────────────────

    def _check_exposure(self, condition_id: str, usdc_amount: float) -> Tuple[bool, str]:
        """Return (ok, reason) for exposure limit checks."""
        market_exp = self.db.get_market_exposure(condition_id)
        if market_exp + usdc_amount > self.config.MAX_POSITION_PER_MARKET:
            return False, (
                f"Market exposure ${market_exp:.2f} + ${usdc_amount:.2f} "
                f"> limit ${self.config.MAX_POSITION_PER_MARKET:.2f}"
            )

        total_exp = self.db.get_total_exposure()
        if total_exp + usdc_amount > self.config.MAX_TOTAL_EXPOSURE:
            return False, (
                f"Total exposure ${total_exp:.2f} + ${usdc_amount:.2f} "
                f"> limit ${self.config.MAX_TOTAL_EXPOSURE:.2f}"
            )
        return True, ""

    def _size_for_signal(self, signal: Signal) -> float:
        """Determine trade size in USDC based on signal strength."""
        base = self.config.TRADE_SIZE_USDC
        # Scale by signal strength (stronger signal → larger size)
        sized = base * (0.5 + 0.5 * signal.strength)
        return min(max(sized, self.config.MIN_TRADE_SIZE_USDC), self.config.MAX_TRADE_SIZE_USDC)

    # ── Order Execution ───────────────────────────────────────────────────────

    def _place_order(
        self,
        token: MarketToken,
        usdc_amount: float,
        condition_id: str,
        market: Market,
    ) -> bool:
        """Place a single GTC limit order. Returns True on success."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # Convert USDC amount to shares: shares = USDC / price
        if token.price <= 0:
            logger.warning("Invalid token price %.4f — skipping order", token.price)
            return False

        shares = usdc_amount / token.price
        now_str = datetime.now(timezone.utc).isoformat()

        for attempt in range(3):
            try:
                order_args = OrderArgs(
                    price=token.price,
                    size=shares,
                    side=BUY,
                    token_id=token.token_id,
                )
                signed = self._client.create_order(order_args)
                resp = self._client.post_order(signed, OrderType.GTC)

                order_id = ""
                if isinstance(resp, dict):
                    order_id = resp.get("orderID", resp.get("id", ""))

                self.db.record_order(
                    condition_id=condition_id,
                    token_id=token.token_id,
                    market_question=market.question,
                    asset=market.asset,
                    outcome=token.outcome,
                    price=token.price,
                    shares=shares,
                    usdc_amount=usdc_amount,
                    order_id=order_id,
                    status="OPEN",
                    opened_at=now_str,
                )
                logger.info(
                    "Order placed: %s %s %.4f shares @ %.4f ($%.2f) — order=%s",
                    market.asset, token.outcome, shares, token.price, usdc_amount, order_id
                )
                return True

            except Exception as e:
                wait = 2 ** attempt
                logger.error("Order attempt %d failed: %s (retry in %ds)", attempt + 1, e, wait)
                if attempt < 2:
                    time.sleep(wait)

        logger.error("All order attempts failed for %s %s", market.asset, token.outcome)
        return False

    # ── Public API ─────────────────────────────────────────────────────────────

    def execute(self, signal: Signal, market: Market) -> bool:
        """
        Execute a trade signal. Handles momentum (one side) and
        arbitrage (both sides) orders.

        Returns True if at least one order succeeded.
        """
        usdc_per_side = self._size_for_signal(signal)

        if signal.direction == Direction.BOTH:
            # Arbitrage: buy both UP and DOWN
            total_usdc = usdc_per_side * 2
            ok, reason = self._check_exposure(market.condition_id, total_usdc)
            if not ok:
                logger.info("Arbitrage skipped — exposure: %s", reason)
                return False

            up_tok = market.up_token
            down_tok = market.down_token
            if not up_tok or not down_tok:
                logger.warning("Arbitrage: missing tokens for %s", market.condition_id[:8])
                return False

            if self.config.DRY_RUN:
                logger.info(
                    "[DRY RUN] ARB: BUY UP %.4f @ %.4f + DOWN %.4f @ %.4f — %s",
                    usdc_per_side / up_tok.price, up_tok.price,
                    usdc_per_side / down_tok.price, down_tok.price,
                    market.asset,
                )
                self._record_dry_run(market, up_tok, usdc_per_side, "arbitrage")
                self._record_dry_run(market, down_tok, usdc_per_side, "arbitrage")
                return True

            if not self._initialized:
                logger.error("CLOB not initialized")
                return False

            success_up = self._place_order(up_tok, usdc_per_side, market.condition_id, market)
            success_down = self._place_order(down_tok, usdc_per_side, market.condition_id, market)
            return success_up or success_down

        else:
            # Momentum: buy one side
            if signal.direction == Direction.UP:
                token = market.up_token
            else:
                token = market.down_token

            if not token:
                logger.warning("No token found for direction %s in market %s", signal.direction, market.condition_id[:8])
                return False

            ok, reason = self._check_exposure(market.condition_id, usdc_per_side)
            if not ok:
                logger.info("Momentum trade skipped — exposure: %s", reason)
                return False

            if self.config.DRY_RUN:
                logger.info(
                    "[DRY RUN] MOMENTUM: BUY %s %s %.4f shares @ %.4f ($%.2f) — %s",
                    market.asset, token.outcome,
                    usdc_per_side / (token.price or 1),
                    token.price, usdc_per_side, signal.reason
                )
                self._record_dry_run(market, token, usdc_per_side, "momentum")
                return True

            if not self._initialized:
                logger.error("CLOB not initialized")
                return False

            return self._place_order(token, usdc_per_side, market.condition_id, market)

    def _record_dry_run(
        self,
        market: Market,
        token: MarketToken,
        usdc_amount: float,
        strategy: str,
    ) -> None:
        """Record a dry-run order in the database."""
        shares = usdc_amount / (token.price or 1)
        self.db.record_order(
            condition_id=market.condition_id,
            token_id=token.token_id,
            market_question=market.question,
            asset=market.asset,
            outcome=token.outcome,
            price=token.price,
            shares=shares,
            usdc_amount=usdc_amount,
            order_id="",
            status="DRY_RUN",
            opened_at=datetime.now(timezone.utc).isoformat(),
            strategy=strategy,
        )
