// ─────────────────────────────────────────────────────────────
// Telegram 通知模块
// ─────────────────────────────────────────────────────────────

use reqwest::Client;
use serde::{Deserialize, Serialize};
use tracing::{debug, error, info, warn};

/// Telegram 配置
#[derive(Debug, Clone)]
pub struct TelegramConfig {
    pub enabled: bool,
    pub bot_token: String,
    pub chat_id: String,
}

/// Telegram 客户端
pub struct TelegramNotifier {
    client: Client,
    config: TelegramConfig,
    base_url: String,
}

impl TelegramNotifier {
    pub fn new(enabled: bool, bot_token: &str, chat_id: &str) -> Self {
        let base_url = format!("https://api.telegram.org/bot{}", bot_token);
        Self {
            client: Client::new(),
            config: TelegramConfig {
                enabled,
                bot_token: bot_token.to_string(),
                chat_id: chat_id.to_string(),
            },
            base_url,
        }
    }

    /// 发送消息
    pub async fn send_message(&self, text: &str) -> Result<(), String> {
        if !self.config.enabled {
            debug!("[Telegram] 未启用，跳过发送");
            return Ok(());
        }

        if self.config.bot_token.is_empty() || self.config.chat_id.is_empty() {
            warn!("[Telegram] Bot Token 或 Chat ID 未配置");
            return Err("Telegram配置不完整".to_string());
        }

        let url = format!("{}/sendMessage", self.base_url);

        let response = self.client
            .post(&url)
            .form(&[
                ("chat_id", self.config.chat_id.as_str()),
                ("text", text),
                ("parse_mode", "HTML"),
            ])
            .send()
            .await
            .map_err(|e| format!("请求失败: {}", e))?;

        if response.status().is_success() {
            debug!("[Telegram] 消息发送成功");
            Ok(())
        } else {
            let text = response.text().await.unwrap_or_default();
            error!("[Telegram] 发送失败: {}", text);
            Err(format!("发送失败: {}", text))
        }
    }

    /// 发送开仓通知
    pub async fn notify_open(
        &self,
        side: &str,
        entry_price: f64,
        quantity: f64,
        obi: f64,
        balance: f64,
    ) {
        let emoji = match side {
            "LONG" => "🟢",
            "SHORT" => "🔴",
            _ => "⚪",
        };

        let text = format!(
            r#"{emoji} <b>开仓通知</b>

<b>方向:</b> {side}
<b>价格:</b> ${entry_price:.2}
<b>数量:</b> {quantity:.6} BTC
<b>OBI:</b> {obi:+.3}

💰 余额: {balance:.2} USDT
⏰ {time}"#,
            emoji = emoji,
            side = side,
            entry_price = entry_price,
            quantity = quantity,
            obi = obi,
            balance = balance,
            time = chrono::Local::now().format("%H:%M:%S"),
        );

        if let Err(e) = self.send_message(&text).await {
            warn!("[Telegram] 开仓通知发送失败: {}", e);
        }
    }

    /// 发送平仓通知
    pub async fn notify_close(
        &self,
        side: &str,
        entry_price: f64,
        exit_price: f64,
        pnl: f64,
        pnl_pct: f64,
        hold_time: i64,
        reason: &str,
        total_trades: u32,
        win_rate: f64,
        total_pnl: f64,
        balance: f64,
    ) {
        let emoji = if pnl >= 0.0 { "✅" } else { "❌" };
        let pnl_emoji = if pnl >= 0.0 { "📈" } else { "📉" };

        let text = format!(
            r#"{emoji} <b>平仓通知</b> ({reason})

<b>方向:</b> {side}
<b>入场:</b> ${entry_price:.2}
<b>出场:</b> ${exit_price:.2}
<b>持仓:</b> {hold_time}秒

{pnl_emoji} <b>盈亏:</b> {pnl:+.4} USDT ({pnl_pct:+.2}%)

──────────────
📊 累计: {total_trades}笔 | 胜率: {win_rate:.1}%
💰 总盈亏: {total_pnl:+.2} USDT
💼 余额: {balance:.2} USDT
⏰ {time}"#,
            emoji = emoji,
            reason = reason,
            side = side,
            entry_price = entry_price,
            exit_price = exit_price,
            hold_time = hold_time,
            pnl_emoji = pnl_emoji,
            pnl = pnl,
            pnl_pct = pnl_pct * 100.0,
            total_trades = total_trades,
            win_rate = win_rate,
            total_pnl = total_pnl,
            balance = balance,
            time = chrono::Local::now().format("%H:%M:%S"),
        );

        if let Err(e) = self.send_message(&text).await {
            warn!("[Telegram] 平仓通知发送失败: {}", e);
        }
    }

    /// 发送每日报告
    pub async fn send_daily_report(
        &self,
        total_trades: u32,
        wins: u32,
        losses: u32,
        total_pnl: f64,
        win_rate: f64,
        balance: f64,
    ) {
        let pnl_emoji = if total_pnl >= 0.0 { "📈" } else { "📉" };

        let text = format!(
            r#"📊 <b>每日报告</b>

📅 日期: {date}

<b>交易统计:</b>
• 总交易: {total_trades}笔
• 胜: {wins}笔 | 负: {losses}笔
• 胜率: {win_rate:.1}%

{pnl_emoji} <b>盈亏:</b> {total_pnl:+.2} USDT
💰 <b>余额:</b> {balance:.2} USDT"#,
            date = chrono::Local::now().format("%Y-%m-%d"),
            total_trades = total_trades,
            wins = wins,
            losses = losses,
            win_rate = win_rate,
            pnl_emoji = pnl_emoji,
            total_pnl = total_pnl,
            balance = balance,
        );

        if let Err(e) = self.send_message(&text).await {
            warn!("[Telegram] 每日报告发送失败: {}", e);
        }
    }

    /// 发送启动通知
    pub async fn notify_startup(&self, symbol: &str, leverage: u32, balance: f64) {
        let text = format!(
            r#"🚀 <b>BTC Futures Sniper 启动</b>

📊 合约: {symbol}
⚡ 杠杆: {leverage}x
💰 初始余额: {balance:.2} USDT
🎮 模式: Dry Run

⏰ {time}"#,
            symbol = symbol,
            leverage = leverage,
            balance = balance,
            time = chrono::Local::now().format("%Y-%m-%d %H:%M:%S"),
        );

        if let Err(e) = self.send_message(&text).await {
            warn!("[Telegram] 启动通知发送失败: {}", e);
        }
    }
}
