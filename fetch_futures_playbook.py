"""
Fetch Binance USD-M FUTURES klines for the user's real memecoins
(RAVE/LAB/AKE/BTW/BANK) and run the 6-stage playbook detector to
cross-validate against the user's handwritten timeline.

Futures klines: https://fapi.binance.com/fapi/v1/klines
Format identical to spot: [open_time, open, high, low, close, volume, close_time, ...]
"""
import os, sys, json, time, urllib.request
import pandas as pd, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memecoin_playbook_scanner import detect, add_indicators

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
FUT = "https://fapi.binance.com/fapi/v1/klines"

def fetch_futures(sym, interval, start_ms, limit=1000):
    import http.client, urllib.error
    out = []
    s = start_ms
    while True:
        u = f"{FUT}?symbol={sym}&interval={interval}&startTime={s}&limit={limit}"
        data = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    raw = resp.read()
                data = json.loads(raw)
                break
            except (http.client.IncompleteRead, urllib.error.URLError, Exception) as e:
                time.sleep(0.3 * (attempt + 1))
        if data is None:
            print(f"  [WARN] {sym} {interval} fetch failed, skipping batch from {s}")
            break
        d = data
        if not d:
            break
        out.extend(d)
        if len(d) < limit:
            break
        s = d[-1][0] + 1
        time.sleep(0.05)
    return out

def to_csv(sym, interval, rows):
    path = os.path.join(DATA, f"{sym}_{interval}_raw.csv")
    with open(path, "w", newline="") as f:
        f.write("ts_ms,open,high,low,close,volume\n")
        for k in rows:
            f.write(f"{k[0]},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]}\n")
    return path

SYMS = ["RAVEUSDT", "LABUSDT", "AKEUSDT", "BTWUSDT", "BANKUSDT"]
# start from 2026-02-01 to cover RAVE/LAB April/May launches
START = int(pd.Timestamp("2026-02-01", tz="UTC").timestamp() * 1000)

if __name__ == "__main__":
    for sym in SYMS:
        p4 = fetch_futures(sym, "4h", START)
        p1 = fetch_futures(sym, "1h", START)
        to_csv(sym, "4h", p4)
        to_csv(sym, "1h", p1)
        print(f"[fetch] {sym}: 4h={len(p4)} bars, 1h={len(p1)} bars")
        df4 = pd.read_csv(os.path.join(DATA, f"{sym}_4h_raw.csv"))
        df4["ts"] = pd.to_datetime(df4["ts_ms"], unit="ms", utc=True)
        df1 = pd.read_csv(os.path.join(DATA, f"{sym}_1h_raw.csv"))
        df1["ts"] = pd.to_datetime(df1["ts_ms"], unit="ms", utc=True)
        df, events, levels = detect(df4, df1)
        print(f"\n=== {sym} 4h playbook ===")
        print(f"range {df['ts'].iloc[0].date()}..{df['ts'].iloc[-1].date()}  "
              f"latest stage={levels['stage']} price={levels['price']:.5f}  "
              f"MA25_4h={levels['ma25_4h']} MA99_4h={levels['ma99_4h']} MA99_1h={levels['ma99_1h']}")
        for e in events:
            print(f"  {e['type']:12s} {e['ts'][:16]}  px={e['price']:<10} {e['note']}")
