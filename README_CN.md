# Freqtrade — 开源加密货币交易机器人

> 原始英文文档：[README.md](README.md) | 官方文档：[freqtrade.io](https://www.freqtrade.io)

## 免责声明

本软件仅供教育目的。不要投入你无法承受损失的资金。**使用本软件风险自担**。作者及所有关联方对你的交易结果不承担任何责任。

请务必先在**模拟交易（Dry-Run）**模式下运行，在完全理解其工作机制和预期盈亏之前不要投入真金白银。

---

## 功能特性

- 基于 Python 3.11+，支持 Windows、macOS、Linux
- 支持所有主流交易所（Binance、Bybit、OKX、Kraken、Gate.io、Hyperliquid 等）的现货和合约交易
- **模拟交易**：无需真实资金即可运行
- **回测**：对你的买卖策略进行历史模拟
- **策略优化（Hyperopt）**：使用 Optuna 机器学习优化策略参数
- **FreqAI**：自适应机器学习框架（scikit-learn、LightGBM、XGBoost、强化学习）
- **内置 Web UI**：通过浏览器管理机器人
- **Telegram 控制**：通过 Telegram 远程管理
- 动态白名单/黑名单管理
- 盈亏统计与报告

---

## 硬件要求

- 最低配置：2GB RAM、1GB 磁盘空间、2 vCPU
- 时钟必须准确，建议配置 NTP 时间同步

---

## Linux 部署指南

以下步骤在 Ubuntu/Debian 系统上验证，其他发行版请适当调整包管理器命令。

### 方式一：Docker 部署（推荐）

Docker 是最简单的部署方式，无需手动安装 Python 和 TA-Lib。

#### 1. 安装 Docker

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
# 重新登录使 docker 组生效
```

#### 2. 创建工作目录

```bash
mkdir -p ~/freqtrade/user_data/strategies
cd ~/freqtrade
```

#### 3. 创建配置文件

```bash
# 下载示例配置
curl -o config.json https://raw.githubusercontent.com/freqtrade/freqtrade/develop/config_examples/config_full.example.json

# 编辑配置（至少填写 exchange API key/secret）
nano config.json
```

配置文件中必须修改的关键字段：

```json
{
  "exchange": {
    "name": "binance",
    "key": "你的API_KEY",
    "secret": "你的API_SECRET",
    "pair_whitelist": ["BTC/USDT", "ETH/USDT"]
  },
  "telegram": {
    "enabled": true,
    "token": "你的TELEGRAM_BOT_TOKEN",
    "chat_id": "你的CHAT_ID"
  }
}
```

#### 4. 使用 Docker Compose 运行

创建 `docker-compose.yml`：

```yaml
version: '3'
services:
  freqtrade:
    image: freqtradeorg/freqtrade:stable
    restart: unless-stopped
    container_name: freqtrade
    volumes:
      - ./user_data:/freqtrade/user_data
      - ./config.json:/freqtrade/config.json
    ports:
      - "8080:8080"
    command: >
      trade
      --logfile /freqtrade/user_data/logs/freqtrade.log
      --config /freqtrade/config.json
```

启动：

```bash
docker compose up -d
```

#### 5. 常用 Docker 命令

```bash
# 查看日志
docker compose logs -f

# 停止
docker compose down

# 更新到最新版本
docker compose pull
docker compose up -d

# 进入容器执行命令
docker compose exec freqtrade freqtrade <command>

# 回测示例
docker compose exec freqtrade freqtrade backtesting \
  --config /freqtrade/config.json \
  --strategy MyStrategy \
  --timerange 20230101-20231231
```

---

### 方式二：原生 Python 安装

#### 1. 安装系统依赖

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git build-essential \
  libssl-dev libffi-dev libgfortran5 pkg-config cmake gcc curl
```

#### 2. 安装 TA-Lib（C 库）

TA-Lib 是技术分析指标库，必须先安装 C 语言版本：

```bash
cd /tmp
curl -L -o ta-lib-0.6.4-src.tar.gz https://github.com/ta-lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz
tar xzf ta-lib-0.6.4-src.tar.gz
cd ta-lib-0.6.4
./configure --prefix=/usr/local
make -j$(nproc)
sudo make install
sudo ldconfig
cd /tmp && rm -rf ta-lib-0.6.4*
```

#### 3. 创建虚拟环境

```bash
python3 -m venv ~/freqtrade/venv
source ~/freqtrade/venv/bin/activate
pip install --upgrade pip wheel
```

#### 4. 安装 Freqtrade

```bash
cd ~
git clone https://github.com/freqtrade/freqtrade.git
cd freqtrade

# 安装核心依赖
pip install -r requirements.txt

# 安装 Freqtrade 本体
pip install -e .

# 安装 FreqUI（Web 界面）
freqtrade install-ui
```

如果需要 Hyperopt 功能：

```bash
pip install -r requirements-hyperopt.txt
```

如果需要 FreqAI 功能：

```bash
pip install -r requirements-freqai.txt
```

如果需要强化学习（FreqAI-RL）：

```bash
pip install -r requirements-freqai-rl.txt
```

#### 5. 初始化用户目录和配置

```bash
# 创建用户数据目录结构
freqtrade create-userdir --userdir user_data

# 创建新配置文件
freqtrade new-config --config user_data/config.json
```

#### 6. 验证安装

```bash
freqtrade --version
```

#### 7. 运行机器人

```bash
# 模拟交易
freqtrade trade --config user_data/config.json --strategy SampleStrategy

# 后台运行（使用 nohup）
nohup freqtrade trade --config user_data/config.json --strategy SampleStrategy \
  --logfile user_data/logs/freqtrade.log &

# 使用 systemd 管理（推荐）
```

#### 8. 配置 systemd 服务（推荐）

创建 `/etc/systemd/system/freqtrade.service`：

```ini
[Unit]
Description=Freqtrade Trading Bot
After=network.target

[Service]
Type=simple
User=你的用户名
WorkingDirectory=/home/你的用户名/freqtrade
ExecStart=/home/你的用户名/freqtrade/venv/bin/freqtrade trade \
  --config /home/你的用户名/freqtrade/user_data/config.json \
  --strategy SampleStrategy \
  --logfile /home/你的用户名/freqtrade/user_data/logs/freqtrade.log
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable freqtrade
sudo systemctl start freqtrade

# 查看状态
sudo systemctl status freqtrade

# 查看日志
sudo journalctl -u freqtrade -f
```

---

## 常用命令速查

### 交易相关

```bash
freqtrade trade                          # 启动交易
freqtrade trade --dry-run                # 模拟交易（不实际下单）
freqtrade trade --config myconfig.json   # 指定配置文件
```

### 回测

```bash
# 基本回测
freqtrade backtesting --config user_data/config.json --strategy MyStrategy

# 指定时间范围
freqtrade backtesting --strategy MyStrategy --timerange 20230101-20231231

# 查看历史回测结果
freqtrade backtesting-show --show-trades
```

### Hyperopt 策略优化

```bash
# 运行优化
freqtrade hyperopt --hyperopt-loss SharpeHyperOptLoss --strategy MyStrategy -e 500

# 列出优化结果
freqtrade hyperopt-list --profit

# 查看最佳结果详情
freqtrade hyperopt-show -n 1
```

### 数据管理

```bash
# 下载历史数据
freqtrade download-data --timerange 20230101-20231231 --pairs BTC/USDT ETH/USDT

# 列出已下载的数据
freqtrade list-data

# 转换数据格式
freqtrade convert-data --format-from json --format-to jsongz
```

### 其他工具

```bash
freqtrade list-exchanges                # 列出支持的交易所
freqtrade list-pairs --exchange binance  # 列出交易所的交易对
freqtrade list-timeframes                # 列出可用时间周期
freqtrade test-pairlist                  # 测试 pairlist 配置
freqtrade plot-dataframe                 # 绘制 K 线和指标图
freqtrade plot-profit                    # 绘制利润图
freqtrade webserver                      # 启动 Web UI 服务
```

---

## 策略开发

策略是 Freqtrade 的核心。你需要创建一个继承 `IStrategy` 的 Python 类，实现以下关键方法：

```python
from freqtrade.strategy import IStrategy, DecimalParameter
import talib.abstract as ta
import pandas as pd

class MyStrategy(IStrategy):
    # 策略接口版本（3 = 支持做多/做空/杠杆）
    INTERFACE_VERSION = 3

    # 时间周期
    timeframe = '5m'

    # 最小 ROI 表
    minimal_roi = {
        "60": 0.01,
        "30": 0.02,
        "0": 0.04
    }

    # 止损
    stoploss = -0.10

    # 启用做空
    can_short = True

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """计算技术指标"""
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        dataframe['ema_fast'] = ta.EMA(dataframe, timeperiod=12)
        dataframe['ema_slow'] = ta.EMA(dataframe, timeperiod=26)
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """定义入场条件"""
        dataframe.loc[
            (
                (dataframe['rsi'] < 30) &
                (dataframe['ema_fast'] > dataframe['ema_slow'])
            ),
            'enter_long'] = 1

        dataframe.loc[
            (
                (dataframe['rsi'] > 70) &
                (dataframe['ema_fast'] < dataframe['ema_slow'])
            ),
            'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """定义出场条件"""
        dataframe.loc[
            (
                (dataframe['rsi'] > 70) &
                (dataframe['ema_fast'] < dataframe['ema_slow'])
            ),
            'exit_long'] = 1

        dataframe.loc[
            (
                (dataframe['rsi'] < 30) &
                (dataframe['ema_fast'] > dataframe['ema_slow'])
            ),
            'exit_short'] = 1
        return dataframe
```

将策略文件放在 `user_data/strategies/` 目录下，然后通过 `--strategy MyStrategy` 参数指定运行。

---

## Telegram 机器人配置

1. 在 Telegram 中搜索 `@BotFather`，发送 `/newbot` 创建机器人，获取 Token
2. 获取你的 Chat ID：给 `@userinfobot` 发消息，它会回复你的 ID
3. 在 `config.json` 中配置：

```json
{
  "telegram": {
    "enabled": true,
    "token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    "chat_id": "12345678"
  }
}
```

### 常用 Telegram 命令

| 命令 | 说明 |
|------|------|
| `/start` | 启动交易 |
| `/stop` | 停止交易 |
| `/stopentry` | 停止开新仓 |
| `/status` | 查看当前持仓 |
| `/profit` | 查看累计收益 |
| `/balance` | 查看账户余额 |
| `/daily <n>` | 最近 n 天盈亏 |
| `/forceexit <id>` | 强制平仓 |

---

## 支持的交易所

### 现货交易

Binance、BingX、Bitget、Bitmart、Bybit、Gate.io、HTX、Hyperliquid（DEX）、Kraken、OKX

### 合约交易

Binance、Bitget、Gate.io、Hyperliquid（DEX）、OKX、Bybit、Kraken

### 社区验证

Bitvavo、Kucoin

> 各交易所可能需要特定配置，详见 [交易所说明文档](https://www.freqtrade.io/en/stable/exchanges/)。

---

## 故障排查

### 常见问题

**TA-Lib 安装失败**：确保先安装 C 语言版本的 TA-Lib，再通过 pip 安装 Python 包。

**时钟不同步**：交易所 API 对时间敏感，确保系统时间准确：

```bash
sudo apt install -y ntp
sudo systemctl enable --now ntp
```

**权限问题**：不要以 root 用户运行 Freqtrade，使用普通用户 + systemd 服务管理。

**内存不足**：FreqAI 模型训练需要较多内存，建议 4GB+ RAM。

---

## 项目分支

- `develop` — 开发分支，包含最新功能，可能存在破坏性变更
- `stable` — 稳定发布版本，经过充分测试
- `feat/*` — 功能分支，用于特定功能开发

## 社区支持

- [Discord](https://discord.gg/p7nuUNVfP7) — 社区讨论和技术支持
- [GitHub Issues](https://github.com/freqtrade/freqtrade/issues) — Bug 报告和功能请求
- [官方文档](https://www.freqtrade.io) — 完整的使用和开发文档
