"""
Memecoin 6-stage pump-and-dump playbook scanner.

Implements the playbook from the user's writeup:
  S1 accumulation (long flat + low volume)
  S2 launch (first volume spike after consolidation, breaks range)
  S3 shakeout (drawdown 30-60% from swing high)
  S4 markup (new high on *drier* volume)
  S5 wick top (4h long upper wick / parabola reversal)
  S6 crash + death-bounce (sharp drop, then a high-volume failed rebound)

Inputs: 4h OHLCV (stage detection), 1h OHLCV (MA99 trailing stop).
Works on any coin with those CSVs. Validated on BANKUSDT (the only one of the
user's 5 coins listed on Binance).

Usage:
  python memecoin_playbook_scanner.py --sym BANKUSDT
  python memecoin_playbook_scanner.py --csv4h path.csv --csv1h path.csv --name BTW
"""
import os, sys, json, argparse
import pandas as pd, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# ---- tunable thresholds (from the playbook) ----
LAUNCH_VOL   = 2.5    # S2: volume > 2.5x 20-bar avg
BREAKOUT     = 0.05   # S2: close > 5% above recent range high
CONS_FLAT    = 0.25   # S1: 15-bar (high-low)/avg close < 25%
CONS_VOL     = 1.10   # S1: 15-bar median volume ratio < 1.10
SHAKE_LO, SHAKE_HI = 0.30, 0.65  # S3 drawdown band
WICK_UPPER   = 0.50   # S5: upper wick > 50% of range
WICK_CLOSE   = 0.90   # S5: close < 90% of high
CRASH_DD     = 0.30   # S6: >=30% drop from peak
BOUNCE_VOL   = 2.0    # S6 death-bounce: volume spike on rebound

def load_csv(path):
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return df

def add_indicators(df):
    df = df.copy()
    df["ma25"] = df["close"].rolling(25, min_periods=25).mean()
    df["ma99"] = df["close"].rolling(99, min_periods=99).mean()
    df["vol_sma20"] = df["volume"].rolling(20, min_periods=20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_sma20"]
    df["ret"] = df["close"].pct_change()
    df["range_pct"] = (df["high"] - df["low"]) / df["close"]
    # consolidation flag: last 15 bars flat + quiet
    hi = df["high"].rolling(15).max(); lo = df["low"].rolling(15).min()
    med_close = df["close"].rolling(15).mean()
    flat = (hi - lo) / med_close < CONS_FLAT
    qvol = df["vol_ratio"].rolling(15).median() < CONS_VOL
    df["in_consol"] = (flat & qvol).fillna(False)
    return df

def detect(df4h, df1h=None):
    df = add_indicators(df4h)
    n = len(df)
    close = df["close"].values; high = df["high"].values; low = df["low"].values
    op = df["open"].values; volr = df["vol_ratio"].values
    consol = df["in_consol"].values
    stage = np.array(["S1"] * n, dtype=object)
    events = []  # {type, i, ts, price, note}
    in_play = False; launch_idx = None; peak = 0.0; launch_vol = 0.0
    phase = "idle"; bounced = False
    for i in range(1, n):
        if in_play and close[i] > peak:
            peak = close[i]
        dd = (peak - close[i]) / peak if in_play and peak > 0 else 0.0
        if not in_play:
            recent_hi = np.max(close[max(0, i-15):i])
            if (volr[i] > LAUNCH_VOL and close[i] > recent_hi * (1+BREAKOUT)
                    and consol[i-1]):
                stage[i] = "S2"
                in_play = True; launch_idx = i; peak = close[i]; launch_vol = volr[i]
                phase = "launch"; bounced = False
                events.append(dict(type="LAUNCH", i=i, ts=str(df["ts"].iloc[i]),
                                   price=round(float(close[i]), 6),
                                   note=f"vol_ratio={volr[i]:.1f}x breakout"))
                continue
        else:
            if phase != "crash" and SHAKE_LO <= dd <= SHAKE_HI and phase != "shakeout":
                phase = "shakeout"; stage[i] = "S3"
                if not any(e["type"] == "SHAKEOUT" and e["i"] > launch_idx for e in events):
                    events.append(dict(type="SHAKEOUT", i=i, ts=str(df["ts"].iloc[i]),
                                       price=round(float(close[i]), 6),
                                       note=f"drawdown {dd*100:.0f}% from peak {peak:.4f}"))
                continue
            if close[i] > peak and volr[i] < launch_vol * 0.9:
                phase = "markup"; stage[i] = "S4"; continue
            rng = (high[i] - low[i])
            upper_wick = (high[i] - max(op[i], close[i])) / (rng + 1e-12)
            if (upper_wick > WICK_UPPER and close[i] < high[i] * WICK_CLOSE
                    and i > launch_idx + 3 and phase not in ("top", "crash")):
                phase = "top"; stage[i] = "S5"
                if not any(e["type"] == "WICKTOP" and e["i"] > launch_idx for e in events):
                    events.append(dict(type="WICKTOP", i=i, ts=str(df["ts"].iloc[i]),
                                       price=round(float(close[i]), 6),
                                       note=f"upper_wick={upper_wick*100:.0f}% of range, high={high[i]:.4f}"))
                continue
            if dd >= CRASH_DD and phase in ("markup", "top"):
                phase = "crash"; stage[i] = "S6"
                events.append(dict(type="CRASH", i=i, ts=str(df["ts"].iloc[i]),
                                   price=round(float(close[i]), 6),
                                   note=f"drawdown {dd*100:.0f}% from peak {peak:.4f}"))
                bounced = False; continue
            if phase == "crash" and not bounced and volr[i] > BOUNCE_VOL and close[i] > close[i-1]*1.05:
                bounced = True; stage[i] = "S6"
                events.append(dict(type="DEATHBOUNCE", i=i, ts=str(df["ts"].iloc[i]),
                                   price=round(float(close[i]), 6),
                                   note=f"vol_ratio={volr[i]:.1f}x rebound, failed (no new high)"))
                continue
            # hold current phase label; allow a new cycle once re-consolidated
            if phase == "crash" and consol[i-1] and i > launch_idx + 20:
                in_play = False; phase = "idle"; stage[i] = "S1"; continue
            stage[i] = {"launch":"S2","shakeout":"S3","markup":"S4","top":"S5","crash":"S6"}.get(phase, "S1")
    df["stage"] = stage
    # key levels (latest)
    latest = df.iloc[-1]
    levels = dict(
        price=float(latest["close"]),
        ma25_4h=(None if pd.isna(latest["ma25"]) else float(latest["ma25"])),
        ma99_4h=(None if pd.isna(latest["ma99"]) else float(latest["ma99"])),
        ma99_1h=None,
        stage=str(latest["stage"]),
    )
    if df1h is not None:
        d1 = add_indicators(df1h)
        last1 = d1.iloc[-1]
        levels["ma99_1h"] = (None if pd.isna(last1["ma99"]) else float(last1["ma99"]))
    return df, events, levels

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="BANKUSDT")
    ap.add_argument("--csv4h", default=None)
    ap.add_argument("--csv1h", default=None)
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    name = a.name or a.sym
    if a.csv4h:
        df4h = load_csv(a.csv4h)
        df1h = load_csv(a.csv1h) if a.csv1h else None
    else:
        df4h = load_csv(os.path.join(DATA, f"{a.sym}_4h_raw.csv"))
        df1h = load_csv(os.path.join(DATA, f"{a.sym}_1h_raw.csv")) if os.path.exists(
            os.path.join(DATA, f"{a.sym}_1h_raw.csv")) else None
    df, events, levels = detect(df4h, df1h)
    # stage distribution
    dist = pd.Series(df["stage"]).value_counts().to_dict()
    print(f"=== {name} 4h playbook scan ===")
    print(f"bars={len(df)} range {df['ts'].iloc[0].date()}..{df['ts'].iloc[-1].date()}")
    print(f"latest stage={levels['stage']} price={levels['price']:.5f} "
          f"MA25_4h={levels['ma25_4h']} MA99_4h={levels['ma99_4h']} MA99_1h={levels['ma99_1h']}")
    print(f"stage distribution: {dist}")
    print("--- detected events ---")
    for e in events:
        print(f"  {e['type']:12s} {e['ts'][:16]}  price={e['price']:<10} {e['note']}")
    out = dict(name=name, bars=int(len(df)),
               range_start=str(df['ts'].iloc[0]), range_end=str(df['ts'].iloc[-1]),
               levels=levels, stage_distribution={k:int(v) for k,v in dist.items()},
               events=events)
    with open(os.path.join(HERE, f"playbook_{name}.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"WROTE playbook_{name}.json")

if __name__ == "__main__":
    main()
