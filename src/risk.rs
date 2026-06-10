// ─────────────────────────────────────────────────────────────
// 风险管理模块
// ─────────────────────────────────────────────────────────────

use crate::config::RiskConfig;
use tracing::{info, warn};

/// 持仓信息
#[derive(Debug, Clone)]
pub struct Position {
    pub entry_price: f64,
    pub quantity: f64,
    pub side: String,  // "LONG" or "SHORT"
    pub entry_time: i64,
    pub highest_price: f64,  // 最高价（用于移动止损）
    pub lowest_price: f64,   // 最低价（用于移动止损）
}

impl Position {
    pub fn new(entry_price: f64, quantity: f64, side: &str) -> Self {
        Self {
            entry_price,
            quantity,
            side: side.to_string(),
            entry_time: chrono::Utc::now().timestamp(),
            highest_price: entry_price,
            lowest_price: entry_price,
        }
    }

    /// 更新最高/最低价
    pub fn update_price(&mut self, current_price: f64) {
        if current_price > self.highest_price {
            self.highest_price = current_price;
        }
        if current_price < self.lowest_price {
            self.lowest_price = current_price;
        }
    }
}

/// 风险管理器
pub struct RiskManager {
    config: RiskConfig,
    position: Option<Position>,
}

impl RiskManager {
    pub fn new(config: RiskConfig) -> Self {
        Self {
            config,
            position: None,
        }
    }

    /// 开仓
    pub fn open_position(&mut self, entry_price: f64, quantity: f64, side: &str) {
        self.position = Some(Position::new(entry_price, quantity, side));
        info!("[风控] 开仓: {} {:.6} @ {:.2}", side, quantity, entry_price);
    }

    /// 平仓
    pub fn close_position(&mut self) {
        self.position = None;
        info!("[风控] 平仓");
    }

    /// 是否有持仓
    pub fn has_position(&self) -> bool {
        self.position.is_some()
    }

    /// 获取持仓
    pub fn get_position(&self) -> Option<&Position> {
        self.position.as_ref()
    }

    /// 计算止盈止损价格
    pub fn calculate_sl_tp(&self, entry_price: f64, side: &str) -> (f64, f64) {
        match side {
            "LONG" | "BUY" => {
                // 做多：止损在下，止盈在上
                let stop_loss = entry_price * (1.0 - self.config.stop_loss_pct);
                let take_profit = entry_price * (1.0 + self.config.take_profit_pct);
                (stop_loss, take_profit)
            }
            "SHORT" | "SELL" => {
                // 做空：止损在上，止盈在下
                let stop_loss = entry_price * (1.0 + self.config.stop_loss_pct);
                let take_profit = entry_price * (1.0 - self.config.take_profit_pct);
                (stop_loss, take_profit)
            }
            _ => (0.0, 0.0),
        }
    }

    /// 检查是否触发止损止盈
    /// 返回: (是否平仓, 平仓原因)
    pub fn check_stop_conditions(&mut self, current_price: f64) -> (bool, Option<String>) {
        let pos = match &mut self.position {
            Some(p) => p,
            None => return (false, None),
        };

        // 更新最高/最低价
        pos.update_price(current_price);

        let entry_time = pos.entry_time;
        let current_time = chrono::Utc::now().timestamp();
        let hold_time = current_time - entry_time;

        // 检查持仓时间
        if hold_time > self.config.max_hold_time_secs as i64 {
            return (true, Some("超时平仓".to_string()));
        }

        // 计算盈亏比例
        let pnl_pct = match pos.side.as_str() {
            "LONG" | "BUY" => (current_price - pos.entry_price) / pos.entry_price,
            "SHORT" | "SELL" => (pos.entry_price - current_price) / pos.entry_price,
            _ => 0.0,
        };

        // 止盈
        if pnl_pct >= self.config.take_profit_pct {
            return (true, Some("止盈".to_string()));
        }

        // 止损
        if pnl_pct <= -self.config.stop_loss_pct {
            return (true, Some("止损".to_string()));
        }

        // 移动止损
        if self.config.trailing_stop {
            let trailing_pct = self.config.trailing_stop_pct;
            match pos.side.as_str() {
                "LONG" | "BUY" => {
                    // 做多：从最高价回撤
                    let trailing_stop = pos.highest_price * (1.0 - trailing_pct);
                    if current_price <= trailing_stop && pnl_pct > 0.0 {
                        return (true, Some("移动止损".to_string()));
                    }
                }
                "SHORT" | "SELL" => {
                    // 做空：从最低价反弹
                    let trailing_stop = pos.lowest_price * (1.0 + trailing_pct);
                    if current_price >= trailing_stop && pnl_pct > 0.0 {
                        return (true, Some("移动止损".to_string()));
                    }
                }
                _ => {}
            }
        }

        (false, None)
    }

    /// 计算仓位大小
    pub fn calculate_position_size(&self, balance: f64, entry_price: f64, leverage: u32, position_size_pct: f64) -> f64 {
        // 仓位 = 余额 * 仓位比例 * 杠杆 / 入场价
        let position_value = balance * position_size_pct;
        let quantity = position_value * leverage as f64 / entry_price;

        // BTC最小下单量 0.001
        let min_qty = 0.001;
        if quantity < min_qty {
            warn!("[风控] 计算仓位过小: {:.6} < {:.6}, 使用最小值", quantity, min_qty);
            return min_qty;
        }

        quantity
    }
}
