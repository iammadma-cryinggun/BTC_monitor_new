// ─────────────────────────────────────────────────────────────
// 交易引擎
// ─────────────────────────────────────────────────────────────

use crate::binance_rest::BinanceRestClient;
use crate::binance_ws::OrderBook;
use crate::config::Config;
use crate::database::{Database, TradeRecord};
use crate::risk::RiskManager;
use crate::signal::{SignalEngine, TradeSignal};
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{error, info, warn};

pub struct FuturesTrader {
    config: Config,
    signal_engine: SignalEngine,
    risk_manager: RiskManager,
    rest_client: BinanceRestClient,
    orderbook: Arc<RwLock<OrderBook>>,
    db: Database,
    is_running: bool,
}

impl FuturesTrader {
    pub fn new(
        config: Config,
        orderbook: Arc<RwLock<OrderBook>>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let signal_engine = SignalEngine::new(config.signal.clone());
        let risk_manager = RiskManager::new(config.risk.clone());
        let rest_client = BinanceRestClient::new(&config.binance);
        let db = Database::new(&config.database.path)?;

        Ok(Self {
            config,
            signal_engine,
            risk_manager,
            rest_client,
            orderbook,
            db,
            is_running: false,
        })
    }

    /// 初始化
    pub async fn init(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        // 设置杠杆
        self.rest_client
            .set_leverage(&self.config.trading.symbol, self.config.trading.leverage)
            .await?;

        info!("交易引擎初始化完成");
        info!("  合约: {}", self.config.trading.symbol);
        info!("  杠杆: {}x", self.config.trading.leverage);
        info!("  止损: {:.1}%", self.config.risk.stop_loss_pct * 100.0);
        info!("  止盈: {:.1}%", self.config.risk.take_profit_pct * 100.0);
        info!("  OBI做多阈值: >= {:.2}", self.config.signal.gold_bull_obi);
        info!("  OBI做空阈值: <= {:.2}", self.config.signal.bear_no_obi);

        Ok(())
    }

    /// 主循环
    pub async fn run(&mut self) {
        self.is_running = true;
        let mut interval = tokio::time::interval(tokio::time::Duration::from_millis(500));

        info!("🚀 交易引擎启动");

        while self.is_running {
            interval.tick().await;

            // 1. 如果有持仓，检查止损止盈
            if self.risk_manager.has_position() {
                let current_price = {
                    let ob = self.orderbook.read().await;
                    (ob.best_bid + ob.best_ask) / 2.0
                }; // ob在这里被释放

                let (should_close, reason) = self.risk_manager.check_stop_conditions(current_price);
                if should_close {
                    self.close_position(current_price, reason).await;
                    continue;
                }
            }

            // 2. 如果没有持仓，生成信号
            if !self.risk_manager.has_position() {
                let (current_price, obi) = {
                    let ob = self.orderbook.read().await;
                    let obi = SignalEngine::calculate_obi(ob.bids_volume, ob.asks_volume);
                    let price = (ob.best_bid + ob.best_ask) / 2.0;
                    (price, obi)
                }; // ob在这里被释放

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
        }
    }

    /// 开仓
    async fn open_position(&mut self, signal: TradeSignal, entry_price: f64) {
        let side = match signal {
            TradeSignal::Long => "BUY",
            TradeSignal::Short => "SELL",
            _ => return,
        };

        // 获取余额
        let balance = match self.rest_client.get_balance().await {
            Ok(b) => b,
            Err(e) => {
                error!("[交易] 获取余额失败: {}", e);
                return;
            }
        };

        if balance < 10.0 {
            warn!("[交易] 余额不足: {:.2} USDT", balance);
            return;
        }

        // 计算仓位
        let quantity = self.risk_manager.calculate_position_size(
            balance,
            entry_price,
            self.config.trading.leverage,
            self.config.trading.position_size_pct,
        );

        // 开仓
        match self.rest_client
            .open_position(&self.config.trading.symbol, side, quantity)
            .await
        {
            Ok(result) => {
                info!("[交易] 开仓成功: {} {} {:.6} @ {:.2}",
                      side, self.config.trading.symbol, quantity, entry_price);

                // 设置止盈止损
                let (sl, tp) = self.risk_manager.calculate_sl_tp(entry_price, side);
                let _ = self.rest_client
                    .set_stop_loss_take_profit(&self.config.trading.symbol, side, sl, tp, quantity)
                    .await;

                // 更新风控
                self.risk_manager.open_position(entry_price, quantity, side);

                // 记录交易
                let ob = self.orderbook.read().await;
                let obi = SignalEngine::calculate_obi(ob.bids_volume, ob.asks_volume);

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

                if let Err(e) = self.db.save_trade(&record) {
                    error!("[交易] 保存记录失败: {}", e);
                }
            }
            Err(e) => {
                error!("[交易] 开仓失败: {}", e);
            }
        }
    }

    /// 平仓
    async fn close_position(&mut self, exit_price: f64, reason: Option<String>) {
        let pos = match self.risk_manager.get_position() {
            Some(p) => p.clone(),
            None => return,
        };

        let close_side = if pos.side == "LONG" || pos.side == "BUY" { "BUY" } else { "SELL" };

        match self.rest_client
            .close_position(&self.config.trading.symbol, close_side, pos.quantity)
            .await
        {
            Ok(_) => {
                // 计算盈亏
                let pnl = match pos.side.as_str() {
                    "LONG" | "BUY" => (exit_price - pos.entry_price) * pos.quantity,
                    "SHORT" | "SELL" => (pos.entry_price - exit_price) * pos.quantity,
                    _ => 0.0,
                };

                let hold_time = chrono::Utc::now().timestamp() - pos.entry_time;

                info!("[交易] 平仓成功: {} PnL={:.4} USDT, 持仓{}s, 原因:{}",
                      pos.side, pnl, hold_time, reason.as_ref().unwrap_or(&"未知".to_string()));

                // 更新风控
                self.risk_manager.close_position();

                // 更新数据库
                let _ = self.db.update_trade_result(1, exit_price, pnl, hold_time);
            }
            Err(e) => {
                error!("[交易] 平仓失败: {}", e);
            }
        }
    }

    /// 停止
    pub fn stop(&mut self) {
        self.is_running = false;
        info!("交易引擎停止");
    }
}
