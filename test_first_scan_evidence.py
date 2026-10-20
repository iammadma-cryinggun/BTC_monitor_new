"""Hermetic regression test: the first scan after a restart must carry FULL
evidence everywhere (2026-10-20 12:47 incident).

Symptom the user caught: right after a redeploy/restart with an empty
tracker, the initial Telegram digest showed "第4轮重入：前 2/3 轮胜" (it
recomputes value_board from the tracker AFTER the backfill) while the web
dashboard showed "新面孔·无战绩" for the SAME coins in the SAME scan — the
ev fields had been attached BEFORE the backfill ran. Two views of one scan,
two different conclusions.

Root cause: the first_run backfill ran at the END of run_scan, after every
evidence consumer had already read the empty tracker.

Fix under test: the backfill now runs right after the derivatives pass (the
scan has just written the kline CSVs), BEFORE tracked_map / ambush_note /
attach_evidence. Assertions:

  1. FIRST scan (empty tracker + stubbed backfill): last_ranked ev_note is
     the real re-entry record, NOT 新面孔; dashboard_data.json carries ev.
  2. The initial digest's value board and the dashboard's ev_note agree
     (same scan, same conclusion).
  3. backfill runs exactly once (not again on the second scan).
  4. AMBUSH on the second scan carries the historical launch-window note.

Run: python test_first_scan_evidence.py   (exit 0 = pass)
"""
import os, sys, json, types, shutil, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

tmp = tempfile.mkdtemp(prefix="firstscan_test_")
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
_MOVED = []
for name in ("ambush_track.json", "signals_log.json", "monitor_state.json",
             "scan_summary.json", "dashboard_data.json", "dashboard_data.html",
             "dashboard.html"):
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
sys.modules["timing"] = _fake_timing

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)


# ---------------------------------------------------------------------------
# stub backfill: populates the tracker the way the real one would (from the
# kline CSVs this scan just wrote) — 25 launched samples for the base-rate
# note + two prior finalized rounds for the coin under test.
# ---------------------------------------------------------------------------
BACKFILL_CALLS = []

def _hist_ent(sym, ts, status, delay, peak):
    return dict(sym=sym, ts=ts, source="backfill", tier="main",
                entry_price=1.0, score=85, status=status,
                launch_delay_d=delay, peak_gain=peak,
                final_ts=ts, last_price=1.0, last_seen=ts)

def fake_backfill(market="futures", quiet=False):
    BACKFILL_CALLS.append(market)
    entries = [_hist_ent(f"H{i:02d}USDT", "2026-08-01T00:00:00+00:00",
                         "win", 1.0, 0.5) for i in range(25)]
    entries += [
        _hist_ent("TESTUSDT", "2026-06-01T00:00:00+00:00", "win", 2.0, 0.60),
        _hist_ent("TESTUSDT", "2026-07-15T00:00:00+00:00", "miss", 20.0, -0.10),
    ]
    ambush_tracker.save({"entries": entries})

ambush_tracker.backfill = fake_backfill


def fake_score_coin(sym, market="futures", force_fetch=False):
    return dict(sym=sym, label="COILING", stage="S1", score=80, price=0.001,
                vol_tick=True, flat_run=30, range_w=0.12, pv=0.02,
                swing=2.0, events=[], potential=60.0, day_move=0.0, spark=[],
                reason="coiling 30 bars"), None

monitor.score_coin = fake_score_coin


class FakeNotifier:
    def __init__(self):
        self.sent = []
    def send(self, title, message, level="info"):
        self.sent.append((title, message, level))
        return True


try:
    ambush_tracker.save({"entries": []})   # restart with a LOST tracker
    CFG = dict(market="futures", universe="list", symbols=["TESTUSDT"],
               scan_interval_minutes=120, digest_hour=8,
               local_utc_offset_hours=8, request_sleep=0.0, web={})
    mon = monitor.Monitor(CFG, FakeNotifier())

    # ---- FIRST scan: the incident scenario (emit=True + first_run) ----
    mon.run_scan(emit=True)

    check("first scan: backfill ran exactly once", len(BACKFILL_CALLS) == 1)

    rec = next((r for r in (mon.last_ranked or []) if r["sym"] == "TESTUSDT"), None)
    check("first scan: candidate still in ranked", rec is not None)
    if rec:
        check("first scan: ev attached on the FIRST scan (not 新面孔)",
              "新面孔" not in (rec.get("ev_note") or "") and bool(rec.get("ev_note")))
        check("first scan: ev_note carries the real re-entry record",
              "1/2轮胜" in (rec.get("ev_note") or ""))
        check("first scan: ev_pts beats the (recomputed) potential by >= 30 re-entry bonus",
              isinstance(rec.get("ev_pts"), (int, float)) and isinstance(rec.get("potential"), (int, float))
              and rec["ev_pts"] >= rec["potential"] + 30 - 0.1)

    dd_path = os.path.join(DATA, "dashboard_data.json")
    html_path = os.path.join(DATA, "dashboard.html")
    check("first scan: dashboard data written", os.path.exists(dd_path))
    if os.path.exists(dd_path):
        dd = json.load(open(dd_path, encoding="utf-8"))
        drec = next((r for r in dd.get("ranked", []) if r["sym"] == "TESTUSDT"), None)
        check("first scan: dashboard JSON carries ev fields from the FIRST scan",
              drec is not None and "1/2轮胜" in (drec.get("ev_note") or ""))
    if os.path.exists(html_path):
        html_txt = open(html_path, encoding="utf-8").read()
        check("first scan: static HTML snapshot agrees (no 新面孔 for TESTUSDT)",
              "1/2轮胜" in html_txt)

    # digest and dashboard must agree — the original symptom was divergence
    digests = [m for t, m, l in mon.notifier.sent if "埋伏价值榜" in m]
    check("first scan: initial digest sent", len(digests) == 1)
    if digests:
        check("CONSISTENCY: digest board and dashboard ev say the same thing",
              ("第3轮重入" in digests[0] and "前 1/2 轮" in digests[0]
               and rec is not None and "1/2轮胜" in (rec.get("ev_note") or "")))

    # ---- SECOND scan: no re-backfill, AMBUSH fires with the base-rate note ----
    mon.run_scan(emit=True)
    check("second scan: backfill NOT called again", len(BACKFILL_CALLS) == 1)

    amb = [m for t, m, l in mon.notifier.sent
           if "AMBUSH" in t and "TESTUSDT" in m]
    check("second scan: AMBUSH fired (vt_streak crossed)", len(amb) >= 1)
    if amb:
        check("second scan: AMBUSH carries the historical launch-window note",
              "历史结论" in amb[0])

    total = 0
finally:
    for bak, cur in _MOVED:
        if os.path.exists(bak):
            shutil.move(bak, cur)
    for name in ("monitor_state.json", "scan_summary.json", "dashboard_data.json",
                 "dashboard_data.html", "dashboard.html", "signals_log.json",
                 "ambush_track.json"):
        p = os.path.join(DATA, name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAIL:
    print(f"FAILED {len(FAIL)} checks:")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
