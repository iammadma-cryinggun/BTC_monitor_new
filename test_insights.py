"""Hermetic tests for the conclusion engine (insights.py) + its wiring into
the digest / AMBUSH pushes / Telegram /value command (no network).

The 2026-10-20 feedback this locks in: "我需要类似的结论，而不是大量的推荐——
这样我才能找到有价值的标的埋伏". The monitor must surface EVIDENCE-ranked
conclusions (value board, aging verdicts, historical launch-window base
rates) instead of raw 18-row recommendation tables.

Covers:
  1. launch_window_stats: p1/p3/p7 shares + median over synthetic delays.
  2. launch_window_note: empty below MIN_LW_SAMPLE, carries the base rate above.
  3. coin_history: rounds / wins / best for the re-entry edge.
  4. value_board: re-entry record outranks raw potential; squeeze-fuel and
     timing bonuses; top cap; lite tier tagged.
  5. aging_lines: golden-window / aging split, live-only, historical shares.
  6. monitor integration: AMBUSH push carries the historical conclusion line;
     run_digest leads with the value board + aging and DROPS the raw tables.
  7. telegram routing: /value and natural language reach the board.

Run: python test_insights.py   (exit 0 = pass)
"""
import os, sys, json, time, types, shutil, tempfile
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

tmp = tempfile.mkdtemp(prefix="insights_test_")
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
monitor.CONFIRM_MIN_H = 0   # offline test: scans run back-to-back, kill the wall-clock gate
import ambush_tracker
import insights

_fake_timing = types.ModuleType("timing")
_fake_timing.analyze = lambda sym, market="futures": {"ready": True, "vwap_dist": 0.001, "ema_bull": True}
_fake_timing.regime = lambda market="futures": "bullish"
sys.modules["timing"] = _fake_timing

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)


def _ent(sym, ts, status="win", source="backfill", delay=None, peak=0.5):
    e = dict(sym=sym, ts=ts, source=source, tier="main",
             entry_price=1.0, score=85, status=status,
             launch_delay_d=delay, peak_gain=peak,
             final_ts=ts, last_price=1.0, last_seen=ts)
    return e


# ---------------------------------------------------------------------------
# synthetic history: 25 launched entries with known delay distribution
# ---------------------------------------------------------------------------
DELAYS = [0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.2, 2.5, 2.8, 3.0, 3.5, 4.0, 4.5,
          5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 0.7, 1.1, 2.4, 6.5, 9.5]
hist_entries = [_ent(f"H{i:02d}USDT", "2026-08-01T00:00:00+00:00",
                     status="win", delay=d) for i, d in enumerate(DELAYS)]
# p1 = 4/25, p3 = 13/25, p7 = 20/25, median = 3.0
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
# re-entry coin with two big prior wins + a 2-day-old live call
hist_entries += [
    _ent("ONEUSDT", "2026-05-01T00:00:00+00:00", status="win_strong", peak=5.99, source="backfill"),
    _ent("ONEUSDT", "2026-06-15T00:00:00+00:00", status="win_strong", peak=5.00, source="backfill"),
    dict(sym="ONEUSDT", ts="2026-10-18T00:00:00+00:00", source="live", tier="main",
         entry_price=1.0, score=80, status="waiting", launch_delay_d=None,
         peak_gain=None, final_ts=None, last_price=1.0, last_seen="2026-10-18T00:00:00+00:00"),
]
# aging coin: one failed prior round + a 10-day-old live call
hist_entries += [
    _ent("AGOUSDT", "2026-07-01T00:00:00+00:00", status="pre_fail", peak=-0.25, source="backfill"),
    dict(sym="AGOUSDT", ts="2026-10-10T00:00:00+00:00", source="live", tier="main",
         entry_price=1.0, score=75, status="waiting", launch_delay_d=None,
         peak_gain=None, final_ts=None, last_price=1.0, last_seen="2026-10-10T00:00:00+00:00"),
]
# stale backfill waiting entry — must NOT appear in aging verdicts
hist_entries.append(dict(sym="BACKUSDT", ts="2026-07-10T00:00:00+00:00",
                         source="backfill", tier="main", entry_price=1.0, score=70,
                         status="waiting", launch_delay_d=None, peak_gain=None,
                         final_ts=None, last_price=1.0, last_seen="2026-07-10T00:00:00+00:00"))

# ---------- 1. launch_window_stats ----------
lw = insights.launch_window_stats(hist_entries)
check("lw: n=25", lw["n"] == 25)
check("lw: p1 = 4/25", lw["p1"] == round(4 / 25, 3))
check("lw: p3 = 13/25", lw["p3"] == round(13 / 25, 3))
check("lw: p7 = 20/25", lw["p7"] == round(20 / 25, 3))
check("lw: median = 3.0", lw["median"] == 3.0)
lw_empty = insights.launch_window_stats([])
check("lw: empty input is safe", lw_empty["n"] == 0 and lw_empty["p3"] is None)

# ---------- 2. launch_window_note ----------
note_small = insights.launch_window_note(insights.launch_window_stats(hist_entries[:5]))
check("note: empty below MIN_LW_SAMPLE", note_small == "")
note = insights.launch_window_note(lw)
check("note: carries base rate above sample floor",
      "历史结论" in note and "52%" in note and "25 笔样本" in note)

# ---------- 3. coin_history ----------
h1 = insights.coin_history(hist_entries, "ONEUSDT")
check("coin_history: ONE 2 rounds / 2 wins / best 5.99",
      h1["rounds"] == 2 and h1["wins"] == 2 and h1["best"] == 5.99)
h2 = insights.coin_history(hist_entries, "AGOUSDT")
check("coin_history: AGO 1 round / 0 wins / best -0.25",
      h2["rounds"] == 1 and h2["wins"] == 0 and h2["best"] == -0.25)
h3 = insights.coin_history(hist_entries, "NOPEUSDT")
check("coin_history: unknown coin = zero rounds", h3["rounds"] == 0)

# ---------- 4. value_board ----------
def cand(sym, potential=50, label="COILING", funding=None, oi=None,
         vol_tick=False, timing=None):
    return dict(sym=sym, label=label, score=80, price=0.001, potential=potential,
                funding=funding, oi_chg_24h=oi, vol_tick=vol_tick, timing=timing)

ranked = [
    cand("ONEUSDT", potential=50, vol_tick=True,
         timing={"ready": True, "vwap_dist": 0.001}),
    cand("NEWUSDT", potential=70),
    cand("SQUEEUSDT", potential=65, funding=-0.002, oi=0.22),
    cand("AGOUSDT", potential=60),
    cand("LITEUSDT", potential=58, label="COILING_LITE"),
    cand("EXTRA1USDT", potential=40),
    cand("EXTRA2USDT", potential=30),
]
rows, n_c = insights.value_board(ranked, hist_entries, top=5)
check("board: 7 candidates detected", n_c == 7)
check("board: capped at top=5", len(rows) == 5)
# ONE: 50 + 30(last-round win, flat) + 8(timing) + 5(vol_tick) = 93 -> first
check("board: re-entry record outranks raw potential",
      rows[0]["sym"] == "ONEUSDT" and rows[0]["pts"] == 93)
# SQUEE: 65 + 10(fuel) = 75 > NEW 70
check("board: squeeze fuel adds points",
      rows[1]["sym"] == "SQUEEUSDT" and rows[1]["pts"] == 75)
# NEW: 70, no bonuses
row_new = next(r for r in rows if r["sym"] == "NEWUSDT")
check("board: fresh face keeps raw potential", row_new["pts"] == 70)
row_one = rows[0]
check("board: ONE reason cites the re-entry evidence",
      any("第3轮重入" in s and "2/2" in s and "+599%" in s for s in row_one["reasons"]))
row_squee = rows[1]
check("board: squeeze reason present", any("逼空燃料" in s for s in row_squee["reasons"]))
row_new_reasons = " ".join(row_new["reasons"])
check("board: no-history candidate says 新面孔", "新面孔" in row_new_reasons)
lite_row = next(r for r in rows if r["sym"] == "LITEUSDT")
check("board: lite tier tagged", lite_row["tier"] == "lite")

# ---------- 4a-bis. recency-based re-entry scoring (2026-10-20 review) ----------
rows_full, _ = insights.value_board(ranked, hist_entries, top=10)
row_ago = next(r for r in rows_full if r["sym"] == "AGOUSDT")
check("board: last-round loss penalized -15",
      row_ago["pts"] == 45 and any("连败 1 轮" in s for s in row_ago["reasons"]))
check("board: lose note carries warning marker", "⚠️连败1轮" in row_ago["note"])
lose3 = [_ent("L3USDT", "2026-06-%02dT00:00:00+00:00" % (1 + i * 5),
              status="pre_fail", peak=-0.21) for i in range(3)]
rows_l3, _ = insights.value_board([cand("L3USDT", potential=60)],
                                  hist_entries + lose3, top=1)
check("board: losing streak >=2 penalized -25 (3 straight losses)",
      rows_l3[0]["pts"] == 35)
mixed = [_ent("MXUSDT", "2026-05-01T00:00:00+00:00", status="win", peak=0.5),
         _ent("MXUSDT", "2026-06-01T00:00:00+00:00", status="pre_fail", peak=-0.2)]
rows_mx, _ = insights.value_board([cand("MXUSDT", potential=60)],
                                  hist_entries + mixed, top=1)
check("board: win-then-lose counts as a loser (recency beats aggregate)",
      rows_mx[0]["pts"] == 45 and "连败 1 轮" in " ".join(rows_mx[0]["reasons"]))
lastwin = [_ent("LWUSDT", "2026-05-01T00:00:00+00:00", status="pre_fail", peak=-0.2),
           _ent("LWUSDT", "2026-06-01T00:00:00+00:00", status="win", peak=0.5)]
rows_lw, _ = insights.value_board([cand("LWUSDT", potential=60)],
                                  hist_entries + lastwin, top=1)
check("board: lose-then-win gets the flat +30 (streak length irrelevant)",
      rows_lw[0]["pts"] == 90)

# ---------- 4c. record_entry captures the full feature set ----------
ambush_tracker.save({"entries": []})
rec_full = dict(sym="FEATUSDT", price=0.5, score=88, flat_run=40, range_w=0.08,
                pv=-0.05, ma99_1h=0.49, potential=72.0, funding=-0.001,
                oi_chg_24h=0.15, vol_tick=True,
                timing={"ready": True, "vwap_dist": 0.002})
saved_ent = ambush_tracker.record_entry(rec_full)
check("record_entry: full feature set captured",
      saved_ent is not None and saved_ent.get("potential") == 72.0
      and saved_ent.get("funding") == -0.001 and saved_ent.get("oi_chg_24h") == 0.15
      and saved_ent.get("vol_tick") is True and saved_ent.get("timing_ready") is True)
check("record_entry: legacy fields intact",
      saved_ent.get("flat_run") == 40 and saved_ent.get("range_w") == 0.08
      and saved_ent.get("pv") == -0.05)

bl = insights.board_lines(rows, n_c, lw)
text_bl = "\n".join(bl)
check("board_lines: header + numbered items + method line",
      "价值榜" in bl[0] and " 1. " in text_bl and " 5. " in text_bl
      and "排序口径" in text_bl and "52%" in text_bl)
check("board_lines: lite tag rendered", "[次级] LITEUSDT" in text_bl)

# ---------- 4a-bis-2. compact digest shortlist: certainty-first (2026-10-21
# 用户指令：不设固定条数，只推确定性高的；资金有限，没有就直说空仓) ----------
c_conf = dict(cand("CONFUSDT", potential=50, vol_tick=True),
              vt_confirmed=True, timing={"ready": True, "vwap_dist": 0.001})
rows_cf, _ = insights.value_board([c_conf] + ranked, hist_entries, top=10)
bl_conf = insights.board_lines(rows_cf, 10, compact=True)
check("compact: confirmed coin leads the shortlist",
      bool(bl_conf) and "高确定性入场" in bl_conf[0]
      and "CONFUSDT" in "\n".join(bl_conf) and "现在可打" in "\n".join(bl_conf))
rows_nc, _ = insights.value_board(ranked, hist_entries, top=10)
bl_strong = insights.board_lines(rows_nc, 10, compact=True)
check("compact: no confirm -> single strongest watcher only",
      bool(bl_strong) and "最接近的观察标的" in bl_strong[0]
      and "ONEUSDT" in bl_strong[-1] and len(bl_strong) == 2)
rows_new, _ = insights.value_board([cand("NEWUSDT", potential=60)], hist_entries, top=5)
bl_flat = insights.board_lines(rows_new, 1, compact=True)
check("compact: no hard evidence -> flat-cash line, no coin names",
      bool(bl_flat) and "空仓等待" in bl_flat[0] and "NEWUSDT" not in "\n".join(bl_flat))

# ---------- 4b. compact note + attach_evidence (dashboard/ambush ordering) ----------
rows_full, _ = insights.value_board(ranked, hist_entries, top=10)
row_one_full = next(r for r in rows_full if r["sym"] == "ONEUSDT")
check("board: compact note present",
      "2/2轮胜" in row_one_full["note"] and "+599%" in row_one_full["note"])

ranked2 = [dict(c) for c in ranked]
insights.attach_evidence(ranked2, hist_entries)
one_rec = next(r for r in ranked2 if r["sym"] == "ONEUSDT")
check("attach: ev_pts/ev_note set on coiling rec",
      one_rec.get("ev_pts") == 93 and "2/2轮胜" in one_rec.get("ev_note", "")
      and "上轮胜" in one_rec.get("ev_note", ""))
new_rec = next(r for r in ranked2 if r["sym"] == "NEWUSDT")
check("attach: fresh coin gets 新面孔 note", "新面孔" in new_rec.get("ev_note", ""))
ranked2.append(dict(sym="PUMPUSDT", label="PUMPED", score=40, price=1.0,
                    potential=None, vol_tick=False, timing=None))
insights.attach_evidence(ranked2, hist_entries)
pump_rec = next(r for r in ranked2 if r["sym"] == "PUMPUSDT")
check("attach: non-coiling recs get NO ev fields", pump_rec.get("ev_pts") is None)

# ---------- 5. aging_lines ----------
ag = insights.aging_lines(hist_entries, lw=lw, now=NOW)
ag_text = "\n".join(ag)
check("aging: header present", ag and "在途埋伏老化结论" in ag[0] and "2 笔" in ag[0])
check("aging: ONE in golden window with historical share",
      "黄金窗" in ag_text and "ONEUSDT" in ag_text and "52%" in ag_text)
check("aging: AGO flagged as aging with waited days",
      "老化" in ag_text and "AGOUSDT" in ag_text and "已等10天" in ag_text)
check("aging: backfill waiting entries excluded", "BACKUSDT" not in ag_text)
check("aging: empty when no live calls",
      insights.aging_lines([_ent("XUSDT", "2026-08-01T00:00:00+00:00")],
                            lw=lw, now=NOW) == [])

# ---------------------------------------------------------------------------
# 6. monitor integration (hermetic: offline, stubbed timing + score_coin)
# ---------------------------------------------------------------------------
ambush_tracker.save({"entries": hist_entries})

def fake_score_coin(sym, market="futures", force_fetch=False):
    return dict(sym=sym, label="COILING", stage="S1", score=80, price=0.001,
                vol_tick=True, flat_run=30, range_w=0.12, pv=0.02,
                swing=2.0, events=[], potential=60.0, day_move=0.0, spark=[],
                reason="coiling 30 bars"), None

monitor.score_coin = fake_score_coin

class FakeNotifier:
    def __init__(self): self.sent = []
    bot_token, chat_id = "x", "1"
    def send(self, title, message, level="info"):
        self.sent.append((title, message, level)); return True

CFG = dict(market="futures", universe="list", symbols=["TESTUSDT"],
           scan_interval_minutes=120, digest_hour=8,
           local_utc_offset_hours=8, request_sleep=0.0, web={})
mon = monitor.Monitor(CFG, FakeNotifier())
mon.run_scan(emit=True)   # scan 1: baseline (vt_streak 0 -> 1), digest fires
mon.run_scan(emit=True)   # scan 2: streak crosses 2 -> AMBUSH fires

# title uses the CN display name (SIG_TITLE, 2026-10-21): 埋伏触发·可入场
ambush_msgs = [s for s in mon.notifier.sent if "埋伏触发·可入场" in s[0]]
check("monitor: AMBUSH fired on streak cross", len(ambush_msgs) == 1)
if ambush_msgs:
    check("monitor: AMBUSH push carries the historical conclusion",
          "历史结论" in ambush_msgs[0][1] and "52%" in ambush_msgs[0][1])

mon.run_digest()
digests = [s for s in mon.notifier.sent if "每日妖币监控" in s[0]]
check("monitor: digest sent", bool(digests))
dtxt = digests[-1][1] if digests else ""
check("monitor: digest leads with the confirmed-entry shortlist",
      "高确定性入场" in dtxt and "TESTUSDT" in dtxt)
check("monitor: digest contains aging verdicts", "在途老化" in dtxt)
check("monitor: digest has the compact rest line", "其余：" in dtxt)
check("monitor: raw 18-row watchlist table REMOVED", "埋伏观察列表" not in dtxt)
check("monitor: raw lite table REMOVED", "次级蓄力（大盘币新周期横盘，仓位小于主档" not in dtxt)
check("monitor: raw fresh/launched/risk tables REMOVED",
      "近期启动（做 S3→S4）" not in dtxt and "已启动动能衰减" not in dtxt
      and "需警惕（已拉完" not in dtxt)
check("monitor: digest is conclusion-sized (not a wall of rows)", len(dtxt) < 2500)

# ---------- 7. telegram routing ----------
from telegram_agent import TelegramAgent
ag_bot = TelegramAgent(FakeNotifier(), mon)
r1 = ag_bot.answer("/value")
check("tg: /value returns the board", "价值榜" in (r1 or ""))
r2 = ag_bot.answer("值得埋谁")
check("tg: natural '值得埋谁' routes to the board", "价值榜" in (r2 or ""))
r3 = ag_bot.answer("有什么结论")
check("tg: natural '结论' routes to the board", "价值榜" in (r3 or ""))
r4 = ag_bot.answer("价值榜")
check("tg: '价值榜' routes to the board", "价值榜" in (r4 or ""))
check("tg: aging verdicts included in /value", "老化" in (r1 or ""))
check("tg: board same source as digest (TESTUSDT is a fresh face there)",
      "TESTUSDT" in (r1 or ""))

# ---------- 7b. evidence ordering everywhere (web + /ambush) ----------
rec_t = next((r for r in (mon.last_ranked or []) if r["sym"] == "TESTUSDT"), None)
check("monitor: scan enriched recs with ev_pts/ev_note",
      rec_t is not None and isinstance(rec_t.get("ev_pts"), (int, float))
      and "新面孔" in rec_t.get("ev_note", ""))
try:
    dd = json.load(open(os.path.join(DATA, "dashboard_data.json"), encoding="utf-8"))
    rec_dd = next((r for r in dd.get("ranked", []) if r["sym"] == "TESTUSDT"), None)
    check("dashboard: live data json carries ev_pts",
          rec_dd is not None and isinstance(rec_dd.get("ev_pts"), (int, float)))
    html_txt = open(os.path.join(DATA, "dashboard.html"), encoding="utf-8").read()
    check("dashboard: static html has 证据分 column + ev note",
          "证据分" in html_txt and "新面孔" in html_txt)
except FileNotFoundError:
    check("dashboard: files written", False)

ranked3 = [
    cand("NEWUSDT", potential=70),
    cand("ONEUSDT", potential=50, vol_tick=True,
         timing={"ready": True, "vwap_dist": 0.001}),
]
insights.attach_evidence(ranked3, hist_entries)

class FakeMon2:
    last_ranked = ranked3

ag_bot2 = TelegramAgent(FakeNotifier(), FakeMon2())
r5 = ag_bot2.ans_ambush()
coin_lines = [l for l in (r5 or "").split("\n")
              if "NEWUSDT" in l or "ONEUSDT" in l]
check("tg: /ambush evidence-ordered (re-entry coin above raw potential)",
      bool(coin_lines) and "ONEUSDT" in coin_lines[0])
check("tg: /ambush rows carry the evidence note",
      "证据" in (r5 or "") and "2/2轮胜" in (r5 or ""))

# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------
for bak, cur in _MOVED:
    if os.path.exists(bak):
        shutil.move(bak, cur)
shutil.rmtree(tmp, ignore_errors=True)
for name in ("monitor_state.json", "scan_summary.json", "dashboard_data.json",
             "dashboard.html", "dashboard_data.html", "signals_log.json",
             "ambush_track.json"):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        os.remove(p)

print()
if FAIL:
    print(f"{len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("ALL INSIGHTS CHECKS PASSED")
