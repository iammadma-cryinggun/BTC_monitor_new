// ─────────────────────────────────────────────────────────────
// Binance WebSocket - 订单簿数据流
// 正确实现：先获取REST快照，再应用WebSocket增量更新
// ─────────────────────────────────────────────────────────────

use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{debug, error, info, warn};

/// Binance订单簿深度数据（增量更新）
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

/// REST API 快照响应
#[derive(Debug, Clone, Deserialize)]
pub struct DepthSnapshot {
    pub lastUpdateId: i64,
    pub bids: Vec<Vec<String>>,
    pub asks: Vec<Vec<String>>,
}

/// 订单簿状态
#[derive(Debug, Clone)]
pub struct OrderBook {
    pub best_bid: f64,
    pub best_ask: f64,
    pub bids_volume: f64,
    pub asks_volume: f64,
    pub last_update_time: i64,
    pub is_valid: bool,
}

impl Default for OrderBook {
    fn default() -> Self {
        Self {
            best_bid: 0.0,
            best_ask: 0.0,
            bids_volume: 0.0,
            asks_volume: 0.0,
            last_update_time: 0,
            is_valid: false,
        }
    }
}

/// 内部订单簿（使用BTreeMap维护价格排序）
struct InternalOrderBook {
    bids: BTreeMap<String, f64>,  // price -> quantity (降序需要反转)
    asks: BTreeMap<String, f64>,  // price -> quantity (升序)
    last_update_id: i64,
}

/// Binance WebSocket客户端
pub struct BinanceWsClient {
    orderbook: Arc<RwLock<OrderBook>>,
    internal: Arc<RwLock<InternalOrderBook>>,
    symbol: String,
}

impl BinanceWsClient {
    pub fn new(symbol: &str) -> Self {
        Self {
            orderbook: Arc::new(RwLock::new(OrderBook::default())),
            internal: Arc::new(RwLock::new(InternalOrderBook {
                bids: BTreeMap::new(),
                asks: BTreeMap::new(),
                last_update_id: 0,
            })),
            symbol: symbol.to_uppercase(),
        }
    }

    /// 获取订单簿引用
    pub fn orderbook(&self) -> Arc<RwLock<OrderBook>> {
        self.orderbook.clone()
    }

    /// 获取REST快照
    async fn fetch_snapshot(&self) -> Result<DepthSnapshot, Box<dyn std::error::Error>> {
        let url = format!(
            "https://fapi.binance.com/fapi/v1/depth?symbol={}&limit=100",
            self.symbol
        );

        info!("[Binance REST] 获取订单簿快照: {}", url);

        let response = reqwest::get(&url).await?;
        let snapshot: DepthSnapshot = response.json().await?;

        info!("[Binance REST] 快照获取成功, lastUpdateId: {}", snapshot.lastUpdateId);

        Ok(snapshot)
    }

    /// 启动WebSocket连接
    pub async fn connect(&self) -> Result<(), Box<dyn std::error::Error>> {
        // 1. 先获取REST快照
        let snapshot = self.fetch_snapshot().await?;

        // 2. 初始化内部订单簿
        {
            let mut internal = self.internal.write().await;
            internal.last_update_id = snapshot.lastUpdateId;

            for bid in snapshot.bids {
                if bid.len() >= 2 {
                    let price = bid[0].clone();
                    let qty: f64 = bid[1].parse().unwrap_or(0.0);
                    if qty > 0.0 {
                        internal.bids.insert(price, qty);
                    }
                }
            }

            for ask in snapshot.asks {
                if ask.len() >= 2 {
                    let price = ask[0].clone();
                    let qty: f64 = ask[1].parse().unwrap_or(0.0);
                    if qty > 0.0 {
                        internal.asks.insert(price, qty);
                    }
                }
            }

            info!("[Binance] 订单簿初始化: {} bids, {} asks",
                  internal.bids.len(), internal.asks.len());
        }

        // 3. 连接WebSocket
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
        let internal = self.internal.clone();

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
                if ob.is_valid {
                    debug!("[心跳] 订单簿: bid={:.2}, ask={:.2}, obi={:.3}",
                           ob.best_bid, ob.best_ask,
                           (ob.bids_volume - ob.asks_volume) / (ob.bids_volume + ob.asks_volume + 0.0001));
                }
            }
        });

        // 消息处理
        tokio::spawn(async move {
            while let Some(msg) = read.next().await {
                match msg {
                    Ok(Message::Text(text)) => {
                        if let Ok(update) = serde_json::from_str::<DepthUpdate>(&text) {
                            Self::process_depth_update(&orderbook, &internal, update).await;
                        }
                    }
                    Ok(Message::Ping(_)) => {
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

        // 等待数据初始化
        tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;

        Ok(())
    }

    /// 处理深度更新
    async fn process_depth_update(
        orderbook: &Arc<RwLock<OrderBook>>,
        internal: &Arc<RwLock<InternalOrderBook>>,
        update: DepthUpdate,
    ) {
        let mut internal_guard = internal.write().await;

        // 检查更新ID是否有效（丢弃过期更新）
        if update.final_update_id <= internal_guard.last_update_id {
            return;
        }

        // 应用增量更新
        for bid in &update.bids {
            if bid.len() >= 2 {
                let price = bid[0].clone();
                let qty: f64 = bid[1].parse().unwrap_or(0.0);
                if qty == 0.0 {
                    internal_guard.bids.remove(&price);
                } else {
                    internal_guard.bids.insert(price, qty);
                }
            }
        }

        for ask in &update.asks {
            if ask.len() >= 2 {
                let price = ask[0].clone();
                let qty: f64 = ask[1].parse().unwrap_or(0.0);
                if qty == 0.0 {
                    internal_guard.asks.remove(&price);
                } else {
                    internal_guard.asks.insert(price, qty);
                }
            }
        }

        internal_guard.last_update_id = update.final_update_id;

        // 计算前5档汇总
        let mut bids_vol = 0.0;
        let mut asks_vol = 0.0;
        let mut best_bid = 0.0;
        let mut best_ask = f64::MAX;

        // BTreeMap是升序，bids需要降序（取最后5个）
        for (price_str, qty) in internal_guard.bids.iter().rev().take(5) {
            if let Ok(price) = price_str.parse::<f64>() {
                bids_vol += qty;
                if price > best_bid {
                    best_bid = price;
                }
            }
        }

        // asks是升序（取前5个）
        for (price_str, qty) in internal_guard.asks.iter().take(5) {
            if let Ok(price) = price_str.parse::<f64>() {
                asks_vol += qty;
                if price < best_ask {
                    best_ask = price;
                }
            }
        }

        // 更新公开订单簿
        let mut ob = orderbook.write().await;
        ob.best_bid = best_bid;
        ob.best_ask = best_ask;
        ob.bids_volume = bids_vol;
        ob.asks_volume = asks_vol;
        ob.last_update_time = update.event_time;
        ob.is_valid = best_bid > 0.0 && best_ask < f64::MAX;
    }
}
