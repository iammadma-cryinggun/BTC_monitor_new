// ─────────────────────────────────────────────────────────────
// Binance WebSocket - 订单簿数据流
// 正确实现：先获取REST快照，再应用WebSocket增量更新
// 使用数值排序确保价格正确
// ─────────────────────────────────────────────────────────────

use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
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
    pub reference_price: f64,  // 参考价格，用于检测异常
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
            reference_price: 0.0,
        }
    }
}

/// 价格档位（用于正确排序）
#[derive(Debug, Clone)]
struct PriceLevel {
    price: f64,
    quantity: f64,
}

/// 内部订单簿（使用Vec维护，手动排序）
struct InternalOrderBook {
    bids: Vec<PriceLevel>,  // 降序排列（最高价在前）
    asks: Vec<PriceLevel>,  // 升序排列（最低价在前）
    last_update_id: i64,
}

impl InternalOrderBook {
    fn new() -> Self {
        Self {
            bids: Vec::new(),
            asks: Vec::new(),
            last_update_id: 0,
        }
    }

    /// 更新买单列表
    fn update_bid(&mut self, price: f64, qty: f64) {
        // 移除旧的价格档位
        self.bids.retain(|l| l.price != price);

        if qty > 0.0 {
            // 插入新档位
            self.bids.push(PriceLevel { price, quantity: qty });
            // 降序排序（最高价在前）
            self.bids.sort_by(|a, b| b.price.partial_cmp(&a.price).unwrap_or(std::cmp::Ordering::Equal));
        }
    }

    /// 更新卖单列表
    fn update_ask(&mut self, price: f64, qty: f64) {
        // 移除旧的价格档位
        self.asks.retain(|l| l.price != price);

        if qty > 0.0 {
            // 插入新档位
            self.asks.push(PriceLevel { price, quantity: qty });
            // 升序排序（最低价在前）
            self.asks.sort_by(|a, b| a.price.partial_cmp(&b.price).unwrap_or(std::cmp::Ordering::Equal));
        }
    }

    /// 获取最佳买价（最高）
    fn best_bid(&self) -> Option<f64> {
        self.bids.first().map(|l| l.price)
    }

    /// 获取最佳卖价（最低）
    fn best_ask(&self) -> Option<f64> {
        self.asks.first().map(|l| l.price)
    }

    /// 获取前N档买单总量
    fn top_bids_volume(&self, n: usize) -> f64 {
        self.bids.iter().take(n).map(|l| l.quantity).sum()
    }

    /// 获取前N档卖单总量
    fn top_asks_volume(&self, n: usize) -> f64 {
        self.asks.iter().take(n).map(|l| l.quantity).sum()
    }
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
            internal: Arc::new(RwLock::new(InternalOrderBook::new())),
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
                    let price: f64 = bid[0].parse().unwrap_or(0.0);
                    let qty: f64 = bid[1].parse().unwrap_or(0.0);
                    if price > 0.0 && qty > 0.0 {
                        internal.update_bid(price, qty);
                    }
                }
            }

            for ask in snapshot.asks {
                if ask.len() >= 2 {
                    let price: f64 = ask[0].parse().unwrap_or(0.0);
                    let qty: f64 = ask[1].parse().unwrap_or(0.0);
                    if price > 0.0 && qty > 0.0 {
                        internal.update_ask(price, qty);
                    }
                }
            }

            // 打印初始化后的最佳价格（用于验证）
            if let (Some(bid), Some(ask)) = (internal.best_bid(), internal.best_ask()) {
                info!("[Binance] 订单簿初始化: {} bids, {} asks, best_bid={}, best_ask={}",
                      internal.bids.len(), internal.asks.len(), bid, ask);
            }
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
        let heartbeat_internal = internal.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(tokio::time::Duration::from_secs(30));
            loop {
                interval.tick().await;
                if let Err(e) = write.send(Message::Ping(vec![])).await {
                    error!("[Binance WS] 心跳失败: {}", e);
                    break;
                }
                let ob = heartbeat_orderbook.read().await;
                let internal = heartbeat_internal.read().await;
                if ob.is_valid {
                    info!("[心跳] BTC价格: bid={:.2}, ask={:.2} (档位: {} bids, {} asks)",
                          ob.best_bid, ob.best_ask, internal.bids.len(), internal.asks.len());
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
                let price: f64 = bid[0].parse().unwrap_or(0.0);
                let qty: f64 = bid[1].parse().unwrap_or(0.0);
                internal_guard.update_bid(price, qty);
            }
        }

        for ask in &update.asks {
            if ask.len() >= 2 {
                let price: f64 = ask[0].parse().unwrap_or(0.0);
                let qty: f64 = ask[1].parse().unwrap_or(0.0);
                internal_guard.update_ask(price, qty);
            }
        }

        internal_guard.last_update_id = update.final_update_id;

        // 获取最佳价格和前5档总量
        let best_bid = internal_guard.best_bid().unwrap_or(0.0);
        let best_ask = internal_guard.best_ask().unwrap_or(f64::MAX);
        let bids_vol = internal_guard.top_bids_volume(5);
        let asks_vol = internal_guard.top_asks_volume(5);

        // 更新公开订单簿
        let mut ob = orderbook.write().await;

        // 价格合理性检查
        let mid_price = (best_bid + best_ask) / 2.0;
        let spread_pct = if best_bid > 0.0 { (best_ask - best_bid) / best_bid } else { 1.0 };

        // 检查条件：
        // 1. 基本有效性：bid > 0, ask < MAX, ask > bid
        // 2. 价差合理：< 0.5%（异常数据通常价差巨大）
        // 3. 如果有参考价格，新价格偏差 < 5%
        let basic_valid = best_bid > 0.0 && best_ask < f64::MAX && best_ask > best_bid;
        let spread_valid = spread_pct < 0.005;  // 0.5%
        let reference_valid = if ob.reference_price > 0.0 {
            let deviation = (mid_price - ob.reference_price).abs() / ob.reference_price;
            deviation < 0.05  // 5%
        } else {
            true
        };

        if basic_valid && spread_valid && reference_valid {
            ob.best_bid = best_bid;
            ob.best_ask = best_ask;
            ob.bids_volume = bids_vol;
            ob.asks_volume = asks_vol;
            ob.last_update_time = update.event_time;
            ob.is_valid = true;
            // 更新参考价格（缓慢移动平均）
            if ob.reference_price > 0.0 {
                ob.reference_price = ob.reference_price * 0.99 + mid_price * 0.01;
            } else {
                ob.reference_price = mid_price;
            }
        } else {
            // 价格异常，标记无效
            ob.is_valid = false;
            warn!("[Binance] 价格异常: bid={:.2}, ask={:.2}, spread={:.3}%, ref={:.2}",
                  best_bid, best_ask, spread_pct * 100.0, ob.reference_price);
        }
    }
}