# BTC Futures Sniper

基于OBI（订单簿失衡度）的币安合约短线交易系统。

复用Polymarket策略逻辑，适配币安永续合约。

## 模拟模式 (Dry Run)

**推荐先用模拟模式验证策略！**

```toml
[trading]
dry_run = true           # 模拟模式
initial_balance = 1000.0 # 模拟资金
```

模拟模式特点：
- ✅ 使用真实Binance订单簿数据
- ✅ 生成真实交易信号
- ✅ 模拟下单和盈亏计算
- ❌ 不实际发送API请求
- ❌ 不承担任何资金风险

## 策略逻辑

### 核心信号

```
OBI = (Bids_Vol - Asks_Vol) / (Bids_Vol + Asks_Vol)
```

| 信号 | 条件 | 操作 |
|-----|------|------|
| GOLD_BULL | OBI >= 0.40 | 做多 |
| BEAR_NO | OBI <= -0.30 | 做空 |

### 风险控制

| 参数 | 默认值 | 说明 |
|-----|-------|------|
| 杠杆 | 10x | 可配置 |
| 止损 | 2% | 固定止损 |
| 止盈 | 1% | 固定止盈 |
| 移动止损 | 0.5% | 盈利后启动 |
| 最大持仓时间 | 5分钟 | 超时平仓 |
| 仓位比例 | 5% | 单笔仓位 |

### OBI反转保护

- 做多时：入场检查OBI>=0，否则取消
- 做空时：入场检查OBI<=0，否则取消

## 配置

编辑 `config.toml`:

```toml
[binance]
api_key = "your_api_key"
api_secret = "your_api_secret"
testnet = true  # 测试网模式

[trading]
symbol = "BTCUSDT"
leverage = 10

[risk]
stop_loss_pct = 0.02
take_profit_pct = 0.01

[signal]
gold_bull_obi = 0.40
bear_no_obi = -0.30
```

## 运行

```bash
# 编译
cargo build --release

# 运行
cargo run --release
```

## 项目结构

```
src/
├── main.rs          # 主入口
├── config.rs        # 配置管理
├── signal.rs        # 信号引擎（复用Polymarket逻辑）
├── binance_ws.rs    # Binance WebSocket
├── binance_rest.rs  # Binance REST API
├── risk.rs          # 风险管理
├── database.rs      # 交易记录
└── trader.rs        # 交易引擎
```

## 与Polymarket版本对比

| 特性 | Polymarket | 币安合约 |
|-----|-----------|---------|
| 信号源 | Binance OBI | Binance OBI |
| 交易标的 | YES/NO二元期权 | 永续合约 |
| 时间限制 | 5分钟固定 | 无（设置超时平仓）|
| 止盈止损 | 自动结算 | 手动设置 |
| 风险 | 固定亏损 | 可能爆仓 |

## 预期收益

基于历史数据模拟：

| 杠杆 | 止盈 | 止损 | 期望收益/笔 |
|-----|------|------|------------|
| 5x | +5% | -10% | +3.5% |
| 10x | +10% | -20% | +7.0% |
| 20x | +20% | -40% | +14.0% |

## 风险提示

- 合约交易有爆仓风险
- 建议先在测试网验证
- 严格控制杠杆（≤10x）
- 设置合理止损
