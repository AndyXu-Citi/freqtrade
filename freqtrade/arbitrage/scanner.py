"""
Multi-exchange funding rate and price scanner.
Scans all configured exchanges and computes pairwise funding rate differences.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from freqtrade.arbitrage.config import ArbConfig
from freqtrade.exchange import Exchange


logger = logging.getLogger(__name__)


@dataclass
class PriceFundingInfo:
    """Price and funding rate for a single symbol on a single exchange."""

    symbol: str
    exchange: str
    price: float
    funding_rate: float


@dataclass
class ArbOpportunity:
    """
    A potential arbitrage opportunity between two exchanges.
    exchange_short has the higher funding rate (short there to collect).
    exchange_long has the lower funding rate (long there to pay less).
    """

    symbol: str
    exchange_short: str
    exchange_long: str
    price_short: float
    price_long: float
    funding_rate_short: float
    funding_rate_long: float
    funding_rate_diff: float  # |rate_short - rate_long|
    basis_rate: float  # (price_short - price_long) / price_long
    quick_profit: float  # funding_rate_diff - slippage - fee
    basis_profit: float  # funding_rate_diff + basis_rate - slippage - fee


class ArbScanner:
    """
    Scans multiple exchanges for funding rate arbitrage opportunities.
    """

    def __init__(self, exchanges: dict[str, Exchange], config: ArbConfig):
        self.exchanges = exchanges
        self.config = config

    def batch_fetch(self, exchange_name: str) -> dict[str, PriceFundingInfo]:
        """
        Batch fetch prices and funding rates for all configured symbols
        on a single exchange.
        """
        exchange = self.exchanges[exchange_name]
        symbols = self.config.symbols
        result: dict[str, PriceFundingInfo] = {}

        try:
            # Batch fetch tickers (prices)
            tickers = exchange._api.fetch_tickers(symbols)
        except Exception as e:
            logger.warning("Failed to fetch tickers from %s: %s", exchange_name, e)
            tickers = {}

        try:
            # Batch fetch funding rates
            funding_rates = exchange._api.fetch_funding_rates(symbols)
        except Exception as e:
            logger.warning(
                "Failed to fetch funding rates from %s: %s", exchange_name, e
            )
            funding_rates = {}

        for symbol in symbols:
            ticker = tickers.get(symbol, {})
            fr = funding_rates.get(symbol, {})

            price = ticker.get("last")
            funding_rate = fr.get("fundingRate")

            if price is not None and funding_rate is not None:
                result[symbol] = PriceFundingInfo(
                    symbol=symbol,
                    exchange=exchange_name,
                    price=float(price),
                    funding_rate=float(funding_rate),
                )

        logger.info(
            "[%s] Fetched %d/%d symbols with price+rate",
            exchange_name,
            len(result),
            len(symbols),
        )
        return result

    def scan_all(self) -> list[ArbOpportunity]:
        """
        Scan all exchange pairs and return opportunities sorted by
        funding rate difference (descending).
        """
        # 1. Fetch data from all exchanges in parallel
        all_data: dict[str, dict[str, PriceFundingInfo]] = {}
        with ThreadPoolExecutor(max_workers=len(self.exchanges)) as executor:
            futures = {
                executor.submit(self.batch_fetch, name): name
                for name in self.exchanges
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    all_data[name] = future.result()
                except Exception as e:
                    logger.error("Failed to fetch data from %s: %s", name, e)
                    all_data[name] = {}

        # 2. Compute pairwise opportunities
        exchange_names = list(self.exchanges.keys())
        opportunities: list[ArbOpportunity] = []

        for i in range(len(exchange_names)):
            for j in range(i + 1, len(exchange_names)):
                ex_a = exchange_names[i]
                ex_b = exchange_names[j]
                data_a = all_data.get(ex_a, {})
                data_b = all_data.get(ex_b, {})

                for symbol in self.config.symbols:
                    info_a = data_a.get(symbol)
                    info_b = data_b.get(symbol)

                    if not info_a or not info_b:
                        continue

                    opp = self._calc_opportunity(info_a, info_b)
                    if opp:
                        opportunities.append(opp)

        # 3. Sort by funding rate difference (descending)
        opportunities.sort(key=lambda o: o.funding_rate_diff, reverse=True)

        logger.info(
            "Scan complete: %d opportunities found across %d exchanges",
            len(opportunities),
            len(exchange_names),
        )
        return opportunities

    def _calc_opportunity(
        self, info_a: PriceFundingInfo, info_b: PriceFundingInfo
    ) -> ArbOpportunity | None:
        """
        Calculate arbitportunity opportunity between two exchanges.
        The exchange with the higher funding rate is the short side.
        """
        rate_diff = abs(info_a.funding_rate - info_b.funding_rate)

        # Determine which is short (higher rate) and which is long (lower rate)
        if info_a.funding_rate >= info_b.funding_rate:
            short_info, long_info = info_a, info_b
        else:
            short_info, long_info = info_b, info_a

        # Basis rate: (short_price - long_price) / long_price
        if long_info.price == 0:
            return None

        basis_rate = (short_info.price - long_info.price) / long_info.price

        # Quick profit: rate_diff - slippage - fee
        quick_profit = (
            rate_diff - self.config.slippage - self.config.fee_rate
        )

        # Basis profit: rate_diff + basis_rate - slippage - fee
        basis_profit = (
            rate_diff + basis_rate - self.config.slippage - self.config.fee_rate
        )

        return ArbOpportunity(
            symbol=info_a.symbol,
            exchange_short=short_info.exchange,
            exchange_long=long_info.exchange,
            price_short=short_info.price,
            price_long=long_info.price,
            funding_rate_short=short_info.funding_rate,
            funding_rate_long=long_info.funding_rate,
            funding_rate_diff=rate_diff,
            basis_rate=basis_rate,
            quick_profit=quick_profit,
            basis_profit=basis_profit,
        )
