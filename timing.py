"""
Entry timing gate — the low-timeframe "WHEN to pull the trigger" layer.

The 4h six-stage machine answers WHICH coin / WHICH zone (selection).
This module answers WHEN: a 15m alignment filter + a BTC regime check,
adapted from the classic intraday template:

    longs only when price > session VWAP AND EMA9 > EMA20 (15m)
    BTC regime bearish -> postpone / halve new entries

It exists because "好币 + 好区间" still dies when the whole market dumps
— the regime gate keeps us from recommending fresh longs into those.

ORB mapping (crypto is 24/7, no session open): the "opening range" role
is played by the 4h coil range — the ENTER signal IS the range breakout.
This module only adds the timing confirmation on top of it, plus the
take-profit/exits stay where they already are (MA25/MA99/TOP signals).

VWAP anchor: current UTC day (00:00 UTC). If the day has < 4 bars so
far, fall back to a rolling 24h VWAP.
"""
import time as _time
from datetime import datetime, timezone

import numpy as np

from memecoin_scanner import fetch_klines

TF = "15m"
LIMIT = 200          # 15m bars: EMA20 warmup + day anchor
EMA_FAST, EMA_SLOW = 9, 20
REGIME_CN = {"bullish": "多头", "bearish": "空头", "neutral": "中性"}
_regime_cache = {}    # market -> (ts, regime)


def analyze(sym, market="futures"):
    """15m VWAP + 9/20 EMA alignment for one symbol.

    Returns dict(price, vwap, vwap_dist, anchor, ema9, ema20, ema_bull,
    above_vwap, ready) or None when the fetch fails / not enough bars.
    """
    try:
        df = fetch_klines(sym, TF, LIMIT, market=market)
    except Exception:
        return None
    if df is None or len(df) < 30:
        return None
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    vols = df["volume"].values
    ts_ms = df["ts_ms"].values
    price = float(closes[-1])

    # session VWAP: anchored to 00:00 UTC today (>=4 bars), else rolling 24h
    day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    ms0 = int(day0.timestamp() * 1000)
    mask = ts_ms >= ms0
    anchor = "当日"
    if int(mask.sum()) < 4:
        mask = np.zeros(len(closes), dtype=bool)
        mask[-96:] = True          # last 24h of 15m bars
        anchor = "24h"
    if mask.any():
        tp = (highs[mask] + lows[mask] + closes[mask]) / 3.0
        vv = vols[mask]
        vwap = float((tp * vv).sum() / vv.sum()) if vv.sum() > 0 else float(closes[-1])
    else:
        vwap = float(closes[-1])

    # EMA9/20 on 15m closes (pandas ewm)
    ema9 = float(df["close"].ewm(span=EMA_FAST, adjust=False).mean().iloc[-1])
    ema20 = float(df["close"].ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1])

    return dict(
        price=price, vwap=vwap, anchor=anchor,
        vwap_dist=round(price / vwap - 1.0, 4) if vwap > 0 else None,
        ema9=ema9, ema20=ema20, ema_bull=bool(ema9 > ema20),
        above_vwap=bool(price > vwap),
        ready=bool(price > vwap and ema9 > ema20),
    )


def regime(market="futures", max_age=600):
    """BTC 15m regime: bullish (ready) / bearish (below VWAP AND ema bearish)
    / neutral. Cached 10 min — one call serves a whole scan + agent queries."""
    now = _time.time()
    hit = _regime_cache.get(market)
    if hit and now - hit[0] < max_age:
        return hit[1]
    t = analyze("BTCUSDT", market=market)
    if t is None:
        return None
    if t["ready"]:
        r = "bullish"
    elif (not t["above_vwap"]) and (not t["ema_bull"]):
        r = "bearish"
    else:
        r = "neutral"
    _regime_cache[market] = (now, r)
    return r


def _selftest():
    """Offline math check with a synthetic frame (no network)."""
    import pandas as pd
    n = 120
    now = datetime.now(timezone.utc)
    base = int(now.timestamp() * 1000)
    # rising closes -> ema9>ema20, price above a mid VWAP
    closes = np.linspace(100, 120, n)
    highs = closes + 1
    lows = closes - 1
    vols = np.full(n, 10.0)
    vols[-1] = 50.0                      # last bar heavy -> vwap below price
    ts = np.array([base - (n - i) * 900_000 for i in range(n)])
    df = pd.DataFrame(dict(ts_ms=ts, open=closes, high=highs, low=lows,
                           close=closes, volume=vols, tbuy=vols / 2))
    g = globals()
    orig = fetch_klines
    g["fetch_klines"] = lambda *a, **k: df
    t = analyze("TESTUSDT")
    g["fetch_klines"] = orig
    assert t is not None and t["ema_bull"] and t["ready"] and t["above_vwap"], t
    assert t["anchor"] in ("当日", "24h")
    print("timing selftest OK:", {k: t[k] for k in ("price", "vwap", "vwap_dist", "ema_bull", "ready")})
    # falling closes -> bearish regime
    closes = np.linspace(120, 100, n)
    df2 = pd.DataFrame(dict(ts_ms=ts, open=closes, high=highs, low=lows,
                           close=closes, volume=vols, tbuy=vols / 2))
    g["fetch_klines"] = lambda *a, **k: df2
    t2 = analyze("TESTUSDT")
    g["fetch_klines"] = orig
    assert t2 is not None and (not t2["ema_bull"]) and (not t2["ready"]), t2
    print("timing selftest bearish OK")


if __name__ == "__main__":
    _selftest()
