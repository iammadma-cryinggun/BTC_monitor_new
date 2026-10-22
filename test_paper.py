# -*- coding: utf-8 -*-
"""Paper-portfolio tests (2026-10-22): 模拟持仓 = 交易员纪律的机器执行。
覆盖：只买✅可入场、边沿触发（止损后同轮不回补、信号重置后才可再入场）、
连败过滤、仓位/上限、-20% 止损、+30% 减半+保本、45 天超时、
价格回退的权益计算、持久化、快照结构、build_snapshot 集成。
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import paper

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


def cand(sym, price, ready=True, streak=2, flat=10, rng=0.08, ev=90,
         label="COILING", vol_tick=True, potential=50):
    return dict(sym=sym, label=label, vol_tick=vol_tick,
                vt_streak=streak, vt_confirmed=streak >= 2,
                flat_run=flat, range_w=rng, price=price, score=ev,
                potential=potential, ev_pts=ev, timing={"ready": ready})


T0 = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
tmpdir = tempfile.mkdtemp()
PP = os.path.join(tmpdir, "paper.json")


def scan(ranked, dt, tracker_entries=None):
    return paper.on_scan(ranked, now=T0 + dt, path=PP,
                         tracker_entries=tracker_entries or [])


H = timedelta(hours=1)

# ---------- 1. fresh start ----------
st, ev = scan([], H)
check("fresh: cash = 10000, no positions",
      st["cash"] == 10000.0 and st["positions"] == [] and ev == [])

# ---------- 2. entry on a confirmed+ready candidate ----------
st, ev = scan([cand("AAAUSDT", 1.0)], 2 * H)
check("entry: opened AAA", any(e[0] == "enter" and e[1] == "AAAUSDT" for e in ev))
p = st["positions"][0]
check("entry: size = 10% of equity (=1000 USDT)",
      abs(p["qty"] * p["entry"] - 1000.0) < 1e-6)
check("entry: cash reduced to 9000", abs(st["cash"] - 9000.0) < 1e-6)
check("entry: stop/target set (-20%/+30%)",
      abs(p["stop"] - 0.8) < 1e-9 and abs(p["target"] - 1.3) < 1e-9)

# ---------- 3. held -> no re-buy; new symbol opens ----------
st, ev = scan([cand("AAAUSDT", 1.05), cand("BBBUSDT", 2.0)], 4 * H)
check("dedup: AAA not re-bought",
      len([e for e in ev if e[1] == "AAAUSDT" and e[0] == "enter"]) == 0)
check("new: BBB opened", any(e[1] == "BBBUSDT" for e in ev if e[0] == "enter"))

# ---------- 4. gates: ⏳ / 未确认 / 成熟度不过 / 连败≥2 ----------
st, ev = scan([cand("CCCUSDT", 1.0, ready=False)], 6 * H)
check("gate: ⏳挂单等点位 不买", not any(e[0] == "enter" for e in ev))
st, ev = scan([cand("DDDUSDT", 1.0, streak=1)], 7 * H)
check("gate: 量能首现(未确认) 不买", not any(e[0] == "enter" for e in ev))
st, ev = scan([cand("EEEUSDT", 1.0, flat=0, rng=0.3)], 8 * H)
check("gate: 成熟度门槛未过(flat=0/range=30%) 不买",
      not any(e[0] == "enter" for e in ev))
bad_hist = [
    dict(sym="FFFUSDT", ts="2026-09-01T00:00:00+00:00", status="pre_fail",
         peak_gain=-0.2, final_ts="2026-09-20T00:00:00+00:00"),
    dict(sym="FFFUSDT", ts="2026-09-25T00:00:00+00:00", status="pre_fail",
         peak_gain=-0.2, final_ts="2026-10-10T00:00:00+00:00"),
]
st, ev = scan([cand("FFFUSDT", 1.0)], 9 * H, tracker_entries=bad_hist)
check("filter: 连败≥2 不买", not any(e[0] == "enter" for e in ev))

# ---------- 5. stop-loss: 平仓且同轮不回补（边沿触发核心用例） ----------
st, ev = scan([cand("AAAUSDT", 0.79)], 10 * H)
check("stop: AAA closed",
      any(e[0] == "exit" and e[1] == "AAAUSDT" for e in ev))
check("stop: AAA NOT re-bought in the same scan (edge trigger)",
      not any(e[0] == "enter" and e[1] == "AAAUSDT" for e in ev))
c = [c for c in st["closed"] if c["sym"] == "AAAUSDT"][0]
check("stop: reason recorded", "止损" in c["reason"])
check("stop: pnl = scan price fill (-21%)",
      abs(c["pnl_pct"] - (0.79 / 1.0 - 1)) < 1e-6)
cash_after_stop = st["cash"]
check("stop: cash = 7995(BBB后) + 0.79*1000",
      abs(cash_after_stop - (7995.0 + 790.0)) < 1e-6)

# ---------- 6. signal stays on -> still no re-entry next scan ----------
st, ev = scan([cand("AAAUSDT", 0.81)], 11 * H)
check("edge: 信号未熄灭期间持续不回补",
      not any(e[0] == "enter" and e[1] == "AAAUSDT" for e in ev))

# ---------- 7. signal resets, then re-fires -> re-entry allowed ----------
st, ev = scan([cand("AAAUSDT", 0.80, ready=False)], 12 * H)   # 信号熄灭
st, ev = scan([cand("AAAUSDT", 0.85)], 13 * H)                # 重新点亮
check("edge: 信号重置后允许再入场",
      any(e[0] == "enter" and e[1] == "AAAUSDT" for e in ev))

# ---------- 8. +30% half-sell then breakeven exit ----------
st, _ = scan([cand("GGGUSDT", 1.0)], 14 * H)
st, ev = scan([cand("GGGUSDT", 1.35)], 15 * H)
check("tp: half sold at +30%+",
      any(e[0] == "tp_half" and e[1] == "GGGUSDT" for e in ev))
pg = [p for p in st["positions"] if p["sym"] == "GGGUSDT"][0]
check("tp: half_taken + stop moved to breakeven",
      pg["half_taken"] is True and abs(pg["stop"] - 1.0) < 1e-9)
st, ev = scan([cand("GGGUSDT", 0.99)], 16 * H)
cg = [c for c in st["closed"] if c["sym"] == "GGGUSDT"][0]
check("tp: closed at breakeven line",
      any(e[1] == "GGGUSDT" and e[0] == "exit" for e in ev))
check("tp: blended pnl ~ +17%", abs(cg["pnl_pct"] - 0.17) < 0.005)
check("tp: GGG not re-bought same scan",
      not any(e[0] == "enter" and e[1] == "GGGUSDT" for e in ev))

# ---------- 9. 45-day timeout ----------
st, _ = scan([cand("HHHUSDT", 1.0)], 17 * H)
st, ev = scan([cand("HHHUSDT", 1.05)], timedelta(days=46))
ch = [c for c in st["closed"] if c["sym"] == "HHHUSDT"][0]
check("timeout: closed after 45d", "超时" in ch["reason"])

# ---------- 10. caps: max 2 new per scan; missed trigger not chased ----------
# 此刻已有 BBB/AAA 两笔遗留仓（无行情，靠 last_price 计值）
syms = ["M%dUSDT" % i for i in range(4)]
st6, ev6 = scan([cand(s, 1.0) for s in syms], timedelta(days=46, hours=2))
n_first = len([e for e in ev6 if e[0] == "enter"])
check("cap: max 2 new per scan", n_first == 2)
# 同一批信号继续亮着：M2/M3 上轮已被观测为 ✅ → 不追（宁可错过）
st6, ev6 = scan([cand(s, 1.0) for s in syms], timedelta(days=46, hours=4))
check("edge: 满仓错过的触发不追",
      len([e for e in ev6 if e[0] == "enter"]) == 0)
# 新批次首次出现：补位到 MAX_POS=5 后停止
st6, ev6 = scan([cand(s, 1.0) for s in syms]
                + [cand("M%dUSDT" % i, 1.0) for i in range(4, 8)],
                timedelta(days=46, hours=6))
n_third = len([e for e in ev6 if e[0] == "enter"])
check("edge+cap: 新触发补位入场，仓位到 5 封顶",
      n_third == 1 and len(st6["positions"]) == 5)
# 满仓后即使再来全新触发也不入场
st6, ev6 = scan([cand("NEWTRIGUSDT", 1.0)], timedelta(days=46, hours=8))
check("cap: 满仓后新触发不入场",
      len(st6["positions"]) == 5
      and not any(e[1] == "NEWTRIGUSDT" for e in ev6 if e[0] == "enter"))
n_pos_now = 5

# ---------- 11. equity falls back to last known price ----------
# M 币掉出观察池（ranked 为空）时权益不塌陷
snap_fallback = paper.snapshot([], path=PP)
check("fallback: equity uses last_price when not in ranked",
      snap_fallback["n_pos"] == n_pos_now
      and snap_fallback["equity"] > 5000)

# ---------- 12. persistence + snapshot structure ----------
st7 = paper.load(PP)
check("persist: reload keeps positions/closed",
      len(st7["positions"]) == n_pos_now and len(st7["closed"]) >= 3)
snap = paper.snapshot([cand(s, 1.02) for s in
                       ["M0USDT", "M1USDT", "M2USDT", "M3USDT"]],
                      path=PP)
check("snapshot: fields present",
      all(k in snap for k in ("equity", "cash", "n_pos", "positions", "closed",
                              "stats", "rules")))
check("snapshot: stats win_rate computed",
      isinstance(snap["stats"]["n"], int) and snap["stats"]["n"] >= 3)

# ---------- 13. build_snapshot integration ----------
import dashboard
ds = dashboard.build_snapshot([cand("ZZZUSDT", 1.0)], [])
check("build_snapshot: paper key present",
      "paper" in ds and ds["paper"] is not None)
check("build_snapshot: paper equity is a number",
      isinstance(ds["paper"].get("equity"), (int, float)))

fails = [n for n, ok in checks if not ok]
print()
print("ALL PAPER CHECKS PASSED" if not fails else "FAILURES: %s" % fails)
sys.exit(1 if fails else 0)
