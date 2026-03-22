"""SQLite database for Polymarket Positioner — tracks orders and positions."""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)


class Database:
    """Thread-safe SQLite wrapper for order and position tracking."""

    def __init__(self, path: str = "positioner.db") -> None:
        self.path = path
        self._create_tables()

    # ── Connection ─────────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Schema ─────────────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS orders (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition_id    TEXT    NOT NULL,
                    token_id        TEXT    NOT NULL,
                    market_question TEXT    NOT NULL,
                    asset           TEXT    NOT NULL,
                    outcome         TEXT    NOT NULL,
                    strategy        TEXT    NOT NULL DEFAULT 'unknown',
                    price           REAL    NOT NULL,
                    shares          REAL    NOT NULL,
                    usdc_amount     REAL    NOT NULL,
                    order_id        TEXT    NOT NULL DEFAULT '',
                    status          TEXT    NOT NULL DEFAULT 'OPEN',
                    pnl             REAL,
                    resolution_price REAL,
                    opened_at       TEXT    NOT NULL,
                    closed_at       TEXT
                );

                CREATE TABLE IF NOT EXISTS stats (
                    key     TEXT PRIMARY KEY,
                    value   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_orders_condition ON orders(condition_id);
                CREATE INDEX IF NOT EXISTS idx_orders_status    ON orders(status);
                CREATE INDEX IF NOT EXISTS idx_orders_asset     ON orders(asset);
            """)

    def reset(self) -> None:
        """Drop and recreate all tables."""
        with self._conn() as conn:
            conn.executescript("""
                DROP TABLE IF EXISTS orders;
                DROP TABLE IF EXISTS stats;
            """)
        self._create_tables()
        logger.info("Database reset complete")

    # ── Orders ─────────────────────────────────────────────────────────────────

    def record_order(
        self,
        condition_id: str,
        token_id: str,
        market_question: str,
        asset: str,
        outcome: str,
        price: float,
        shares: float,
        usdc_amount: float,
        order_id: str,
        status: str,
        opened_at: str,
        strategy: str = "unknown",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO orders
                    (condition_id, token_id, market_question, asset, outcome,
                     strategy, price, shares, usdc_amount, order_id, status, opened_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    condition_id, token_id, market_question, asset, outcome,
                    strategy, price, shares, usdc_amount, order_id, status, opened_at,
                ),
            )
            return cur.lastrowid

    def close_order(
        self,
        order_id_or_db_id: str,
        resolution_price: float,
        pnl: float,
        closed_at: Optional[str] = None,
    ) -> None:
        if closed_at is None:
            closed_at = datetime.now(timezone.utc).isoformat()

        with self._conn() as conn:
            conn.execute(
                """
                UPDATE orders
                SET status='CLOSED', resolution_price=?, pnl=?, closed_at=?
                WHERE (order_id=? OR id=?) AND status NOT IN ('CLOSED', 'DRY_RUN')
                """,
                (resolution_price, pnl, closed_at, str(order_id_or_db_id), str(order_id_or_db_id)),
            )

    def get_open_orders(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status='OPEN' ORDER BY opened_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_orders_for_market(self, condition_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE condition_id=? ORDER BY opened_at DESC",
                (condition_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def has_open_position(self, condition_id: str) -> bool:
        """True if there's already an open (non-dry-run) order for this market."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE condition_id=? AND status='OPEN'",
                (condition_id,),
            ).fetchone()
            return row[0] > 0

    def has_any_position(self, condition_id: str) -> bool:
        """True if any order (open or dry-run) was placed for this market."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE condition_id=? AND status IN ('OPEN','DRY_RUN')",
                (condition_id,),
            ).fetchone()
            return row[0] > 0

    # ── Exposure ───────────────────────────────────────────────────────────────

    def get_market_exposure(self, condition_id: str) -> float:
        """Total USDC at risk in a specific market (open + dry-run orders)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usdc_amount),0) FROM orders WHERE condition_id=? AND status IN ('OPEN','DRY_RUN')",
                (condition_id,),
            ).fetchone()
            return float(row[0])

    def get_total_exposure(self) -> float:
        """Total USDC at risk across all open positions."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usdc_amount),0) FROM orders WHERE status IN ('OPEN','DRY_RUN')"
            ).fetchone()
            return float(row[0])

    # ── Statistics ─────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Return summary statistics."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            open_count = conn.execute("SELECT COUNT(*) FROM orders WHERE status='OPEN'").fetchone()[0]
            dry_count = conn.execute("SELECT COUNT(*) FROM orders WHERE status='DRY_RUN'").fetchone()[0]
            closed = conn.execute("SELECT COUNT(*) FROM orders WHERE status='CLOSED'").fetchone()[0]
            total_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM orders WHERE status='CLOSED'"
            ).fetchone()[0]
            wins = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status='CLOSED' AND pnl > 0"
            ).fetchone()[0]
            losses = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status='CLOSED' AND pnl <= 0"
            ).fetchone()[0]

            # By strategy
            momentum_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM orders WHERE status='CLOSED' AND strategy='momentum'"
            ).fetchone()[0]
            arb_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM orders WHERE status='CLOSED' AND strategy='arbitrage'"
            ).fetchone()[0]

            # Recent orders (last 10)
            recent = conn.execute(
                "SELECT * FROM orders ORDER BY opened_at DESC LIMIT 10"
            ).fetchall()

        win_rate = (wins / closed * 100) if closed > 0 else 0.0
        exposure = self.get_total_exposure()

        return {
            "total_orders": total,
            "open_orders": open_count,
            "dry_run_orders": dry_count,
            "closed_orders": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": float(total_pnl),
            "momentum_pnl": float(momentum_pnl),
            "arbitrage_pnl": float(arb_pnl),
            "total_exposure": exposure,
            "recent_orders": [dict(r) for r in recent],
        }
