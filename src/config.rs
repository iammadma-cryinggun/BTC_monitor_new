use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Config {
    pub binance: BinanceConfig,
    pub trading: TradingConfig,
    pub telegram: TelegramConfig,
    pub risk: RiskConfig,
    pub signal: SignalConfig,
    pub database: DatabaseConfig,
    pub logging: LoggingConfig,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TelegramConfig {
    pub enabled: bool,
    pub bot_token: String,
    pub chat_id: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BinanceConfig {
    pub api_key: String,
    pub api_secret: String,
    pub testnet: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TradingConfig {
    pub symbol: String,
    pub leverage: u32,
    pub position_size_pct: f64,
    pub max_positions: u32,
    pub dry_run: bool,           // 模拟模式
    pub initial_balance: f64,    // 模拟初始资金
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct RiskConfig {
    pub stop_loss_pct: f64,
    pub take_profit_pct: f64,
    pub max_hold_time_secs: u64,
    pub trailing_stop: bool,
    pub trailing_stop_pct: f64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SignalConfig {
    pub gold_bull_obi: f64,
    pub bear_no_obi: f64,
    pub diff_threshold: f64,
    pub time_min: f64,
    pub time_max: f64,
    pub obi_reversal_check: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DatabaseConfig {
    pub path: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LoggingConfig {
    pub level: String,
}

impl Config {
    pub fn load(path: &str) -> Result<Self, Box<dyn std::error::Error>> {
        let content = std::fs::read_to_string(path)?;
        let config: Config = toml::from_str(&content)?;
        Ok(config)
    }

    pub fn default() -> Self {
        Self {
            binance: BinanceConfig {
                api_key: String::new(),
                api_secret: String::new(),
                testnet: true,
            },
            trading: TradingConfig {
                symbol: "BTCUSDT".to_string(),
                leverage: 10,
                position_size_pct: 0.05,
                max_positions: 1,
                dry_run: true,
                initial_balance: 1000.0,
            },
            telegram: TelegramConfig {
                enabled: false,
                bot_token: String::new(),
                chat_id: String::new(),
            },
            risk: RiskConfig {
                stop_loss_pct: 0.02,
                take_profit_pct: 0.01,
                max_hold_time_secs: 300,
                trailing_stop: true,
                trailing_stop_pct: 0.005,
            },
            signal: SignalConfig {
                gold_bull_obi: 0.40,
                bear_no_obi: -0.30,
                diff_threshold: 40.0,
                time_min: 10.0,
                time_max: 45.0,
                obi_reversal_check: true,
            },
            database: DatabaseConfig {
                path: "./data/trades.db".to_string(),
            },
            logging: LoggingConfig {
                level: "info".to_string(),
            },
        }
    }
}
