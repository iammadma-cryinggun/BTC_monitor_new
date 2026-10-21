# -*- coding: utf-8 -*-
"""env_regime (4h/1d) offline math check — synthetic rising/falling frames."""
import sys
sys.path.insert(0, "D:/memecoin-monitor")
import numpy as np, pandas as pd
from datetime import datetime, timezone
import timing

now = datetime.now(timezone.utc)
base = int(now.timestamp() * 1000)

def frame(c0, c1, n, step_ms):
    closes = np.linspace(c0, c1, n)
    return pd.DataFrame(dict(
        ts_ms=np.array([base - (n - i) * step_ms for i in range(n)]),
        open=closes, high=closes + 1, low=closes - 1,
        close=closes, volume=np.full(n, 10.0)))

real_fk = timing.fetch_klines
FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAIL.append(name)

# rising BTC on both TFs -> bullish
timing.fetch_klines = lambda sym, tf, lim, market="futures": (
    frame(100, 120, 150, 4 * 3600_000) if tf == "4h" else frame(100, 130, 120, 86400_000))
timing._env_cache.clear()
e = timing.env_regime()
check("rising both -> bullish", e and e["r"] == "bullish" and e["r4h"] == "bullish" and e["r1d"] == "bullish")

# falling on both -> bearish
timing.fetch_klines = lambda sym, tf, lim, market="futures": (
    frame(120, 100, 150, 4 * 3600_000) if tf == "4h" else frame(130, 100, 120, 86400_000))
timing._env_cache.clear()
e = timing.env_regime()
check("falling both -> bearish", e and e["r"] == "bearish" and e["r4h"] == "bearish" and e["r1d"] == "bearish")

# 4h bullish + 1d bearish -> bearish (conservative: broken daily vetoes)
timing.fetch_klines = lambda sym, tf, lim, market="futures": (
    frame(100, 120, 150, 4 * 3600_000) if tf == "4h" else frame(130, 100, 120, 86400_000))
timing._env_cache.clear()
e = timing.env_regime()
check("4h up + 1d down -> bearish", e and e["r"] == "bearish" and e["r4h"] == "bullish" and e["r1d"] == "bearish")

# 4h history too short (<60 bars) -> env None (fails safe, no env context)
timing.fetch_klines = lambda sym, tf, lim, market="futures": (
    frame(100, 120, 50, 4 * 3600_000) if tf == "4h" else frame(100, 130, 120, 86400_000))
timing._env_cache.clear()
e = timing.env_regime()
check("4h history too short -> None", e is None)

# both fetches fail -> None
timing.fetch_klines = lambda sym, tf, lim, market="futures": None
timing._env_cache.clear()
e = timing.env_regime()
check("fetch fail -> None", e is None)

timing.fetch_klines = real_fk
print()
if FAIL:
    print("%d FAILED" % len(FAIL)); sys.exit(1)
print("ALL ENV-REGIME CHECKS PASSED")
