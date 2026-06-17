"""
Arbitrage engine: orchestrates scanner, executor, monitor, and notifications.
"""

import logging
import time
from typing import Any

from freqtrade.arbitrage.config import ArbConfig
from freqtrade.arbitrage.executor import ArbExecutor
from freqtrade.arbitrage.models import ArbTrade
from freqtrade.arbitrage.monitor import ArbMonitor
from freqtrade.arbitrage.notifier import ArbNotifier
from freqtrade.arbitrage.risk import ArbRiskManager
from freqtrade.arbitrage.scanner import ArbScanner
from freqtrade.exchange import Exchange


logger = logging.getLogger(__name__)


class ArbEngine:
    """
    Main arbitrage engine.
    Orchestrates: scan -> decide -> execute -> monitor loop.
    """

    def __init__(
        self,
        config: dict[str, Any],
        exchanges: dict[str, Exchange],
        arb_config: ArbConfig,
    ):
        self.config = config
        self.exchanges = exchanges
        self.arb_config = arb_config

        # Core modules
        self.scanner = ArbScanner(exchanges, arb_config)
        self.executor = ArbExecutor(exchanges, arb_config)
        self.risk = ArbRiskManager(arb_config)
        self.notifier = ArbNotifier(config)
        self.monitor = ArbMonitor(
            exchanges, arb_config, self.executor, self.risk, self.notifier
        )

        # Status notification counter
        self._tick_count = 0
        self._status_interval = 360  # Send status every ~1 hour (360 * 10s)

    def run(self) -> None:
        """
        Main loop: scan for opportunities, execute trades, monitor positions.
        """
        logger.info(
            "Arbitrage engine started. Monitoring %d symbols across %d exchanges.",
            len(self.arb_config.symbols),
            len(self.exchanges),
        )

        # Startup notification
        self.notifier.send_text(
            f"🚀 <b>套利系统启动</b>\n"
            f"监控币种: {len(self.arb_config.symbols)}\n"
            f"交易所: {', '.join(self.exchanges.keys())}\n"
            f"最大并发: {self.arb_config.max_concurrent_positions}\n"
            f"扫描间隔: {self.arb_config.scan_interval}秒"
        )

        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Arbitrage engine stopped by user.")
                self.notifier.send_text("🛑 <b>套利系统已停止</b> (用户中断)")
                break
            except Exception as e:
                logger.error("Error in arbitrage loop: %s", e, exc_info=True)
                self.notifier.notify_warning(
                    type("_Dummy", (), {"ft_pair": "N/A",
                                        "exchange_short": "N/A",
                                        "exchange_long": "N/A"})(),
                    f"主循环异常: {e}",
                )

            time.sleep(self.arb_config.scan_interval)

    def _tick(self) -> None:
        """
        Single iteration of the main loop.
        """
        self._tick_count += 1

        # 1. Get current open trades
        open_trades = ArbTrade.get_open_trades()

        # 2. Monitor existing trades
        if open_trades:
            self.monitor.monitor_tick(open_trades)

        # 3. Periodic status notification
        if self._tick_count >= self._status_interval:
            self._tick_count = 0
            self.notifier.notify_status(
                len(open_trades),
                self.arb_config.max_concurrent_positions,
                self.risk.daily_pnl,
            )

        # 4. Check if we can open new positions
        if len(open_trades) >= self.arb_config.max_concurrent_positions:
            return

        if self.risk.is_paused:
            logger.debug("Arbitrage paused: %s", self.risk.pause_reason)
            return

        # 5. Scan for new opportunities
        opportunities = self.scanner.scan_all()

        # Log scan results
        if opportunities:
            logger.info(
                "Scan: %d opportunities | Top: %s short@%s(%.4f%%) long@%s(%.4f%%) "
                "diff=%.4f%% basis=%.4f%% quickP=%.4f%% basisP=%.4f%%",
                len(opportunities),
                opportunities[0].symbol,
                opportunities[0].exchange_short,
                opportunities[0].funding_rate_short * 100,
                opportunities[0].exchange_long,
                opportunities[0].funding_rate_long * 100,
                opportunities[0].funding_rate_diff * 100,
                opportunities[0].basis_rate * 100,
                opportunities[0].quick_profit * 100,
                opportunities[0].basis_profit * 100,
            )

        for opp in opportunities:
            # Risk check
            ok, reason = self.risk.can_open(open_trades, opp.symbol)
            if not ok:
                logger.debug("Skipping %s: %s", opp.symbol, reason)
                continue

            # Decide strategy
            strategy = self.executor.decide_strategy(opp)
            if strategy:
                logger.info(
                    "Opening %s arbitrage: %s short@%s long@%s",
                    strategy,
                    opp.symbol,
                    opp.exchange_short,
                    opp.exchange_long,
                )
                arb_trade = self.executor.execute_open(opp, strategy)
                if arb_trade:
                    self.notifier.notify_open(arb_trade, opp)
                break  # One trade per tick
