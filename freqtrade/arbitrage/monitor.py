"""
Arbitrage position monitor.
Handles quick-in/out and basis arbitrage monitoring logic.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from freqtrade.arbitrage.config import ArbConfig
from freqtrade.arbitrage.models import ArbTrade
from freqtrade.exchange import Exchange


logger = logging.getLogger(__name__)


class ArbMonitor:
    """
    Monitors open arbitrage positions and triggers closes when conditions are met.
    """

    def __init__(
        self,
        exchanges: dict[str, Exchange],
        config: ArbConfig,
        executor,  # ArbExecutor
        risk_manager,  # ArbRiskManager
        notifier=None,  # ArbNotifier
    ):
        self.exchanges = exchanges
        self.config = config
        self.executor = executor
        self.risk = risk_manager
        self.notifier = notifier

    def monitor_tick(self, arb_trades: list[ArbTrade]) -> None:
        """
        Called every scan_interval seconds. Dispatches to the appropriate
        monitoring logic based on strategy type.
        """
        for trade in arb_trades:
            try:
                if trade.strategy_type == "quick":
                    self._monitor_quick(trade)
                elif trade.strategy_type == "basis":
                    self._monitor_basis(trade)
            except Exception as e:
                logger.error(
                    "Error monitoring ArbTrade %d: %s", trade.id, e, exc_info=True
                )

    def _monitor_quick(self, trade: ArbTrade) -> None:
        """
        Quick in/out monitor: wait for settlement, then close.

        Quick strategy holds for ~30 seconds to collect the funding rate,
        then closes both sides immediately.
        """
        now = datetime.now(UTC)
        open_date = trade.open_date
        if open_date.tzinfo is None:
            open_date = open_date.replace(tzinfo=UTC)

        elapsed = (now - open_date).total_seconds()

        # Wait for settlement (default ~30 seconds)
        if elapsed < self.config.quick_advance_time:
            return

        logger.info(
            "Quick trade %d: settlement time reached (%.0fs elapsed), closing",
            trade.id,
            elapsed,
        )

        # Calculate PnL before closing
        pnl = self._estimate_pnl(trade)

        # Close both sides
        success = self.executor.execute_close(trade)
        if success:
            trade.exit_price_short = self._get_current_price(
                trade.exchange_short, trade.ft_pair
            )
            trade.exit_price_long = self._get_current_price(
                trade.exchange_long, trade.ft_pair
            )
            trade.close_trade("quick_settlement", pnl)
            self.risk.record_trade_result(pnl or 0)

            if self.notifier:
                self.notifier.notify_close(trade, "quick_settlement", pnl)
        else:
            logger.error("Failed to close quick trade %d", trade.id)
            if self.notifier:
                self.notifier.notify_warning(
                    trade, "Failed to close quick trade"
                )

    def _monitor_basis(self, trade: ArbTrade) -> None:
        """
        Basis arbitrage monitor: check multiple conditions.

        Priority order:
        1. Margin ratio > threshold -> close
        2. Funding rate reversal -> close
        3. Basis reversed (basis_rate <= 0) -> close
        4. Basis converged > target -> close (take profit)
        5. Single-side floating loss > stop_loss -> close
        6. Hold time > max -> close (timeout)
        """
        now = datetime.now(UTC)
        open_date = trade.open_date
        if open_date.tzinfo is None:
            open_date = open_date.replace(tzinfo=UTC)

        # 1. Check margin ratio
        for ex_name in [trade.exchange_short, trade.exchange_long]:
            exchange = self.exchanges.get(ex_name)
            if exchange:
                safe, reason = self.risk.check_margin(exchange, trade.ft_pair)
                if not safe:
                    logger.warning(
                        "ArbTrade %d: margin check failed on %s: %s",
                        trade.id, ex_name, reason,
                    )
                    self._close_trade(trade, f"margin_exceeded_{ex_name}")
                    return

        # 2. Check funding rate reversal
        current_rate_diff = self._get_current_funding_rate_diff(trade)
        if current_rate_diff is not None:
            rate_change = trade.entry_funding_rate_diff - current_rate_diff
            if rate_change > self.config.fee_reversal_threshold:
                logger.info(
                    "ArbTrade %d: funding rate reversed (%.4f%% -> %.4f%%)",
                    trade.id,
                    trade.entry_funding_rate_diff * 100,
                    current_rate_diff * 100,
                )
                self._close_trade(trade, "funding_rate_reversal")
                return

        # 3. Check basis direction
        current_basis = self._get_current_basis_rate(trade)
        if current_basis is not None and current_basis <= 0:
            logger.info(
                "ArbTrade %d: basis reversed (%.4f%% -> %.4f%%)",
                trade.id,
                trade.entry_basis_rate * 100,
                current_basis * 100,
            )
            self._close_trade(trade, "basis_reversed")
            return

        # 4. Check basis convergence (take profit)
        if (
            current_basis is not None
            and trade.entry_basis_rate != 0
        ):
            converge_ratio = abs(
                (trade.entry_basis_rate - current_basis) / trade.entry_basis_rate
            )
            if converge_ratio >= self.config.basis_converge_target:
                logger.info(
                    "ArbTrade %d: basis converged %.1f%% (target %.1f%%)",
                    trade.id,
                    converge_ratio * 100,
                    self.config.basis_converge_target * 100,
                )
                self._close_trade(trade, "basis_converged")
                return

        # 5. Check single-side floating loss (stop loss)
        pnl = self._estimate_pnl(trade)
        if pnl is not None and pnl < 0 and abs(pnl) >= self.config.stop_loss:
            logger.info(
                "ArbTrade %d: stop loss triggered (loss=%.4f%%)",
                trade.id,
                pnl * 100,
            )
            self._close_trade(trade, "stop_loss")
            return

        # 6. Check max hold time
        hold_days = (now - open_date).total_seconds() / 86400
        if hold_days >= self.config.max_hold_time:
            logger.info(
                "ArbTrade %d: max hold time reached (%.1f days)",
                trade.id,
                hold_days,
            )
            self._close_trade(trade, "max_hold_time")
            return

    def _close_trade(self, trade: ArbTrade, reason: str) -> None:
        """
        Close a trade with the given reason.
        """
        pnl = self._estimate_pnl(trade)
        success = self.executor.execute_close(trade)

        if success:
            trade.exit_price_short = self._get_current_price(
                trade.exchange_short, trade.ft_pair
            )
            trade.exit_price_long = self._get_current_price(
                trade.exchange_long, trade.ft_pair
            )
            trade.close_trade(reason, pnl)
            self.risk.record_trade_result(pnl or 0)

            if self.notifier:
                self.notifier.notify_close(trade, reason, pnl)
        else:
            logger.error("Failed to close ArbTrade %d (%s)", trade.id, reason)
            if self.notifier:
                self.notifier.notify_warning(
                    trade, f"Failed to close: {reason}"
                )

    def _estimate_pnl(self, trade: ArbTrade) -> float | None:
        """
        Estimate current PnL for a trade.
        PnL = (entry_price_short - current_price_short) * amount_short
            + (current_price_long - entry_price_long) * amount_long
            - costs
        """
        try:
            price_short = self._get_current_price(
                trade.exchange_short, trade.ft_pair
            )
            price_long = self._get_current_price(
                trade.exchange_long, trade.ft_pair
            )

            if price_short is None or price_long is None:
                return None

            # Short leg PnL (profit when price goes down)
            pnl_short = (trade.entry_price_short - price_short) * trade.amount_short
            # Long leg PnL (profit when price goes up)
            pnl_long = (price_long - trade.entry_price_long) * trade.amount_long

            total_pnl = pnl_short + pnl_long
            # Deduct estimated costs
            costs = (
                (trade.entry_price_short * trade.amount_short
                 + trade.entry_price_long * trade.amount_long)
                * (self.config.slippage + self.config.fee_rate)
            )

            return total_pnl - costs

        except Exception as e:
            logger.warning("Failed to estimate PnL for trade %d: %s", trade.id, e)
            return None

    def _get_current_price(self, exchange_name: str, symbol: str) -> float | None:
        """Get current price from an exchange."""
        exchange = self.exchanges.get(exchange_name)
        if not exchange:
            return None
        try:
            ticker = exchange.fetch_ticker(symbol)
            return float(ticker.get("last", 0))
        except Exception as e:
            logger.warning(
                "Failed to get price from %s for %s: %s",
                exchange_name, symbol, e,
            )
            return None

    def _get_current_funding_rate_diff(self, trade: ArbTrade) -> float | None:
        """Get current funding rate difference between two exchanges."""
        try:
            ex_short = self.exchanges.get(trade.exchange_short)
            ex_long = self.exchanges.get(trade.exchange_long)
            if not ex_short or not ex_long:
                return None

            rate_short = ex_short.fetch_funding_rate(trade.ft_pair)
            rate_long = ex_long.fetch_funding_rate(trade.ft_pair)

            fr_short = float(rate_short.get("fundingRate", 0))
            fr_long = float(rate_long.get("fundingRate", 0))

            return abs(fr_short - fr_long)
        except Exception as e:
            logger.warning("Failed to get funding rates: %s", e)
            return None

    def _get_current_basis_rate(self, trade: ArbTrade) -> float | None:
        """Get current basis rate between two exchanges."""
        price_short = self._get_current_price(trade.exchange_short, trade.ft_pair)
        price_long = self._get_current_price(trade.exchange_long, trade.ft_pair)

        if price_short is None or price_long is None or price_long == 0:
            return None

        return (price_short - price_long) / price_long
