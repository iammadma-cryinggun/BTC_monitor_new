"""Hermetic smoke test for the total-outage resilience fixes (no network).

Incident that motivated this (2026-10-19): a Zeabur redeploy landed the service
on an egress IP Binance refuses; every scan round came back 0/519 and the user
only noticed because Telegram went quiet. The dashboard blanked, the monitor
sat in the dark for a full 2h interval, and nothing ever SAID the feed died.

Covers:
  1. a total-failure round KEEPS the last good watchlist (dashboard stays
     readable instead of blanking); state file is preserved untouched.
  2. outage alert: exactly ONE Telegram alert on transition into failure,
     silence on subsequent failing rounds, ONE recovery note when back.
  3. fast retry: _effective_interval_s() drops 120min -> 20min while the feed
     is down, back to 120min after recovery; next_scan_epoch follows it.
  4. connectivity diagnostics run on a failed round (stubbed, counted).

Run: python test_outage_resilience.py  (exit 0 = pass)
"""
import os, sys, json, time, tempfile, shutil, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

# --- isolate all data dirs BEFORE importing the modules under test ---
tmp = tempfile.mkdtemp(prefix="outage_test_")
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
import memecoin_scanner
import ambush_tracker

# hermetic: stub the 15m timing module BEFORE run_scan imports it
_fake_timing = types.ModuleType("timing")
_fake_timing.analyze = lambda sym, market="futures": {"ready": True, "vwap_dist": 0.001, "ema_bull": True}
_fake_timing.regime = lambda market="futures": "bullish"
sys.modules["timing"] = _fake_timing

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)

# ---- stubs: controllable score_coin + connectivity probe + notifier ----
MODE = {"ok": True}          # flip to simulate the outage
PRICE = {"AAAUSDT": 1.00, "BBBUSDT": 2.00}
CONN_CALLS = {"n": 0}

def fake_score_coin(sym, market="futures", force_fetch=False):
    if not MODE["ok"]:
        return None, "fetch fail: timed out"
    return dict(sym=sym, label="COILING", stage="S1", score=95, price=PRICE[sym],
                vol_tick=False, flat_run=40, range_w=0.10, pv=-0.05,
                swing=2.0, events=[], potential=50.0), None

def fake_connectivity(market="futures"):
    CONN_CALLS["n"] += 1
    return {"fapi": "ERR:URLError"}

monitor.score_coin = fake_score_coin
memecoin_scanner.connectivity_check = fake_connectivity

class FakeNotifier:
    def __init__(self): self.sent = []
    def send(self, title, message, level="info"):
        self.sent.append((title, level))
        return True

CFG = dict(market="futures", universe="list", symbols=["AAAUSDT", "BBBUSDT"],
           scan_interval_minutes=120, digest_hour=8,
           local_utc_offset_hours=8, request_sleep=0.0, web={})

mon = monitor.Monitor(CFG, FakeNotifier())

def alerts():
    """Alert-level sends only — the first run_scan also fires the initial
    daily digest (level='digest'), which is expected and not an alert."""
    return [s for s in mon.notifier.sent if s[1] == "alert"]

STATE_PATH = monitor.STATE_PATH

# ---------- round 1: healthy baseline ----------
mon.run_scan(emit=True)
check("round1: 2/2 ok", mon.scan_summary.get("ok") == 2)
check("round1: last_ranked populated", len(mon.last_ranked) == 2)
check("round1: no alerts", len(alerts()) == 0)
check("round1: feed flag True", mon._feed_was_ok is True)
check("round1: interval 120min", mon._effective_interval_s() == 120 * 60)

# ---------- round 2: TOTAL outage ----------
good_ranked = list(mon.last_ranked)
state_before = json.load(open(STATE_PATH, encoding="utf-8"))
MODE["ok"] = False
mon.run_scan(emit=True)
check("round2: 0/2 ok", mon.scan_summary.get("ok") == 0)
check("round2: watchlist KEPT (not blanked)", mon.last_ranked is good_ranked or
      [r["sym"] for r in mon.last_ranked] == ["AAAUSDT", "BBBUSDT"])
check("round2: state file preserved", json.load(open(STATE_PATH, encoding="utf-8")) == state_before)
check("round2: exactly ONE outage alert", len(alerts()) == 1
      and "数据断流" in alerts()[0][0])
check("round2: fast retry 20min", mon._effective_interval_s() == 20 * 60)
nxt = mon.next_scan_epoch()
import datetime as _dt
last = _dt.datetime.fromisoformat(mon.last_scan_iso).timestamp()
check("round2: countdown reflects fast retry", 0 < nxt - last <= 20 * 60 + 5)
check("round2: connectivity probed", CONN_CALLS["n"] >= 1)

# ---------- round 3: still down -> no repeat alert ----------
mon.run_scan(emit=True)
check("round3: still no extra alerts", len(alerts()) == 1)

# ---------- round 4: recovery ----------
MODE["ok"] = True
PRICE["AAAUSDT"] = 1.10  # price moved -> proves ranked got refreshed
mon.run_scan(emit=True)
check("round4: 2/2 ok again", mon.scan_summary.get("ok") == 2)
check("round4: ranked refreshed with new price",
      any(r["price"] == 1.10 for r in mon.last_ranked if r["sym"] == "AAAUSDT"))
check("round4: exactly ONE recovery alert", len(alerts()) == 2
      and "数据恢复" in alerts()[1][0])
check("round4: interval back to 120min", mon._effective_interval_s() == 120 * 60)
check("round4: feed flag True", mon._feed_was_ok is True)

# ---------- cleanup ----------
for bak, cur in _MOVED:
    if os.path.exists(bak):
        shutil.move(bak, cur)
shutil.rmtree(tmp, ignore_errors=True)
# fresh files created by the test rounds (no .bak existed for them)
for name in ("monitor_state.json", "scan_summary.json", "dashboard_data.json",
             "dashboard_data.html", "signals_log.json", "ambush_track.json"):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        os.remove(p)

print()
if FAIL:
    print(f"{len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("ALL OUTAGE-RESILIENCE CHECKS PASSED")
