# -*- coding: utf-8 -*-
"""用线上真实数据预览极简版每日日报（2026-10-21 确定性优先版）。"""
import json, sys, urllib.request
sys.path.insert(0, "D:/memecoin-monitor")

PROXY = "http://localhost:15236"
BASE = "https://yaobi.zeabur.app"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

d = None
for _ in range(5):
    try:
        d = json.load(opener.open(BASE + "/dashboard_data.json", timeout=90))
        break
    except Exception as e:
        print("retry after err:", str(e)[:80])
if d is None:
    raise SystemExit("dashboard_data.json unreachable")
ranked = d.get("ranked") or []
meta = d.get("meta") or {}
amb = d.get("ambush") or {}
store = json.load(open("C:/Users/Martin/Downloads/ambush_track.json", encoding="utf-8"))
entries = store.get("entries", [])

import insights
from monitor import battle_report_lines

lines = []
env = {}
try:
    env = (json.load(open("C:/Users/Martin/Downloads/regime_log.json", encoding="utf-8")) or [])[-1]
except Exception:
    pass
r4, r1 = env.get("btc_4h") or "—", env.get("btc_1d") or "—"
rg = r4 if (r4 == r1 and r4 != "—") else "neutral"
_cn = {"bullish": "多头", "bearish": "空头", "neutral": "中性"}.get(rg, rg)
lines.append(f"环境：BTC {_cn}（4h {r4} / 1d {r1}）——"
             + ("逆风，新埋伏减半或推迟" if rg == "bearish" else "可正常埋伏"))
lines.append("资金纪律：同时最多 2-3 仓 · 单仓 ≤1/3 资金 · 只在埋伏★量能确认后入场")
lines.append("")

lw = insights.launch_window_stats(entries)
rows, n_cands = insights.value_board(ranked, entries)
if rows:
    lines += insights.board_lines(rows, n_cands, lw, compact=True)
    lines.append("")
br = battle_report_lines(entries, compact=True)
if br:
    lines += br
    lines.append("")
ag = insights.aging_lines(entries, lw=lw, compact=True)
if ag:
    lines += ag
    lines.append("")
lite = sum(1 for r in ranked if r.get("label") == "COILING_LITE")
fresh = sum(1 for r in ranked if r.get("label") == "FRESH_LAUNCH")
lines.append(f"其余：监控 {len(ranked)} 币 · 次级 {lite} · 刚启动 {fresh} · 明细看面板或 /ambush")
st = amb.get("stats") or {}
_rv = insights.win_rate_review(entries)
_ov = _rv.get("overall") or {}
if _rv.get("evaluated"):
    _rate = f"{_ov.get('rate', 0):.0%}"
    _ev = f"{_ov.get('ev', 0):+.1%}"
    _live = sum(1 for e in entries if e.get("source") == "live")
    lines.append(f"复盘：已结算 {_rv['evaluated']}（实盘 {_live} 笔）· "
                 f"胜率 {_rate} · 期望 {_ev}/笔 · 详情 /review")

print("=== 预览：新版每日妖币监控（确定性优先，全部真实数据） ===")
print("\n".join(lines))
print("=== 总长度 %d 字（旧版约 3000+ 字） ===" % len("\n".join(lines)))
