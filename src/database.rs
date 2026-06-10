// ─────────────────────────────────────────────────────────────
// 数据库模块 - 记录交易
// ─────────────────────────────────────────────────────────────

use rusqlite::{Connection, Result};
use chrono::{DateTime, Utc};
use crate::report::TradeRecord;

pub struct Database {
    conn: Connection,
}

impl Database {
    pub fn new(path: &str) -> Result<Self> {
        // 确保目录存在
        if let Some(parent) = std::path::Path::new(path).parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let conn = Connection::open(path)?;
        let db = Self { conn };
        db.init()?;
        Ok(db)
    }

    fn init(&self) -> Result<()> {
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity REAL NOT NULL,
                pnl REAL,
                obi REAL NOT NULL,
                diff REAL NOT NULL,
                hold_time INTEGER,
                reason TEXT NOT NULL,
                status TEXT NOT NULL
            )",
            [],
        )?;
        Ok(())
    }

    /// 保存交易记录
    pub fn save_trade(&self, record: &TradeRecord) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO trades (
                timestamp, symbol, side, entry_price, exit_price,
                quantity, pnl, obi, diff, hold_time, reason, status
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            rusqlite::params![
                record.timestamp.to_rfc3339(),
                record.symbol,
                record.side,
                record.entry_price,
                record.exit_price,
                record.quantity,
                record.pnl,
                record.obi,
                record.diff,
                record.hold_time,
                record.reason,
                record.status,
            ],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    /// 更新交易结果
    pub fn update_trade_result(&self, id: i64, exit_price: f64, pnl: f64, hold_time: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE trades SET
                exit_price = ?1,
                pnl = ?2,
                hold_time = ?3,
                status = 'closed'
            WHERE id = ?4",
            rusqlite::params![exit_price, pnl, hold_time, id],
        )?;
        Ok(())
    }

    /// 获取所有交易
    pub fn get_all_trades(&self) -> Result<Vec<TradeRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, timestamp, symbol, side, entry_price, exit_price,
                    quantity, pnl, obi, diff, hold_time, reason, status
             FROM trades ORDER BY timestamp DESC"
        )?;

        let records = stmt.query_map([], |row| {
            Ok(TradeRecord {
                id: row.get(0)?,
                timestamp: DateTime::parse_from_rfc3339(&row.get::<_, String>(1)?)
                    .map(|dt| dt.with_timezone(&Utc))
                    .unwrap_or_else(|_| Utc::now()),
                symbol: row.get(2)?,
                side: row.get(3)?,
                entry_price: row.get(4)?,
                exit_price: row.get(5)?,
                quantity: row.get(6)?,
                pnl: row.get(7)?,
                obi: row.get(8)?,
                diff: row.get(9)?,
                hold_time: row.get(10)?,
                reason: row.get(11)?,
                status: row.get(12)?,
            })
        })?.collect::<Result<Vec<_>>>()?;

        Ok(records)
    }

    /// 按时间范围获取交易
    pub fn get_trades_by_range(&self, start: DateTime<Utc>, end: DateTime<Utc>) -> Result<Vec<TradeRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, timestamp, symbol, side, entry_price, exit_price,
                    quantity, pnl, obi, diff, hold_time, reason, status
             FROM trades
             WHERE timestamp >= ?1 AND timestamp <= ?2
             ORDER BY timestamp DESC"
        )?;

        let records = stmt.query_map(
            rusqlite::params![start.to_rfc3339(), end.to_rfc3339()],
            |row| {
                Ok(TradeRecord {
                    id: row.get(0)?,
                    timestamp: DateTime::parse_from_rfc3339(&row.get::<_, String>(1)?)
                        .map(|dt| dt.with_timezone(&Utc))
                        .unwrap_or_else(|_| Utc::now()),
                    symbol: row.get(2)?,
                    side: row.get(3)?,
                    entry_price: row.get(4)?,
                    exit_price: row.get(5)?,
                    quantity: row.get(6)?,
                    pnl: row.get(7)?,
                    obi: row.get(8)?,
                    diff: row.get(9)?,
                    hold_time: row.get(10)?,
                    reason: row.get(11)?,
                    status: row.get(12)?,
                })
            }
        )?.collect::<Result<Vec<_>>>()?;

        Ok(records)
    }

    /// 获取最近N笔交易
    pub fn get_recent_trades(&self, limit: usize) -> Result<Vec<TradeRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, timestamp, symbol, side, entry_price, exit_price,
                    quantity, pnl, obi, diff, hold_time, reason, status
             FROM trades ORDER BY id DESC LIMIT ?1"
        )?;

        let records = stmt.query_map(
            rusqlite::params![limit as i64],
            |row| {
                Ok(TradeRecord {
                    id: row.get(0)?,
                    timestamp: DateTime::parse_from_rfc3339(&row.get::<_, String>(1)?)
                        .map(|dt| dt.with_timezone(&Utc))
                        .unwrap_or_else(|_| Utc::now()),
                    symbol: row.get(2)?,
                    side: row.get(3)?,
                    entry_price: row.get(4)?,
                    exit_price: row.get(5)?,
                    quantity: row.get(6)?,
                    pnl: row.get(7)?,
                    obi: row.get(8)?,
                    diff: row.get(9)?,
                    hold_time: row.get(10)?,
                    reason: row.get(11)?,
                    status: row.get(12)?,
                })
            }
        )?.collect::<Result<Vec<_>>>()?;

        Ok(records)
    }
}