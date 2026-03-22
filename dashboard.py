"""Rich terminal dashboard for Polymarket Positioner."""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import Config
from database import Database
from market_data import Market
from price_feed import PriceFeed


class Dashboard:
    """Rich terminal dashboard displaying prices, signals, and PnL."""

    def __init__(self, config: Config, db: Database, price_feed: PriceFeed) -> None:
        self.config = config
        self.db = db
        self.price_feed = price_feed
        self.console = Console()

        # State updated by main loop
        self.status = "STARTING"
        self.open_markets: List[Market] = []
        self.last_signals: List[Dict[str, Any]] = []
        self.last_render_time = ""
        self.cycle_count = 0
        self._start_time = time.time()

    # ── Header Panel ──────────────────────────────────────────────────────────

    def _header(self) -> Panel:
        uptime_s = int(time.time() - self._start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode = "[red]LIVE[/red]" if not self.config.DRY_RUN else "[yellow]DRY RUN[/yellow]"

        txt = Text()
        txt.append("Polymarket Positioner  ", style="bold cyan")
        txt.append(f"│ {mode}  ", style="dim")
        txt.append(f"│ {now}  ", style="dim")
        txt.append(f"│ Uptime: {h:02d}:{m:02d}:{s:02d}  ", style="dim")
        txt.append(f"│ Strategy: {self.config.STRATEGY.upper()}  ", style="dim")
        txt.append(f"│ Status: {self.status}", style="bold green")

        return Panel(txt, border_style="cyan")

    # ── Price Table ───────────────────────────────────────────────────────────

    def _price_table(self) -> Panel:
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Asset", style="cyan", width=6)
        table.add_column("Price", justify="right", width=12)
        table.add_column("RSI", justify="right", width=7)
        table.add_column("MACD", justify="right", width=10)
        table.add_column("Δ5m %", justify="right", width=8)
        table.add_column("Feed Age", justify="right", width=10)

        for asset in self.config.TARGET_ASSETS:
            price = self.price_feed.get_price(asset)
            rsi = self.price_feed.rsi(asset)
            macd_data = self.price_feed.macd(asset)
            pct = self.price_feed.price_change_pct(asset, lookback_bars=5)
            tick = self.price_feed.get_tick(asset)

            price_str = f"${price:,.2f}" if price else "—"

            if rsi is not None:
                if rsi >= self.config.RSI_OVERBOUGHT:
                    rsi_str = f"[green]{rsi:.0f}[/green]"
                elif rsi <= self.config.RSI_OVERSOLD:
                    rsi_str = f"[red]{rsi:.0f}[/red]"
                else:
                    rsi_str = f"[white]{rsi:.0f}[/white]"
            else:
                rsi_str = "—"

            if macd_data:
                hist = macd_data["histogram"]
                macd_str = (
                    f"[green]{hist:+.4f}[/green]"
                    if hist > 0
                    else f"[red]{hist:+.4f}[/red]"
                )
            else:
                macd_str = "—"

            if pct is not None:
                pct_str = (
                    f"[green]{pct:+.2f}%[/green]"
                    if pct > 0
                    else f"[red]{pct:+.2f}%[/red]"
                )
            else:
                pct_str = "—"

            if tick:
                age = (datetime.now(timezone.utc) - tick.timestamp).total_seconds()
                age_str = f"{age:.0f}s" if age < 60 else f"{age/60:.1f}m"
                age_style = "green" if age < 15 else "yellow" if age < 30 else "red"
                age_str = f"[{age_style}]{age_str}[/{age_style}]"
            else:
                age_str = "[red]no data[/red]"

            table.add_row(asset, price_str, rsi_str, macd_str, pct_str, age_str)

        return Panel(table, title="[bold]Price Feed (Binance)[/bold]", border_style="magenta")

    # ── Active Markets ────────────────────────────────────────────────────────

    def _markets_table(self) -> Panel:
        table = Table(show_header=True, header_style="bold blue", expand=True)
        table.add_column("Asset", width=5)
        table.add_column("Question", min_width=30)
        table.add_column("UP", justify="right", width=7)
        table.add_column("DOWN", justify="right", width=7)
        table.add_column("Sum", justify="right", width=7)
        table.add_column("Elapsed", justify="right", width=9)
        table.add_column("Remaining", justify="right", width=10)

        markets = sorted(self.open_markets, key=lambda m: m.asset)
        if not markets:
            table.add_row("—", "[dim]No open 15-minute markets found[/dim]", "—", "—", "—", "—", "—")
        else:
            for m in markets[:15]:
                up_tok = m.up_token
                down_tok = m.down_token
                up_price = f"{up_tok.price:.3f}" if up_tok else "—"
                down_price = f"{down_tok.price:.3f}" if down_tok else "—"
                combined = m.combined_price
                combined_str = (
                    f"[green]{combined:.3f}[/green]"
                    if combined < self.config.ARB_MAX_COMBINED
                    else f"[white]{combined:.3f}[/white]"
                )
                elapsed = m.seconds_elapsed
                remaining = m.seconds_remaining
                elapsed_str = f"{elapsed:.0f}s"
                remain_str = (
                    f"[yellow]{remaining:.0f}s[/yellow]"
                    if remaining < 60
                    else f"{remaining:.0f}s"
                )
                question_abbr = m.question[:40] + ("…" if len(m.question) > 40 else "")
                table.add_row(
                    m.asset, question_abbr, up_price, down_price,
                    combined_str, elapsed_str, remain_str
                )

        return Panel(table, title=f"[bold]Open 15-Minute Markets ({len(markets)})[/bold]", border_style="blue")

    # ── Performance ───────────────────────────────────────────────────────────

    def _stats_panel(self, stats: Dict[str, Any]) -> Panel:
        total_pnl = stats.get("total_pnl", 0.0)
        exposure = stats.get("total_exposure", 0.0)
        win_rate = stats.get("win_rate", 0.0)
        closed = stats.get("closed_orders", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        open_count = stats.get("open_orders", 0) + stats.get("dry_run_orders", 0)
        mom_pnl = stats.get("momentum_pnl", 0.0)
        arb_pnl = stats.get("arbitrage_pnl", 0.0)

        pnl_style = "green" if total_pnl >= 0 else "red"
        pnl_str = f"[{pnl_style}]${total_pnl:+.2f}[/{pnl_style}]"

        table = Table.grid(padding=(0, 2))
        table.add_column()
        table.add_column()
        table.add_column()
        table.add_column()

        table.add_row(
            f"Total PnL: {pnl_str}",
            f"Exposure: [yellow]${exposure:.2f}[/yellow]",
            f"Win Rate: [cyan]{win_rate:.1f}%[/cyan]",
            f"Trades: [white]{wins}W/{losses}L ({closed} closed)[/white]",
        )
        table.add_row(
            f"Open: [white]{open_count}[/white]",
            f"Momentum PnL: [{'green' if mom_pnl >= 0 else 'red'}]${mom_pnl:+.2f}[/{'green' if mom_pnl >= 0 else 'red'}]",
            f"Arb PnL: [{'green' if arb_pnl >= 0 else 'red'}]${arb_pnl:+.2f}[/{'green' if arb_pnl >= 0 else 'red'}]",
            f"Cycles: [dim]{self.cycle_count}[/dim]",
        )

        return Panel(table, title="[bold]Performance[/bold]", border_style="green")

    # ── Recent Orders ─────────────────────────────────────────────────────────

    def _orders_table(self, stats: Dict[str, Any]) -> Panel:
        table = Table(show_header=True, header_style="bold white", expand=True)
        table.add_column("Asset", width=5)
        table.add_column("Strategy", width=10)
        table.add_column("Side", width=6)
        table.add_column("Price", justify="right", width=7)
        table.add_column("USDC", justify="right", width=8)
        table.add_column("Status", width=9)
        table.add_column("PnL", justify="right", width=9)
        table.add_column("Time", width=10)

        recent = stats.get("recent_orders", [])
        if not recent:
            table.add_row("—", "—", "—", "—", "—", "[dim]No orders yet[/dim]", "—", "—")
        else:
            for order in recent:
                status = order.get("status", "?")
                pnl = order.get("pnl")
                opened = (order.get("opened_at", "") or "")[:19].replace("T", " ")

                if status == "CLOSED":
                    status_str = "[green]CLOSED[/green]"
                elif status == "DRY_RUN":
                    status_str = "[yellow]DRY_RUN[/yellow]"
                else:
                    status_str = "[blue]OPEN[/blue]"

                if pnl is not None:
                    pnl_str = (
                        f"[green]${pnl:+.2f}[/green]"
                        if pnl >= 0
                        else f"[red]${pnl:+.2f}[/red]"
                    )
                else:
                    pnl_str = "—"

                table.add_row(
                    order.get("asset", "?"),
                    order.get("strategy", "?"),
                    order.get("outcome", "?"),
                    f"{order.get('price', 0):.3f}",
                    f"${order.get('usdc_amount', 0):.2f}",
                    status_str,
                    pnl_str,
                    opened[-8:] if len(opened) >= 8 else opened,
                )

        return Panel(table, title="[bold]Recent Orders (last 10)[/bold]", border_style="white")

    # ── Render ────────────────────────────────────────────────────────────────

    def render(self, stats: Dict[str, Any]) -> None:
        """Print a full dashboard snapshot."""
        self.console.clear()
        self.console.print(self._header())
        self.console.print(self._price_table())
        self.console.print(self._markets_table())
        self.console.print(self._stats_panel(stats))
        self.console.print(self._orders_table(stats))
