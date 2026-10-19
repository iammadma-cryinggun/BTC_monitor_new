"""Hermetic smoke test for the outcome-feedback loop (no network).

Covers:
  1. update() returns (store, events) and emits events for LIVE entries only:
     launched / milestone(+30%, +80% once each) / final (all statuses).
  2. outcome_event_signal() formats TP/OUT messages for each event type.
  3. enter_hit_note() / reentry_note() message augmentation.
  4. battle_report_lines() digest section: launches, finals, cumulative.
  5. active_map() / last_final_map() bookkeeping.

Run: python test_outcome_push.py  (exit 0 = pass)
"""
import os, sys, json, tempfile, shutil, types
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

tmp = tempfile.mkdtemp(prefix="outcome_test_")
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
for name in ("ambush_track.json", "signals_log.json", "monitor_state.json"):
    bak, cur = os.path.join(tmp, name + ".bak"), os.path.join(DATA, name)
    if os.path.exists(cur):
        shutil.move(cur, bak)

import monitor
from monitor import (outcome_event_signal, enter_hit_note, reentry_note,
                     battle_report_lines)
import ambush_tracker

# hermetic timing stub (same reason as test_flicker_fix)
_fake = types.ModuleType("timing")
_fake.analyze = lambda sym, market="futures": {"ready": True, "vwap_dist": 0.001, "ema_bull": True}
_fake.regime = lambda market="futures": "bullish"
sys.modules["timing"] = _fake
monitor.timing = _fake

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)

def wstore(entries):
    with open(ambush_tracker.TRACK_PATH, "w", encoding="utf-8") as f:
        json.dump({"entries": entries}, f, ensure_ascii=False)

def entry(sym, ts, status="waiting", source="live", **kw):
    e = dict(sym=sym, ts=ts, source=source, tier="main", entry_price=1.0,
             score=95, status=status, launched_ts=None, launched_price=None,
             launch_delay_d=None, max_price=1.0, min_price=1.0, peak_gain=None,
             final_ts=None, last_price=1.0, last_seen=None)
    e.update(kw)
    return e

T0 = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
def iso(dt):
    return dt.isoformat()

# ---------- 1. launched event (live only) ----------
wstore([entry("AAAUSDT", T0), entry("BBBUSDT", T0, source="backfill")])
ranked = [{"sym": "AAAUSDT", "stage": "S2", "label": "FRESH_LAUNCH", "price": 1.10},
          {"sym": "BBBUSDT", "stage": "S2", "label": "FRESH_LAUNCH", "price": 1.10}]
store, events = ambush_tracker.update(ranked, now=NOW)
launched_ev = [e for e in events if e["ev"] == "launched"]
check("launched event fired for live entry", len(launched_ev) == 1 and launched_ev[0]["sym"] == "AAAUSDT")
check("launched event gain computed", abs(launched_ev[0]["gain"] - 0.10) < 1e-6)
check("launched event NOT fired for backfill", not any(e["sym"] == "BBBUSDT" for e in events))

# ---------- 2. milestone events: +30% once, +80% once, no duplicates ----------
ranked = [{"sym": "AAAUSDT", "stage": "S3", "label": "LAUNCHED", "price": 1.40}]
store, events = ambush_tracker.update(ranked, now=NOW + timedelta(hours=2))
ms = [e for e in events if e["ev"] == "milestone"]
check("milestone +30% fired once", [e["level"] for e in ms] == [0.30])
ranked = [{"sym": "AAAUSDT", "stage": "S3", "label": "LAUNCHED", "price": 1.90}]
store, events = ambush_tracker.update(ranked, now=NOW + timedelta(hours=4))
ms = [e for e in events if e["ev"] == "milestone"]
check("milestone +80% fired once (30 not repeated)", [e["level"] for e in ms] == [0.80])
ranked = [{"sym": "AAAUSDT", "stage": "S3", "label": "LAUNCHED", "price": 2.50}]
store, events = ambush_tracker.update(ranked, now=NOW + timedelta(hours=6))
ms = [e for e in events if e["ev"] == "milestone"]
check("no duplicate milestones on further rise", ms == [])

# ---------- 3. finalize event (win_strong) ----------
ent = store["entries"][0]
check("entry is launched with milestones recorded", ent["status"] == "launched" and ent.get("milestones") == [0.30, 0.80])
ranked = [{"sym": "AAAUSDT", "stage": "S6", "label": "CRASHING", "price": 1.80}]
store, events = ambush_tracker.update(ranked, now=NOW + timedelta(days=1))
fin = [e for e in events if e["ev"] == "final"]
check("final win_strong event fired", len(fin) == 1 and fin[0]["status"] == "win_strong" and fin[0]["peak_gain"] == 1.5)

# ---------- 4. pre_fail / timeout events ----------
wstore([entry("CCCUSDT", T0), entry("DDDUSDT", T0)])
ranked = [{"sym": "CCCUSDT", "stage": "S1", "label": "COILING", "price": 0.78}]
store, events = ambush_tracker.update(ranked, now=NOW)
pf = [e for e in events if e["ev"] == "final" and e["status"] == "pre_fail"]
check("pre_fail event fired", len(pf) == 1 and pf[0]["sym"] == "CCCUSDT")
wstore([entry("EEEUSDT", "2026-08-01T00:00:00+00:00")])
store, events = ambush_tracker.update([], now=NOW)
to = [e for e in events if e["ev"] == "final" and e["status"] == "timeout"]
check("timeout event fired after 45d", len(to) == 1 and to[0]["sym"] == "EEEUSDT")

# ---------- 5. outcome_event_signal formatting ----------
lvl, msg = outcome_event_signal({"ev": "launched", "sym": "AAAUSDT", "day": "2026-10-01",
                                 "entry_price": 1.0, "launched_price": 1.1, "gain": 0.10, "delay_d": 4.0})
check("launched -> TP level", lvl == "TP" and "命中" in msg and "4" in msg)
lvl, msg = outcome_event_signal({"ev": "milestone", "sym": "AAAUSDT", "level": 0.30, "gain": 0.35, "day": "2026-10-01"})
check("milestone 30 -> TP with 第一档", lvl == "TP" and "第一档" in msg)
lvl, msg = outcome_event_signal({"ev": "milestone", "sym": "AAAUSDT", "level": 0.80, "gain": 0.9, "day": "2026-10-01"})
check("milestone 80 -> TP with 大赢", lvl == "TP" and "大赢" in msg)
lvl, msg = outcome_event_signal({"ev": "final", "sym": "AAAUSDT", "status": "pre_fail", "peak_gain": -0.21, "day": "2026-10-01"})
check("pre_fail -> OUT with 止损", lvl == "OUT" and "止损" in msg)
lvl, msg = outcome_event_signal({"ev": "final", "sym": "AAAUSDT", "status": "timeout", "peak_gain": 0.02, "day": "2026-10-01"})
check("timeout -> OUT with 超时", lvl == "OUT" and "超时" in msg)
lvl, msg = outcome_event_signal({"ev": "final", "sym": "AAAUSDT", "status": "win_strong", "peak_gain": 1.5, "day": "2026-10-01"})
check("win_strong -> OUT with 大赢", lvl == "OUT" and "大赢" in msg)
lvl, msg = outcome_event_signal({"ev": "??", "sym": "X"})
check("unknown event -> skip", lvl is None)

# ---------- 6. enter_hit_note / reentry_note ----------
note = enter_hit_note(entry("AAAUSDT", T0, launch_delay_d=4.0, last_price=1.12))
check("enter_hit_note has date + entry price", "10-01" in note and "命中" in note and "1" in note)
note = reentry_note(entry("ONEUSDT", "2026-09-09T00:00:00+00:00", status="win_strong",
                          peak_gain=5.99, source="backfill", final_ts="2026-10-05T00:00:00+00:00"))
check("reentry_note has 重入 + prior outcome", "重入" in note and "大赢" in note and "+599%" in note)

# ---------- 7. battle_report_lines ----------
entries = [
    entry("AAAUSDT", T0, status="win_strong", peak_gain=1.5, source="live",
          launched_ts="2026-10-03T00:00:00+00:00", final_ts="2026-10-04T00:00:00+00:00",
          max_price=2.5),
    entry("CCCUSDT", T0, status="pre_fail", peak_gain=-0.21, source="live",
          final_ts="2026-10-04T00:00:00+00:00"),
    entry("FFFUSDT", T0, status="launched", source="live",
          launched_ts="2026-10-04T00:00:00+00:00", max_price=1.4, last_price=1.35),
    entry("GGGUSDT", T0, status="waiting", source="live"),
    entry("HHHUSDT", T0, status="win", peak_gain=0.4, source="backfill",
          final_ts="2026-10-04T00:00:00+00:00"),
]
br = battle_report_lines(entries, days=7, now="2026-10-05T00:00:00+00:00")
txt = "\n".join(br)
check("battle report non-empty", len(br) >= 3 and "战报" in br[0])
check("battle report lists launch hit", "FFFUSDT" in txt and "启动" in txt)
check("battle report lists win + loss finals", "AAAUSDT" in txt and "CCCUSDT" in txt)
check("battle report has cumulative", "累计" in txt)
check("battle report excludes backfill-only entries", "HHHUSDT" not in txt)
check("battle report empty when no live entries", battle_report_lines([]) == [])

# ---------- 8. active_map / last_final_map ----------
wstore(entries)
am = ambush_tracker.active_map()
check("active_map = non-final only", set(am) == {"FFFUSDT", "GGGUSDT"})
lf = ambush_tracker.last_final_map()
check("last_final_map = final, newest per sym", set(lf) == {"AAAUSDT", "CCCUSDT", "HHHUSDT"})
check("active_syms still works (back-compat)", ambush_tracker.active_syms() == set(am))

# ---------- cleanup ----------
for name in ("ambush_track.json", "signals_log.json", "monitor_state.json"):
    cur, bak = os.path.join(DATA, name), os.path.join(tmp, name + ".bak")
    if os.path.exists(cur):
        os.remove(cur)
    if os.path.exists(bak):
        shutil.move(bak, cur)
shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("ALL PASS" if not FAIL else f"FAILED: {FAIL}"))
sys.exit(0 if not FAIL else 1)
