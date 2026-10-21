# -*- coding: utf-8 -*-
"""Delta direction gate test (2026-10-21, learned from KBeast.eth's order-flow
system): vol_tick must require NET BUYING volume — a crash bar has huge
volume too (MITO/PTB fell 20%+ and crash volume fired ambush signals).

Crafted 4h history (160 bars):
  0..99   slow pump 0.5 -> ~1.01 (volume flat, no LAUNCH event fires)
  100..101 dump to the coil base 0.60
  102..156 flat coil, tiny noise, baseline volume
  157..159 volume 3x spike with taker-buy share:
            0.65 -> accumulation  (vol_tick must stay True)
            0.20 -> distribution (vol_tick must flip False)
            no tbuy column -> legacy cache, gate skipped (True)
"""
import pandas as pd

import memecoin_scanner as msc


def make_4h(spike_share):
    n = 160
    rows = []
    price = 0.5
    for i in range(n):
        if i < 100:                      # slow pump (0.7%/bar -> 2x total)
            price *= 1.007
        elif i < 102:                    # dump to coil base
            price = 0.60
        else:                            # flat coil, tiny noise
            price = 0.60 * (1 + 0.001 * ((i % 5) - 2))
        vol = 300 if i >= n - 3 else 100
        tb = vol * (spike_share if i >= n - 3 else 0.5)
        rows.append(dict(ts_ms=1600000000000 + i * 14400000,
                         open=price, high=price * 1.001, low=price * 0.999,
                         close=price, volume=vol, tbuy=tb))
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return df


def make_1h():
    rows = [dict(ts_ms=1600000000000 + i * 3600000,
                 open=0.60, high=0.601, low=0.599, close=0.60,
                 volume=100, tbuy=50.0) for i in range(240)]
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return df


_orig = msc.load_or_fetch


def run_case(spike_share, drop_tbuy=False):
    df4 = make_4h(spike_share)
    if drop_tbuy:
        df4 = df4.drop(columns=["tbuy"])
    df1 = make_1h()

    def fake_load(sym, interval, limit, market="spot", force_fetch=False):
        return df4 if interval == "4h" else df1

    msc.load_or_fetch = fake_load
    try:
        rec, err = msc.score_coin("TESTUSDT", market="futures")
    finally:
        msc.load_or_fetch = _orig
    return rec, err


checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# --- case 1: net-buying spike -> vol_tick True ---
rec_buy, err = run_case(0.65)
check("buy case: no fetch error", err is None and rec_buy is not None)
check("buy case: COILING label", rec_buy and rec_buy["label"] == "COILING")
check("buy case: coil is mature (flat_run >= 2)",
      rec_buy and (rec_buy.get("flat_run") or 0) >= 2)
check("buy case: vol_tick True (net-buying spike passes)",
      rec_buy and rec_buy["vol_tick"] is True)
check("buy case: tbuy_ratio ~0.65 reported",
      rec_buy and abs((rec_buy["tbuy_ratio"] or 0) - 0.65) < 0.02)

# --- case 2: same volume spike, sellers dominate -> vol_tick False ---
rec_sell, _ = run_case(0.20)
check("sell case: still COILING (shape unchanged)",
      rec_sell and rec_sell["label"] == "COILING")
check("sell case: vol_tick False (delta gate kills falling-knife volume)",
      rec_sell and rec_sell["vol_tick"] is False)
check("sell case: tbuy_ratio ~0.20 reported",
      rec_sell and abs((rec_sell["tbuy_ratio"] or 0) - 0.20) < 0.02)

# --- case 3: legacy cache without tbuy column -> gate skipped ---
rec_none, _ = run_case(0.20, drop_tbuy=True)
check("legacy cache: no tbuy column -> ratio None",
      rec_none and rec_none["tbuy_ratio"] is None)
check("legacy cache: gate skipped, vol_tick True (old behavior)",
      rec_none and rec_none["vol_tick"] is True)

fails = [n for n, ok in checks if not ok]
print()
print("ALL PASSED (%d checks)" % len(checks) if not fails else "FAILED: %s" % fails)
raise SystemExit(1 if fails else 0)
