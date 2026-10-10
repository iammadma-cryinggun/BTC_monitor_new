# 🐸 妖币埋伏监控器 (Memecoin Ambush Monitor)

基于你手写的「妖币六阶段吸筹砸盘剧本」自动检测 + 24h 云端监控。只**报警、不交易**，你手动操作。

- **数据源**：Binance USD-M 合约公开行情 klines（只读、无需 API key、低权重，防封）
- **检测逻辑**：`memecoin_playbook_scanner.detect` — 4h 六阶段（S1 横盘→S2 启动→S3 洗盘→S4 主升→S5 插针见顶→S6 崩盘+死猫反弹）+ 1h MA99 关键线
- **信号**：🚀ENTER(启动) / 🔥AMBUSH(埋伏★) / 💧DIP(洗盘) / ⚠️TOP(见顶) / 🩸CRASH(崩盘) / ↩️BOUNCE(死猫反弹)
- **呈现**：① Telegram 实时推送 ② 网页看板（同信号，实时刷新）

---

## 1. 本地配置

编辑 `monitor_config.json`：

```jsonc
{
  "market": "futures",        // 妖币在合约市场，现货几乎不含
  "universe": "curated",      // curated=约80个精选妖币池(默认,防封)；exchange=全量合约；list=自定义
  "scan_interval_minutes": 120,
  "web": {"enabled": true, "host": "0.0.0.0", "port": 8080},
  "notification": {
    "channel": "telegram",    // telegram | webhook | log
    "telegram_bot_token": "你的BOT_TOKEN",
    "telegram_chat_id": "你的CHAT_ID",
    "webhook_url": ""         // 可选：Discord/飞书/企微/Slack 通用 webhook
  }
}
```

> 拿 chat_id：给 @userinfobot 发任意消息即可；bot_token 来自 @BotFather。
>
> 也可以**不写 config 文件**，直接用环境变量注入（优先级高于 `monitor_config.json`）：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`WEBHOOK_URL`。适合 Zeabur / CI 等不提交密钥的场景。`monitor_config.json` 已被 `.gitignore` 忽略，仓库内置的是 `monitor_config.example.json` 占位模板，克隆后需自行补一份或设环境变量。
>
> **更多可调环境变量**（Zeabur 后台直接改，无需改代码）：
> - `MONITOR_UNIVERSE`：`curated`（默认，~80 精选妖币）｜ `exchange`（动态拉全部真实永续，自动剔除下架币、覆盖新上币，推荐用这个让监控池始终有效）｜ `list`（配合 `monitor_config.json` 的 `symbols`）
> - `MONITOR_MARKET`：`futures`（默认，妖币主战场）｜ `spot`
> - `SCAN_INTERVAL_MINUTES`：扫描间隔，默认 120
> - `DIGEST_HOUR`：每日摘要的本地时刻（小时），默认 8
> - `LOCAL_UTC_OFFSET`：本地时区相对 UTC 偏移，默认 8（北京时间）
> - `REQUEST_SLEEP`：每币请求间隔（秒），默认 0.15，调大可进一步降权防封
>
> **若看板上「监控币数」远小于预期**（例如只有 3/80），多半是 curated 列表里大量 symbol 在合约上已不存在（404）或被限流。把 `MONITOR_UNIVERSE` 设成 `exchange` 即可让监控池回到全量真实永续。看板顶部「扫描诊断」一行会显示尝试/成功/跳过(及原因)，用于定位。

### 本地试运行

```bash
# 仅测试 Telegram 是否连通
python monitor.py --test-notify

# 单次扫描 5 个币 + 推送 + 起网页看板（Ctrl-C 停止）
python monitor.py --once --web --sym-list RAVEUSDT,LABUSDT,BTWUSDT,BANKUSDT,AKUSDT --port 8080

# 长期运行（同 Zeabur 行为）
python monitor.py --web --port 8080
```

网页看板：浏览器打开 `http://localhost:8080`（每 60s 自动刷新，显示 KPI / 埋伏观察列表 / 全量监控 / 信号流）。

---

## 2. 部署到 Zeabur（24h 云端）

1. 把整个 `memecoin-monitor/` 推到 GitHub 私有仓库。
2. Zeabur 新建 Project → Service → 导入该仓库。
3. **Builder** 选 Dockerfile（已就绪）。
4. **Region**：选 Binance 友好区（Singapore / Tokyo）降低延迟与风控。
5. **Port**：Zeabur 会自动分配并注入 `PORT` 环境变量，Dockerfile 已用 `--port ${PORT:-8080}` 接管，无需手动填。
6. 在 Service 的 **Environment Variables / Secrets** 里设好（密钥项勾选 Secret）：
   - `TELEGRAM_BOT_TOKEN`（Secret）— 否则收不到 Telegram 警报
   - `TELEGRAM_CHAT_ID`（Secret）
   - `MONITOR_UNIVERSE` = `exchange` — **推荐**，动态拉全部真实永续，监控池始终有效（默认 `curated` 写死 ~80 个 symbol，很多已下架会显示「跳过」）
   - `PORT`（Zeabur 自动给，可留空让其注入）
   - 其余调优变量可选：`MONITOR_MARKET` / `SCAN_INTERVAL_MINUTES` / `DIGEST_HOUR` / `LOCAL_UTC_OFFSET` / `REQUEST_SLEEP`（见上方说明）
   - 不需任何密钥也能跑（回退到 `log` 通道）；但 Telegram 凭据建议用 Secrets 注入。
7. **Volume**：挂一个持久卷到 `/app/data`，否则重启后监控状态/信号历史会丢失。
8. **敏感项**：`monitor_config.json` 含 Telegram token，建议用 Zeabur 的 Environment Variables / Secrets 注入，或私有仓库 + 不公开 config。
9. Deploy。部署后 Zeabur 给一个公开 URL → 就是你的网页看板；Telegram 同时推送信号。

### 关闭 / 切换通知
- 不想用网页：`monitor_config.json` 里 `web.enabled=false`，或运行时 `--no-web`。
- 不想用 Telegram：把 `notification.channel` 改成 `log`（只写本地日志）或 `webhook`（填 webhook_url）。

---

## 3. 文件结构

| 文件 | 作用 |
|---|---|
| `monitor.py` | 24h 监控主循环：扫描 → 跨轮状态比对 → 触发信号 → 推送 + 写看板 |
| `dashboard.py` | 网页看板生成 + 内置 HTTP 服务（端口暴露） |
| `notifier.py` | 可插拔通知（Telegram / Webhook / 本地日志） |
| `memecoin_scanner.py` | 全市场扫描 + 打分 + 报告 HTML |
| `memecoin_playbook_scanner.py` | 六阶段剧本检测器核心 |
| `data/monitor_state.json` | 每币当前阶段/分数/事件（持久化） |
| `data/signals_log.json` | 信号历史（看板信号流数据源） |
| `data/dashboard.html` | 生成的网页看板（每次扫描覆盖） |

## 4. 风控与边界
- 纯只读行情，不下单、不读账户，**只为报警**。
- 默认 curated 池（~80 币）+ 每币 sleep 0.15s，整体权重极低，正常不会被封。
- 切换到 `universe: "exchange"`（全量 ~300+ 合约）会显著增加请求量，谨慎使用。
- 检测是「剧本匹配」不是「预测」——信号给出的是**值得盯的时点**，最终买卖由你决定。
