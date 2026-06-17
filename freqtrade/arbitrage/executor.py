"""
Arbitrage executor: strategy decision and dual-side order execution.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from freqtrade.arbitrage.config import ArbConfig
from freqtrade.arbitrage.models import ArbTrade
from freqtrade.arbitrage.scanner import ArbOpportunity
from freqtrade.exchange import Exchange


logger = logging.getLogger(__name__)


class ArbExecutor:
    """
    Decides arbitrage strategy and executes dual-side orders.
    """

    def __init__(self, exchanges: dict[str, Exchange], config: ArbConfig):
        self.exchanges = exchanges
        self.config = config

    def decide_strategy(self, opp: ArbOpportunity) -> str | None:
        """
        Decide which strategy to use based on funding rate and basis.

        Returns:
            "quick" for quick in/out, "basis" for basis arbitrage, None to skip.
        """
        if opp.funding_rate_diff <= self.config.fee_threshold:
            return None

        # Check if basis direction matches funding direction
        # (short price > long price means positive basis)
        if opp.price_short > opp.price_long:
            # Basis direction consistent with funding direction
            if opp.basis_profit > self.config.basis_profit_threshold:
                return "basis"
        else:
            # Basis direction inconsistent - use quick in/out
            if opp.quick_profit > self.config.quick_profit_threshold:
                return "quick"

        return None

    def execute_open(
        self, opp: ArbOpportunity, strategy: str, balance: float | None = None
    ) -> ArbTrade | None:
        """
        Execute dual-side opening orders.

        :param opp: The arbitrage opportunity.
        :param strategy: "quick" or "basis".
        :param balance: Available balance in USDT. If None, will query exchange.
        :returns: ArbTrade record or None on failure.
        """
        # Calculate position size
        ratio = (
            self.config.quick_position_ratio
            if strategy == "quick"
            else self.config.basis_position_ratio
        )

        if balance is None:
            balance = self._get_available_balance(opp.exchange_long)

        if balance is None or balance <= 0:
            logger.error("No available balance on %s", opp.exchange_long)
            return None

        usdt_amount = balance * ratio
        if usdt_amount <= 0:
            logger.error("Position size too small: %s USDT", usdt_amount)
            return None

        # Calculate aligned amounts for both exchanges
        ex_short = self.exchanges[opp.exchange_short]
        ex_long = self.exchanges[opp.exchange_long]

        amount_short, amount_long = self._calc_aligned_amounts(
            ex_short, ex_long, opp.symbol, usdt_amount
        )

        if amount_short <= 0 or amount_long <= 0:
            logger.error(
                "Aligned amounts too small: short=%s long=%s",
                amount_short,
                amount_long,
            )
            return None

        logger.info(
            "Executing %s arbitrage: %s short=%s@%s long=%s@%s amount=%s",
            strategy,
            opp.symbol,
            opp.exchange_short,
            opp.price_short,
            opp.exchange_long,
            opp.price_long,
            usdt_amount,
        )

        # Batch order on both exchanges
        order_id_short = self._batch_order(
            ex_short, opp.symbol, "sell", amount_short, opp.price_short
        )
        order_id_long = self._batch_order(
            ex_long, opp.symbol, "buy", amount_long, opp.price_long
        )

        if not order_id_short or not order_id_long:
            logger.error(
                "One or both orders failed: short=%s long=%s",
                order_id_short,
                order_id_long,
            )
            # TODO: rollback successful side
            return None

        # Create ArbTrade record
        arb_trade = ArbTrade(
            ft_pair=opp.symbol,
            strategy_type=strategy,
            exchange_short=opp.exchange_short,
            order_id_short=order_id_short,
            entry_price_short=opp.price_short,
            amount_short=amount_short,
            exchange_long=opp.exchange_long,
            order_id_long=order_id_long,
            entry_price_long=opp.price_long,
            amount_long=amount_long,
            entry_funding_rate_diff=opp.funding_rate_diff,
            entry_basis_rate=opp.basis_rate,
            ft_is_open=True,
            leverage=self.config.leverage,
        )
        ArbTrade.session.add(arb_trade)
        ArbTrade.session.commit()

        logger.info(
            "ArbTrade opened: id=%d pair=%s strategy=%s",
            arb_trade.id,
            arb_trade.ft_pair,
            arb_trade.strategy_type,
        )
        return arb_trade

    def execute_close(self, arb_trade: ArbTrade) -> bool:
        """
        Execute dual-side closing orders (market orders).

        :returns: True if both sides closed successfully.
        """
        ex_short = self.exchanges.get(arb_trade.exchange_short)
        ex_long = self.exchanges.get(arb_trade.exchange_long)

        if not ex_short or not ex_long:
            logger.error(
                "Exchange not found: short=%s long=%s",
                arb_trade.exchange_short,
                arb_trade.exchange_long,
            )
            return False

        # Close both sides in parallel
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_short = pool.submit(
                self._close_position,
                ex_short,
                arb_trade.ft_pair,
                "buy",  # Close short = buy
                arb_trade.amount_short,
            )
            future_long = pool.submit(
                self._close_position,
                ex_long,
                arb_trade.ft_pair,
                "sell",  # Close long = sell
                arb_trade.amount_long,
            )

            short_ok = future_short.result()
            long_ok = future_long.result()

        if short_ok and long_ok:
            logger.info("ArbTrade closed successfully: id=%d", arb_trade.id)
            return True
        else:
            logger.error(
                "ArbTrade close partial failure: id=%d short=%s long=%s",
                arb_trade.id,
                short_ok,
                long_ok,
            )
            return False

    def _batch_order(
        self,
        exchange: Exchange,
        symbol: str,
        side: str,
        total_amount: float,
        price: float,
    ) -> str | None:
        """
        Execute a batch order: split total_amount into multiple smaller orders.

        :returns: The last order ID, or None on failure.
        """
        batch_count = max(1, self.config.batch_count)
        amount_per_batch = total_amount / batch_count
        last_order_id = None

        for batch_num in range(batch_count):
            # Last batch gets the remainder
            if batch_num == batch_count - 1:
                batch_amount = total_amount - amount_per_batch * (batch_count - 1)
            else:
                batch_amount = amount_per_batch

            batch_amount = exchange.amount_to_precision(symbol, batch_amount)
            if batch_amount <= 0:
                logger.warning("Batch %d amount too small, skipping", batch_num + 1)
                continue

            order_id = self._place_order_with_retry(
                exchange, symbol, side, batch_amount, price
            )
            if not order_id:
                logger.error(
                    "Batch %d/%d failed for %s %s",
                    batch_num + 1,
                    batch_count,
                    side,
                    symbol,
                )
                return None

            last_order_id = order_id

            # Wait between batches (except after the last one)
            if batch_num < batch_count - 1:
                time.sleep(self.config.batch_interval)

        return last_order_id

    def _place_order_with_retry(
        self,
        exchange: Exchange,
        symbol: str,
        side: str,
        amount: float,
        price: float,
    ) -> str | None:
        """
        Place a single market order with retry logic.
        """
        for attempt in range(self.config.batch_retry_count):
            try:
                order = exchange.create_order(
                    pair=symbol,
                    ordertype="market",
                    side=side,  # type: ignore[arg-type]
                    amount=amount,
                    rate=price,
                    leverage=float(self.config.leverage),
                )
                order_id = order.get("id", "")
                logger.info(
                    "Order placed: %s %s %s amount=%s order_id=%s",
                    side,
                    symbol,
                    exchange.name,
                    amount,
                    order_id,
                )
                return order_id
            except Exception as e:
                logger.warning(
                    "Order attempt %d/%d failed: %s %s %s - %s",
                    attempt + 1,
                    self.config.batch_retry_count,
                    side,
                    symbol,
                    exchange.name,
                    e,
                )
                if attempt < self.config.batch_retry_count - 1:
                    time.sleep(self.config.batch_retry_interval)

        return None

    def _close_position(
        self, exchange: Exchange, symbol: str, side: str, amount: float
    ) -> bool:
        """
        Close a single position leg with a market order.
        """
        try:
            order = exchange.create_order(
                pair=symbol,
                ordertype="market",
                side=side,  # type: ignore[arg-type]
                amount=amount,
                rate=0,  # Market order, rate not needed
                leverage=float(self.config.leverage),
                reduceOnly=True,
            )
            logger.info(
                "Position closed: %s %s %s amount=%s order_id=%s",
                side,
                symbol,
                exchange.name,
                amount,
                order.get("id"),
            )
            return True
        except Exception as e:
            logger.error(
                "Failed to close position: %s %s %s - %s",
                side,
                symbol,
                exchange.name,
                e,
            )
            return False

    def _calc_aligned_amounts(
        self,
        ex_short: Exchange,
        ex_long: Exchange,
        symbol: str,
        usdt_amount: float,
    ) -> tuple[float, float]:
        """
        Calculate aligned amounts for both exchanges, handling different
        contract sizes and precision.
        """
        market_short = ex_short.markets.get(symbol, {})
        market_long = ex_long.markets.get(symbol, {})

        if not market_short or not market_long:
            logger.error("Market info not found for %s", symbol)
            return 0.0, 0.0

        # Get precision (lot size) for each exchange
        lot_short = self._get_lot_size(ex_short, symbol, market_short)
        lot_long = self._get_lot_size(ex_long, symbol, market_long)

        # Get min amount for each exchange
        min_short = float(market_short.get("limits", {}).get("amount", {}).get("min", 0))
        min_long = float(market_long.get("limits", {}).get("amount", {}).get("min", 0))

        # Get current prices from tickers (markets only has static info)
        price_short = 0.0
        price_long = 0.0
        try:
            ticker_short = ex_short.fetch_ticker(symbol)
            price_short = float(ticker_short.get("last", 0))
        except Exception as e:
            logger.warning("Failed to get ticker from %s for %s: %s", ex_short.name, symbol, e)
        try:
            ticker_long = ex_long.fetch_ticker(symbol)
            price_long = float(ticker_long.get("last", 0))
        except Exception as e:
            logger.warning("Failed to get ticker from %s for %s: %s", ex_long.name, symbol, e)

        if price_short <= 0 or price_long <= 0:
            logger.error("Price not available: short=%s long=%s", price_short, price_long)
            return 0.0, 0.0

        # Contract value (how much 1 unit of base currency is worth in quote)
        contract_val_short = float(
            market_short.get("contractSize", 1)
        )
        contract_val_long = float(
            market_long.get("contractSize", 1)
        )

        # Amount in base currency
        raw_amount_short = usdt_amount / (price_short * contract_val_short)
        raw_amount_long = usdt_amount / (price_long * contract_val_long)

        # Align to lot size (round down)
        if lot_short > 0:
            amount_short = int(raw_amount_short / lot_short) * lot_short
        else:
            amount_short = ex_short.amount_to_precision(symbol, raw_amount_short)

        if lot_long > 0:
            amount_long = int(raw_amount_long / lot_long) * lot_long
        else:
            amount_long = ex_long.amount_to_precision(symbol, raw_amount_long)

        # Check minimum amounts
        if min_short > 0 and amount_short < min_short:
            logger.warning(
                "Amount %s below minimum %s on %s",
                amount_short, min_short, ex_short.name,
            )
            amount_short = 0
        if min_long > 0 and amount_long < min_long:
            logger.warning(
                "Amount %s below minimum %s on %s",
                amount_long, min_long, ex_long.name,
            )
            amount_long = 0

        logger.info(
            "Aligned amounts: %s short=%s (lot=%s) long=%s (lot=%s)",
            symbol, amount_short, lot_short, amount_long, lot_long,
        )
        return float(amount_short), float(amount_long)

    def _get_lot_size(
        self, exchange: Exchange, symbol: str, market: dict
    ) -> float:
        """
        Get the lot size (minimum step) for an exchange/symbol.
        """
        # Try precision.amount first
        precision = market.get("precision", {}).get("amount")
        if precision is not None:
            return float(precision)

        # Try limits.amount.min as fallback
        min_amount = market.get("limits", {}).get("amount", {}).get("min")
        if min_amount is not None:
            return float(min_amount)

        return 0.0

    def _get_available_balance(self, exchange_name: str) -> float | None:
        """
        Get available USDT balance on an exchange.
        In dry-run mode, returns the configured dry_run_wallet value.
        """
        exchange = self.exchanges.get(exchange_name)
        if not exchange:
            return None

        # In dry-run mode, use the configured wallet value
        if exchange._config.get("dry_run", False):
            wallet = float(exchange._config.get("dry_run_wallet", 10000))
            logger.info("[dry-run] Using wallet balance: %s USDT on %s", wallet, exchange_name)
            return wallet

        try:
            balances = exchange.get_balances()
            usdt_balance = balances.get("USDT", {})
            free = usdt_balance.get("free", 0)
            return float(free) if free else 0.0
        except Exception as e:
            logger.error("Failed to get balance from %s: %s", exchange_name, e)
            return None
