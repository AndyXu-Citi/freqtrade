"""
Database model for cross-exchange arbitrage trades.
"""

import logging
from datetime import datetime
from typing import ClassVar

from sqlalchemy import Float, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from freqtrade.persistence.base import ModelBase, SessionType
from freqtrade.util import dt_now


logger = logging.getLogger(__name__)


class ArbTrade(ModelBase):
    """
    Represents a cross-exchange arbitrage position.
    Each ArbTrade tracks both legs: short on one exchange, long on another.
    """

    __tablename__ = "arb_trades"
    session: ClassVar[SessionType]

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Basic info
    ft_pair: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    strategy_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "quick" / "basis"

    # Exchange A (short side)
    exchange_short: Mapped[str] = mapped_column(String(25), nullable=False)
    order_id_short: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entry_price_short: Mapped[float] = mapped_column(Float(), nullable=False)
    amount_short: Mapped[float] = mapped_column(Float(), nullable=False)
    exit_price_short: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # Exchange B (long side)
    exchange_long: Mapped[str] = mapped_column(String(25), nullable=False)
    order_id_long: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entry_price_long: Mapped[float] = mapped_column(Float(), nullable=False)
    amount_long: Mapped[float] = mapped_column(Float(), nullable=False)
    exit_price_long: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # Funding rate and basis at entry
    entry_funding_rate_diff: Mapped[float] = mapped_column(Float(), nullable=False)
    entry_basis_rate: Mapped[float] = mapped_column(Float(), nullable=False)

    # Status
    ft_is_open: Mapped[bool] = mapped_column(nullable=False, default=True, index=True)
    open_date: Mapped[datetime] = mapped_column(nullable=False, default=dt_now)
    close_date: Mapped[datetime | None] = mapped_column(nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # PnL
    realized_pnl: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # Leverage
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    @staticmethod
    def get_open_trades() -> list["ArbTrade"]:
        """
        Retrieve all open arbitrage trades.
        """
        return list(
            ArbTrade.session.scalars(
                select(ArbTrade).where(ArbTrade.ft_is_open.is_(True))
            ).all()
        )

    @staticmethod
    def get_open_trade_count() -> int:
        """
        Count open arbitrage trades.
        """
        return len(ArbTrade.get_open_trades())

    @staticmethod
    def has_open_trade_for_pair(pair: str) -> bool:
        """
        Check if there is an open arbitrage trade for the given pair.
        """
        result = ArbTrade.session.scalars(
            select(ArbTrade).where(
                ArbTrade.ft_is_open.is_(True), ArbTrade.ft_pair == pair
            )
        ).first()
        return result is not None

    def close_trade(self, close_reason: str, pnl: float | None = None) -> None:
        """
        Close this arbitrage trade.
        """
        self.ft_is_open = False
        self.close_date = dt_now()
        self.close_reason = close_reason
        self.realized_pnl = pnl
        self.session.commit()

        logger.info(
            "ArbTrade closed: id=%d pair=%s strategy=%s "
            "ex_short=%s ex_long=%s reason=%s pnl=%s",
            self.id,
            self.ft_pair,
            self.strategy_type,
            self.exchange_short,
            self.exchange_long,
            close_reason,
            pnl,
        )

    def __repr__(self) -> str:
        return (
            f"ArbTrade(id={self.id}, pair={self.ft_pair}, "
            f"strategy={self.strategy_type}, "
            f"short={self.exchange_short}@{self.entry_price_short}, "
            f"long={self.exchange_long}@{self.entry_price_long}, "
            f"is_open={self.ft_is_open})"
        )
