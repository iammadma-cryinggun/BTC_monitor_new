// ─────────────────────────────────────────────────────────────
// Binance REST API - 合约交易
// ─────────────────────────────────────────────────────────────

use crate::config::BinanceConfig;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use tracing::{debug, error, info};

/// Binance合约API客户端
pub struct BinanceRestClient {
    client: Client,
    api_key: String,
    api_secret: String,
    base_url: String,
}

impl BinanceRestClient {
    pub fn new(config: &BinanceConfig) -> Self {
        let base_url = if config.testnet {
            "https://testnet.binancefuture.com".to_string()
        } else {
            "https://fapi.binance.com".to_string()
        };

        Self {
            client: Client::new(),
            api_key: config.api_key.clone(),
            api_secret: config.api_secret.clone(),
            base_url,
        }
    }

    /// 获取账户余额
    pub async fn get_balance(&self) -> Result<f64, String> {
        let url = format!("{}/fapi/v2/balance", self.base_url);
        let timestamp = chrono::Utc::now().timestamp_millis();
        let query = format!("timestamp={}", timestamp);
        let signature = self.sign(&query);

        let response = self.client
            .get(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .query(&[
                ("timestamp", timestamp.to_string()),
                ("signature", signature),
            ])
            .send()
            .await
            .map_err(|e| e.to_string())?;

        let balances: Vec<AccountBalance> = response
            .json()
            .await
            .map_err(|e| e.to_string())?;

        // 找USDC余额
        for b in balances {
            if b.asset == "USDT" || b.asset == "USDC" {
                return Ok(b.available_balance.parse().unwrap_or(0.0));
            }
        }

        Ok(0.0)
    }

    /// 设置杠杆
    pub async fn set_leverage(&self, symbol: &str, leverage: u32) -> Result<(), String> {
        let url = format!("{}/fapi/v1/leverage", self.base_url);
        let timestamp = chrono::Utc::now().timestamp_millis();
        let query = format!("symbol={}&leverage={}&timestamp={}", symbol, leverage, timestamp);
        let signature = self.sign(&query);

        let response = self.client
            .post(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .form(&[
                ("symbol", symbol.to_string()),
                ("leverage", leverage.to_string()),
                ("timestamp", timestamp.to_string()),
                ("signature", signature),
            ])
            .send()
            .await
            .map_err(|e| e.to_string())?;

        if response.status().is_success() {
            info!("[Binance] 设置杠杆成功: {} {}x", symbol, leverage);
            Ok(())
        } else {
            let text = response.text().await.unwrap_or_default();
            error!("[Binance] 设置杠杆失败: {}", text);
            Err(text)
        }
    }

    /// 开仓（市价单）
    pub async fn open_position(
        &self,
        symbol: &str,
        side: &str,  // "BUY" or "SELL"
        quantity: f64,
    ) -> Result<OrderResult, String> {
        let url = format!("{}/fapi/v1/order", self.base_url);
        let timestamp = chrono::Utc::now().timestamp_millis();
        let query = format!(
            "symbol={}&side={}&type=MARKET&quantity={:.6}&timestamp={}",
            symbol, side, quantity, timestamp
        );
        let signature = self.sign(&query);

        let response = self.client
            .post(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .form(&[
                ("symbol", symbol.to_string()),
                ("side", side.to_string()),
                ("type", "MARKET".to_string()),
                ("quantity", format!("{:.6}", quantity)),
                ("timestamp", timestamp.to_string()),
                ("signature", signature),
            ])
            .send()
            .await
            .map_err(|e| e.to_string())?;

        if response.status().is_success() {
            let result: OrderResult = response.json().await.map_err(|e| e.to_string())?;
            info!("[Binance] 开仓成功: {} {} {:.6}", symbol, side, quantity);
            Ok(result)
        } else {
            let text = response.text().await.unwrap_or_default();
            error!("[Binance] 开仓失败: {}", text);
            Err(text)
        }
    }

    /// 平仓
    pub async fn close_position(&self, symbol: &str, side: &str, quantity: f64) -> Result<OrderResult, String> {
        // 平仓方向与开仓相反
        let close_side = if side == "BUY" { "SELL" } else { "BUY" };
        self.open_position(symbol, close_side, quantity).await
    }

    /// 设置止损止盈
    pub async fn set_stop_loss_take_profit(
        &self,
        symbol: &str,
        side: &str,
        stop_loss: f64,
        take_profit: f64,
        quantity: f64,
    ) -> Result<(), String> {
        // 止损单
        let stop_side = if side == "BUY" { "SELL" } else { "BUY" };
        let timestamp = chrono::Utc::now().timestamp_millis();

        // 止损
        let sl_query = format!(
            "symbol={}&side={}&type=STOP_MARKET&stopPrice={:.2}&closePosition=true&timestamp={}",
            symbol, stop_side, stop_loss, timestamp
        );
        let sl_signature = self.sign(&sl_query);
        let url = format!("{}/fapi/v1/order", self.base_url);

        let _ = self.client
            .post(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .form(&[
                ("symbol", symbol.to_string()),
                ("side", stop_side.to_string()),
                ("type", "STOP_MARKET".to_string()),
                ("stopPrice", format!("{:.2}", stop_loss)),
                ("closePosition", "true".to_string()),
                ("timestamp", timestamp.to_string()),
                ("signature", sl_signature),
            ])
            .send()
            .await;

        // 止盈
        let tp_query = format!(
            "symbol={}&side={}&type=TAKE_PROFIT_MARKET&stopPrice={:.2}&closePosition=true&timestamp={}",
            symbol, stop_side, take_profit, timestamp
        );
        let tp_signature = self.sign(&tp_query);

        let _ = self.client
            .post(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .form(&[
                ("symbol", symbol.to_string()),
                ("side", stop_side.to_string()),
                ("type", "TAKE_PROFIT_MARKET".to_string()),
                ("stopPrice", format!("{:.2}", take_profit)),
                ("closePosition", "true".to_string()),
                ("timestamp", timestamp.to_string()),
                ("signature", tp_signature),
            ])
            .send()
            .await;

        info!("[Binance] 设置止损止盈: SL={:.2}, TP={:.2}", stop_loss, take_profit);
        Ok(())
    }

    /// 获取当前持仓
    pub async fn get_position(&self, symbol: &str) -> Result<Option<Position>, String> {
        let url = format!("{}/fapi/v2/positionRisk", self.base_url);
        let timestamp = chrono::Utc::now().timestamp_millis();
        let query = format!("timestamp={}", timestamp);
        let signature = self.sign(&query);

        let response = self.client
            .get(&url)
            .header("X-MBX-APIKEY", &self.api_key)
            .query(&[
                ("timestamp", timestamp.to_string()),
                ("signature", signature),
            ])
            .send()
            .await
            .map_err(|e| e.to_string())?;

        let positions: Vec<Position> = response
            .json()
            .await
            .map_err(|e| e.to_string())?;

        for p in positions {
            if p.symbol == symbol {
                let qty: f64 = p.position_amt.parse().unwrap_or(0.0);
                if qty.abs() > 0.0 {
                    return Ok(Some(p));
                }
            }
        }

        Ok(None)
    }

    /// 签名
    fn sign(&self, query: &str) -> String {
        use hmac::{Hmac, Mac};
        use sha2::Sha256;
        type HmacSha256 = Hmac<Sha256>;

        let mut mac = HmacSha256::new_from_slice(self.api_secret.as_bytes())
            .expect("HMAC can take key of any size");
        mac.update(query.as_bytes());
        let result = mac.finalize();
        hex::encode(result.into_bytes())
    }
}

#[derive(Debug, Deserialize)]
struct AccountBalance {
    asset: String,
    #[serde(rename = "availableBalance")]
    available_balance: String,
}

#[derive(Debug, Deserialize)]
pub struct OrderResult {
    #[serde(rename = "orderId")]
    pub order_id: i64,
    pub symbol: String,
    pub status: String,
}

#[derive(Debug, Deserialize)]
pub struct Position {
    pub symbol: String,
    #[serde(rename = "positionAmt")]
    pub position_amt: String,
    #[serde(rename = "entryPrice")]
    pub entry_price: String,
    #[serde(rename = "unRealizedProfit")]
    pub unrealized_profit: String,
}
