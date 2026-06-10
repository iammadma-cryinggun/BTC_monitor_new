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
        // 先尝试从文件加载
        if std::path::Path::new(path).exists() {
            let content = std::fs::read_to_string(path)?;
            let mut config: Config = toml::from_str(&content)?;
            // 用环境变量覆盖
            config.apply_env_overrides();
            Ok(config)
        } else {
            // 没有配置文件，使用默认值+环境变量
            let mut config = Self::default();
            config.apply_env_overrides();
            Ok(config)
        }
    }

    /// 从环境变量覆盖配置
    fn apply_env_overrides(&mut self) {
        // Telegram
        if let Ok(token) = std::env::var("TELEGRAM_BOT_TOKEN") {
            self.telegram.bot_token = token;
        }
        if let Ok(chat_id) = std::env::var("TELEGRAM_CHAT_ID") {
            self.telegram.chat_id = chat_id;
        }
        if let Ok(enabled) = std::env::var("TELEGRAM_ENABLED") {
            self.telegram.enabled = enabled == "true";
        }

        // Trading
        if let Ok(dry_run) = std::env::var("DRY_RUN") {
            self.trading.dry_run = dry_run == "true";
        }
        if let Ok(balance) = std::env::var("INITIAL_BALANCE") {
            if let Ok(b) = balance.parse() {
                self.trading.initial_balance = b;
            }
        }
        if let Ok(leverage) = std::env::var("LEVERAGE") {
            if let Ok(l) = leverage.parse() {
                self.trading.leverage = l;
            }
        }

        // Binance
        if let Ok(key) = std::env::var("BINANCE_API_KEY") {
            self.binance.api_key = key;
        }
        if let Ok(secret) = std::env::var("BINANCE_API_SECRET") {
            self.binance.api_secret = secret;
        }

        // Database
        if let Ok(db_path) = std::env::var("DATABASE_PATH") {
            self.database.path = db_path;
        }
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
                max_hold_time_secs: 0,  // 0 = 无限制
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
