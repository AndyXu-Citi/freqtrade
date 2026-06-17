"""
Configuration dataclass for cross-exchange funding rate arbitrage.
"""

import logging
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


@dataclass
class ArbConfig:
    """
    Configuration for the arbitrage engine.
    Loaded from the "arb" section of the config JSON.
    """

    # Exchange list: [{"name": "binance", "key": "...", "secret": "..."}]
    exchanges: list[dict[str, str]] = field(default_factory=list)

    # Symbols to monitor: ["BTC/USDT:USDT", "ETH/USDT:USDT", ...]
    symbols: list[str] = field(default_factory=list)

    # Cost parameters
    slippage: float = 0.002  # 0.2%
    fee_rate: float = 0.001  # 0.1%

    # Strategy thresholds
    fee_threshold: float = 0.0015  # 0.15%
    quick_profit_threshold: float = 0.0005  # 0.05%
    basis_profit_threshold: float = 0.002  # 0.2%

    # Position sizing
    quick_position_ratio: float = 0.05  # 5% of balance
    basis_position_ratio: float = 0.10  # 10% of balance
    leverage: int = 5

    # Batch order parameters
    batch_count: int = 3
    batch_interval: float = 30.0  # seconds
    batch_retry_count: int = 3
    batch_retry_interval: float = 5.0  # seconds

    # System parameters
    scan_interval: float = 10.0  # seconds
    max_concurrent_positions: int = 5
    quick_advance_time: float = 10.0  # seconds before settlement

    # Monitoring parameters
    margin_threshold: float = 0.10  # 10%
    fee_reversal_threshold: float = 0.0005  # 0.05%
    basis_converge_target: float = 0.80  # 80%
    stop_loss: float = 0.05  # 5%
    max_hold_time: int = 7  # days

    # Risk control
    max_daily_loss: float = 0.05  # 5%
    max_single_loss: float = 0.02  # 2%

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArbConfig":
        """Create ArbConfig from a dict, ignoring unknown keys."""
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)
