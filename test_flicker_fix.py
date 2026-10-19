"""Hermetic smoke test for the AMBUSH flicker fixes (no network).

Covers:
  1. classify(): vol_tick streak >= 2 (with prev_streak < 2) gates AMBUSH /
     AMBUSH_LITE — a single True scan must NOT fire; crossing 2 must fire;
     streak 3+ (already confirmed) must NOT re-fire.
  2. run_scan streak bookkeeping: label excursion (coil -> launch -> re-coil)
     resets the streak so a NEW episode still fires.
  3. ambush_tracker.active_syms() returns exactly the non-final coins.
  4. dashboard.append_signal cap is SIGNALS_LIMIT (2000), not 200.

Run: python test_flicker_fix.py  (exit 0 = pass)
"""
import os, sys, json, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

# --- isolate all data dirs BEFORE importing the modules under test ---
tmp = tempfile.mkdtemp(prefix="flicker_test_")
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
for name in ("ambush_track.json", "signals_log.json", "monitor_state.json"):
    bak, cur = os.path.join(tmp, name + ".bak"), os.path.join(DATA, name)
    if os.path.exists(cur):
        shutil.move(cur, bak)

import monitor
from monitor import classify
import ambush_tracker
import dashboard

# hermetic: stub the 15m timing module BEFORE classify runs — its messages call
# timing_gate() -> timing.analyze() -> live Binance klines, which HANGS on this
# machine's firewalled network instead of failing fast.
import types
_fake_timing = types.ModuleType("timing")
_fake_timing.analyze = lambda sym, market="futures": {"ready": True, "vwap_dist": 0.001, "ema_bull": True}
_fake_timing.regime = lambda market="futures": "bullish"
sys.modules["timing"] = _fake_timing
monitor.timing = _fake_timing

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)

def mkrec(sym="TESTUSDT", label="COILING", vt=True, streak=None, **kw):
    r = dict(sym=sym, label=label, stage="S1", score=95, price=1.0,
             vol_tick=vt, flat_run=40, range_w=0.10, pv=-0.05,
             swing=2.0, events=[])
    if streak is not None:
        r["vt_streak"] = streak
    r.update(kw)
    return r

def mkprev(label="COILING", vt=True, streak=0, **kw):
    p = dict(label=label, vol_tick=vt, vt_streak=streak, event_sigs=[])
    p.update(kw)
    return p

def fired_levels(signals):
    return [s[0] for s in signals]

# ---------- 1. streak gating in classify ----------
# scan 1 of an episode: vol_tick first True (streak=1, prev 0) -> NO fire
sigs, _ = classify("TESTUSDT", mkrec(vt=True, streak=1), mkprev(vt=False, streak=0))
check("single True scan does not fire AMBUSH", "AMBUSH" not in fired_levels(sigs))

# scan 2: streak crosses 2 (prev_streak 1) -> fire
sigs, _ = classify("TESTUSDT", mkrec(vt=True, streak=2), mkprev(vt=True, streak=1))
check("streak crossing 2 fires AMBUSH", "AMBUSH" in fired_levels(sigs))

# scan 3+: already confirmed (prev_streak >= 2) -> no re-fire
sigs, _ = classify("TESTUSDT", mkrec(vt=True, streak=5), mkprev(vt=True, streak=4))
check("streak 3+ does not re-fire AMBUSH", "AMBUSH" not in fired_levels(sigs))

# flicker pattern True -> False -> True: streak restarts, needs 2 again
sigs, _ = classify("TESTUSDT", mkrec(vt=True, streak=1), mkprev(vt=False, streak=0))
check("post-flicker restart needs re-confirmation", "AMBUSH" not in fired_levels(sigs))

# LITE tier same gates
sigs, _ = classify("TESTUSDT", mkrec(label="COILING_LITE", vt=True, streak=1), mkprev(label="COILING_LITE", vt=False, streak=0))
check("LITE single True does not fire", "AMBUSH_LITE" not in fired_levels(sigs))
sigs, _ = classify("TESTUSDT", mkrec(label="COILING_LITE", vt=True, streak=2), mkprev(label="COILING_LITE", vt=True, streak=1))
check("LITE streak crossing 2 fires", "AMBUSH_LITE" in fired_levels(sigs))
sigs, _ = classify("TESTUSDT", mkrec(label="COILING_LITE", vt=True, streak=3), mkprev(label="COILING_LITE", vt=True, streak=2))
check("LITE streak 3+ does not re-fire", "AMBUSH_LITE" not in fired_levels(sigs))

# event signals unaffected: MA99_BREAK still fires regardless of streak
sigs, _ = classify("TESTUSDT", mkrec(label="PUMPED", vt=False, streak=0,
                                     events=[{"type": "MA99_BREAK", "note": "close 1.0 broke 1.2"}]),
                   mkprev(label="PUMPED", vt=False, streak=0))
check("MA99_BREAK still fires", "MA99" in fired_levels(sigs))

# old state without vt_streak field (deploy transition): prev_streak reads 0
sigs, _ = classify("TESTUSDT", mkrec(vt=True, streak=1), dict(label="COILING", vol_tick=True, event_sigs=[]))
check("old-format prev (no vt_streak) treated as streak 0", "AMBUSH" not in fired_levels(sigs))

# ---------- 2. streak bookkeeping (replicates run_scan's loop logic) ----------
def streak_step(rec_label, rec_vt, prev):
    _ready = rec_label in ("COILING", "COILING_LITE") and bool(rec_vt)
    return ((prev.get("vt_streak") or 0) + 1) if _ready else 0

# episode: False, True, True  ->  0, 1, 2 (fires)
st = streak_step("COILING", False, {})
check("bookkeep: scan0 off -> 0", st == 0)
st = streak_step("COILING", True, {"vt_streak": 0})
check("bookkeep: first on -> 1", st == 1)
st = streak_step("COILING", True, {"vt_streak": 1})
check("bookkeep: second on -> 2 (fires)", st == 2)
# label excursion resets: launch episode then re-coil
st = streak_step("FRESH_LAUNCH", True, {"vt_streak": 3})
check("bookkeep: label excursion resets to 0", st == 0)
st = streak_step("COILING", True, {"vt_streak": 0})
check("bookkeep: new episode starts at 1", st == 1)

# ---------- 3. active_syms() ----------
store = {"entries": [
    {"sym": "AAAUSDT", "status": "waiting"},
    {"sym": "BBBUSDT", "status": "launched"},
    {"sym": "CCCUSDT", "status": "win"},
    {"sym": "DDDUSDT", "status": "timeout"},
    {"sym": "AAAUSDT", "status": "miss"},
]}
with open(os.path.join(DATA, "ambush_track.json"), "w", encoding="utf-8") as f:
    json.dump(store, f)
act = ambush_tracker.active_syms()
check("active_syms = waiting+launched only", act == {"AAAUSDT", "BBBUSDT"})

# ---------- 4. signals log cap ----------
for i in range(dashboard.SIGNALS_LIMIT + 50):
    dashboard.append_signal("INFO", f"S{i}USDT", "m", ev_type=None)
sigs = dashboard.load_signals()
check(f"signals log capped at {dashboard.SIGNALS_LIMIT}", len(sigs) == dashboard.SIGNALS_LIMIT)
check("log kept the NEWEST entries", sigs[-1]["sym"] == f"S{dashboard.SIGNALS_LIMIT + 49}USDT")
check("cap constant is 2000", dashboard.SIGNALS_LIMIT == 2000)

# ---------- cleanup / restore ----------
for name in ("ambush_track.json", "signals_log.json", "monitor_state.json"):
    cur, bak = os.path.join(DATA, name), os.path.join(tmp, name + ".bak")
    if os.path.exists(cur):
        os.remove(cur)
    if os.path.exists(bak):
        shutil.move(bak, cur)
shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("ALL PASS" if not FAIL else f"FAILED: {FAIL}"))
sys.exit(0 if not FAIL else 1)
