// ─────────────────────────────────────────────────────────────
// 统计报告模块
// ─────────────────────────────────────────────────────────────

use crate::database::Database;
use chrono::{DateTime, Utc};
use tracing::info;

/// 交易统计
#[derive(Debug, Clone)]
pub struct TradeStats {
    pub total_trades: u32,
    pub wins: u32,
    pub losses: u32,
    pub total_pnl: f64,
    pub avg_pnl: f64,
    pub avg_hold_time: f64,
    pub win_rate: f64,
    pub profit_factor: f64,
    pub max_drawdown: f64,
    pub sharpe_ratio: f64,
}

impl TradeStats {
    pub fn new() -> Self {
        Self {
            total_trades: 0,
            wins: 0,
            losses: 0,
            total_pnl: 0.0,
            avg_pnl: 0.0,
            avg_hold_time: 0.0,
            win_rate: 0.0,
            profit_factor: 0.0,
            max_drawdown: 0.0,
            sharpe_ratio: 0.0,
        }
    }

    /// 从交易记录计算统计
    pub fn from_records(records: &[TradeRecord]) -> Self {
        if records.is_empty() {
            return Self::new();
        }

        let total_trades = records.len() as u32;
        let wins = records.iter().filter(|r| r.pnl.map(|p| p > 0.0).unwrap_or(false)).count() as u32;
        let losses = total_trades - wins;
        let total_pnl: f64 = records.iter().filter_map(|r| r.pnl).sum();
        let avg_pnl = total_pnl / total_trades as f64;

        let hold_times: Vec<i64> = records.iter().filter_map(|r| r.hold_time).collect();
        let avg_hold_time = if !hold_times.is_empty() {
            hold_times.iter().sum::<i64>() as f64 / hold_times.len() as f64
        } else {
            0.0
        };

        let win_rate = wins as f64 / total_trades as f64 * 100.0;

        // 盈亏比
        let total_profit: f64 = records.iter()
            .filter_map(|r| r.pnl.filter(|&p| p > 0.0))
            .sum();
        let total_loss: f64 = records.iter()
            .filter_map(|r| r.pnl.filter(|&p| p < 0.0).map(|p| p.abs()))
            .sum();
        let profit_factor = if total_loss > 0.0 {
            total_profit / total_loss
        } else {
            f64::INFINITY
        };

        // 最大回撤
        let mut max_drawdown = 0.0;
        let mut peak = 0.0;
        let mut cumulative = 0.0;
        for r in records {
            if let Some(pnl) = r.pnl {
                cumulative += pnl;
                if cumulative > peak {
                    peak = cumulative;
                }
                let drawdown = peak - cumulative;
                if drawdown > max_drawdown {
                    max_drawdown = drawdown;
                }
            }
        }

        // Sharpe Ratio (简化版)
        let pnls: Vec<f64> = records.iter().filter_map(|r| r.pnl).collect();
        let sharpe_ratio = if pnls.len() > 1 {
            let mean = pnls.iter().sum::<f64>() / pnls.len() as f64;
            let variance: f64 = pnls.iter().map(|p| (p - mean).powi(2)).sum::<f64>() / pnls.len() as f64;
            let std = variance.sqrt();
            if std > 0.0 {
                mean / std * (252.0_f64).sqrt() // 年化
            } else {
                0.0
            }
        } else {
            0.0
        };

        Self {
            total_trades,
            wins,
            losses,
            total_pnl,
            avg_pnl,
            avg_hold_time,
            win_rate,
            profit_factor,
            max_drawdown,
            sharpe_ratio,
        }
    }

    /// 打印报告
    pub fn print_report(&self) {
        info!("");
        info!("╔══════════════════════════════════════════════════════════════╗");
        info!("║                    📊 交易统计报告                            ║");
        info!("╠══════════════════════════════════════════════════════════════╣");
        info!("║  总交易数: {:>6}                                            ║", self.total_trades);
        info!("║  胜: {:>6}  负: {:>6}  胜率: {:>5.1}%                     ║",
              self.wins, self.losses, self.win_rate);
        info!("╠──────────────────────────────────────────────────────────────╣");
        info!("║  总盈亏: {:+>10.2} USDT                                   ║", self.total_pnl);
        info!("║  平均盈亏: {:+>10.4} USDT                                 ║", self.avg_pnl);
        info!("║  平均持仓: {:>8.1} 秒                                     ║", self.avg_hold_time);
        info!("╠──────────────────────────────────────────────────────────────╣");
        info!("║  盈亏比: {:>8.2}                                          ║", self.profit_factor);
        info!("║  最大回撤: {:>8.2} USDT                                   ║", self.max_drawdown);
        info!("║  Sharpe比率: {:>8.2}                                      ║", self.sharpe_ratio);
        info!("╚══════════════════════════════════════════════════════════════╝");
    }
}

/// 交易记录（用于统计）
#[derive(Debug, Clone)]
pub struct TradeRecord {
    pub id: i64,
    pub timestamp: DateTime<Utc>,
    pub symbol: String,
    pub side: String,
    pub entry_price: f64,
    pub exit_price: Option<f64>,
    pub quantity: f64,
    pub pnl: Option<f64>,
    pub obi: f64,
    pub diff: f64,
    pub hold_time: Option<i64>,
    pub reason: String,
    pub status: String,
}

/// 报告生成器
pub struct Reporter {
    db: Database,
}

impl Reporter {
    pub fn new(db_path: &str) -> Result<Self, Box<dyn std::error::Error>> {
        let db = Database::new(db_path)?;
        Ok(Self { db })
    }

    /// 生成报告
    pub fn generate_report(&self) -> Result<TradeStats, Box<dyn std::error::Error>> {
        let records = self.db.get_all_trades()?;
        let stats = TradeStats::from_records(&records);
        stats.print_report();
        Ok(stats)
    }

    /// 按日期范围生成报告
    pub fn generate_report_range(
        &self,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> Result<TradeStats, Box<dyn std::error::Error>> {
        let records = self.db.get_trades_by_range(start, end)?;
        let stats = TradeStats::from_records(&records);
        stats.print_report();
        Ok(stats)
    }
}
