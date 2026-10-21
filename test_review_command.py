# -*- coding: utf-8 -*-
"""Offline test for /review (win-rate accountability report).

2026-10-21 user directive: "复盘是为了计算胜率，验证你这套程序是不是成功".
Covers insights.win_rate_review math (outcome distribution / overall /
monthly / conditional re-entry rates) on a synthetic tracker, plus the
TelegramAgent routing (/review, 复盘, 胜率) and the report formatting.

Run: python test_review_command.py   (exit 0 = pass)
"""
import os, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

import insights
import ambush_tracker
import telegram_agent

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)

def E(sym, ts, status, peak=None, src="live", delay=None):
    return dict(sym=sym, ts=ts, status=status, peak_gain=peak,
                source=src, launch_delay_d=delay, entry_price=1.0)

entries = [
    # coin A: win, win, miss
    E("AAAUSDT", "2026-08-01T00:00:00", "win", 0.50),
    E("AAAUSDT", "2026-08-15T00:00:00", "win_strong", 0.90),
    E("AAAUSDT", "2026-09-01T00:00:00", "miss", 0.05),
    # coin B: pre_fail, then win
    E("BBBUSDT", "2026-08-05T00:00:00", "pre_fail", -0.22),
    E("BBBUSDT", "2026-09-10T00:00:00", "win", 0.35),
    # coin C: timeout only
    E("CCCUSDT", "2026-09-20T00:00:00", "timeout", 0.00, src="backfill"),
    # coin D: lose, lose, win  (lose2p bucket)
    E("DDDUSDT", "2026-08-02T00:00:00", "miss", 0.02, src="backfill"),
    E("DDDUSDT", "2026-09-05T00:00:00", "timeout", 0.00, src="backfill"),
    E("DDDUSDT", "2026-09-25T00:00:00", "win", 0.44, src="backfill"),
    # one open entry: not counted in any settled cut
    E("EEEUSDT", "2026-10-01T00:00:00", "waiting"),
]

rv = insights.win_rate_review(entries)
check("total counts open entries", rv["total"] == 10)
check("evaluated = settled only", rv["evaluated"] == 9)
check("open_waiting tracked", rv["open_waiting"] == 1)
# overall: wins = A1,A2,B2,D3 = 4 of 9
check("overall rate 4/9", rv["overall"]["rate"] == round(4 / 9, 3))
# discipline EV (2026-10-21): win/win_strong=+0.30, weak=+0.10,
# pre_fail=-0.20, miss/timeout=actual peak.
#   A1 win .30 | A2 strong .30 | A3 miss .05 | B1 fail -.20 | B2 win .30
#   C timeout .00 | D1 miss .02 | D2 timeout .00 | D3 win .30  => sum 1.07
check("overall EV (discipline lens) +0.119", rv["overall"]["ev"] == round(1.07 / 9, 3))
# dist: win = A1,B2,D3 ; win_strong = A2 ; miss = A3,D1 ; timeout = C,D2 ; pre_fail = B1
check("dist win=3 win_strong=1 miss=2 timeout=2 pre_fail=1",
      rv["dist"].get("win") == 3 and rv["dist"].get("win_strong") == 1
      and rv["dist"].get("miss") == 2 and rv["dist"].get("timeout") == 2
      and rv["dist"].get("pre_fail") == 1)
# by source: live = A1,A2,A3,B1,B2 (5, wins 3); backfill = C,D1,D2,D3 (4, wins 1)
check("live cut 3/5", rv["by_src"]["live"]["rate"] == 0.6)
check("backfill cut 1/4", rv["by_src"]["backfill"]["rate"] == 0.25)
# monthly: 08 -> A1,A2,B1,D1 = 4, wins 2 ; 09 -> A3,B2,C,D2,D3 = 5, wins 2
# (fixture months are tiny — lower the noise floor so they still show)
insights.MIN_MONTH_N = 1
rv = insights.win_rate_review(entries)
check("monthly 08 = 2/4", rv["monthly"]["2026-08"]["rate"] == 0.5)
check("monthly 09 = 2/5", rv["monthly"]["2026-09"]["rate"] == 0.4)
# default floor 30: a 4/5-sample month is filtered as noise
insights.MIN_MONTH_N = 30
rv30 = insights.win_rate_review(entries)
check("monthly noise filtered at default floor",
      "2026-08" not in rv30["monthly"] and "2026-09" not in rv30["monthly"])
insights.MIN_MONTH_N = 1
# conditional:
#  fresh:    A1, B1, C1, D1                     -> n=4, wins: A1 -> 1/4
#  last_win: A2 (prev A1 win), A3 (prev A2 win) -> n=2, wins: A2 -> 1/2
#  lose1:    B2 (prev B1 lose, streak1), D2 (prev D1 lose, streak1) -> n=2, wins: B2 -> 1/2
#  lose2p:   D3 (D2,D1 both lose -> streak2)     -> n=1, wins: D3 -> 1/1
c = rv["cond"]
check("cond fresh 1/4", c["fresh"]["n"] == 4 and c["fresh"]["wins"] == 1)
check("cond last_win 1/2", c["last_win"]["n"] == 2 and c["last_win"]["wins"] == 1)
check("cond lose1 1/2", c["lose1"]["n"] == 2 and c["lose1"]["wins"] == 1)
check("cond lose2p 1/1", c["lose2p"]["n"] == 1 and c["lose2p"]["wins"] == 1)

# ---------------- agent-level: routing + formatting ----------------
class FakeNotifier:
    bot_token, chat_id = "x", "1"

class FakeMonitor:
    market, last_ranked = "futures", []

# point the tracker at the synthetic entries
_orig_load = ambush_tracker.load
ambush_tracker.load = lambda: {"entries": entries}
ag = telegram_agent.TelegramAgent(FakeNotifier(), FakeMonitor())

s = ag.answer("/review")
check("route /review", "胜率复盘" in s and "结局分布" in s)
check("report shows overall rate", "整体胜率" in s)
check("report shows conditional rates", "条件胜率" in s)
check("report shows monthly trend", "按月胜率" in s)
check("report carries the honesty caveat", "未计滑点" in s)
check("report carries the EV line", "每笔期望" in s and "纪律口径" in s)
check("report mentions live/backfill split", "实盘" in s and "回填" in s)

s2 = ag.answer("帮我复盘一下")
check("route natural 复盘", "胜率复盘" in s2)
s3 = ag.answer("现在的胜率是多少")
check("route natural 胜率", "胜率复盘" in s3)
s4 = ag.answer("/track")
check("/track still works (not hijacked)", "埋伏" in s4 or "自检" in s4 or "追踪" in s4)

# empty-settled path
ambush_tracker.load = lambda: {"entries": [E("ZZZUSDT", "2026-10-01T00:00:00", "waiting")]}
s5 = ag.answer("/review")
check("no-settled guard message", "还没有已结算" in s5)

ambush_tracker.load = _orig_load

print()
if FAIL:
    print("%d FAILED: %s" % (len(FAIL), FAIL))
    sys.exit(1)
print("ALL REVIEW-COMMAND CHECKS PASSED")
