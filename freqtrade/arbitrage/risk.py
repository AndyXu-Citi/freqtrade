"""
Risk management for cross-exchange arbitrage.
"""

import logging
from datetime import UTC, datetime

from freqtrade.arbitrage.config import ArbConfig
from freqtrade.arbitrage.models import ArbTrade
from freqtrade.exchange import Exchange


logger = logging.getLogger(__name__)


class ArbRiskManager:
    """
    Risk manager for arbitrage operations.
    Checks position limits, margin ratios, daily loss limits, etc.
    """

    def __init__(self, config: ArbConfig):
        self.config = config
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.last_reset_date: datetime = datetime.now(UTC).date()
        self._paused: bool = False
        self._pause_reason: str = ""

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    def pause(self, reason: str) -> None:
        """Manually pause arbitrage."""
        self._paused = True
        self._pause_reason = reason
        logger.warning("Arbitrage paused: %s", reason)

    def resume(self) -> None:
        """Resume arbitrage."""
        self._paused = False
        self._pause_reason = ""
        logger.info("Arbitrage resumed")

    def can_open(
        self, open_trades: list[ArbTrade], symbol: str
    ) -> tuple[bool, str]:
        """
        Check if a new arbitrage position can be opened.

        :returns: (allowed, reason)
        """
        # Check if manually paused
        if self._paused:
            return False, f"Paused: {self._pause_reason}"

        # Reset daily stats if new day
        self._check_daily_reset()

        # 1. Max concurrent positions
        if len(open_trades) >= self.config.max_concurrent_positions:
            return (
                False,
                f"Max concurrent positions reached ({self.config.max_concurrent_positions})",
            )

        # 2. Duplicate pair check
        for trade in open_trades:
            if trade.ft_pair == symbol:
                return False, f"Already have open position for {symbol}"

        # 3. Daily loss limit
        if self.daily_pnl < 0 and abs(self.daily_pnl) >= self.config.max_daily_loss:
            return (
                False,
                f"Daily loss limit reached: {self.daily_pnl:.4f}",
            )

        # 4. Consecutive loss limit
        if self.consecutive_losses >= 3:
            self.pause(f"3 consecutive losses")
            return False, "3 consecutive losses - paused"

        return True, "ok"

    def check_margin(self, exchange: Exchange, symbol: str) -> tuple[bool, str]:
        """
        Check if margin ratio is within limits.

        :returns: (safe, reason). True means safe to continue.
        """
        try:
            balance_info = exchange.get_balances()
            usdt_info = balance_info.get("USDT", {})

            total = float(usdt_info.get("total", 0) or 0)
            used = float(usdt_info.get("used", 0) or 0)

            if total <= 0:
                return True, "No balance info available"

            margin_ratio = used / total

            if margin_ratio > self.config.margin_threshold:
                return (
                    False,
                    f"Margin ratio {margin_ratio:.2%} exceeds threshold "
                    f"{self.config.margin_threshold:.2%}",
                )

            return True, f"Margin ratio {margin_ratio:.2%} OK"

        except Exception as e:
            logger.warning("Failed to check margin on %s: %s", exchange.name, e)
            return True, "Could not check margin"

    def record_trade_result(self, pnl: float) -> None:
        """
        Record the result of a closed trade.

        :param pnl: Realized profit/loss (positive = profit, negative = loss).
        """
        self._check_daily_reset()

        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
            logger.info(
                "Trade loss recorded: %.4f USDT, consecutive losses: %d, daily PnL: %.4f",
                pnl,
                self.consecutive_losses,
                self.daily_pnl,
            )
        else:
            self.consecutive_losses = 0
            logger.info(
                "Trade profit recorded: %.4f USDT, daily PnL: %.4f",
                pnl,
                self.daily_pnl,
            )

        # Check if we should pause
        if self.consecutive_losses >= 3:
            self.pause("3 consecutive losses")
        elif (
            self.daily_pnl < 0
            and abs(self.daily_pnl) >= self.config.max_daily_loss
        ):
            self.pause(
                f"Daily loss {self.daily_pnl:.4f} exceeds limit "
                f"{self.config.max_daily_loss}"
            )

    def _check_daily_reset(self) -> None:
        """Reset daily stats at midnight."""
        today = datetime.now(UTC).date()
        if today != self.last_reset_date:
            logger.info(
                "New day: resetting daily PnL (was %.4f) and loss count (was %d)",
                self.daily_pnl,
                self.consecutive_losses,
            )
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self.last_reset_date = today
            # Auto-resume if was paused due to daily loss
            if self._paused and "Daily loss" in self._pause_reason:
                self.resume()
