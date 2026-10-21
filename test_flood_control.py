"""Hermetic smoke test for the Telegram flood-control fixes (no network).

2026-10-20 incident: after the volume wipe + crash fix, the refilled state
re-based and a market-wide dump produced 1016 individual Telegram messages
in 24h. Composition: MA25 347, MA99 252, ENTER 231, TOP 95, DIP 71 — and
AXLUSDT's IDENTICAL events re-fired twice (17:21 and 01:24) because events
flicker out of (and back into) the detection window every time a new 4h bar
opens, so event_sigs alone cannot dedup them.

Covers:
  1. fired-event memory: the same event key (sym|TYPE@ts) fires at most ONCE
     even when event_sigs churns (flicker-out then flicker-in).
  2. batching: DIP/TOP/MA25/MA99/CRASH/BOUNCE collapse into ONE per-scan
     "事件速览" message; ENTER stays individual (capped at 8).

Run: python test_flood_control.py   (exit 0 = pass)
"""
import os, sys, json, time, tempfile, shutil, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

tmp = tempfile.mkdtemp(prefix="flood_test_")
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
_MOVED = []
for name in ("ambush_track.json", "signals_log.json", "monitor_state.json",
             "scan_summary.json", "dashboard_data.json", "dashboard_data.html"):
    cur = os.path.join(DATA, name)
    if os.path.exists(cur):
        bak = os.path.join(tmp, name + ".bak")
        shutil.move(cur, bak)
        _MOVED.append((bak, cur))

import monitor
import ambush_tracker

_fake_timing = types.ModuleType("timing")
_fake_timing.analyze = lambda sym, market="futures": {"ready": True, "vwap_dist": 0.001, "ema_bull": True}
_fake_timing.regime = lambda market="futures": "bullish"
_fake_timing.env_regime = lambda market="futures": {"r": "bullish", "r4h": "bullish", "r1d": "bullish"}
_fake_timing.REGIME_CN = {"bullish": "多头", "bearish": "空头", "neutral": "中性"}
sys.modules["timing"] = _fake_timing

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)

EVENTS = [{"type": "MA25_BREAK", "ts": "2026-10-18 08:00:00+00:00",
           "price": 0.0532, "note": "close 0.0532 broke 4h MA25"}]

def fake_score_coin(sym, market="futures", force_fetch=False):
    return dict(sym=sym, label="LAUNCHED", stage="S4", score=60, price=1.0,
                vol_tick=False, flat_run=5, range_w=0.10, pv=-0.05,
                swing=2.0, events=list(EVENTS), potential=50.0,
                day_move=0.01, spark=[],
                reason="coiling 40 bars, range 10% wide"), None

monitor.score_coin = fake_score_coin

class FakeNotifier:
    def __init__(self): self.sent = []
    def send(self, title, message, level="info"):
        self.sent.append((title, message, level)); return True

CFG = dict(market="futures", universe="list", symbols=["AAAUSDT"],
           scan_interval_minutes=120, digest_hour=8,
           local_utc_offset_hours=8, request_sleep=0.0, web={})
mon = monitor.Monitor(CFG, FakeNotifier())
STATE_PATH = monitor.STATE_PATH

def strip_event_sigs():
    """Simulate the detection-window flicker: the event drops out of the
    saved event_sigs, so the NEXT scan sees it as brand new."""
    st = json.load(open(STATE_PATH, encoding="utf-8"))
    if "AAAUSDT" in st:
        st["AAAUSDT"]["event_sigs"] = []
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"))

def sig_log():
    p = os.path.join(DATA, "signals_log.json")
    if not os.path.exists(p):
        return []
    return json.load(open(p, encoding="utf-8"))

# ---------- scan 1: baseline (prev=None -> no emission) ----------
mon.run_scan(emit=True)
ev_sends = [s for s in mon.notifier.sent if "信号" in s[0] or "速览" in s[0]]
check("scan1: baseline emits no event signals (digest ok)", not ev_sends)
check("scan1: state written", os.path.exists(STATE_PATH))

# ---------- scan 2: flicker -> event fires ONCE ----------
strip_event_sigs()
mon.run_scan(emit=True)
log2 = [s for s in sig_log() if s.get("level") == "MA25"]
check("scan2: MA25 event fired (first time, into the batch)", len(log2) == 1)
batch2 = [s for s in mon.notifier.sent if "事件速览" in s[0]]
check("scan2: merged digest sent", len(batch2) == 1 and "AAAUSDT" in batch2[0][1])
st = json.load(open(STATE_PATH, encoding="utf-8"))
fired = st.get("_fired_events", {})
check("scan2: fired-event ledger persisted",
      any(k.startswith("AAAUSDT|MA25_BREAK@") for k in fired))

# ---------- scan 3: flicker AGAIN -> must NOT re-fire ----------
strip_event_sigs()
n_sends_before = len(mon.notifier.sent)
n_log_before = len([s for s in sig_log() if s.get("level") == "MA25"])
mon.run_scan(emit=True)
log3 = [s for s in sig_log() if s.get("level") == "MA25"]
check("scan3: identical re-fire SUPPRESSED", len(log3) == n_log_before)
new_sends = mon.notifier.sent[n_sends_before:]
check("scan3: no new messages of any kind for the re-fired event",
      not any("事件速览" in s[0] or "MA25" in s[0] for s in new_sends))

# ---------- scan 4: batched levels never send individually ----------
def many_events_score(sym, market="futures", force_fetch=False):
    evs = [
        {"type": "MA25_BREAK", "ts": "2026-10-19 04:00:00+00:00", "price": 1.0, "note": "broke"},
        {"type": "WICKTOP", "ts": "2026-10-19 04:00:00+00:00", "price": 1.2, "note": "wick 70%"},
        {"type": "SHAKEOUT", "ts": "2026-10-19 04:00:00+00:00", "price": 0.9, "note": "drawdown 31%"},
    ]
    return dict(sym=sym, label="LAUNCHED", stage="S4", score=60, price=1.0,
                vol_tick=False, flat_run=5, range_w=0.10, pv=-0.05,
                swing=2.0, events=evs, potential=50.0, day_move=0.01, spark=[],
                reason="coiling 40 bars, range 10% wide"), None

monitor.score_coin = many_events_score
CFG2 = dict(market="futures", universe="list",
            symbols=["BBBUSDT", "CCCUSDT", "DDDUSDT"],
            scan_interval_minutes=120, digest_hour=8,
            local_utc_offset_hours=8, request_sleep=0.0, web={})
mon2 = monitor.Monitor(CFG2, FakeNotifier())
mon2.run_scan(emit=True)   # baseline
st2 = json.load(open(monitor.STATE_PATH, encoding="utf-8"))
for k in ("BBBUSDT", "CCCUSDT", "DDDUSDT"):
    st2[k]["event_sigs"] = []          # flicker everything in at once
json.dump(st2, open(monitor.STATE_PATH, "w", encoding="utf-8"))
mon2.run_scan(emit=True)

sends = mon2.notifier.sent
individual_event = [s for s in sends if any(x in s[0] for x in ("MA25", "TOP", "DIP"))
                    and "速览" not in s[0]]
check("batch: zero individual MA25/TOP/DIP messages", len(individual_event) == 0)
batch_msgs = [s for s in sends if "事件速览" in s[0]]
check("batch: exactly ONE merged digest message", len(batch_msgs) == 1)
if batch_msgs:
    body = batch_msgs[0][1]
    check("batch: contains all 9 event lines (3 coins x 3 events)",
          body.count("· ") == 9)
    check("batch: message body present", len(body) > 200)
check("batch: signals log keeps all 9 individually",
      len([s for s in sig_log() if s.get("level") in ("MA25", "TOP", "DIP")
           and s.get("sym") in ("BBBUSDT", "CCCUSDT", "DDDUSDT")]) == 9)

# cleanup
for bak, cur in _MOVED:
    if os.path.exists(bak):
        shutil.move(bak, cur)
shutil.rmtree(tmp, ignore_errors=True)
for name in ("monitor_state.json", "scan_summary.json", "dashboard_data.json",
             "dashboard_data.html", "signals_log.json", "ambush_track.json"):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        os.remove(p)

print()
if FAIL:
    print(f"{len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("ALL FLOOD-CONTROL CHECKS PASSED")
