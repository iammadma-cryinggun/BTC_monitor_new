// ─────────────────────────────────────────────────────────────
// Binance WebSocket - 订单簿数据流
// ─────────────────────────────────────────────────────────────

use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use std::sync::Arc;
use tokio::sync::RwLock;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{debug, error, info, warn};

/// Binance订单簿深度数据
#[derive(Debug, Clone, Deserialize)]
pub struct DepthUpdate {
    #[serde(rename = "e")]
    pub event_type: String,
    #[serde(rename = "E")]
    pub event_time: i64,
    #[serde(rename = "s")]
    pub symbol: String,
    #[serde(rename = "U")]
    pub first_update_id: i64,
    #[serde(rename = "u")]
    pub final_update_id: i64,
    #[serde(rename = "b")]
    pub bids: Vec<Vec<String>>,  // [price, qty]
    #[serde(rename = "a")]
    pub asks: Vec<Vec<String>>,  // [price, qty]
}

/// 订单簿状态
#[derive(Debug, Clone)]
pub struct OrderBook {
    pub best_bid: f64,
    pub best_ask: f64,
    pub bids_volume: f64,
    pub asks_volume: f64,
    pub last_update_time: i64,
}

impl Default for OrderBook {
    fn default() -> Self {
        Self {
            best_bid: 0.0,
            best_ask: f64::MAX,
            bids_volume: 0.0,
            asks_volume: 0.0,
            last_update_time: 0,
        }
    }
}

/// Binance WebSocket客户端
pub struct BinanceWsClient {
    orderbook: Arc<RwLock<OrderBook>>,
    symbol: String,
}

impl BinanceWsClient {
    pub fn new(symbol: &str) -> Self {
        Self {
            orderbook: Arc::new(RwLock::new(OrderBook::default())),
            symbol: symbol.to_uppercase(),
        }
    }

    /// 获取订单簿引用
    pub fn orderbook(&self) -> Arc<RwLock<OrderBook>> {
        self.orderbook.clone()
    }

    /// 启动WebSocket连接
    pub async fn connect(&self) -> Result<(), Box<dyn std::error::Error>> {
        let symbol_lower = self.symbol.to_lowercase();
        let url = format!(
            "wss://fstream.binance.com/ws/{}@depth@100ms",
            symbol_lower
        );

        info!("[Binance WS] 连接: {}", url);

        let (ws_stream, _) = connect_async(&url).await?;
        info!("[Binance WS] 连接成功");

        let (mut write, mut read) = ws_stream.split();
        let orderbook = self.orderbook.clone();

        // 心跳任务
        let heartbeat_orderbook = orderbook.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(tokio::time::Duration::from_secs(30));
            loop {
                interval.tick().await;
                if let Err(e) = write.send(Message::Ping(vec![])).await {
                    error!("[Binance WS] 心跳失败: {}", e);
                    break;
                }
                let ob = heartbeat_orderbook.read().await;
                debug!("[心跳] 订单簿: bid={:.2}, ask={:.2}, obi={:.3}",
                       ob.best_bid, ob.best_ask,
                       (ob.bids_volume - ob.asks_volume) / (ob.bids_volume + ob.asks_volume + 0.0001));
            }
        });

        // 消息处理
        tokio::spawn(async move {
            while let Some(msg) = read.next().await {
                match msg {
                    Ok(Message::Text(text)) => {
                        if let Ok(update) = serde_json::from_str::<DepthUpdate>(&text) {
                            Self::process_depth_update(&orderbook, update).await;
                        }
                    }
                    Ok(Message::Ping(data)) => {
                        debug!("[Binance WS] 收到Ping");
                    }
                    Ok(Message::Pong(_)) => {
                        debug!("[Binance WS] 收到Pong");
                    }
                    Err(e) => {
                        error!("[Binance WS] 错误: {}", e);
                        break;
                    }
                    _ => {}
                }
            }
            warn!("[Binance WS] 连接断开");
        });

        Ok(())
    }

    /// 处理深度更新
    async fn process_depth_update(orderbook: &Arc<RwLock<OrderBook>>, update: DepthUpdate) {
        let mut ob = orderbook.write().await;

        // 计算前5档的买卖量
        let mut bids_vol = 0.0;
        let mut asks_vol = 0.0;
        let mut best_bid = 0.0;
        let mut best_ask = f64::MAX;

        // 买单
        for bid in update.bids.iter().take(5) {
            if bid.len() >= 2 {
                if let (Ok(price), Ok(qty)) = (bid[0].parse::<f64>(), bid[1].parse::<f64>()) {
                    bids_vol += qty;
                    if price > best_bid {
                        best_bid = price;
                    }
                }
            }
        }

        // 卖单
        for ask in update.asks.iter().take(5) {
            if ask.len() >= 2 {
                if let (Ok(price), Ok(qty)) = (ask[0].parse::<f64>(), ask[1].parse::<f64>()) {
                    asks_vol += qty;
                    if price < best_ask {
                        best_ask = price;
                    }
                }
            }
        }

        ob.bids_volume = bids_vol;
        ob.asks_volume = asks_vol;
        ob.best_bid = best_bid;
        ob.best_ask = best_ask;
        ob.last_update_time = update.event_time;
    }
}
