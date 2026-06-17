"""
Lightweight notification helper for arbitrage system.
Sends messages directly via Telegram Bot API without requiring FreqtradeBot.
"""

import logging
from typing import Any

from freqtrade.arbitrage.models import ArbTrade
from freqtrade.arbitrage.scanner import ArbOpportunity


logger = logging.getLogger(__name__)


class ArbNotifier:
    """
    Sends arbitrage notifications via Telegram.
    Uses the Telegram Bot API directly (no dependency on FreqtradeBot/RPCManager).
    """

    def __init__(self, config: dict[str, Any]):
        self.enabled = False
        self.token: str = ""
        self.chat_id: str = ""

        telegram_conf = config.get("telegram", {})
        if telegram_conf.get("enabled", False):
            self.token = telegram_conf.get("token", "")
            self.chat_id = telegram_conf.get("chat_id", "")
            if self.token and self.chat_id:
                self.enabled = True
                logger.info("Telegram notifications enabled for chat_id=%s", self.chat_id)
            else:
                logger.warning("Telegram enabled but token/chat_id missing")

    def send_text(self, text: str) -> bool:
        """
        Send a plain text message via Telegram Bot API.
        """
        if not self.enabled:
            return False

        try:
            import requests

            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            else:
                logger.warning(
                    "Telegram send failed: %s %s", resp.status_code, resp.text
                )
                return False
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return False

    def notify_open(
        self, arb_trade: ArbTrade, opp: ArbOpportunity
    ) -> None:
        """
        Notify about a new arbitrage position.
        """
        strategy_name = "基差套利" if arb_trade.strategy_type == "basis" else "快进快出"
        text = (
            f"🎯 <b>套利开仓 [{strategy_name}]</b>\n"
            f"币种: {arb_trade.ft_pair}\n"
            f"做空: {arb_trade.exchange_short} @ {arb_trade.entry_price_short} "
            f"(费率: {opp.funding_rate_short*100:.4f}%)\n"
            f"做多: {arb_trade.exchange_long} @ {arb_trade.entry_price_long} "
            f"(费率: {opp.funding_rate_long*100:.4f}%)\n"
            f"费率差: {opp.funding_rate_diff*100:.4f}% | "
            f"基差率: {opp.basis_rate*100:.4f}%\n"
            f"杠杆: {arb_trade.leverage}x"
        )
        self.send_text(text)

    def notify_close(
        self, arb_trade: ArbTrade, reason: str, pnl: float | None
    ) -> None:
        """
        Notify about a closed arbitrage position.
        """
        strategy_name = "基差套利" if arb_trade.strategy_type == "basis" else "快进快出"
        pnl_str = f"{pnl:+.4f}U" if pnl is not None else "N/A"
        emoji = "💰" if (pnl or 0) >= 0 else "💸"

        exit_short = arb_trade.exit_price_short or "N/A"
        exit_long = arb_trade.exit_price_long or "N/A"

        text = (
            f"{emoji} <b>套利平仓 [{strategy_name}]</b>\n"
            f"币种: {arb_trade.ft_pair}\n"
            f"做空: {arb_trade.exchange_short} "
            f"入场 {arb_trade.entry_price_short} → 出场 {exit_short}\n"
            f"做多: {arb_trade.exchange_long} "
            f"入场 {arb_trade.entry_price_long} → 出场 {exit_long}\n"
            f"盈亏: {pnl_str} | 原因: {reason}"
        )
        self.send_text(text)

    def notify_warning(self, arb_trade: ArbTrade, message: str) -> None:
        """
        Send a warning notification.
        """
        text = (
            f"⚠️ <b>套利预警</b>\n"
            f"币种: {arb_trade.ft_pair}\n"
            f"做空: {arb_trade.exchange_short} | "
            f"做多: {arb_trade.exchange_long}\n"
            f"原因: {message}"
        )
        self.send_text(text)

    def notify_risk(self, message: str) -> None:
        """
        Send a risk control notification.
        """
        text = f"🛑 <b>风控触发</b>\n{message}"
        self.send_text(text)

    def notify_status(self, open_count: int, max_count: int, daily_pnl: float) -> None:
        """
        Send a status update.
        """
        text = (
            f"📊 <b>套利系统状态</b>\n"
            f"当前持仓: {open_count}/{max_count}\n"
            f"今日盈亏: {daily_pnl:+.4f}U"
        )
        self.send_text(text)
