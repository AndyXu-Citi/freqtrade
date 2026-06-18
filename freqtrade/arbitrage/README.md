# 跨交易所资金费率套利系统

## 一、系统架构

```
freqtrade arb --config config_arb.json
       │
       ▼
┌─────────────────────────────────────────────────────┐
│                    ArbEngine                         │
│                 (engine.py 主循环)                    │
│                                                     │
│   ┌───────────┐  ┌───────────┐  ┌───────────┐      │
│   │  Scanner  │→ │ Executor  │→ │  Monitor  │      │
│   │  (扫描器)  │  │  (执行器)  │  │  (监控器)  │      │
│   └───────────┘  └───────────┘  └───────────┘      │
│        ↑               ↑              ↑             │
│   ┌───────────┐  ┌───────────┐  ┌───────────┐      │
│   │ Exchange  │  │   Risk    │  │ Notifier  │      │
│   │ Binance   │  │ (风控)    │  │ (Telegram) │      │
│   │ OKX       │  └───────────┘  └───────────┘      │
│   └───────────┘                                     │
│                     ↓                               │
│              ┌───────────┐                          │
│              │  ArbTrade │                          │
│              │ (数据库)   │                          │
│              └───────────┘                          │
└─────────────────────────────────────────────────────┘
```

## 二、核心数据流

### 2.1 数据模型

```
ArbTrade (arb_trades 表)
├── id                          # 主键
├── ft_pair                     # 交易对 (如 BTC/USDT:USDT)
├── strategy_type               # 策略类型: "quick" / "basis"
│
├── exchange_short              # 做空交易所 (费率高的一方)
├── entry_price_short           # 做空入场价
├── amount_short                # 做空数量
├── exit_price_short            # 做空出场价
├── order_id_short              # 做空订单号
│
├── exchange_long               # 做多交易所 (费率低的一方)
├── entry_price_long            # 做多入场价
├── amount_long                 # 做多数量
├── exit_price_long             # 做多出场价
├── order_id_long               # 做多订单号
│
├── entry_funding_rate_diff     # 入场时费率差
├── entry_basis_rate            # 入场时基差率
│
├── ft_is_open                  # 是否开放持仓
├── open_date                   # 开仓时间
├── close_date                  # 平仓时间
├── close_reason                # 平仓原因
├── realized_pnl                # 已实现盈亏
└── leverage                    # 杠杆倍数
```

### 2.2 扫描结果模型

```
ArbOpportunity (内存对象，不存数据库)
├── symbol                      # 交易对
├── exchange_short              # 做空交易所
├── exchange_long               # 做多交易所
├── price_short                 # 做空价格
├── price_long                  # 做多价格
├── funding_rate_short          # 做空交易所费率
├── funding_rate_long           # 做多交易所费率
├── funding_rate_diff           # 费率差 |rate_short - rate_long|
├── basis_rate                  # 基差率 (price_short - price_long) / price_long
├── quick_profit                # 快进快出收益 = 费率差 - 滑点 - 手续费
└── basis_profit                # 基差套利收益 = 费率差 + 基差率 - 滑点 - 手续费
```

## 三、完整执行流程

### 3.1 主循环 (engine.py - `_tick()`)

```
每 scan_interval 秒执行一次 (默认 10 秒)
│
├── 1. 获取当前开放持仓 (ArbTrade.get_open_trades)
│
├── 2. 监控现有持仓 (monitor.monitor_tick)
│   ├── 快进快出: 等结算 → 平仓
│   └── 基差套利: 检查 6 个条件 → 触发平仓
│
├── 3. 定时状态通知 (每小时推送一次)
│
├── 4. 检查是否可开新仓
│   ├── 持仓数 ≥ 最大并发? → 跳过
│   └── 风控暂停中? → 跳过
│
└── 5. 扫描新机会 (scanner.scan_all)
    ├── 并行获取所有交易所数据
    ├── 计算所有交易所对的费率差
    ├── 排序 (费率差从大到小)
    └── 逐个判断:
        ├── 风控检查 (risk.can_open)
        ├── 策略决策 (executor.decide_strategy)
        └── 执行开仓 (executor.execute_open)
```

### 3.2 扫描流程 (scanner.py)

```
batch_fetch(exchange_name)
│
├── fetch_tickers(symbols)        # 批量获取价格
├── fetch_funding_rates(symbols)  # 批量获取费率
└── 合并为 PriceFundingInfo 列表

scan_all()
│
├── ThreadPoolExecutor 并行获取每个交易所数据
│   ├── Binance: batch_fetch("binance")
│   └── OKX: batch_fetch("okx")
│
├── 计算所有交易所对
│   ├── Binance vs OKX
│   └── (如果有 N 个交易所, 共 N*(N-1)/2 个对)
│
├── 对每个交易对的每个币种:
│   ├── 计算费率差
│   ├── 计算基差率
│   ├── 计算快进快出收益
│   └── 计算基差套利收益
│
└── 按费率差排序, 返回列表
```

### 3.3 策略决策流程 (executor.py - `decide_strategy()`)

```
输入: ArbOpportunity
│
├── 费率差 > 费率阈值? (默认 0.15%)
│   └── 否 → 返回 None (不执行)
│
├── 基差方向判断: 做空价 > 做多价?
│   │
│   ├── 是 (基差方向一致):
│   │   └── 基差套利收益 > 基差套利收益阈值? (默认 0.2%)
│   │       ├── 是 → 返回 "basis"
│   │       └── 否 → 返回 None
│   │
│   └── 否 (基差方向不一致):
│       └── 快进快出收益 > 快进快出收益阈值? (默认 0.05%)
│           ├── 是 → 返回 "quick"
│           └── 否 → 返回 None
```

### 3.4 开仓流程 (executor.py - `execute_open()`)

```
execute_open(opportunity, strategy)
│
├── 1. 计算仓位大小
│   ├── quick: balance × quick_position_ratio (5%)
│   └── basis: balance × basis_position_ratio (10%)
│
├── 2. 计算对齐数量 (_calc_aligned_amounts)
│   ├── 获取两个交易所的 ticker 价格
│   ├── 获取两个交易所的 lot_size (最小步长)
│   ├── 获取两个交易所的 min_amount (最小数量)
│   ├── 获取 contractSize (合约面值)
│   ├── 计算: amount = usdt / (price × contractSize)
│   ├── 按 lot_size 向下取整
│   └── 检查是否 ≥ min_amount
│
├── 3. 分批下单 (_batch_order)
│   ├── 将总数量分成 batch_count 批 (默认 3 批)
│   ├── 每批:
│   │   ├── 调用 _place_order_with_retry
│   │   │   ├── 市价单 (market order)
│   │   │   └── 失败重试 batch_retry_count 次 (默认 3 次)
│   │   └── 等待 batch_interval 秒 (默认 30 秒)
│   └── 返回最后一笔订单号
│
├── 4. 双边同时执行
│   ├── 做空方: _batch_order(ex_short, symbol, "sell", amount)
│   └── 做多方: _batch_order(ex_long, symbol, "buy", amount)
│
└── 5. 写入数据库
    ├── 创建 ArbTrade 记录
    └── session.commit()
```

### 3.5 监控流程 (monitor.py)

#### 快进快出监控 (`_monitor_quick`)

```
每 10 秒检查一次
│
├── 当前时间 - 开仓时间 > quick_advance_time? (默认 10 秒)
│   └── 否 → 继续等待
│
└── 是 → 执行平仓
    ├── 计算预估盈亏
    ├── 双边市价单平仓
    ├── 更新 ArbTrade (close_date, close_reason, pnl)
    ├── 更新风控 (record_trade_result)
    └── 发送 Telegram 通知
```

#### 基差套利监控 (`_monitor_basis`)

```
每 10 秒检查一次, 按优先级顺序:
│
├── 1. 保证金比率检查
│   ├── 获取两个交易所余额
│   ├── 计算: margin_ratio = used / total
│   └── 超过 margin_threshold (10%)? → 平仓
│
├── 2. 费率反转检查
│   ├── 获取当前费率差
│   ├── 计算: rate_change = 入场费率差 - 当前费率差
│   └── 超过 fee_reversal_threshold (0.05%)? → 平仓
│
├── 3. 基差反向检查
│   ├── 获取当前基差率
│   └── 基差率 ≤ 0? → 平仓
│
├── 4. 基差收敛检查 (止盈)
│   ├── 计算: converge_ratio = |入场基差 - 当前基差| / |入场基差|
│   └── ≥ basis_converge_target (80%)? → 平仓
│
├── 5. 止损检查
│   ├── 计算预估盈亏
│   └── 亏损 ≥ stop_loss (5%)? → 平仓
│
└── 6. 超时检查
    ├── 计算持仓天数
    └── ≥ max_hold_time (7 天)? → 平仓
```

## 四、风控规则 (risk.py)

### 4.1 开仓前检查 (`can_open`)

```
输入: 当前持仓列表, 新币种
│
├── 手动暂停中? → 拒绝
├── 当前持仓数 ≥ 最大并发 (5)? → 拒绝
├── 该币种已有持仓? → 拒绝
├── 当日亏损 ≥ 最大日亏损 (5%)? → 拒绝
└── 连续亏损 ≥ 3 次? → 暂停系统, 拒绝
```

### 4.2 每日重置

```
每天 UTC 0 点:
├── 重置 daily_pnl = 0
├── 重置 consecutive_losses = 0
└── 如果因日亏损暂停 → 自动恢复
```

## 五、通知系统 (notifier.py)

直接调用 Telegram Bot API, 不依赖 FreqtradeBot 的 RPC 系统。

| 通知类型 | 触发时机 | 内容 |
|---------|---------|------|
| 🚀 启动 | 引擎启动 | 监控币种数、交易所、最大并发 |
| 🎯 开仓 | 成功开仓 | 交易对、做空/做多交易所和价格、费率差、基差率 |
| 💰 平仓 | 成功平仓 | 入场/出场价、盈亏、平仓原因 |
| ⚠️ 预警 | 平仓失败等 | 交易对、原因 |
| 🛑 风控 | 风控触发 | 原因、状态 |
| 📊 状态 | 每小时 | 当前持仓数、今日盈亏 |

## 六、配置参数说明

### 6.1 成本参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| slippage | 0.002 (0.2%) | 滑点成本 |
| fee_rate | 0.001 (0.1%) | 双边开平总手续费 |

### 6.2 策略阈值

| 参数 | 默认值 | 说明 |
|------|--------|------|
| fee_threshold | 0.0015 (0.15%) | 费率差阈值, 低于此值不考虑 |
| quick_profit_threshold | 0.0005 (0.05%) | 快进快出收益阈值 |
| basis_profit_threshold | 0.002 (0.2%) | 基差套利收益阈值 |

### 6.3 仓位参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| quick_position_ratio | 0.05 (5%) | 快进快出单笔仓位占比 |
| basis_position_ratio | 0.10 (10%) | 基差套利单笔仓位占比 |
| leverage | 5 | 杠杆倍数 |

### 6.4 分批下单参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| batch_count | 3 | 分批次数 |
| batch_interval | 30 秒 | 批次间隔 |
| batch_retry_count | 3 | 单批重试次数 |
| batch_retry_interval | 5 秒 | 重试间隔 |

### 6.5 系统参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| scan_interval | 10 秒 | 扫描间隔 |
| max_concurrent_positions | 5 | 最大并发持仓数 |
| quick_advance_time | 10 秒 | 快进快出结算前等待时间 |

### 6.6 监控参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| margin_threshold | 0.10 (10%) | 保证金比率阈值 |
| fee_reversal_threshold | 0.0005 (0.05%) | 费率反转阈值 |
| basis_converge_target | 0.80 (80%) | 基差收敛目标 |
| stop_loss | 0.05 (5%) | 止损线 |
| max_hold_time | 7 天 | 最大持仓时间 |

### 6.7 风控参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| max_daily_loss | 0.05 (5%) | 最大日亏损 |
| max_single_loss | 0.02 (2%) | 单笔最大亏损 |

## 七、收益计算公式

```
费率差 = |做空交易所费率 - 做多交易所费率|

基差率 = (做空价 - 做多价) / 做多价

快进快出收益 = 费率差 - 滑点 - 手续费
基差套利收益 = 费率差 + 基差率 - 滑点 - 手续费

预估盈亏 = (入场做空价 - 当前做空价) × 做空数量
         + (当前做多价 - 入场做多价) × 做多数量
         - (做空金额 + 做多金额) × (滑点 + 手续费)
```

## 八、使用方式

### 8.1 扫描模式 (不下单)

```bash
freqtrade arb --config config_arb.json --scan-only
```

### 8.2 测试交易 (强制开一笔)

```bash
freqtrade arb --config config_arb.json --test-trade
```

### 8.3 全自动模式

```bash
freqtrade arb --config config_arb.json
```

### 8.4 Docker 部署

```bash
docker compose up -d --build
docker compose logs -f
```

## 九、文件清单

```
freqtrade/arbitrage/
├── __init__.py       # 模块初始化
├── README.md         # 本文档
├── config.py         # 配置数据类 (ArbConfig)
├── models.py         # 数据库模型 (ArbTrade)
├── scanner.py        # 多交易所扫描器
├── executor.py       # 策略决策 + 双边分批下单
├── monitor.py        # 快进快出/基差套利监控
├── risk.py           # 风控模块
├── notifier.py       # Telegram 通知
└── engine.py         # 主引擎 (编排所有模块)
```

## 十、CLI 注册文件

```
freqtrade/commands/arb_commands.py    # CLI 入口 (start_arb, _run_scan_only, _run_test_trade)
freqtrade/commands/arguments.py       # 注册 arb 子命令
freqtrade/commands/cli_options.py     # --scan-only, --test-trade 选项
freqtrade/commands/__init__.py        # 导出 start_arb
freqtrade/persistence/models.py       # 注册 ArbTrade 到数据库
```
