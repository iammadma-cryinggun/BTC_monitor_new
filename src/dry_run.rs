// ─────────────────────────────────────────────────────────────
// 模拟交易引擎 - Dry Run模式
// 使用真实市场数据，模拟下单，不实际发送API请求
// ─────────────────────────────────────────────────────────────

use crate::binance_ws::OrderBook;
use crate::config::Config;
use crate::database::Database;
use crate::report::TradeRecord;
use crate::risk::RiskManager;
use crate::signal::{SignalEngine, TradeSignal};
use crate::telegram::TelegramNotifier;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{info, warn};

/// 模拟持仓
#[derive(Debug, Clone)]
struct SimPosition {
    entry_price: f64,
    quantity: f64,
    side: String,
    entry_time: i64,
    highest_price: f64,
    lowest_price: f64,
}

/// 模拟交易引擎
pub struct DryRunTrader {
    config: Config,
    signal_engine: SignalEngine,
    risk_manager: RiskManager,
    orderbook: Arc<RwLock<OrderBook>>,
    db: Database,
    telegram: TelegramNotifier,
    is_running: bool,

    // 模拟状态
    balance: f64,
    position: Option<SimPosition>,
    total_trades: u32,
    wins: u32,
    losses: u32,
    total_pnl: f64,
}

impl DryRunTrader {
    pub fn new(
        config: Config,
        orderbook: Arc<RwLock<OrderBook>>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let signal_engine = SignalEngine::new(config.signal.clone());
        let risk_manager = RiskManager::new(config.risk.clone());
        let db = Database::new(&config.database.path)?;
        let balance = config.trading.initial_balance;
        let telegram = TelegramNotifier::new(
            config.telegram.enabled,
            &config.telegram.bot_token,
            &config.telegram.chat_id,
        );

        Ok(Self {
            config,
            signal_engine,
            risk_manager,
            orderbook,
            db,
            telegram,
            is_running: false,
            balance,
            position: None,
            total_trades: 0,
            wins: 0,
            losses: 0,
            total_pnl: 0.0,
        })
    }

    /// 初始化
    pub async fn init(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        info!("╔══════════════════════════════════════════════════════╗");
        info!("║       🎮 模拟交易引擎 (Dry Run Mode)                 ║");
        info!("╚══════════════════════════════════════════════════════╝");
        info!("");
        info!("📊 配置:");
        info!("  合约: {}", self.config.trading.symbol);
        info!("  杠杆: {}x", self.config.trading.leverage);
        info!("  模拟资金: {:.2} USDT", self.balance);
        info!("  止损: {:.1}%", self.config.risk.stop_loss_pct * 100.0);
        info!("  止盈: {:.1}%", self.config.risk.take_profit_pct * 100.0);
        info!("  OBI做多阈值: >= {:.2}", self.config.signal.gold_bull_obi);
        info!("  OBI做空阈值: <= {:.2}", self.config.signal.bear_no_obi);
        info!("");
        info!("⚠️  模拟模式：使用真实市场数据，不实际下单");
        info!("");

        // 发送Telegram启动通知
        self.telegram.notify_startup(
            &self.config.trading.symbol,
            self.config.trading.leverage,
            self.balance,
        ).await;

        Ok(())
    }

    /// 主循环
    pub async fn run(&mut self) {
        self.is_running = true;
        let mut interval = tokio::time::interval(tokio::time::Duration::from_millis(500));
        let mut last_print = std::time::Instant::now();

        info!("🚀 开始监控市场...");

        while self.is_running {
            interval.tick().await;

            // 获取当前价格
            let ob = self.orderbook.read().await;
            let current_price = (ob.best_bid + ob.best_ask) / 2.0;
            let obi = SignalEngine::calculate_obi(ob.bids_volume, ob.asks_volume);

            // 1. 如果有持仓，检查止损止盈
            if let Some(ref mut pos) = self.position {
                // 更新最高/最低价
                if current_price > pos.highest_price {
                    pos.highest_price = current_price;
                }
                if current_price < pos.lowest_price {
                    pos.lowest_price = current_price;
                }

                // 计算盈亏
                let pnl_pct = match pos.side.as_str() {
                    "LONG" => (current_price - pos.entry_price) / pos.entry_price,
                    "SHORT" => (pos.entry_price - current_price) / pos.entry_price,
                    _ => 0.0,
                };

                let hold_time = chrono::Utc::now().timestamp() - pos.entry_time;

                // 检查平仓条件
                let should_close = self.check_close_conditions(
                    pnl_pct, hold_time, current_price, pos
                );

                if let Some(reason) = should_close {
                    self.close_position(current_price, reason).await;
                }
            }

            // 2. 如果没有持仓，生成信号
            if self.position.is_none() {
                // 生成信号
                let diff = obi * 100.0;
                let time = 30.0;
                let signal = self.signal_engine.generate_signal(obi, diff, time);

                if signal != TradeSignal::None {
                    // OBI反转检查
                    if self.signal_engine.check_obi_reversal(&signal, obi) {
                        self.open_position(signal, current_price).await;
                    }
                }
            }

            // 每30秒打印一次状态
            if last_print.elapsed().as_secs() >= 30 {
                self.print_status(current_price, obi);
                last_print = std::time::Instant::now();
            }
        }
    }

    /// 检查平仓条件
    fn check_close_conditions(
        &self,
        pnl_pct: f64,
        hold_time: i64,
        current_price: f64,
        pos: &SimPosition,
    ) -> Option<String> {
        // 超时平仓
        if hold_time > self.config.risk.max_hold_time_secs as i64 {
            return Some("超时".to_string());
        }

        // 止盈
        if pnl_pct >= self.config.risk.take_profit_pct {
            return Some("止盈".to_string());
        }

        // 止损
        if pnl_pct <= -self.config.risk.stop_loss_pct {
            return Some("止损".to_string());
        }

        // 移动止损
        if self.config.risk.trailing_stop && pnl_pct > 0.0 {
            let trailing_pct = self.config.risk.trailing_stop_pct;
            match pos.side.as_str() {
                "LONG" => {
                    let trailing_stop = pos.highest_price * (1.0 - trailing_pct);
                    if current_price <= trailing_stop {
                        return Some("移动止损".to_string());
                    }
                }
                "SHORT" => {
                    let trailing_stop = pos.lowest_price * (1.0 + trailing_pct);
                    if current_price >= trailing_stop {
                        return Some("移动止损".to_string());
                    }
                }
                _ => {}
            }
        }

        None
    }

    /// 开仓（模拟）
    async fn open_position(&mut self, signal: TradeSignal, entry_price: f64) {
        let side = match signal {
            TradeSignal::Long => "LONG",
            TradeSignal::Short => "SHORT",
            _ => return,
        };

        // 计算仓位
        let position_value = self.balance * self.config.trading.position_size_pct;
        let quantity = position_value * self.config.trading.leverage as f64 / entry_price;

        // 模拟开仓
        self.position = Some(SimPosition {
            entry_price,
            quantity,
            side: side.to_string(),
            entry_time: chrono::Utc::now().timestamp(),
            highest_price: entry_price,
            lowest_price: entry_price,
        });

        let ob = self.orderbook.read().await;
        let obi = SignalEngine::calculate_obi(ob.bids_volume, ob.asks_volume);

        info!("");
        info!("╔══════════════════════════════════════════════════════╗");
        info!("║  🎯 模拟开仓                                         ║");
        info!("╠══════════════════════════════════════════════════════╣");
        info!("║  方向: {:6}  价格: ${:.2}                          ║", side, entry_price);
        info!("║  数量: {:.6} BTC                               ║", quantity);
        info!("║  OBI: {:+.3}                                        ║", obi);
        info!("║  保证金: {:.2} USDT                               ║", position_value);
        info!("╚══════════════════════════════════════════════════════╝");

        // 发送Telegram通知
        self.telegram.notify_open(side, entry_price, quantity, obi, self.balance).await;

        // 记录到数据库
        let record = TradeRecord {
            id: 0,
            timestamp: chrono::Utc::now(),
            symbol: self.config.trading.symbol.clone(),
            side: side.to_string(),
            entry_price,
            exit_price: None,
            quantity,
            pnl: None,
            obi,
            diff: obi * 100.0,
            hold_time: None,
            reason: format!("signal_{}", side),
            status: "open".to_string(),
        };

        let _ = self.db.save_trade(&record);
    }

    /// 平仓（模拟）
    async fn close_position(&mut self, exit_price: f64, reason: String) {
        let pos = match self.position.take() {
            Some(p) => p,
            None => return,
        };

        // 计算盈亏
        let pnl_pct = match pos.side.as_str() {
            "LONG" => (exit_price - pos.entry_price) / pos.entry_price,
            "SHORT" => (pos.entry_price - exit_price) / pos.entry_price,
            _ => 0.0,
        };

        // 杠杆收益
        let leverage_pnl_pct = pnl_pct * self.config.trading.leverage as f64;
        let position_value = self.balance * self.config.trading.position_size_pct;
        let pnl = position_value * leverage_pnl_pct;

        // 更新余额
        self.balance += pnl;
        self.total_pnl += pnl;
        self.total_trades += 1;
        if pnl > 0.0 {
            self.wins += 1;
        } else {
            self.losses += 1;
        }

        let hold_time = chrono::Utc::now().timestamp() - pos.entry_time;
        let win_rate = if self.total_trades > 0 {
            self.wins as f64 / self.total_trades as f64 * 100.0
        } else {
            0.0
        };

        info!("");
        info!("╔══════════════════════════════════════════════════════╗");
        info!("║  {} 模拟平仓 ({})                                    ║",
              if pnl >= 0.0 { "✅" } else { "❌" }, reason);
        info!("╠══════════════════════════════════════════════════════╣");
        info!("║  方向: {:6}  持仓: {}s                            ║", pos.side, hold_time);
        info!("║  入场: ${:.2}  出场: ${:.2}                      ║", pos.entry_price, exit_price);
        info!("║  收益: {:+.2}% ({}x杠杆)                        ║", pnl_pct * 100.0, self.config.trading.leverage);
        info!("║  盈亏: {:+.4} USDT                               ║", pnl);
        info!("╠──────────────────────────────────────────────────────╣");
        info!("║  累计: {}笔  胜率: {:.1}%  总盈亏: {:+.2} USDT   ║",
              self.total_trades, win_rate, self.total_pnl);
        info!("║  余额: {:.2} USDT                                 ║", self.balance);
        info!("╚══════════════════════════════════════════════════════╝");

        // 发送Telegram通知
        self.telegram.notify_close(
            &pos.side,
            pos.entry_price,
            exit_price,
            pnl,
            pnl_pct,
            hold_time,
            &reason,
            self.total_trades,
            win_rate,
            self.total_pnl,
            self.balance,
        ).await;

        // 更新数据库
        let _ = self.db.update_trade_result(self.total_trades as i64, exit_price, pnl, hold_time);
    }

    /// 打印状态
    fn print_status(&self, price: f64, obi: f64) {
        let win_rate = if self.total_trades > 0 {
            self.wins as f64 / self.total_trades as f64 * 100.0
        } else {
            0.0
        };

        info!("");
        info!("┌──────────────────────────────────────────────────────┐");
        info!("│  📊 市场状态                                          │");
        info!("├──────────────────────────────────────────────────────┤");
        info!("│  BTC: ${:.2}  OBI: {:+.3}                           │", price, obi);
        info!("│  持仓: {}                                            │",
              if self.position.is_some() { "有" } else { "无" });
        info!("├──────────────────────────────────────────────────────┤");
        info!("│  📈 统计                                              │");
        info!("│  交易: {}笔  胜率: {:.1}%  盈亏: {:+.2} USDT      │",
              self.total_trades, win_rate, self.total_pnl);
        info!("│  余额: {:.2} USDT                                    │", self.balance);
        info!("└──────────────────────────────────────────────────────┘");
    }

    /// 停止
    pub fn stop(&mut self) {
        self.is_running = false;

        // 打印最终统计
        let win_rate = if self.total_trades > 0 {
            self.wins as f64 / self.total_trades as f64 * 100.0
        } else {
            0.0
        };

        info!("");
        info!("╔══════════════════════════════════════════════════════╗");
        info!("║  📊 最终统计                                          ║");
        info!("╠══════════════════════════════════════════════════════╣");
        info!("║  总交易: {}笔                                         ║", self.total_trades);
        info!("║  胜: {}笔  负: {}笔  胜率: {:.1}%                 ║",
              self.wins, self.losses, win_rate);
        info!("║  总盈亏: {:+.2} USDT                                 ║", self.total_pnl);
        info!("║  最终余额: {:.2} USDT                                ║", self.balance);
        info!("╚══════════════════════════════════════════════════════╝");
    }
}
