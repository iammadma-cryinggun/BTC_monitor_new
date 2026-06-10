// ─────────────────────────────────────────────────────────────
// BTC Futures Sniper - 主入口
// ─────────────────────────────────────────────────────────────

mod binance_rest;
mod binance_ws;
mod config;
mod database;
mod dry_run;
mod report;
mod risk;
mod signal;
mod trader;

use binance_ws::BinanceWsClient;
use config::Config;
use database::Database;
use dry_run::DryRunTrader;
use report::{Reporter, TradeStats};
use std::env;
use tracing::info;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 解析命令行参数
    let args: Vec<String> = env::args().collect();

    // 命令行工具
    if args.len() > 1 {
        match args[1].as_str() {
            "report" | "stats" => {
                return run_report();
            }
            "trades" => {
                return run_list_trades(args.get(2).and_then(|s| s.parse().ok()).unwrap_or(10));
            }
            "help" | "-h" | "--help" => {
                print_help();
                return Ok(());
            }
            _ => {}
        }
    }

    // 初始化日志
    tracing_subscriber::fmt()
        .with_max_level(tracing::Level::INFO)
        .init();

    info!("🚀 BTC Futures Sniper 启动");

    // 加载配置
    let config = Config::load("config.toml")
        .unwrap_or_else(|_| {
            info!("使用默认配置");
            Config::default()
        });

    // 创建WebSocket客户端
    let ws_client = BinanceWsClient::new(&config.trading.symbol);
    let orderbook = ws_client.orderbook();

    // 连接WebSocket
    ws_client.connect().await?;

    // 等待数据初始化
    tokio::time::sleep(tokio::time::Duration::from_secs(2)).await;

    // 根据模式选择引擎
    if config.trading.dry_run {
        // 模拟模式
        let mut trader = DryRunTrader::new(config, orderbook)?;
        trader.init().await?;
        trader.run().await;
    } else {
        // 实盘模式
        use trader::FuturesTrader;

        let mut trader = FuturesTrader::new(config, orderbook)?;
        trader.init().await?;
        trader.run().await;
    }

    Ok(())
}

/// 打印帮助信息
fn print_help() {
    println!("");
    println!("BTC Futures Sniper - 基于OBI的币安合约短线交易系统");
    println!("");
    println!("用法:");
    println!("  btc_futures_sniper          启动交易引擎");
    println!("  btc_futures_sniper report   查看统计报告");
    println!("  btc_futures_sniper trades [N]  查看最近N笔交易 (默认10)");
    println!("  btc_futures_sniper help     显示帮助");
    println!("");
    println!("配置文件: config.toml");
    println!("数据库: data/trades.db");
    println!("");
}

/// 运行报告
fn run_report() -> Result<(), Box<dyn std::error::Error>> {
    // 初始化日志
    tracing_subscriber::fmt()
        .with_max_level(tracing::Level::INFO)
        .init();

    let reporter = Reporter::new("./data/trades.db")?;
    let stats = reporter.generate_report()?;

    // 额外输出
    println!("");
    println!("📈 策略评估:");
    if stats.win_rate >= 90.0 {
        println!("  ✅ 胜率优秀 (>=90%)");
    } else if stats.win_rate >= 80.0 {
        println!("  ✅ 胜率良好 (>=80%)");
    } else if stats.win_rate >= 60.0 {
        println!("  ⚠️  胜率一般 (>=60%)");
    } else {
        println!("  ❌ 胜率偏低 (<60%)");
    }

    if stats.profit_factor >= 2.0 {
        println!("  ✅ 盈亏比优秀 (>=2.0)");
    } else if stats.profit_factor >= 1.5 {
        println!("  ✅ 盈亏比良好 (>=1.5)");
    } else if stats.profit_factor >= 1.0 {
        println!("  ⚠️  盈亏比一般 (>=1.0)");
    } else {
        println!("  ❌ 盈亏比亏损 (<1.0)");
    }

    if stats.sharpe_ratio >= 2.0 {
        println!("  ✅ Sharpe优秀 (>=2.0)");
    } else if stats.sharpe_ratio >= 1.0 {
        println!("  ✅ Sharpe良好 (>=1.0)");
    } else if stats.sharpe_ratio >= 0.0 {
        println!("  ⚠️  Sharpe一般 (>=0)");
    } else {
        println!("  ❌ Sharpe负值 (<0)");
    }

    println!("");
    Ok(())
}

/// 列出最近交易
fn run_list_trades(limit: usize) -> Result<(), Box<dyn std::error::Error>> {
    let db = Database::new("./data/trades.db")?;
    let trades = db.get_recent_trades(limit)?;

    println!("");
    println!("╔══════════════════════════════════════════════════════════════════════════╗");
    println!("║  最近 {} 笔交易                                                            ║", limit);
    println!("╠══════════════════════════════════════════════════════════════════════════╣");

    for t in trades {
        let pnl_str = match t.pnl {
            Some(p) => format!("{:+.2}", p),
            None => "开仓中".to_string(),
        };
        let status = if t.pnl.is_some() { "已平仓" } else { "持仓中" };

        println!("║  ID:{} {:6} @ ${:.2}  PnL: {:>10}  [{}]",
                 t.id, t.side, t.entry_price, pnl_str, status);
    }

    println!("╚══════════════════════════════════════════════════════════════════════════╝");
    println!("");
    Ok(())
}
