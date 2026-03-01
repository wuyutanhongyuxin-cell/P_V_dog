# Paradex × Variational DCA/Grid 套利机器人

跨交易所 DCA（Dollar Cost Averaging）渐进建仓套利系统。监控 [Paradex](https://paradex.trade)（Starknet L2 永续合约）和 [Variational](https://variational.io)（Arbitrum L2 期权做市）之间的价差，当价差满足条件时以固定数量逐笔加仓，构建完美对冲仓位。

## 策略原理

```
传统套利: 看到信号 → 一次性全仓进出 → 依赖单次时机
DCA 套利: 看到信号 → 只加一个 qty → 重复多次 → 均价入场 → 风险分散
```

**核心思路**：不追求一次完美入场，而是通过多次小额交易，在价差有利区间内渐进建仓。

### 运行机制

1. 每秒检查 Paradex 和 Variational 的实时价差
2. 当价差进入 `[mingap, maxgap]` 区间时，执行一笔双腿交易（加仓）
3. 每次只交易 `qty` 数量，不超过 `max-position` 上限
4. 两次加仓之间至少间隔 `interval` 秒
5. 双腿使用 `asyncio.gather` 并行执行，确保同时成交
6. 净持仓始终为零（A交易所做多 = B交易所做空）

### 关键优势

- **零手续费**：Paradex Interactive Token + Variational RFQ 均为零费率
- **渐进建仓**：DCA 均价效应，降低单次入场风险
- **完美对冲**：净持仓永远为零，市场涨跌无影响
- **风控完善**：单腿失败自动撤销、系统熔断、限速保护

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的密钥
```

需要准备：
- **Paradex**: Starknet L2 私钥 + 地址（从 Paradex 账户导出）
- **Variational**: 浏览器 Cookie（F12 > Application > Cookies 获取 `vr-token`）
- **Telegram**（可选）: Bot Token + Group ID

### 3. 启动机器人

```bash
python main.py --ticker BTC --direction long --qty 0.005 \
  --max-position 0.1 --mingap 33 --maxgap 44 --interval 30 \
  2>&1 | tee -a logs/run_BTC_$(date +%F_%H%M%S).log
```

## 参数说明

### 核心参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--ticker` | 否 | `BTC` | 交易对。支持: BTC, ETH, SOL, ARB, DOGE, AVAX, LINK, OP, WIF, PEPE |
| `--direction` | 否 | `long` | 方向: `long`=Paradex做多+Variational做空, `short`=反向 |
| `--qty` | **是** | - | 每次加仓数量 (BTC) |
| `--max-position` | **是** | - | 最大累积仓位 (BTC) |

### DCA 参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--mingap` | **是** | - | 最小价差 (USD)。价差 >= mingap 才加仓 |
| `--maxgap` | 否 | `9999` | 最大价差 (USD)。价差 > maxgap 停止加仓（不追极端行情） |
| `--closegap` | 否 | `0` | 减仓阈值 (USD)。反向价差 >= closegap 时减仓。0=不主动减仓 |
| `--interval` | 否 | `30` | 两次加仓最小间隔 (秒) |

### 风控参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--fill-timeout` | `5` | 订单成交超时 (秒) |
| `--min-balance` | `10` | 最低余额 (USDC)，低于此值自动停机 |

### 运行模式

| 参数 | 说明 |
|------|------|
| `--dry-run` | 试运行：监控价差、模拟加仓，但不实际下单 |
| `--env-file` | .env 文件路径 (默认: `.env`) |
| `--variational-auth-mode` | 认证模式: `cookie` (默认) 或 `siwe` |

## 参数实例

### 标准 BTC 做多 DCA

```bash
python main.py --ticker BTC --direction long \
  --qty 0.005 --max-position 0.1 \
  --mingap 33 --maxgap 44 --interval 30 \
  2>&1 | tee -a logs/run_BTC_$(date +%F_%H%M%S).log
```

- 每次加仓 0.005 BTC (~$325)
- 最多累积 0.1 BTC (~$6500)
- 价差 $33-$44 时加仓
- 30 秒间隔

### 保守模式（小仓位 + 高阈值）

```bash
python main.py --ticker BTC --direction long \
  --qty 0.001 --max-position 0.01 \
  --mingap 40 --maxgap 55 --interval 60 \
  2>&1 | tee -a logs/run_BTC_conservative_$(date +%F_%H%M%S).log
```

- 每次仅 0.001 BTC (~$65)
- 最多 0.01 BTC (~$650)
- 只在较大价差 ($40+) 时进场
- 1 分钟间隔

### 激进模式（大仓位 + 低阈值）

```bash
python main.py --ticker BTC --direction long \
  --qty 0.006 --max-position 0.15 \
  --mingap 25 --maxgap 50 --interval 15 \
  2>&1 | tee -a logs/run_BTC_aggressive_$(date +%F_%H%M%S).log
```

- 每次 0.006 BTC
- 最多 0.15 BTC
- 较低阈值 ($25)，更频繁进场
- 15 秒间隔

### 带减仓功能

```bash
python main.py --ticker BTC --direction long \
  --qty 0.005 --max-position 0.1 \
  --mingap 33 --maxgap 44 --closegap 20 --interval 30 \
  2>&1 | tee -a logs/run_BTC_closegap_$(date +%F_%H%M%S).log
```

- `--closegap 20`：当反向价差 >= $20 时开始逐笔减仓

### 做空方向

```bash
python main.py --ticker BTC --direction short \
  --qty 0.005 --max-position 0.1 \
  --mingap 33 --maxgap 44 --interval 30 \
  2>&1 | tee -a logs/run_BTC_short_$(date +%F_%H%M%S).log
```

- Paradex 做空 + Variational 做多

### ETH 交易对

```bash
python main.py --ticker ETH --direction long \
  --qty 0.05 --max-position 1.0 \
  --mingap 5 --maxgap 15 --interval 30 \
  2>&1 | tee -a logs/run_ETH_$(date +%F_%H%M%S).log
```

### 多实例运行

同时运行多个实例覆盖不同参数区间：

```bash
# 实例 1: 标准参数
screen -dmS dca-1 bash -c 'cd /path/to/P_V_dog && \
  ACCOUNT_LABEL=DCA-1 python main.py --ticker BTC --direction long \
  --qty 0.005 --max-position 0.1 --mingap 33 --maxgap 44 --interval 30 \
  2>&1 | tee -a logs/run_BTC_1_$(date +%F_%H%M%S).log'

# 实例 2: 更高阈值
screen -dmS dca-2 bash -c 'cd /path/to/P_V_dog && \
  ACCOUNT_LABEL=DCA-2 python main.py --ticker BTC --direction long \
  --qty 0.005 --max-position 0.1 --mingap 38 --maxgap 50 --interval 30 \
  2>&1 | tee -a logs/run_BTC_2_$(date +%F_%H%M%S).log'
```

> 注意: 多实例共用同一个交易所账户时，`max-position` 之和不应超过账户承受能力。

### 试运行（不下单）

```bash
python main.py --ticker BTC --direction long \
  --qty 0.005 --max-position 0.1 \
  --mingap 20 --maxgap 44 --interval 10 \
  --dry-run \
  2>&1 | tee -a logs/dryrun_BTC_$(date +%F_%H%M%S).log
```

- 获取真实 BBO，计算真实价差
- 模拟加仓记录，但不发送任何订单
- 适合观察价差分布和调参

## 部署指南 (VPS)

### 使用 screen

```bash
# 创建 screen 会话
screen -S dca-arb

# 进入项目目录
cd /path/to/P_V_dog

# 启动（带日志记录）
python main.py --ticker BTC --direction long \
  --qty 0.005 --max-position 0.1 \
  --mingap 33 --maxgap 44 --interval 30 \
  2>&1 | tee -a logs/run_BTC_$(date +%F_%H%M%S).log

# 断开 screen (保持运行)
# Ctrl+A, D

# 重新连接
screen -r dca-arb
```

### 下载日志到本地

```bash
# Windows PowerShell
scp user@your-vps:/path/to/P_V_dog/logs/*.log D:\logs\
```

## 风控机制

### 内置保护

| 机制 | 触发条件 | 动作 |
|------|----------|------|
| **单腿撤销** | 一边成交、另一边失败 | 自动反向平掉成功的那边 |
| **系统熔断** | Paradex 维护模式 (CANCEL_ONLY) | 暂停 10 分钟 |
| **连续失败暂停** | 连续 3 次单腿失败 | 暂停 5 分钟 |
| **Interactive 限速** | 接近 200单/小时 | 暂停交易 |
| **Variational 限速** | 收到 429/418 | 自动冷却 |
| **余额不足** | USDC < min_balance | 平仓退出 |
| **仓位不平衡** | 净仓位 > 2×qty | 告警 |

### Ctrl+C 优雅退出

按 Ctrl+C 会触发：
1. 取消所有挂单
2. 市价平掉两边仓位
3. 循环验证直到仓位归零
4. 记录最终统计

## Telegram 通知

配置 `.env` 中的 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_GROUP_ID` 后，机器人会发送：

- **启动通知**：参数、余额、仓位
- **加仓通知**：价差、利润、当前仓位、还需几笔
- **减仓通知**：价差、剩余仓位
- **心跳 (每5分钟)**：完整状态（仿竞品格式）
- **告警**：单腿失败、熔断、仓位不平衡
- **退出通知**：运行统计

### 心跳格式示例

```
💚 心跳 | 启动时间=2026-0301-15:08 | BTC |
mingap=33 maxgap=44 qty=0.005 interval=30 needed=3 |
A持仓=+0.065000 | B持仓=-0.065000 | 净持仓=0.000000
账户余额: EX=488.67 LG=472.46 | 总策略权益=$961.13 |
盈亏=$3.62 | 总交易量=$83300.45
初始总权益=$957.51 | 交易次数=13
总平均偏移: A=-5.30 (-0.81bps) B=-0.3810 (-0.06bps)
```

## 项目结构

```
P_V_dog/
├── main.py                        # 入口 + CLI 参数
├── config.py                      # 配置管理
├── strategy/
│   ├── dca_engine.py              # 核心 DCA 引擎
│   ├── spread_monitor.py          # 价差监控 (固定阈值)
│   └── position_manager.py        # DCA 状态机
├── exchanges/
│   ├── base.py                    # 交易所基类
│   ├── paradex_client.py          # Paradex API 客户端
│   └── variational_client.py      # Variational API 客户端
├── helpers/
│   ├── logger.py                  # 日志系统
│   ├── telegram_bot.py            # Telegram 通知
│   └── pnl_tracker.py            # P&L 追踪
├── .env.example                   # 环境变量模板
└── requirements.txt               # Python 依赖
```

## DCA 状态机

```
              spread in [mingap, maxgap]
              + interval elapsed
              + position < max
IDLE ─────────────────────────────> ACCUMULATING
  ^                                      │
  │   position == 0                      │  keep adding
  │   after reduction                    │  (one qty per signal)
  │                                      │
  └──── REDUCING <──── FULL <────────────┘
         (closegap)     position >= max_position
```

| 状态 | 说明 |
|------|------|
| `IDLE` | 无仓位，等待价差信号 |
| `ACCUMULATING` | 建仓中，每次信号加一个 qty |
| `FULL` | 满仓，停止加仓 |
| `REDUCING` | 减仓中 (需配置 closegap > 0) |

## FAQ

**Q: mingap 应该设多少？**
A: 取决于交易对。BTC 建议从 20 开始观察（用 `--dry-run`），根据信号频率逐步调整。竞品参考值 31-34 USD。

**Q: 为什么不主动平仓？**
A: 默认 `closegap=0` 意味着持仓不会主动平掉。这和竞品策略一致——通过持有对冲仓位赚取 funding rate 差异和价差收窄。如需主动减仓，设置 `--closegap` 大于 0。

**Q: 多实例会冲突吗？**
A: 如果使用同一个交易所账户，仓位是共享的。建议不同实例设置不同的 `ACCOUNT_LABEL`，并确保 `max-position` 之和在可控范围内。

**Q: Variational Cookie 过期了怎么办？**
A: 重新登录 Variational 网站，F12 获取新的 `vr-token`，更新 `.env`，重启机器人。

**Q: 试运行和实盘有什么区别？**
A: `--dry-run` 获取真实 BBO 和计算真实价差，但不发送任何订单。日志会显示"DRY RUN"标记。适合观察价差分布和验证参数。

## License

MIT
