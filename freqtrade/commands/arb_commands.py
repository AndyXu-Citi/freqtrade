"""
CLI entry point for cross-exchange funding rate arbitrage.
"""

import logging
from typing import Any


logger = logging.getLogger(__name__)


def start_arb(args: dict[str, Any]) -> int:
    """
    Entry point for `freqtrade arb` subcommand.
    """
    from freqtrade.arbitrage.config import ArbConfig
    from freqtrade.arbitrage.scanner import ArbScanner
    from freqtrade.configuration.config_setup import setup_utils_configuration
    from freqtrade.enums import RunMode
    from freqtrade.resolvers.exchange_resolver import ExchangeResolver

    config = setup_utils_configuration(args, RunMode.UTIL_EXCHANGE, set_dry=False)

    # Initialize database (required for ArbTrade.session)
    from freqtrade.persistence.models import init_db

    db_url = config.get("db_url", "sqlite:///arb_trades.sqlite")
    init_db(db_url)

    arb_conf_dict = config.get("arb", {})
    if not arb_conf_dict:
        logger.error(
            "No 'arb' section found in config. "
            "Please add an 'arb' section to your config file."
        )
        return 1

    arb_config = ArbConfig.from_dict(arb_conf_dict)

    if not arb_config.exchanges:
        logger.error("No exchanges configured in 'arb.exchanges'.")
        return 1

    if not arb_config.symbols:
        logger.error("No symbols configured in 'arb.symbols'.")
        return 1

    # Initialize exchange connections
    logger.info("Initializing %d exchange connections...", len(arb_config.exchanges))
    exchanges = {}
    original_exchange_name = config.get("exchange", {}).get("name", "")
    for ex_conf in arb_config.exchanges:
        ex_name = ex_conf["name"]
        try:
            # ExchangeResolver reads exchange name from config["exchange"]["name"],
            # so we temporarily override it for each arbitrage exchange.
            config.setdefault("exchange", {})["name"] = ex_name
            exchanges[ex_name] = ExchangeResolver.load_exchange(
                config, exchange_config=ex_conf, validate=True
            )
            logger.info("Exchange %s connected", ex_name)
        except Exception as e:
            logger.error("Failed to connect to %s: %s", ex_name, e)
            return 1
    # Restore original exchange name
    config["exchange"]["name"] = original_exchange_name

    # Scan-only mode
    if args.get("scan_only"):
        return _run_scan_only(exchanges, arb_config)

    # Full arbitrage engine (Phase 3)
    logger.info("Starting arbitrage engine...")
    from freqtrade.arbitrage.engine import ArbEngine

    engine = ArbEngine(config, exchanges, arb_config)
    engine.run()
    return 0


def _run_scan_only(exchanges: dict, arb_config) -> int:
    """
    Scan-only mode: fetch data and print opportunities without trading.
    """
    from freqtrade.arbitrage.scanner import ArbScanner

    scanner = ArbScanner(exchanges, arb_config)
    opportunities = scanner.scan_all()

    if not opportunities:
        print("No arbitrage opportunities found.")
        return 0

    print(f"\n{'='*80}")
    print(f"Found {len(opportunities)} arbitrage opportunities")
    print(f"{'='*80}")
    print(
        f"{'Pair':<20} {'Short@':<12} {'Long@':<12} "
        f"{'RateDiff':<10} {'Basis':<10} {'QuickP':<10} {'BasisP':<10}"
    )
    print("-" * 80)

    for opp in opportunities[:30]:  # Show top 30
        print(
            f"{opp.symbol:<20} "
            f"{opp.exchange_short:<12} "
            f"{opp.exchange_long:<12} "
            f"{opp.funding_rate_diff*100:>7.4f}% "
            f"{opp.basis_rate*100:>7.4f}% "
            f"{opp.quick_profit*100:>7.4f}% "
            f"{opp.basis_profit*100:>7.4f}%"
        )

    print(f"\nTop opportunity:")
    top = opportunities[0]
    print(f"  Pair: {top.symbol}")
    print(f"  Short: {top.exchange_short} @ {top.price_short}")
    print(f"  Long:  {top.exchange_long} @ {top.price_long}")
    print(f"  Funding rate diff: {top.funding_rate_diff*100:.4f}%")
    print(f"  Basis rate: {top.basis_rate*100:.4f}%")
    print(f"  Quick profit: {top.quick_profit*100:.4f}%")
    print(f"  Basis profit: {top.basis_profit*100:.4f}%")

    return 0
