# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Freqtrade is an open-source cryptocurrency trading bot written in Python (3.11+). It supports live/dry-run trading, backtesting, hyperparameter optimization (Hyperopt via Optuna), and an adaptive ML framework (FreqAI). It wraps exchanges through ccxt and exposes control via Telegram, REST API/WebSocket, and a built-in Web UI (freqUI).

## Development Commands

### Install for development
```bash
uv pip install -r requirements-dev.txt
uv pip install -e ft_client/
uv pip install -e .
```

### Run tests
```bash
pytest                                                    # all tests
pytest tests/test_<file>.py                               # single file
pytest tests/test_<file>.py::test_<function_name>          # single test
pytest --random-order --durations 20 -n auto              # CI-style parallel + random
pytest --cov=freqtrade --cov=freqtrade_client             # with coverage
```

### Lint and format
```bash
ruff check .         # lint
ruff format .        # auto-format
```

### Type checking
```bash
mypy freqtrade
```

### Run all pre-commit checks
```bash
pre-commit run -a
```

## Architecture

### Entry point and CLI dispatch

`freqtrade/main.py:main()` is the single entry point (console script `freqtrade`). It sets up logging/asyncio, parses CLI args via `Arguments`, and dispatches to the subcommand function in `args["func"](args)`. Each subcommand lives in `freqtrade/commands/` (trade, backtesting, hyperopt, data download, webserver, etc.).

### Trading loop

```
main() -> Arguments -> start_trading() -> Worker -> FreqtradeBot -> trading loop
```

- **Worker** (`freqtrade/worker.py`) — top-level orchestrator. Creates `FreqtradeBot`, runs the throttled main loop, handles state transitions (RELOAD_CONFIG, STOPPED).
- **FreqtradeBot** (`freqtrade/freqtradebot.py`) — core trading logic. Initializes exchange, data provider, strategy, pairlist manager, protection manager, RPC manager, wallets. The main loop calls strategy for entry/exit signals and manages orders.

### Strategy system

- **IStrategy** (`freqtrade/strategy/interface.py`) — abstract base class all user strategies extend. Core methods: `populate_indicators()`, `populate_entry_trend()`, `populate_exit_trend()`. Interface version 3 adds long/short/leverage support.
- Strategies are dynamically loaded by `StrategyResolver` (`freqtrade/resolvers/`).

### Exchange layer

- **Exchange** (`freqtrade/exchange/exchange.py`) — wraps ccxt for all exchange interactions (OHLCV, orders, balances).
- Per-exchange subclasses (Binance, Bybit, OKX, Kraken, Gate, Hyperliquid, etc.) override behavior as needed. Loaded dynamically by `ExchangeResolver`.

### Data flow

- **DataProvider** (`freqtrade/data/dataprovider.py`) — central data access for bot and strategy. Provides live ticker, orderbook, and historical OHLCV data.
- Historical data handlers in `freqtrade/data/history/datahandlers/`.
- Data converters in `freqtrade/data/converter/` (OHLCV, trades, orderflow).

### Plugin system

- **PairlistManager** + filters (`freqtrade/plugins/pairlist/`) — dynamically build tradeable pair lists.
- **ProtectionManager** + protections (`freqtrade/plugins/protections/`) — cooldown, drawdown limits, stoploss guard.
- Both use the resolver pattern for dynamic loading.

### RPC / API

- **RPCManager** (`freqtrade/rpc/rpc_manager.py`) — manages all RPC backends.
- **Telegram** (`freqtrade/rpc/telegram.py`) — full Telegram bot control.
- **ApiServer** (`freqtrade/rpc/api_server/`) — FastAPI REST API + WebSocket + built-in Web UI.
- **Webhook** (`freqtrade/rpc/webhook.py`) and **Discord** (`freqtrade/rpc/discord.py`) for notifications.

### Persistence

SQLAlchemy ORM with SQLite (default) or PostgreSQL. Models: `Trade`, `Order`, `PairLock`, key-value store, wallet history. DB migrations in `freqtrade/persistence/migrations.py`.

### Backtesting and optimization

- **Backtesting** (`freqtrade/optimize/backtesting.py`) — simulates strategies against historical data.
- **Hyperopt** (`freqtrade/optimize/hyperopt/`) — parameter optimization using Optuna.
- **FreqAI** (`freqtrade/freqai/`) — ML/AI framework for adaptive strategies (scikit-learn, LightGBM, XGBoost, RL via Stable Baselines 3).

### Resolver pattern

Dynamic module loading is a core pattern. `freqtrade/resolvers/` contains resolvers for strategies, exchanges, pairlists, protections, FreqAI models, and hyperopt loss functions. They locate and instantiate user-provided or built-in classes by name.

## Code Style

- Line length: 100 characters (ruff enforced)
- Docstrings: reST format (`:param xxx: ...`, `:return: ...`, `:raises KeyError: ...`), double-quoted
- PRs target the `develop` branch, not `stable`
- Ruff replaces flake8/isort/pycodestyle — all linting and formatting through `ruff`
- Max cyclomatic complexity: 12
