# 跨交易所资金费率套利系统 — 实现计划

**版本：** V1.1
**更新日期：** 2026-06-16

---

## 一、架构决策

### 1.1 运行模式：独立子命令

```
freqtrade arb --config user_data/config_arb.json
```

**理由：**
- 套利逻辑和传统策略逻辑差异太大，不适合嵌入 FreqtradeBot 主循环
- 独立子命令可以独立启动、独立停止，不影响传统交易
- 复用 freqtrade 的 Exchange 层、配置系统、日志、RPC 通知、数据库

### 1.2 多交易所连接

利用 `ExchangeResolver.load_exchange(exchange_config=...)` 的 `exchange_config` 覆盖参数，从单个 config 文件创建多个 Exchange 实例：

```python
# 代码中为每个交易所创建独立的 Exchange 实例
exchanges = {}
for ex_conf in config["arb"]["exchanges"]:
    exchanges[ex_conf["name"]] = ExchangeResolver.load_exchange(
        config, exchange_config=ex_conf, validate=True
    )
```

### 1.3 持仓模型：独立 ArbTrade 表

不用现有的 `Trade` 模型（它是单边的），新建 `ArbTrade` 记录双边关联持仓。数据存储在独立的 `arb_trades.sqlite`，和原来的 `tradesv3.sqlite` 分开，互不干扰。

### 1.4 数据获取：直接用 ccxt sync API

- 批量获取费率：`exchange._api.fetch_funding_rates()` （ccxt 原生批量接口）
- 批量获取价格：`exchange._api.fetch_tickers()` （ccxt 原生批量接口）
- 不走 DataProvider（它是为 OHLCV 缓存设计的，不适合实时费率扫描）

### 1.5 模拟盘支持（Dry-Run）

完全支持 `dry_run: true` 模式：
- ✅ 实时获取真实市场数据（价格、费率）
- ✅ 真实扫描、真实决策逻辑
- ✅ 下单模拟（不实际提交到交易所）
- ✅ 持仓记录存数据库（和实盘一样的表结构）
- ✅ Telegram 通知正常推送
- ✅ 监控逻辑正常运行

建议开发流程：先用 `dry_run: true` 跑几天验证逻辑，确认后改成 `dry_run: false` 上实盘。

---

## 二、文件结构

```
freqtrade/
├── arbitrage/
│   ├── __init__.py
│   ├── models.py           # ArbTrade 数据库模型
│   ├── config.py           # 套利配置数据类 + 默认值
│   ├── scanner.py          # 多交易所费率/价格扫描 + 排序
│   ├── executor.py         # 策略决策 + 双边分批下单
│   ├── monitor.py          # 快进快出/基差套利监控循环
│   ├── risk.py             # 风控模块
│   └── engine.py           # 套利主引擎（编排 scanner→executor→monitor）
├── commands/
│   ├── arb_commands.py     # 新增 CLI 子命令
│   └── __init__.py         # 添加 start_arb 导出
├── persistence/
│   └── models.py           # 注册 ArbTrade 到 init_db()
└── rpc/
    └── arb_api.py          # (Phase 4) 套利 REST API 端点
```

---

## 三、各模块详细设计

### 3.1 `arbitrage/models.py` — ArbTrade 数据库模型

```python
class ArbTrade(ModelBase):
    __tablename__ = "arb_trades"
    session: ClassVar[SessionType]

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 基本信息
    ft_pair: Mapped[str] = mapped_column(String(25), nullable=False, index=True)
    strategy_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "quick" / "basis"

    # 交易所 A（做空方）
    exchange_short: Mapped[str] = mapped_column(String(25), nullable=False)
    side_short: Mapped[str] = mapped_column(String(10), nullable=False, default="sell")
    order_id_short: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entry_price_short: Mapped[float] = mapped_column(Float(), nullable=False)
    amount_short: Mapped[float] = mapped_column(Float(), nullable=False)
    exit_price_short: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # 交易所 B（做多方）
    exchange_long: Mapped[str] = mapped_column(String(25), nullable=False)
    side_long: Mapped[str] = mapped_column(String(10), nullable=False, default="buy")
    order_id_long: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entry_price_long: Mapped[float] = mapped_column(Float(), nullable=False)
    amount_long: Mapped[float] = mapped_column(Float(), nullable=False)
    exit_price_long: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # 费率和基差记录
    entry_funding_rate_diff: Mapped[float] = mapped_column(Float(), nullable=False)
    entry_basis_rate: Mapped[float] = mapped_column(Float(), nullable=False)

    # 状态
    ft_is_open: Mapped[bool] = mapped_column(nullable=False, default=True, index=True)
    open_date: Mapped[datetime] = mapped_column(nullable=False, default=dt_now)
    close_date: Mapped[datetime | None] = mapped_column(nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 盈亏
    realized_pnl: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # 杠杆
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
```

### 3.2 `arbitrage/config.py` — 配置数据类

```python
@dataclass
class ArbConfig:
    # 交易所列表
    exchanges: list[dict]           # [{"name": "binance", "key": "...", "secret": "..."}]
    symbols: list[str]              # ["BTC/USDT:USDT", "ETH/USDT:USDT", ...]

    # 成本参数
    slippage: float = 0.002         # 0.2%
    fee_rate: float = 0.001         # 0.1%

    # 策略阈值
    fee_threshold: float = 0.0015   # 0.15%
    quick_profit_threshold: float = 0.0005  # 0.05%
    basis_profit_threshold: float = 0.002   # 0.2%

    # 仓位
    quick_position_ratio: float = 0.05   # 5%
    basis_position_ratio: float = 0.10   # 10%
    leverage: int = 5

    # 分批下单
    batch_count: int = 3
    batch_interval: float = 30.0    # 秒
    batch_retry_count: int = 3
    batch_retry_interval: float = 5.0

    # 系统参数
    scan_interval: float = 10.0     # 秒
    max_concurrent_positions: int = 5
    quick_advance_time: float = 10.0  # 秒（结算前开仓时间）

    # 监控参数
    margin_threshold: float = 0.10  # 10%
    fee_reversal_threshold: float = 0.0005  # 0.05%
    basis_converge_target: float = 0.80  # 80%
    stop_loss: float = 0.05         # 5%
    max_hold_time: int = 7          # 天

    # 风控
    max_daily_loss: float = 0.05    # 5%
    max_single_loss: float = 0.02   # 2%
```

配置从 `config_arb.json` 的 `"arb"` section 加载。

### 3.3 `arbitrage/scanner.py` — 多交易所扫描器

**核心类：`ArbScanner`**

```python
class ArbScanner:
    def __init__(self, exchanges: dict[str, Exchange], config: ArbConfig):
        self.exchanges = exchanges
        self.config = config

    def batch_fetch(self, exchange_name: str) -> dict[str, PriceFundingInfo]:
        """批量获取单个交易所所有币种的价格+费率"""
        exchange = self.exchanges[exchange_name]
        tickers = exchange._api.fetch_tickers(symbols)
        funding_rates = exchange._api.fetch_funding_rates(symbols)
        # 合并返回

    def scan_all(self) -> list[ArbOpportunity]:
        """扫描所有交易所对，返回按费率差排序的机会列表"""
        # 1. 并行获取所有交易所数据
        # 2. 计算所有交易所对的费率差
        # 3. 排序返回
```

**数据类：**
```python
@dataclass
class PriceFundingInfo:
    symbol: str
    exchange: str
    price: float
    funding_rate: float

@dataclass
class ArbOpportunity:
    symbol: str
    exchange_short: str       # 费率高的交易所（做空）
    exchange_long: str        # 费率低的交易所（做多）
    price_short: float
    price_long: float
    funding_rate_short: float
    funding_rate_long: float
    funding_rate_diff: float  # |费率差|
    basis_rate: float         # (做空价 - 做多价) / 做多价
    quick_profit: float       # 费率差 - 滑点 - 手续费
    basis_profit: float       # 费率差 + 基差率 - 滑点 - 手续费
```

### 3.4 `arbitrage/executor.py` — 执行器

**核心类：`ArbExecutor`**

```python
class ArbExecutor:
    def __init__(self, exchanges: dict[str, Exchange], config: ArbConfig):
        self.exchanges = exchanges
        self.config = config

    def decide_strategy(self, opp: ArbOpportunity) -> str | None:
        """决策：快进快出 / 基差套利 / 不执行"""
        if opp.funding_rate_diff > self.config.fee_threshold:
            if opp.price_short > opp.price_long:  # 基差方向一致
                if opp.basis_profit > self.config.basis_profit_threshold:
                    return "basis"
            else:
                if opp.quick_profit > self.config.quick_profit_threshold:
                    return "quick"
        return None

    def execute_open(self, opp: ArbOpportunity, strategy: str) -> ArbTrade:
        """双边分批下单开仓"""

    def execute_close(self, arb_trade: ArbTrade) -> None:
        """双边市价单平仓"""

    def _batch_order(self, exchange, symbol, side, amount) -> str:
        """分批下单"""

    def _calc_aligned_amounts(self, ex_short, ex_long, symbol, usdt_amount) -> tuple[float, float]:
        """计算对齐后的数量（处理不同交易所的合约面值/精度差异）"""
```

### 3.5 `arbitrage/monitor.py` — 监控器

**核心类：`ArbMonitor`**

```python
class ArbMonitor:
    def __init__(self, exchanges, config, executor, rpc_manager):
        self.exchanges = exchanges
        self.config = config
        self.executor = executor
        self.rpc = rpc_manager

    def monitor_tick(self, arb_trades: list[ArbTrade]):
        """每 30 秒调用一次，分发到不同监控逻辑"""
        for trade in arb_trades:
            if trade.strategy_type == "quick":
                self._monitor_quick(trade)
            elif trade.strategy_type == "basis":
                self._monitor_basis(trade)

    def _monitor_quick(self, trade: ArbTrade):
        """快进快出监控：等结算后平仓"""
        # 1. 检查是否已过结算时间
        # 2. 是 → 双边市价平仓
        # 3. 平仓失败 → 重试（最多 3 次）

    def _monitor_basis(self, trade: ArbTrade):
        """基差套利监控：持续监控多个条件"""
        # 按优先级检查：
        # 1. 保证金比率 > 阈值？ → 平仓
        # 2. 费率反转？ → 平仓
        # 3. 基差反向（基差率 ≤ 0）？ → 平仓
        # 4. 基差收敛 > 目标？ → 平仓（止盈）
        # 5. 单边浮亏 > 止损？ → 平仓
        # 6. 持仓时间 > 最大？ → 平仓（超时）
```

### 3.6 `arbitrage/risk.py` — 风控

```python
class ArbRiskManager:
    def __init__(self, config: ArbConfig):
        self.config = config
        self.daily_pnl = 0.0
        self.consecutive_losses = 0

    def can_open(self, arb_trades: list[ArbTrade], symbol: str) -> tuple[bool, str]:
        """检查是否允许开仓"""
        # 1. 持仓数 ≥ 最大并发？ → 拒绝
        # 2. 该币种已有持仓？ → 拒绝
        # 3. 当日亏损 > 最大？ → 拒绝
        # 4. 连续亏损 ≥ 3？ → 暂停

    def check_margin(self, exchange: Exchange, symbol: str) -> bool:
        """检查保证金比率"""

    def record_trade(self, pnl: float):
        """记录交易结果"""
```

### 3.7 `arbitrage/engine.py` — 主引擎

```python
class ArbEngine:
    """套利主引擎，编排 scanner → executor → monitor"""

    def __init__(self, config: dict):
        self.arb_config = ArbConfig.from_dict(config.get("arb", {}))
        self.exchanges = self._init_exchanges(config)
        self.scanner = ArbScanner(self.exchanges, self.arb_config)
        self.executor = ArbExecutor(self.exchanges, self.arb_config)
        self.monitor = ArbMonitor(self.exchanges, self.arb_config, self.executor, rpc)
        self.risk = ArbRiskManager(self.arb_config)

    def run(self):
        """主循环"""
        while True:
            # 1. 获取当前开放的套利持仓
            open_trades = ArbTrade.get_open_trades()

            # 2. 监控现有持仓（每 30 秒）
            if open_trades:
                self.monitor.monitor_tick(open_trades)

            # 3. 扫描新机会（每 scan_interval 秒）
            if len(open_trades) < self.arb_config.max_concurrent_positions:
                opportunities = self.scanner.scan_all()
                for opp in opportunities:
                    ok, reason = self.risk.can_open(open_trades, opp.symbol)
                    if not ok:
                        continue
                    strategy = self.executor.decide_strategy(opp)
                    if strategy:
                        self.executor.execute_open(opp, strategy)
                        break

            # 4. 等待
            time.sleep(self.arb_config.scan_interval)

    def _init_exchanges(self, config) -> dict[str, Exchange]:
        """初始化多个交易所连接"""
```

---

## 四、Telegram 通知设计

每条通知都包含交易所名称，清晰区分不同平台。

### 4.1 开仓通知

```
🎯 套利开仓 [基差套利]
币种: BTC/USDT:USDT
做空: Binance @ 105,320 (费率: 0.035%)
做多: OKX @ 105,100 (费率: 0.005%)
费率差: 0.030% | 基差率: 0.21%
杠杆: 5x | 仓位: 500U
```

### 4.2 平仓通知

```
💰 套利平仓 [快进快出]
币种: ETH/USDT:USDT
做空: Binance 入场 3,200 → 出场 3,198
做多: OKX 入场 3,195 → 出场 3,199
盈亏: +2.5U | 原因: 结算后平仓
```

### 4.3 监控告警

```
⚠️ 套利预警 [基差套利]
币种: SOL/USDT:USDT
做空: Bybit | 做多: OKX
原因: 费率反转 (当前费率差: 0.02% < 初始: 0.08%)
状态: 已自动平仓
```

### 4.4 风控通知

```
🛑 风控触发
原因: 当日亏损已达 -5.2%，超过限制 -5%
状态: 已暂停开仓，等待人工确认
```

### 4.5 系统状态通知

```
📊 套利系统状态
运行中: 已运行 2小时30分
当前持仓: 3/5
今日盈亏: +12.5U
今日交易: 8 笔 (胜率: 75%)
```

---

## 五、UI 扩展设计（Phase 4）

当前 freqUI 不展示套利持仓（UI 为 Trade 模型设计）。Phase 4 新增 REST API 端点：

### 5.1 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/arb/trades` | 查所有套利持仓 |
| GET | `/api/v1/arb/trades/{id}` | 查单个持仓详情 |
| GET | `/api/v1/arb/status` | 系统状态（扫描中/暂停/异常） |
| POST | `/api/v1/arb/close/{id}` | 手动平仓 |
| POST | `/api/v1/arb/pause` | 暂停扫描 |
| POST | `/api/v1/arb/resume` | 恢复扫描 |

### 5.2 后续 UI 扩展

可以开发独立的套利监控页面，调用上述 API 展示：
- 当前持仓列表（交易所、币种、策略类型、盈亏）
- 历史交易记录
- 实时费率差排行
- 系统状态和风控告警

---

## 六、Docker 部署

### 6.1 docker-compose.yml

```yaml
services:
  freqtrade-arb:
    image: freqtradeorg/freqtrade:stable
    restart: unless-stopped
    container_name: freqtrade-arb
    volumes:
      - "./user_data:/freqtrade/user_data"
    ports:
      - "0.0.0.0:8080:8080"
    environment:
      - HTTP_PROXY=http://172.17.0.1:7890
      - HTTPS_PROXY=http://172.17.0.1:7890
      - http_proxy=http://172.17.0.1:7890
      - https_proxy=http://172.17.0.1:7890
      - NO_PROXY=localhost,127.0.0.1
    command: >
      arb
      --logfile /freqtrade/user_data/logs/freqtrade_arb.log
      --db-url sqlite:////freqtrade/user_data/arb_trades.sqlite
      --config /freqtrade/user_data/config_arb.json
```

### 6.2 配置文件示例 config_arb.json

```json
{
  "dry_run": true,
  "dry_run_wallet": 10000,
  "arb": {
    "exchanges": [
      {"name": "binance", "key": "xxx", "secret": "xxx"},
      {"name": "okx", "key": "xxx", "secret": "xxx", "password": "xxx"},
      {"name": "bybit", "key": "xxx", "secret": "xxx"}
    ],
    "symbols": [
      "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
      "BNB/USDT:USDT", "XRP/USDT:USDT"
    ],
    "leverage": 5,
    "slippage": 0.002,
    "fee_rate": 0.001,
    "fee_threshold": 0.0015,
    "quick_profit_threshold": 0.0005,
    "basis_profit_threshold": 0.002,
    "quick_position_ratio": 0.05,
    "basis_position_ratio": 0.10,
    "batch_count": 3,
    "batch_interval": 30,
    "scan_interval": 10,
    "max_concurrent_positions": 5,
    "margin_threshold": 0.10,
    "stop_loss": 0.05,
    "max_hold_time": 7,
    "max_daily_loss": 0.05
  },
  "telegram": {
    "enabled": true,
    "token": "xxx",
    "chat_id": "xxx"
  },
  "exchange": {
    "name": "binance"
  }
}
```

### 6.3 注意事项

- Telegram token 和 chat_id 在 `config_arb.json` 里配置
- 套利数据存在 `arb_trades.sqlite`，和原来的 `tradesv3.sqlite` 分开
- 日志文件单独存 `freqtrade_arb.log`
- `restart: unless-stopped` 确保挂了自动重启，数据库记录了持仓状态，重启后恢复监控

---

## 七、实现顺序（分阶段）

### Phase 1：基础框架（可独立运行，不下单）

| 序号 | 文件 | 说明 |
|------|------|------|
| 1 | `arbitrage/__init__.py` | 模块初始化 |
| 2 | `arbitrage/config.py` | 配置数据类 |
| 3 | `arbitrage/models.py` | ArbTrade 数据库模型 |
| 4 | `arbitrage/scanner.py` | 多交易所扫描 |
| 5 | `commands/arb_commands.py` | CLI 入口（--scan-only 模式） |
| 6 | `commands/__init__.py` | 添加 start_arb 导出 |
| 7 | `commands/arguments.py` | 注册 arb 子命令 |
| 8 | `persistence/models.py` | 注册 ArbTrade 到 init_db() |

**验证：** `freqtrade arb --config config_arb.json --scan-only` 能打印费率差排行

### Phase 2：执行层

| 序号 | 文件 | 说明 |
|------|------|------|
| 9 | `arbitrage/executor.py` | 策略决策 + 双边分批下单 |
| 10 | `arbitrage/risk.py` | 基础风控 |

**验证：** 手动触发一次快进快出/基差套利开平仓（dry-run 模式）

### Phase 3：监控、完整循环、Telegram 通知

| 序号 | 文件 | 说明 |
|------|------|------|
| 11 | `arbitrage/monitor.py` | 快进快出 + 基差套利监控 |
| 12 | `arbitrage/engine.py` | 主引擎循环 + RPC 初始化 |
| 13 | Telegram 通知集成 | 开仓/平仓/预警/风控通知 |

**验证：** `freqtrade arb --config config_arb.json` 完整自动运行 + Telegram 推送

### Phase 4：REST API、完善、测试

| 序号 | 文件 | 说明 |
|------|------|------|
| 14 | `rpc/arb_api.py` | 套利 REST API 端点 |
| 15 | 完善风控 | 日亏损、连续亏损、费率异常等 |
| 16 | 测试 | 单元测试 + 集成测试 |

**验证：** API 可查询套利状态 + 生产就绪

---

## 八、与 Java 实现的对应关系

| Java 方法 | Python 模块 | 改进点 |
|-----------|------------|--------|
| `FundingBusiness.batchGetPriceAndFundingRate()` | `scanner.batch_fetch()` | ccxt 统一封装，不用写 3 套 API |
| `FundingBusiness.scanDeviation()` | `scanner.scan_all()` | 同上 |
| `FundingBusiness.openPosition()` | `executor.execute_open()` | 加入分批下单 |
| `FundingBusiness.closePosition()` | `executor.execute_close()` | 同上 |
| `FundingBusiness.calcAlignedQuantities()` | `executor._calc_aligned_amounts()` | ccxt market info 已有精度 |
| `FundingBusiness.setLeverage()` | `executor` 内调用 `exchange._set_leverage()` | ccxt 统一接口 |
| 无 | `monitor._monitor_quick()` | 新增 |
| 无 | `monitor._monitor_basis()` | 新增 |
| 无 | `risk.can_open()` | 新增 |
| 无 | `engine.run()` | 新增 |
| 无 | Telegram 通知 | 新增 |
| 无 | REST API | 新增 |
