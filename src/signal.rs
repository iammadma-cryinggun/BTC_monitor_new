// ─────────────────────────────────────────────────────────────
// 信号引擎 - 复用Polymarket逻辑
// ─────────────────────────────────────────────────────────────

use crate::config::SignalConfig;
use tracing::{info, warn};

/// 交易信号
#[derive(Debug, Clone, PartialEq)]
pub enum TradeSignal {
    Long,   // 做多
    Short,  // 做空
    None,
}

/// 信号引擎
pub struct SignalEngine {
    config: SignalConfig,
    last_obi: f64,
    decision_obi: Option<f64>,
}

impl SignalEngine {
    pub fn new(config: SignalConfig) -> Self {
        Self {
            config,
            last_obi: 0.0,
            decision_obi: None,
        }
    }

    /// 计算OBI (订单簿失衡度)
    /// OBI = (Bids_Vol - Asks_Vol) / (Bids_Vol + Asks_Vol)
    pub fn calculate_obi(bids_vol: f64, asks_vol: f64) -> f64 {
        if bids_vol + asks_vol == 0.0 {
            return 0.0;
        }
        (bids_vol - asks_vol) / (bids_vol + asks_vol)
    }

    /// 生成交易信号
    ///
    /// 复用Polymarket逻辑：
    /// - GOLD_BULL: OBI >= 0.40 → 做多
    /// - BEAR_NO: OBI <= -0.30 → 做空
    pub fn generate_signal(
        &mut self,
        obi: f64,
        price_diff: f64,  // BTC当前价 - 基准价
        time_remaining: f64,
    ) -> TradeSignal {
        self.last_obi = obi;
        let abs_diff = price_diff.abs();

        // ─────────────────────────────────────────────────────────────
        // 🟢 做多信号：OBI >= threshold
        // ─────────────────────────────────────────────────────────────
        if obi >= self.config.gold_bull_obi {
            if abs_diff >= self.config.diff_threshold {
                // 时间窗口检查
                if time_remaining >= self.config.time_min && time_remaining <= self.config.time_max {
                    // 记录决策时的OBI
                    self.decision_obi = Some(obi);

                    info!("🔥 [GOLD_BULL] 做多信号: OBI={:.3}, diff={:.1}, t={:.0}s",
                          obi, price_diff, time_remaining);
                    return TradeSignal::Long;
                }
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 🔴 做空信号：OBI <= -0.30
        // ─────────────────────────────────────────────────────────────
        if obi <= self.config.bear_no_obi {
            if abs_diff >= self.config.diff_threshold {
                // 时间窗口检查
                if time_remaining >= self.config.time_min && time_remaining <= self.config.time_max {
                    // 记录决策时的OBI
                    self.decision_obi = Some(obi);

                    info!("🔥 [BEAR_NO] 做空信号: OBI={:.3}, diff={:.1}, t={:.0}s",
                          obi, price_diff, time_remaining);
                    return TradeSignal::Short;
                }
            }
        }

        TradeSignal::None
    }

    /// OBI反转检查
    ///
    /// 防止决策→入场期间市场反转
    /// - 做多时：OBI必须仍>=0
    /// - 做空时：OBI必须仍<=0
    pub fn check_obi_reversal(&self, signal: &TradeSignal, current_obi: f64) -> bool {
        if !self.config.obi_reversal_check {
            return true; // 未启用，直接通过
        }

        match signal {
            TradeSignal::Long => {
                if current_obi < 0.0 {
                    warn!("🛑 [OBI反转] 做多信号但OBI={:.3}已转负，取消交易！", current_obi);
                    return false;
                }
                info!("✅ [OBI确认] 做多OBI={:.3} >= 0，买压确认", current_obi);
            }
            TradeSignal::Short => {
                if current_obi > 0.0 {
                    warn!("🛑 [OBI反转] 做空信号但OBI={:.3}已转正，取消交易！", current_obi);
                    return false;
                }
                info!("✅ [OBI确认] 做空OBI={:.3} <= 0，卖压确认", current_obi);
            }
            TradeSignal::None => {}
        }
        true
    }

    /// 获取决策时的OBI
    pub fn decision_obi(&self) -> Option<f64> {
        self.decision_obi
    }

    /// 重置决策OBI
    pub fn reset(&mut self) {
        self.decision_obi = None;
    }
}
