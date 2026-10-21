# -*- coding: utf-8 -*-
"""Offline check: action-first /ambush (2026-10-21) — no network, fake monitor."""
import sys, types
sys.path.insert(0, "D:/memecoin-monitor")

import telegram_agent as ta

class FakeNotifier:
    bot_token, chat_id = "x:y", 123

class FakeMonitor:
    last_ranked = []

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)

ag = ta.TelegramAgent(FakeNotifier(), FakeMonitor())

def coin(sym, label, vol=False, streak=0, ready=None, ev=90, note="1/1轮胜·上轮胜·最佳+50%"):
    r = dict(sym=sym, label=label, vol_tick=vol, vt_streak=streak, price=0.01,
             potential=50, score=70, ev_pts=ev, ev_note=note, pv=-0.05,
             funding=-0.001, oi_chg_24h=0.05, flat_run=100, swing=1.2)
    if ready is not None:
        r["timing"] = {"ready": ready}
    return r

# 1) 无 vol_tick：默认返回"当前无埋伏信号"
FakeMonitor.last_ranked = [coin("AAAUSDT", "COILING"), coin("BBBUSDT", "COILING_LITE")]
s = ag.ans_ambush()
check("no-signal default says 无埋伏信号", "当前无埋伏信号" in s and "AAAUSDT" not in s)

# 2) full=True 仍给旧列表；星标分层：★=确认（streak>=2），＋=量能首现
FakeMonitor.last_ranked = [coin("AAAUSDT", "COILING"),
                            coin("CCCUSDT", "COILING", vol=True, streak=1),
                            coin("DDDUSDT", "COILING", vol=True, streak=2),
                            coin("BBBUSDT", "COILING_LITE")]
s = ag.ans_ambush(full=True)
check("full=True legacy list", "观察候选列表" in s and "AAAUSDT" in s and "次级蓄力" in s)
check("star only when streak>=2", "★  DDDUSDT" in s)
check("plus for first tick", "＋  CCCUSDT" in s)

# 2b) dashboard 标签同语义：埋伏★只给 streak>=2
import dashboard
r1 = dict(label="COILING", vol_tick=True, vt_streak=1)
r2 = dict(label="COILING", vol_tick=True, vt_streak=2)
r0 = dict(label="COILING", vol_tick=False)
check("dash tag first-tick", dashboard._disp_label(r1) == "量能首现")
check("dash tag confirmed", dashboard._disp_label(r2) == "🔥埋伏★")
check("dash tag plain watch", dashboard._disp_label(r0) == "观察")
rl1 = dict(label="COILING_LITE", vol_tick=True, vt_streak=1)
rl2 = dict(label="COILING_LITE", vol_tick=True, vt_streak=2)
check("dash lite first-tick", dashboard._disp_label(rl1) == "次级首现")
check("dash lite confirmed", dashboard._disp_label(rl2) == "🔵次级★")
snap = dashboard.build_snapshot([r1, r2, r0], [])
check("kpi ambush = confirmed only", snap["kpis"]["ambush"] == 1)
check("kpi first_tick = 1", snap["kpis"]["first_tick"] == 1)

# 3) go 优先于 wait 优先于 chk
FakeMonitor.last_ranked = [
    coin("CHKUSDT", "COILING", vol=True, streak=1, ev=95),
    coin("WAITUSDT", "COILING", vol=True, streak=2, ready=False, ev=90),
    coin("GOUSDT", "COILING", vol=True, streak=2, ready=True, ev=80),
    coin("SKIPUSDT", "COILING"),  # 无 vol_tick 不出现
]
s = ag.ans_ambush()
check("go ranked first", s.index("GOUSDT") < s.index("WAITUSDT") < s.index("CHKUSDT"))
check("non-vol coin hidden", "SKIPUSDT" not in s)
check("go verdict text", "✅ 可入场" in s)
check("wait verdict text", "⏳ 挂单等点位" in s)
check("chk verdict text", "量能首现·等下轮确认" in s)
check("footer points to /ambush all", "/ambush all" in s)

# 4) 次级半仓判定
FakeMonitor.last_ranked = [coin("LITEUSDT", "COILING_LITE", vol=True, streak=2, ready=True)]
s = ag.ans_ambush()
check("lite half-position verdict", "次级可入场·半仓" in s)

# 5) 路由：/ambush all -> full；"埋伏" -> 默认
FakeMonitor.last_ranked = [coin("AAAUSDT", "COILING"), coin("LITEUSDT", "COILING_LITE", vol=True, streak=2, ready=True)]
s = ag.answer("/ambush all")
check("route /ambush all", "观察候选列表" in s)
s = ag.answer("/ambush")
check("route /ambush default", "可入场信号" in s)
s = ag.answer("现在有什么可入场的")
check("route natural 可入场", "可入场信号" in s)
s = ag.answer("看看候选")
check("route natural 候选 -> full", "观察候选列表" in s)

# 6) 上限 15 行
FakeMonitor.last_ranked = [coin("C%02dUSDT" % i, "COILING", vol=True, streak=2, ready=True)
                            for i in range(20)]
s = ag.ans_ambush()
n_rows = sum(1 for ln in s.split("\n") if "USDT" in ln and "可入场信号" not in ln)
check("cap 15 rows", n_rows == 15)

print()
if FAIL:
    print("%d FAILED" % len(FAIL)); sys.exit(1)
print("ALL AMBUSH-ACTION CHECKS PASSED")
