"""
24h Memecoin 埋伏监控器 —— 部署到 Zeabur 云端长期运行。

它做什么（你手动交易，它只报警）:
  - 周期性扫描 Binance USD-M 合约妖币池（默认 exchange 全量合约，约 400 个；拉取失败回退 FUTURES_UNIVERSE ~82）
  - 用六阶段剧本检测器 (memecoin_playbook_scanner.detect) 判定每个币当前阶段
  - 跨扫描维护 state.json，只在「阶段/事件发生转移」时推送信号:
        🚀 ENTER   新启动 (S2 第一根巨量突破)  -> 进观察列表做 S3->S4
        💧 DIP     S3 洗盘 -> 可低吸
        🔥 AMBUSH  横盘蓄力 + 量能抬头连续两轮确认 -> 加入埋伏观察，等启动
        🔵 AMBUSH_LITE 次级蓄力（大盘币新周期：横盘更久+贴MA99，20日振幅未达妖币门槛）
                 -> 小仓位埋伏观察，等启动；追踪器按 tier=lite 分开统计胜率
        （AMBUSH 类信号有双重防抖：①vol_tick 需连续两轮扫描确认（4h K 被 2h 扫描
          采样两次会闪烁）；②该币已有活跃埋伏追踪 entry 时不再重发——与追踪器的
          每币单条去重对齐，防止刷屏）
        🎯 TP     埋伏闭环：命中启动 / 峰值达标（+30% 第一档止盈、+80% 大赢区）
        🏁 OUT    埋伏闭环：收官（大赢/赢/弱/未走远/破位止损/45天超时）
        （TP/OUT 只对真实信号（live）推送——回填是历史重放，不是你的仓位；
          日报开头另有「📈 战报」栏目汇总近 7 天真实信号的结果）
        ⚠️ TOP     4h 插针见顶 -> 至少减一半
        📉 MA25    放量跌破 4h MA25 -> 再减一半 (规则#6)
        🚪 MA99    跌破 4h MA99 -> 全部离场 (规则#7)
        🩸 CRASH   崩盘 -> 别接刀
        ↩️ BOUNCE  崩后巨量反弹 -> 阶段6不是底，别追
  - 每天本地 digest_hour 点推送「当日结论摘要」：价值榜（按历史战绩/燃料/
    点位排序的埋伏候选 TOP5）+ 在途埋伏老化结论 + 战报（推荐明细看板/命令里有）

防封策略:
  - 只访问公开行情 klines（无需 API key，权重极低）
  - 每个币之间 sleep（默认 0.15s），整轮约 30~90s，远低于 Binance 限制
  - 多 host 轮换 + 失败指数退避（memecoin_scanner.fetch_klines 已实现）
  - kline host 每轮只探测一次并按延迟排序（pick_kline_hosts），消灭逐币
    逐 host 的超时爬行（2026-10-19 事故：一轮爬 30 小时从未完成）；探测
    全失败时每币快速失败（2 host x 5s），断流告警接管
  - 扫描预算（SCAN_BUDGET_S，默认 2400s）：超时即收尾，剩余币记
    skip_reasons["budget"]，保证每轮都能落盘、看板不冻结在启动时间戳
  - 不读账户、不下单，纯只读

部署: 见 README.md（Zeabur Service + Dockerfile，区域选 Binance 友好区如 SG/JP）。

用法:
  python monitor.py                 # 长期运行（Zeabur 上用这个）
  python monitor.py --once         # 单次扫描+推送（测试用）
  python monitor.py --test-notify  # 仅测试通知渠道是否连通
"""
import os, sys, json, time, argparse, signal
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from memecoin_scanner import (score_coin, FUTURES_UNIVERSE, get_universe,
                              fetch_funding_all, fetch_oi_change,
                              potential_of)  # reuse detection
from notifier import Notifier
import dashboard
from dashboard import DashboardServer, append_signal, load_signals, write_dashboard

STATE_PATH = os.path.join(HERE, "data", "monitor_state.json")
DIGEST_PATH = os.path.join(HERE, "data", "daily_digest.html")
DATA = os.path.join(HERE, "data")

SIG_EMOJI = {"ENTER": "🚀", "DIP": "💧", "AMBUSH": "🔥", "TOP": "⚠️",
             "MA25": "📉", "MA99": "🚪", "CRASH": "🩸", "BOUNCE": "↩️",
             "AMBUSH_LITE": "🔵", "TP": "🎯", "OUT": "🏁"}
SIG_ORDER = ["ENTER", "TP", "AMBUSH", "AMBUSH_LITE", "OUT", "DIP", "TOP", "MA25", "MA99", "CRASH", "BOUNCE"]

# event-keyed signal levels: their identity is (sym, event_sig) — these get the
# persistent fired-event memory so a detection-window flicker can never re-fire
# an already-alerted event. Transition-based levels (AMBUSH/TP/OUT) dedup by
# their own state machines instead.
EVENT_LEVELS = {"ENTER", "DIP", "TOP", "MA25", "MA99", "CRASH", "BOUNCE"}
# how long a fired event is remembered (days) — a bar can only produce the same
# event key once, so this is purely a memory bound
FIRED_EVENT_TTL_DAYS = 14

_STATUS_SHORT = {"pre_fail": "破位-20%", "timeout": "45天超时",
                 "miss": "启动未走远", "weak": "弱+10%"}


# ----------------------------------------------------------------------------
# outcome-feedback helpers: connect ambush CALLS to their RESULTS, so the
# user is pushed the closed loop (埋伏 -> 启动命中 -> 达标止盈 -> 收官/止损)
# instead of an endless firehose of fresh recommendations with no follow-up.
# ----------------------------------------------------------------------------
def _fmt_pct(g):
    return f"{g*100:+.0f}%" if isinstance(g, (int, float)) else "—"


def enter_hit_note(entry):
    """Append to an ENTER message when the coin carries an open ambush call."""
    ep = entry.get("entry_price") or 0.0
    day = (entry.get("ts") or "")[:10]
    delay = entry.get("launch_delay_d")
    lp = entry.get("last_price")
    g = (lp / ep - 1) if (isinstance(lp, (int, float)) and ep) else None
    d_s = f"，等了 {delay} 天" if isinstance(delay, (int, float)) else ""
    g_s = f"，现价 {_fmt_pct(g)} vs 入场" if g is not None else ""
    return (f"\n🎯 命中：你在 {day} 收到的埋伏信号启动了{d_s}{g_s}。"
            f"入场价 {ep:.6g}——按剧本做 S3 洗盘后 S3→S4 主升。")


def reentry_note(prev):
    """Append to an AMBUSH message when this coin had a prior (final) episode."""
    try:
        import ambush_tracker as _at
        cn = _at.STATUS_CN.get(prev.get("status"), prev.get("status"))
    except Exception:
        cn = prev.get("status")
    pg = prev.get("peak_gain")
    day = (prev.get("final_ts") or prev.get("ts") or "")[:10]
    src = "" if prev.get("source") == "live" else "（回填战绩）"
    pgs = f"，峰值 {_fmt_pct(pg)}" if isinstance(pg, (int, float)) else ""
    return (f"\n♻️ 重入：该币上一轮埋伏（{day}）= {cn}{pgs}{src}。"
            f"同币新周期重复启动历史上贡献了最大复利——值得比新面孔多一分信任。")


def outcome_event_signal(evd):
    """(level, msg) for a tracker outcome event, or (None, None) to skip."""
    sym = evd.get("sym")
    if evd.get("ev") == "launched":
        g = evd.get("gain")
        return ("TP",
                f"{sym} 🎯 埋伏命中启动：{evd.get('day')} 的信号等了 "
                f"{evd.get('delay_d')} 天终于启动。\n"
                f"入场 {evd.get('entry_price'):.6g} → 启动价 {evd.get('launched_price'):.6g}"
                f"（{_fmt_pct(g)}）。按剧本：S3 洗盘别丢仓位，S3→S4 主升。")
    if evd.get("ev") == "milestone":
        lvl = evd.get("level")
        tag = "第一档止盈区（+30%）" if lvl == 0.30 else "大赢区（+80%）"
        return ("TP",
                f"{sym} 🎯 埋伏达标：峰值 {_fmt_pct(evd.get('gain'))}，进入{tag}。\n"
                f"止盈阶梯参考：+50% 卖 1/3、+100% 再卖 1/3、剩余移动止损。"
                + ("" if lvl == 0.30 else "这已经是 win_strong 级别的走势。"))
    if evd.get("ev") == "final":
        st = evd.get("status")
        pg = evd.get("peak_gain")
        try:
            import ambush_tracker as _at
            cn = _at.STATUS_CN.get(st, st)
        except Exception:
            cn = st
        if st in ("win_strong", "win", "weak"):
            return ("OUT",
                    f"{sym} 🏁 埋伏收官：{cn}，峰值 {_fmt_pct(pg)}。\n"
                    f"（{evd.get('day')} 入场的信号已走完启动后 20 天窗口）")
        if st == "miss":
            return ("OUT",
                    f"{sym} 🏁 埋伏收官：启动了但没走远（峰值 {_fmt_pct(pg)}）。\n"
                    f"这类启动弱于剧本，S4 主升没来。")
        if st == "pre_fail":
            return ("OUT",
                    f"{sym} 🏁 埋伏止损：启动前跌破 -20%（{evd.get('day')} 入场的信号）。\n"
                    f"-20% 破位止损执行，等它重新横盘会自动再评估。")
        if st == "timeout":
            return ("OUT",
                    f"{sym} 🏁 埋伏超时：45 天未启动（峰值仅 {_fmt_pct(pg)}）。\n"
                    f"横盘是死水不是蓄力，撤。")
    return None, None


def battle_report_lines(entries, days=7, now=None):
    """战报 section for the daily digest: what happened to previous live
    ambush calls (launches / finals in the window, current best peaks,
    cumulative live record). Returns [] when there is nothing to report."""
    now = now or now_iso()
    try:
        t_now = datetime.fromisoformat(now)
    except Exception:
        t_now = datetime.now(timezone.utc)
    live = [e for e in (entries or []) if e.get("source") == "live"]
    if not live:
        return []
    def _inwin(ts):
        try:
            return (t_now - datetime.fromisoformat(ts)).days <= days
        except Exception:
            return False
    launched = [e for e in live if e.get("launched_ts") and _inwin(e["launched_ts"])]
    finals = [e for e in live if e.get("final_ts") and _inwin(e["final_ts"])]
    open_l = [e for e in live if e["status"] == "launched"]
    lines = [f"📈 战报（近 {days} 天，真实信号闭环）："]
    if launched:
        parts = []
        for e in sorted(launched, key=lambda x: x["launched_ts"]):
            ep = e.get("entry_price") or 0
            lp = e.get("last_price")
            g = (lp / ep - 1) if (isinstance(lp, (int, float)) and ep) else None
            parts.append(f"{e['sym']}（等{e.get('launch_delay_d')}天，现{_fmt_pct(g)}）")
        lines.append("  启动命中：" + "、".join(parts))
    if open_l:
        best = sorted(open_l, key=lambda e: -((e.get("max_price") or e.get("entry_price") or 1)
                                              / (e.get("entry_price") or 1) - 1))
        parts = []
        for e in best[:6]:
            ep = e.get("entry_price") or 0
            g = ((e.get("max_price") or ep) / ep - 1) if ep else None
            parts.append(f"{e['sym']} 峰值{_fmt_pct(g)}")
        lines.append("  启动后跟踪中：" + "、".join(parts)
                     + ("…" if len(best) > 6 else ""))
    if finals:
        wins = [e for e in finals if e["status"] in ("win_strong", "win")]
        losses = [e for e in finals if e["status"] in ("pre_fail", "timeout", "miss", "weak")]
        if wins:
            parts = [f"{e['sym']} {_fmt_pct(e.get('peak_gain'))}" for e in wins]
            lines.append("  ✅ 收官赢：" + "、".join(parts))
        if losses:
            parts = [f"{e['sym']}（{_STATUS_SHORT.get(e['status'], e['status'])}）"
                     for e in losses]
            lines.append("  ❌ 收官撤：" + "、".join(parts))
    # cumulative live record
    lf = [e for e in live if e["status"] in ("win_strong", "win", "weak", "miss")]
    w30 = [e for e in lf if (e.get("peak_gain") or 0) >= 0.30]
    n_fin = sum(1 for e in live if e.get("final_ts"))
    cum = (f"  累计（真实信号）：{len(live)} 笔 · 已启动 {len(lf) + sum(1 for e in live if e['status'] == 'launched')}"
           f" · 启动后+30%达标 {len(w30)}/{len(lf) if lf else 0}")
    lines.append(cum + (f" · 已收官 {n_fin}" if n_fin else ""))
    if len(lines) == 1:
        lines.append("  （真实信号都还在等待窗内，尚无结果——首笔结果出来就会出现在这里）")
    return lines

DEFAULT_CONFIG = {
    "market": "futures",
    "universe": "exchange",         # "exchange" (full ~400 perps, curated fallback) | "curated" | "list"
    "symbols": [],                  # used when universe=="list"
    "scan_interval_minutes": 120,   # 信号扫描间隔
    "digest_hour": 8,               # 本地时区的每日摘要时刻（小时）
    "local_utc_offset_hours": 8,    # 本地时区相对 UTC 的偏移（北京时间=8）
    "request_sleep": 0.15,          # 每币之间的 sleep，防封
    "web": {"enabled": True, "host": "0.0.0.0", "port": 8080},  # live dashboard
    "notification": {"channel": "log"},
}


# ----------------------------------------------------------------------------
def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        cfg.update(user)
        # deep-merge notification
        if "notification" in user:
            cfg["notification"] = {**DEFAULT_CONFIG["notification"], **user["notification"]}
    else:
        # fresh clone has no local monitor_config.json (it's gitignored) -> fall back to example
        example = os.path.join(HERE, "monitor_config.example.json")
        if os.path.exists(example):
            with open(example, "r", encoding="utf-8") as f:
                user = json.load(f)
            cfg.update(user)
            if "notification" in user:
                cfg["notification"] = {**DEFAULT_CONFIG["notification"], **user["notification"]}
            print(f"[config] '{path}' not found, using example config "
                  f"(set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars for notifications)")
    return cfg


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _usd(x):
    """Compact USD for signal messages, e.g. 1.2M / 350K."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{x/div:.1f}{suf}"
    return f"{x:.0f}"


def event_sig(e):
    return f"{e.get('type')}@{e.get('ts')}"


def timing_gate(sym, market="futures", btc_regime=None, kind="enter"):
    """One-line 15m entry-timing verdict (VWAP + 9/20 EMA gate). Empty on failure.

    kind: "enter" = breakout entry (momentum follows the break),
          "ambush"/"dip" = buying a zone -> wait for the price to RECLAIM
          the VWAP before paying up (never catch a falling knife)."""
    try:
        import timing
        t = timing.analyze(sym, market=market)
    except Exception:
        return ""
    if not t:
        return ""
    rg = timing.REGIME_CN.get(btc_regime, "—") if btc_regime else "—"
    ok = t["ready"]
    if kind == "enter":
        if ok and btc_regime != "bearish":
            return (f"\n🟢 点位闸门开：15m 价>VWAP + EMA9>20 多头（大盘{rg}）。"
                    f"可按突破进场，带止损。")
        if ok and btc_regime == "bearish":
            return ("\n🟡 点位就绪，但大盘逆风（BTC 15m 空头排列）：减半仓，"
                    "或等 BTC 回上 VWAP 再加。")
        return (f"\n🔴 点位未就绪（15m 价 vs VWAP {t['vwap_dist']*100:+.1f}%，"
                f"EMA{'多头' if t['ema_bull'] else '空头'}）：等价回上 VWAP 再进，防假突破砸回。")
    if ok:
        return f"\n⚡ 点位就绪（15m 价>VWAP，EMA多头）：现在就是打仓位的位置。大盘{rg}。"
    return (f"\n✋ 点位未就绪（15m 价 vs VWAP {t['vwap_dist']*100:+.1f}%，"
            f"EMA{'多头' if t['ema_bull'] else '空头'}）：等回上 VWAP 再打，别接下落刀。大盘{rg}。")


def classify(sym, rec, prev, btc_regime=None, market="futures"):
    """Return (signals, current_event_sigs). signals = list of (level, msg, ev)."""
    cur_sigs = set(event_sig(e) for e in rec.get("events", []))
    prev_sigs = set(prev.get("event_sigs", [])) if prev else set()
    signals = []
    for e in rec.get("events", []):
        if event_sig(e) in prev_sigs:
            continue
        t = e["type"]
        note = e.get("note", "")
        if t == "LAUNCH":
            tbr = rec.get("tbuy_ratio")
            cvd_s = ""
            if isinstance(tbr, (int, float)):
                cvd_s = (f"\n主动买占比 {tbr*100:.0f}%（CVD口径：真实买盘"
                         f"{'强' if tbr >= 0.55 else '一般'}）")
            signals.append(("ENTER",
                f"{sym} 新启动 (S2)：横盘后第一根巨量突破（{note}）。{cvd_s}\n"
                f"按剧本进观察列表，等待 S3 洗盘后做 S3→S4 主升。"
                + timing_gate(sym, market, btc_regime, "enter"), e))
        elif t == "SHAKEOUT":
            signals.append(("DIP",
                f"{sym} 进入 S3 洗盘（{note}）。\n"
                f"这是剧本里的低吸区，用 4h 移动止损，别用固定止损。"
                + timing_gate(sym, market, btc_regime, "dip"), e))
        elif t == "WICKTOP":
            signals.append(("TOP",
                f"{sym} 4h 出现长上影插针（{note}）。\n"
                f"至少减一半仓位，跌破 MA99 全撤。", e))
        elif t == "MA25_BREAK":
            signals.append(("MA25",
                f"{sym} 放量跌破 4h MA25（{note}）。\n"
                f"剧本规则#6：再减一半仓位。", e))
        elif t == "MA99_BREAK":
            signals.append(("MA99",
                f"{sym} 跌破 4h MA99（{note}）。\n"
                f"剧本规则#7：全部离场。", e))
        elif t == "CRASH":
            signals.append(("CRASH",
                f"{sym} 崩盘（{note}）。别接刀，等重新横盘。", e))
        elif t == "DEATHBOUNCE":
            fd = rec.get("funding")
            fd_s = ""
            if isinstance(fd, (int, float)) and fd <= -0.001:
                fd_s = f"\n当前资金费率 {fd*100:+.3f}%（空头挤爆在付钱）—— 剧本：极端负费率不是底。"
            signals.append(("BOUNCE",
                f"{sym} 崩后巨量反弹（{note}）。这是阶段6，不是底，别追。{fd_s}", e))
    # 埋伏★：COILING + 量能抬头连续两轮扫描确认（vt_streak 刚跨过 2）。
    # vol_tick 单轮 True 不可信：4h K 每根会被 2h 扫描采样两次（成形中/收盘后
    # 量不同），网络失败轮走 cached fallback 时 vol_tick=False，True/False 闪烁
    # 会让"状态转换"反复重触发（实测一轮重发过 107 条）。streak 由 run_scan
    # 维护在 state 里；发射端另有按 tracker 活跃 entry 的去重兜底。
    _streak = rec.get("vt_streak") or 0
    _prev_streak = (prev.get("vt_streak") or 0) if prev else 0
    if (rec["label"] == "COILING" and _streak >= 2 and _prev_streak < 2):
        pv = rec.get("pv")
        pv_s = f"{pv*100:+.0f}%" if pv is not None else "n/a"
        # derivatives context: negative funding + rising OI while coiling =
        # shorts paying up while positions pile in -> squeeze fuel if it launches
        fd = rec.get("funding"); oc = rec.get("oi_chg_24h")
        dv = []
        if isinstance(fd, (int, float)):
            dv.append(f"资金费率 {fd*100:+.3f}%")
        if isinstance(oc, (int, float)):
            dv.append(f"OI 24h {oc*100:+.0f}%")
        lr = rec.get("lsr")
        if isinstance(lr, (int, float)):
            dv.append(f"账户多空比 {lr:.2f}")
        lq_l, lq_s = rec.get("liq_long_24h"), rec.get("liq_short_24h")
        if isinstance(lq_l, (int, float)) and isinstance(lq_s, (int, float)) and (lq_l or lq_s):
            dv.append(f"24h清算 多${_usd(lq_l)}/空${_usd(lq_s)}")
        dv_s = ""
        if dv:
            dv_s = "\n" + "·".join(dv)
            if isinstance(fd, (int, float)) and fd < 0 and isinstance(oc, (int, float)) and oc > 0.10:
                dv_s += "（空头付费+仓位堆积，若启动则逼空燃料充足）"
        signals.append(("AMBUSH",
            f"{sym} 埋伏★：横盘蓄力中量能抬头。\n"
            f"横盘 {rec.get('flat_run')} 根 4h，区间 {rec.get('range_w',0)*100:.1f}% 宽，"
            f"价 vs 4h MA99 = {pv_s}。{dv_s}\n"
            f"打法：横盘区内即可分批埋伏（本信号即入场窗口，-20%破位止损）；\n"
            f"或等 🚀ENTER（第一根巨量突破）确认后再入——更稳但价更高。"
            + timing_gate(sym, market, btc_regime, "ambush"), None))
    # 次级蓄力★：COILING_LITE + 量能抬头连续两轮确认（同主档的 flicker 防抖）。
    # 大盘币新周期形态：20 日振幅不够妖币门槛，用更久横盘 + 贴 MA99 补偿。
    if (rec["label"] == "COILING_LITE" and _streak >= 2 and _prev_streak < 2):
        pv = rec.get("pv")
        pv_s = f"{pv*100:+.0f}%" if pv is not None else "n/a"
        fd = rec.get("funding"); oc = rec.get("oi_chg_24h")
        dv = []
        if isinstance(fd, (int, float)):
            dv.append(f"资金费率 {fd*100:+.3f}%")
        if isinstance(oc, (int, float)):
            dv.append(f"OI 24h {oc*100:+.0f}%")
        lr = rec.get("lsr")
        if isinstance(lr, (int, float)):
            dv.append(f"账户多空比 {lr:.2f}")
        lq_l, lq_s = rec.get("liq_long_24h"), rec.get("liq_short_24h")
        if isinstance(lq_l, (int, float)) and isinstance(lq_s, (int, float)) and (lq_l or lq_s):
            dv.append(f"24h清算 多${_usd(lq_l)}/空${_usd(lq_s)}")
        dv_s = ""
        if dv:
            dv_s = "\n" + "·".join(dv)
            if isinstance(fd, (int, float)) and fd < 0 and isinstance(oc, (int, float)) and oc > 0.10:
                dv_s += "（空头付费+仓位堆积，若启动则逼空燃料充足）"
        signals.append(("AMBUSH_LITE",
            f"{sym} 次级蓄力★：大盘币新周期横盘，量能抬头。\n"
            f"横盘 {rec.get('flat_run')} 根 4h（比主档更久），区间 {rec.get('range_w',0)*100:.1f}% 宽，"
            f"20 日振幅 {rec.get('swing',0)*100:.0f}%（未达妖币门槛 130%），"
            f"价 vs 4h MA99 = {pv_s}。{dv_s}\n"
            f"可小仓位埋伏（小于主档），等第一根巨量突破加仓。追踪器按次级档分开算胜率。"
            + timing_gate(sym, market, btc_regime, "ambush"), None))
    return signals, cur_sigs


# ----------------------------------------------------------------------------
class Monitor:
    def __init__(self, cfg, notifier):
        cfg = dict(cfg)
        # env-var overrides so Zeabur can tune without editing the committed config
        env = os.environ
        if env.get("MONITOR_UNIVERSE"):
            cfg["universe"] = env["MONITOR_UNIVERSE"].strip().lower()
        if env.get("MONITOR_MARKET"):
            cfg["market"] = env["MONITOR_MARKET"].strip().lower()
        for _k, _ev in (("scan_interval_minutes", "SCAN_INTERVAL_MINUTES"),
                        ("digest_hour", "DIGEST_HOUR"),
                        ("local_utc_offset_hours", "LOCAL_UTC_OFFSET"),
                        ("request_sleep", "REQUEST_SLEEP")):
            if env.get(_ev):
                try:
                    cfg[_k] = float(env[_ev]) if _k == "request_sleep" else int(env[_ev])
                except ValueError:
                    pass
        self.cfg = cfg
        self.notifier = notifier
        self.market = cfg["market"]
        self.scan_interval = cfg["scan_interval_minutes"]
        self.digest_hour = cfg["digest_hour"]
        self.local_offset = cfg["local_utc_offset_hours"]
        self.request_sleep = cfg["request_sleep"]
        # web dashboard
        wc = cfg.get("web", {}) or {}
        self.web_enabled = bool(wc.get("enabled", False))
        self.web_host = wc.get("host", "0.0.0.0")
        self.web_port = int(wc.get("port", 8080))
        self.server = None
        self.universe = self._build_universe()
        self.state = {}
        self.last_ranked = []
        self.scan_summary = {}
        self.last_scan_iso = now_iso()
        self.stop = False
        self.first_run = True
        # feed health for outage alerting (True = last round had data; the
        # first failing round alerts, then stays quiet until recovery)
        self._feed_was_ok = True

    def _build_universe(self):
        u = self.cfg.get("universe", "curated")
        if u == "list":
            self.universe_source = "list(config)"
            return [s.strip().upper() for s in self.cfg.get("symbols", []) if s.strip()]
        if u == "exchange":
            print("[universe] fetching all Binance futures perps ...")
            sym = get_universe(market=self.market)
            # distinguish live vs cached vs fallback so the dashboard is transparent
            path = os.path.join(DATA, "scan_%s" % self.market, "universe_cache.json")
            live = os.path.exists(path) and (time.time() - os.path.getmtime(path) < 60)
            if len(sym) <= len(FUTURES_UNIVERSE) + 5:
                self.universe_source = "curated(fallback)" if len(sym) <= len(FUTURES_UNIVERSE) else "exchange(cached)"
            else:
                self.universe_source = "exchange(live)" if live else "exchange(cached)"
            print(f"[universe] {len(sym)} perps  source={self.universe_source}")
            return sym
        self.universe_source = "curated"
        return list(FUTURES_UNIVERSE)

    # ---- state ----
    def load_state(self):
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_state(self, state):
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, STATE_PATH)

    # ---- signals log (for web feed) ----
    def log_signal(self, level, sym, msg, ev=None):
        try:
            append_signal(level, sym, msg, ev_type=ev)
        except Exception:
            pass

    def _effective_interval_s(self):
        """Scan interval in seconds — shortened after a TOTAL data outage.

        A 0/N scan round (egress IP geo-blocked by Binance after a redeploy,
        transient ban, ...) must not sit in the dark for a full 2h interval:
        retry every 20 min until the feed recovers. The dashboard countdown
        uses this so it never lies about when the next attempt happens.
        """
        interval_s = self.scan_interval * 60
        try:
            if (self.scan_summary or {}).get("ok") == 0:
                interval_s = min(interval_s, 20 * 60)
        except Exception:
            pass
        return interval_s

    def next_scan_epoch(self, last_scan_ts=None):
        try:
            if last_scan_ts is None:
                last_scan_ts = self.last_scan_iso
            base = datetime.fromisoformat(last_scan_ts).timestamp()
        except Exception:
            base = time.time()
        return int(base + self._effective_interval_s())

    # ---- web dashboard ----
    def write_dashboard(self):
        try:
            signals = load_signals()
            meta = {
                "last_scan": self.last_scan_iso,
                "next_scan_epoch": self.next_scan_epoch(),
                "market": self.market,
                "universe_source": getattr(self, "universe_source", "curated"),
                "universe_size": len(self.universe),
                "scan_interval_minutes": self.scan_interval,
                "scan_summary": getattr(self, "scan_summary", {}),
            }
            write_dashboard(self.last_ranked or [], signals, meta)
        except Exception as e:
            print(f"[dashboard] write skipped: {e}")

    # ---- scanning ----
    def _deriv_pass(self, ranked, offline=False):
        """Attach OI (priority-allocated) + liquidations + LSR to ranked recs.

        OI history costs one API call per symbol no matter the source, so
        coverage must follow potential instead of the alphabetical scan
        order (a coin used to get OI because of its name, not its merit).
        Coinalyze is primary (20 syms/request, plus liquidations and the
        accounts long/short ratio that Binance doesn't serve historically);
        Binance per-coin calls backstop the misses. Mutates recs in place.
        LSR semantics: r = long/short accounts (BTC~1.5, ETH~2.7 in calm
        markets); r << 1 while coiling = crowded shorts = squeeze fuel.
        """
        if offline:
            return
        OI_LABELS = ("COILING", "COILING_LITE", "FRESH_LAUNCH", "LAUNCHED")
        OI_TOP = 100    # OI coverage: top candidates by potential
        XTRA_TOP = 30   # liquidations + LSR: top slice only (3x call cost)
        cands = [r for r in ranked if r.get("label") in OI_LABELS]
        cands.sort(key=lambda r: -r.get("potential", r.get("score", 0)))
        top = cands[:OI_TOP]
        got = set()
        try:
            import coinalyze
            if coinalyze.has_key():
                oi_map = coinalyze.fetch_oi_batch([r["sym"] for r in top]) or {}
                xtra = [r["sym"] for r in cands[:XTRA_TOP]]
                liq_map = coinalyze.fetch_liq_batch(xtra) or {}
                lsr_map = coinalyze.fetch_lsr_batch(xtra) or {}
                for r in top:
                    oc = oi_map.get(r["sym"])
                    if isinstance(oc, (int, float)):
                        r["oi_chg_24h"] = round(oc, 4)
                        got.add(r["sym"])
                    lq = liq_map.get(r["sym"])
                    if lq:
                        r["liq_long_24h"], r["liq_short_24h"] = lq
                    lr = lsr_map.get(r["sym"])
                    if isinstance(lr, (int, float)):
                        r["lsr"] = round(lr, 2)
                print(f"[deriv] coinalyze: OI {len(got)}/{len(top)}, "
                      f"liq {len(liq_map)}, lsr {len(lsr_map)}")
        except Exception as e:
            print(f"[deriv] coinalyze pass failed: {e}")
        # Binance per-coin backstop for whatever Coinalyze missed (cap 60)
        miss = [r for r in top if r["sym"] not in got]
        for r in miss[:60]:
            try:
                oc = fetch_oi_change(r["sym"])
                if oc is not None:
                    r["oi_chg_24h"] = round(oc, 4)
                    time.sleep(0.1)
            except Exception:
                pass
        # recompute potential where OI landed (squeeze bonus now counts)
        for r in top:
            if r.get("label") in ("COILING", "COILING_LITE") and "oi_chg_24h" in r:
                r["potential"] = potential_of(r)

    def run_scan(self, emit=True):
        state = self.load_state()
        state_out = dict(state)
        # persistent fired-event memory: {sym|EVENTSIG: fired_iso}. Events on
        # closed bars flicker in/out of the detection window (each new 4h bar
        # reshuffles it), and event_sigs alone could not stop identical
        # re-fires — observed 300+ duplicate messages per day. This map is the
        # authoritative "already alerted" ledger.
        fired = state.get("_fired_events")
        if not isinstance(fired, dict):
            fired = {}
        else:
            cut = time.time() - FIRED_EVENT_TTL_DAYS * 86400
            fired = {k: v for k, v in fired.items()
                     if isinstance(v, str) and datetime.fromisoformat(v).timestamp() > cut}
        ranked = []
        new_signals = []
        t0 = time.time()
        n_ok = 0
        skip_reasons = {}
        offline = os.environ.get("MONITOR_OFFLINE") == "1"
        # derivatives context: one bulk call funds every coin; OI history is
        # per-coin so it is budgeted to the labels that actually trade on it
        funding_map = {}
        if not offline:
            try:
                funding_map = fetch_funding_all()
                if funding_map:
                    print(f"[deriv] funding: {len(funding_map)} symbols (bulk premiumIndex)")
            except Exception as e:
                print(f"[deriv] funding fetch failed: {e}")
        # BTC 15m regime (VWAP + 9/20 EMA) — computed once per scan, gates all
        # entry-type signal messages ("don't recommend fresh longs into a dump")
        self.btc_regime = None
        if not offline:
            try:
                import timing
                self.btc_regime = timing.regime(market=self.market)
                if self.btc_regime:
                    print(f"[timing] BTC 15m regime: {self.btc_regime}")
            except Exception as e:
                print(f"[timing] regime failed: {e}")
        # scan budget: never let one round crawl for hours. With the kline-host
        # probe a healthy round takes minutes; if we blow past the budget the
        # egress is pathological — finish with what we have and let the outage
        # alerting / fast-retry machinery handle the rest.
        try:
            budget_s = int(os.environ.get("SCAN_BUDGET_S", "2400"))
        except ValueError:
            budget_s = 2400
        deadline = t0 + budget_s
        for i, sym in enumerate(self.universe):
            if time.time() > deadline:
                rem = len(self.universe) - i
                skip_reasons["budget"] = skip_reasons.get("budget", 0) + rem
                print(f"[scan] budget {budget_s}s exceeded after {i}/{len(self.universe)} "
                      f"coins; skipping the remaining {rem} this round")
                break
            try:
                rec, err = score_coin(sym, market=self.market, force_fetch=not offline)
            except Exception as e:
                rec, err = None, f"exc: {e}"
            # resilience: if live fetch failed (or offline mode), fall back to cached klines
            if rec is None and (offline or (err and err.startswith("fetch fail"))):
                try:
                    rec, err = score_coin(sym, market=self.market, force_fetch=False)
                    if rec is not None:
                        err = (err or "") + " (cached fallback)"
                except Exception as e:
                    rec, err = None, f"exc: {e}"
            if rec is None:
                e = (err or "").lower()
                if "404" in e or "http error" in e:
                    reason = "not_on_exchange"
                elif "timeout" in e or "url" in e or "connection" in e or "remote" in e or "fetch fail" in e:
                    reason = "network"
                else:
                    reason = "other"
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                print(f"  skip {sym}: {err}  [{reason}]")
                continue
            n_ok += 1
            # funding comes from ONE bulk call, so every coin gets it up
            # front; OI/liquidations/LSR cost one call per symbol wherever
            # they come from, so they are allocated in a priority pass
            # AFTER scoring (by potential — not alphabetical scan order),
            # and signals are classified in a second pass so their messages
            # can quote the full derivatives context
            f = funding_map.get(sym) if funding_map else None
            if f is not None:
                rec["funding"] = f
            # potential without OI yet; the priority pass recomputes it for
            # the coins that actually get OI (squeeze bonus needs it present)
            if rec["label"] in ("COILING", "COILING_LITE"):
                rec["potential"] = potential_of(rec)
            ranked.append(rec)
            time.sleep(self.request_sleep)
            if (i + 1) % 50 == 0:
                print(f"[scan] progress {i+1}/{len(self.universe)} ok={n_ok} "
                      f"skip={sum(skip_reasons.values())} "
                      f"elapsed={int(time.time()-t0)}s")
        # ---- derivatives priority pass: OI (plus liquidations / accounts
        # long-short ratio) costs one API call per symbol, so coverage is
        # allocated by POTENTIAL, not by the alphabetical scan order that
        # used to decide who got OI. Coinalyze batches 20 symbols per
        # request; Binance per-coin calls backstop whatever it misses.
        self._deriv_pass(ranked, offline=offline)
        # ---- signal classification (second pass, after derivatives so
        # AMBUSH / ENTER messages can quote the full context) ----
        # coins already being tracked: a re-fire carries no new information
        # (the tracker keeps ONE active entry per coin), so don't spam
        # Telegram / the signals log with vol_tick flicker re-fires.
        # tracked_map additionally powers ENTER augmentation (埋伏命中) and
        # last_final powers the ♻️ re-entry annotation on fresh AMBUSH calls.
        tracked_map, last_final = {}, {}
        try:
            import ambush_tracker
            tracked_map = ambush_tracker.active_map()
            last_final = ambush_tracker.last_final_map()
        except Exception as e:
            print(f"[ambush-track] active_map skipped: {e}")
        # historical launch-window conclusion (from the tracker's accumulated
        # outcomes) — appended to every fresh AMBUSH push so each call carries
        # its own base-rate evidence instead of being a bare recommendation.
        ambush_note = ""
        try:
            import ambush_tracker, insights
            ambush_note = insights.launch_window_note(
                insights.launch_window_stats(ambush_tracker.load()["entries"]))
        except Exception as e:
            print(f"[insights] launch-window note skipped: {e}")
        for rec in ranked:
            sym = rec["sym"]
            prev = state.get(sym)
            # vol_tick hysteresis: consecutive scans where the coin is coiling
            # with the volume tick ON. A single True is noise (each 4h bar is
            # sampled twice per 2h-scan cycle; cached-fallback rounds read
            # vol_tick=False), so AMBUSH requires the streak to cross 2 —
            # classify() reads vt_streak from the rec. Label excursions
            # (coil -> launch -> re-coil) reset the streak, so a genuinely
            # NEW episode still fires.
            _ready = (rec.get("label") in ("COILING", "COILING_LITE")
                      and bool(rec.get("vol_tick")))
            # NOTE the prev guard: on a baseline scan (or after a volume wipe)
            # prev is None for EVERY coin — an unguarded prev.get here crashed
            # the whole scan ('NoneType' object has no attribute 'get',
            # 2026-10-19 incident) and, inside the while loop, killed the
            # monitor thread entirely while the dashboard kept serving frozen
            # data forever.
            rec["vt_streak"] = (((prev.get("vt_streak") or 0) if prev else 0) + 1) if _ready else 0
            signals, cur_sigs = classify(sym, rec, prev,
                                         btc_regime=self.btc_regime, market=self.market)
            state_out[sym] = {
                "label": rec["label"], "stage": rec["stage"], "score": rec["score"],
                "vol_tick": rec.get("vol_tick"), "price": rec["price"],
                "pv": rec.get("pv"), "ma99_1h": rec.get("ma99_1h"),
                "flat_run": rec.get("flat_run"), "range_w": rec.get("range_w"),
                "vt_streak": rec["vt_streak"],
                "event_sigs": list(cur_sigs), "updated": now_iso(),
                "first_seen": (prev.get("first_seen") if prev else now_iso()),
            }
            if emit and prev is not None:   # skip signal emission on very first baseline
                for lvl, msg, ev in signals:
                    if lvl in ("AMBUSH", "AMBUSH_LITE") and sym in tracked_map:
                        continue  # already tracked: re-fire is flicker noise
                    # fired-event memory: an event that already alerted (within
                    # the TTL) NEVER fires again, even if the detection window
                    # flickers it out of event_sigs and back in (each 4h bar
                    # open reshuffles the window; event_sigs alone let the SAME
                    # MA25/MA99/WICKTOP re-fire every 4h — 300+ dupes/day).
                    if lvl in EVENT_LEVELS and isinstance(ev, dict):
                        ekey = f"{sym}|{event_sig(ev)}"
                        if ekey in fired:
                            continue
                        fired[ekey] = now_iso()
                    if lvl == "ENTER" and sym in tracked_map:
                        msg = msg + enter_hit_note(tracked_map[sym])
                    elif lvl in ("AMBUSH", "AMBUSH_LITE") and sym in last_final:
                        msg = msg + reentry_note(last_final[sym])
                    if lvl in ("AMBUSH", "AMBUSH_LITE") and ambush_note:
                        msg = msg + ambush_note
                    new_signals.append((lvl, sym, msg, ev))
        # entry-timing badges for the watchlist (15m VWAP/EMA gate):
        # coverage is ALL main-tier COILING + FRESH_LAUNCH (the actionable set),
        # then LITE by *potential* (not score — potential = elasticity x fuel,
        # which is what decides who's worth a 15m watch), capped at 24 total
        # 15m kline calls per scan.
        try:
            import timing
            mains = [r for r in ranked if r.get("label") in ("COILING", "FRESH_LAUNCH")]
            lites = sorted([r for r in ranked if r.get("label") == "COILING_LITE"],
                           key=lambda r: -r.get("potential", r.get("score", 0)))
            cap = 24
            watch_top = mains + lites[:max(0, cap - len(mains))]
            for r in watch_top:
                t = timing.analyze(r["sym"], market=self.market)
                if t:
                    r["timing"] = t
        except Exception as e:
            print(f"[timing] watch badges skipped: {e}")
        # persist the fired-event ledger with the rest of the state
        state_out["_fired_events"] = fired
        self.save_state(state_out)
        if n_ok > 0 or not self.last_ranked:
            self.last_ranked = ranked
        # else: total data outage — keep the LAST GOOD watchlist on the
        # dashboard (the 0/N failure banner is driven by scan_summary.ok),
        # don't blank the tables just because the feed died this round
        self.last_scan_iso = now_iso()
        self.scan_summary = {
            "attempted": len(self.universe),
            "ok": n_ok,
            "skipped": len(self.universe) - n_ok,
            "skip_reasons": skip_reasons,
            "elapsed_s": round(time.time() - t0),
            "ts": self.last_scan_iso,
        }
        try:
            with open(os.path.join(os.path.dirname(STATE_PATH), "scan_summary.json"), "w", encoding="utf-8") as f:
                json.dump(self.scan_summary, f, indent=2, default=str)
        except Exception:
            pass
        dt = time.time() - t0
        print(f"[scan] done: {n_ok}/{len(self.universe)} ok, {dt:.0f}s, {len(new_signals)} new signals")
        # ---- total-outage handling: diagnostics + one alert per episode ----
        # The 2026-10-19 incident: redeploy landed the service on an egress
        # IP Binance refuses; every round came back 0/519 and the user only
        # found out because the Telegram went quiet. The feed must speak up.
        if n_ok == 0:
            conn = {}
            try:
                from memecoin_scanner import connectivity_check
                conn = connectivity_check(market=self.market)
                print(f"[scan] connectivity: {conn}")
            except Exception as e:
                print(f"[scan] connectivity check failed: {e}")
            if getattr(self, "_feed_was_ok", True):
                self._feed_was_ok = False
                try:
                    self.notifier.send(
                        "🔴 数据断流 · 全池拉取失败",
                        f"本轮扫描 0/{len(self.universe)} 个币成功，Binance 行情从部署服务器不可达。\n"
                        f"连通性探测：{conn or '探测失败'}\n"
                        "看板继续显示上一轮良好数据；系统改为每 20 分钟重试。\n"
                        "修复路径：Zeabur 重启服务（换出口 IP）→ 仍失败则设置 HTTPS_PROXY 环境变量"
                        "（指向 Binance 友好区域的代理）或切换部署区域。",
                        level="alert")
                except Exception as e:
                    print(f"[scan] outage alert failed: {e}")
        elif not getattr(self, "_feed_was_ok", True):
            self._feed_was_ok = True
            try:
                self.notifier.send(
                    "🟢 数据恢复",
                    f"行情拉取恢复正常（本轮 {n_ok}/{len(self.universe)} 个币成功）。恢复正常扫描节奏。",
                    level="alert")
            except Exception as e:
                print(f"[scan] recovery alert failed: {e}")
        else:
            self._feed_was_ok = True
        # ambush self-learning loop: record fired AMBUSH calls, then refresh
        # outcome tracking (prices / status transitions) for every open entry
        try:
            import ambush_tracker
            for lvl, sym, msg, ev in new_signals:
                if lvl == "AMBUSH":
                    rec = next((r for r in ranked if r["sym"] == sym), None)
                    if rec is not None:
                        ambush_tracker.record_entry(rec)
                elif lvl == "AMBUSH_LITE":
                    rec = next((r for r in ranked if r["sym"] == sym), None)
                    if rec is not None:
                        ambush_tracker.record_entry(rec, tier="lite")
            res = ambush_tracker.update(ranked)
            outcome_events = res[1] if isinstance(res, tuple) else []
            # push the closed loop: 命中启动 / 达标止盈 / 收官止损 — live
            # entries only (backfill is history replay, not real calls)
            for evd in outcome_events or []:
                lvl, msg = outcome_event_signal(evd)
                if lvl:
                    new_signals.append((lvl, evd.get("sym", "?"), msg, None))
        except Exception as e:
            print(f"[ambush-track] skipped: {e}")
        if self.first_run:
            self.first_run = False
            # bootstrap historical stats from the cached klines once (store is
            # empty) — gives the win-rate dashboard an initial sample instead
            # of starting from zero after every redeploy
            try:
                import ambush_tracker
                if not ambush_tracker.load()["entries"]:
                    ambush_tracker.backfill(market=self.market, quiet=True)
            except Exception as e:
                print(f"[ambush-backfill] skipped: {e}")
            if emit:
                self.run_digest()  # give an initial overview on first baseline
        if emit:
            # Telegram flood control (2026-10-20 incident: a market-wide dump
            # produced 1016 individual messages in 24h — 99% low-value event
            # notes, many literally identical to ones sent hours earlier).
            # Actionable signals (AMBUSH / ENTER / TP / OUT) still go out
            # individually (ENTER capped at 8 per scan); everything else is
            # folded into ONE compact per-scan digest. The web signals log
            # keeps every entry individually — only Telegram is batched.
            BATCHED_LEVELS = ("DIP", "TOP", "MA25", "MA99", "CRASH", "BOUNCE")
            n_enter = 0
            batch_lines = []
            for lvl, sym, msg, ev in new_signals:
                self.log_signal(lvl, sym, msg, ev=ev["type"] if isinstance(ev, dict) else None)
                first_line = msg.split("\n")[0]
                short = first_line if len(first_line) <= 80 else first_line[:80] + "…"
                if lvl in BATCHED_LEVELS or (lvl == "ENTER" and n_enter >= 8):
                    batch_lines.append(f"· {short}")
                    continue
                if lvl == "ENTER":
                    n_enter += 1
                self.notifier.send(f"{SIG_EMOJI.get(lvl,'')} {lvl} 信号 · {sym}", msg, level=lvl)
            if batch_lines:
                shown = batch_lines[:40]
                extra = len(batch_lines) - len(shown)
                body = "\n".join(shown) + (f"\n…另有 {extra} 条（详见看板信号流）" if extra > 0 else "")
                self.notifier.send(
                    "📋 事件速览（本轮合并）",
                    f"低优先级事件 {len(batch_lines)} 条，已合并推送：\n{body}\n\n"
                    "（洗盘/插针/破位类事件不再逐条打扰；看板信号流保留全量明细）",
                    level="info")
        self.write_dashboard()
        return ranked

    # ---- daily digest ----
    def run_digest(self):
        ranked = self.last_ranked or self.run_scan(emit=False) or []
        coiling = sorted([r for r in ranked if r["label"] == "COILING"],
                         key=lambda r: -r.get("potential", r.get("score", 0)))
        # Sort the secondary sub-lists by *relevance*, not by symbol (the universe
        # is alphabetically sorted, so an unsorted slice makes every list look
        # artificially clustered by prefix -- e.g. all "1000xxx" or all "A...").
        #   fresh   -> most recently launched first (lowest launch_recency)
        #   launched-> same: freshest breakouts are the ones most likely to shake out next
        #   risk    -> biggest recent move first (most urgent to avoid)
        fresh = sorted([r for r in ranked if r["label"] == "FRESH_LAUNCH"],
                       key=lambda r: r.get("launch_recency") if r.get("launch_recency") is not None else 1e9)
        launched = sorted([r for r in ranked if r["label"] == "LAUNCHED"],
                          key=lambda r: r.get("launch_recency") if r.get("launch_recency") is not None else 1e9)
        risk = sorted([r for r in ranked if r["label"] in ("PUMPED", "CRASHING")],
                      key=lambda r: -abs(r.get("day_move") or 0))
        date = self.utc_to_local(time.time()).strftime("%Y-%m-%d")

        # note: notifier.send() already prepends the title ("📅 每日妖币监控 · {date}"),
        # so do NOT repeat it here (would show twice on Telegram).
        lines = []
        src = getattr(self, "universe_source", "curated")
        lines.append(f"监控池 {src} · 池大小 {len(self.universe)} · 本轮有效 {len(ranked)} 币 | "
                     f"埋伏候选 {len(coiling)} | 次级 {sum(1 for r in ranked if r['label']=='COILING_LITE')} | 刚启动 {len(fresh)}")
        rg = None
        try:
            import timing as _timing
            rg = _timing.regime(market=self.market) or getattr(self, "btc_regime", None)
        except Exception:
            rg = getattr(self, "btc_regime", None)
        if rg:
            _cn = {"bullish": "多头", "bearish": "空头", "neutral": "中性"}.get(rg, rg)
            lines.append(f"大盘点位闸门（BTC 15m VWAP+EMA）：{_cn}"
                         + ("——逆风期，新埋伏减半或推迟" if rg == "bearish" else ""))
        # battle report FIRST: what happened to previous live calls — the
        # closed loop must lead the digest, or it drowns under the fresh
        # recommendation list it is supposed to hold accountable.
        try:
            import ambush_tracker
            br = battle_report_lines(ambush_tracker.load()["entries"])
            if br:
                lines += br
        except Exception as e:
            print(f"[digest] battle report skipped: {e}")
        # ---- conclusion-driven core (2026-10-20 feedback: "我需要类似的结论，
        # 而不是大量的推荐——这样我才能找到有价值的标的埋伏") ----
        # The raw 18-row watchlist / lite / fresh / launched / risk tables now
        # live on the dashboard + /ambush + /value; the digest leads with what
        # the EVIDENCE says: which candidates are worth ambushing (value board
        # ranked by historical re-entry record, squeeze fuel, entry timing),
        # which open calls are aging into statistical dead water, and one
        # compact line for everything else.
        n_board, n_cands = 0, len(coiling)
        try:
            import ambush_tracker, insights
            _store = ambush_tracker.load()
            _lw = insights.launch_window_stats(_store["entries"])
            _rows, n_cands = insights.value_board(ranked, _store["entries"])
            n_board = len(_rows)
            if _rows:
                lines.append("")
                lines += insights.board_lines(_rows, n_cands, _lw)
            _ag = insights.aging_lines(_store["entries"], lw=_lw)
            if _ag:
                lines.append("")
                lines += _ag
        except Exception as e:
            print(f"[digest] insights skipped: {e}")
        # everything else: counts + the actionable names only; details live
        # on the web dashboard and via the Telegram /ambush command
        lite = [r for r in ranked if r["label"] == "COILING_LITE"]
        lines.append("")
        rest = (f"其余观察（明细看网页看板或发 /ambush）：次级蓄力 {len(lite)} · "
                f"刚启动 {len(fresh)} · 动能衰减 {len(launched)} · 警惕 {len(risk)}")
        if fresh:
            names = "、".join(r["sym"] for r in fresh[:3])
            rest += f"；刚启动（做 S3→S4）：{names}" + ("…" if len(fresh) > 3 else "")
        lines.append(rest)
        lines.append("")
        lines.append("信号会通过本渠道实时推送。手动交易，纪律第一。")
        # ambush self-review: measure our own AMBUSH calls (the self-learning loop)
        try:
            import ambush_tracker
            st = ambush_tracker.stats()
            if st["total"]:
                lines.append("")
                src_note = f"（含回填 {st['backfill']} 笔，实盘 {st['live']} 笔）" if st["backfill"] else ""
                lines.append(f"🎯 埋伏信号自检 · 跟踪 {st['total']} 笔{src_note}:")
                if st["evaluated"]:
                    lr = f"{st['launch_rate']:.0%}" if st["launch_rate"] is not None else "—"
                    w3 = f"{st['win30_rate']:.0%}" if st["win30_rate"] is not None else "—"
                    ap = f"{st['avg_peak']:+.0%}" if st["avg_peak"] is not None else "—"
                    ad = f"{st['avg_delay']}天" if st["avg_delay"] is not None else "—"
                    lines.append(f"  已结算 {st['evaluated']}：启动 {st['launched_n']}（{lr}）｜"
                                 f"启动后 +30% 达标率 {w3} ｜ 平均峰值 {ap} ｜ 平均启动延迟 {ad}")
                    b = st["buckets"]
                    def _pct(x):
                        return f"{x*100:.0f}%" if isinstance(x, (int, float)) else "—"
                    seg = " ｜ ".join(
                        f"{k}分: {v['n']}笔启动{_pct(v['launch_rate'])}"
                        for k, v in b.items() if v["n"])
                    if seg:
                        lines.append(f"  分数段启动率 {seg}")
                lines.append(f"  在途：等待启动 {st['open_waiting']} · 已启动跟踪 {st['open_launched']}")
        except Exception as e:
            print(f"[ambush-digest] skipped: {e}")
        text = "\n".join(lines)
        self.notifier.send(f"📅 每日妖币监控 · {date}", text, level="digest")
        self.log_signal("DIGEST", "",
                        f"每日摘要 · 监控 {len(ranked)} 币 · 价值榜 {n_board}/{n_cands} · 刚启动 {len(fresh)}")
        # also save full HTML for reference
        try:
            from memecoin_scanner import build_html
            html = build_html(ranked, [], 40, market=self.market)
            with open(DIGEST_PATH, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print(f"[digest] html save skipped: {e}")
        self.write_dashboard()

    # ---- time ----
    def utc_to_local(self, ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=self.local_offset)

    # ---- startup state restore ----
    def _restore_dashboard_state(self):
        """Seed self.last_ranked / self.scan_summary from the last persisted
        dashboard_data.json so the panel isn't blank (or clobbered by an empty
        startup write) after a redeploy. The /app/data volume persists across
        Zeabur restarts, so this is usually available. Real data overwrites
        these on the first scan anyway."""
        import json as _json
        p = os.path.join(DATA, "dashboard_data.json")
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8") as f:
            snap = _json.load(f)
        rk = snap.get("ranked") or []
        # Guard against seeding a tiny stale snapshot (e.g. a 1-coin BANKUSDT
        # leftover from a crash-loop restart). A healthy restore should have
        # dozens+ of coins; anything smaller is discarded so the panel doesn't
        # flash stale data before the immediate first scan repopulates it.
        if len(rk) >= 50:
            self.last_ranked = rk
        m = snap.get("meta") or {}
        ss = m.get("scan_summary")
        if isinstance(ss, dict):
            self.scan_summary = ss
            # carry the outage flag across restarts: if the last persisted
            # round was a total failure, the first SUCCESSFUL scan after this
            # boot should still send the 🟢 recovery note (without this the
            # flag resets to True on every restart and the note never fires)
            if ss.get("ok") == 0:
                self._feed_was_ok = False
        print(f"[startup] restored previous dashboard: {len(rk)} coins, "
              f"scan ok={ss.get('ok') if isinstance(ss, dict) else '?'}")

    def _purge_test_signals(self):
        """Drop obviously-fake test signals (msg contains 'test') left over on
        the persistent volume, so the live signal feed isn't polluted by old
        smoke-test artifacts. Idempotent and safe (only matches 'test')."""
        import json as _json
        from dashboard import SIGNALS_PATH, load_signals
        sigs = load_signals()
        if not sigs:
            return
        kept = [s for s in sigs if "test" not in (s.get("msg") or "").lower()]
        if len(kept) != len(sigs):
            with open(SIGNALS_PATH, "w", encoding="utf-8") as f:
                _json.dump(kept, f, indent=2, default=str)
            print(f"[startup] purged {len(sigs)-len(kept)} test-signal artifact(s)")

    # ---- loop ----
    def loop(self):
        if self.web_enabled:
            self.server = DashboardServer(self.web_host, self.web_port, monitor=self)
            self.server.start()
        # startup connectivity self-check so we (and the user) know if Binance is reachable
        try:
            from memecoin_scanner import connectivity_check
            chk = connectivity_check(self.market)
            reachable = [k for k, v in chk.items() if not str(v).startswith("ERR")]
            print(f"[connectivity] {chk}")
            conn_note = (f"Binance 可达端点：{', '.join(reachable) or '无（可能被部署区域封锁，请配置 HTTPS_PROXY 或换区域）'}")
        except Exception as e:
            conn_note = f"连通性自检失败：{e}"
            print(f"[connectivity] err {e}")
        self.notifier.send("🟢 妖币埋伏监控器已启动",
            f"市场: {self.market}\n监控池: {len(self.universe)} 个币（{getattr(self,'universe_source','?')}）\n"
            f"扫描间隔: {self.scan_interval} 分钟\n每日摘要: 本地 {self.digest_hour}:00\n"
            f"通知渠道: {self.notifier.channel}\n"
            f"网页看板: {'http://'+self.web_host+':'+str(self.web_port) if self.web_enabled else '未启用'}\n"
            f"{conn_note}\n\n"
            f"只报警、不交易，你手动操作。", level="info")
        self.log_signal("INFO", "", f"监控器已启动 · 监控 {len(self.universe)} 币 · 网页看板 "
                                     f"{'开启' if self.web_enabled else '关闭'} · {conn_note}")
        # restore last good dashboard state so a redeploy/restart doesn't blank
        # the panel (and doesn't let an empty startup write clobber good data).
        # On Zeabur the /app/data volume persists across restarts, so the
        # previous scan's data is normally available; restore it before writing.
        try:
            self._restore_dashboard_state()
        except Exception as e:
            print(f"[startup] restore skipped: {e}")
        try:
            self._purge_test_signals()
        except Exception as e:
            print(f"[startup] purge skipped: {e}")
        # Telegram Q&A agent (rule-based, read-only): ask the monitor questions
        # directly — /status /ambush /track /check, or "BANKUSDT 还能埋伏吗"
        try:
            from telegram_agent import TelegramAgent
            if (self.notifier.channel == "telegram" and self.notifier.bot_token
                    and self.notifier.chat_id):
                self.agent = TelegramAgent(self.notifier, self)
                self.agent.start()
        except Exception as e:
            print(f"[tg-agent] disabled: {e}")
        # write an initial dashboard immediately so the page shows pool size at once
        # (now seeded with last good data if available, instead of 0/empty)
        try:
            self.write_dashboard()
        except Exception:
            pass
        # Immediate first scan so the web panel is fresh WITHOUT waiting a full
        # scan_interval. This populates self.last_ranked which BOTH the Telegram
        # digest and the live web snapshot read from, so they stay in lockstep.
        # prev is None on this baseline scan, so run_scan's emission guard
        # suppresses any false signal flood; it does send the initial digest.
        try:
            self.run_scan(emit=True)
        except Exception as e:
            print(f"[startup] initial scan failed: {e}")
        last_scan = time.time()
        last_digest_date = None
        while not self.stop:
            now = time.time()
            # fast retry after a total data outage (egress IP blocked after a
            # redeploy, transient ban) — don't sit in the dark for 2 hours
            if now - last_scan >= self._effective_interval_s():
                # a crashing scan must NEVER kill the monitor loop: today a
                # single unhandled exception left the dashboard thread serving
                # frozen data while the scanner was dead (2026-10-19).
                try:
                    self.run_scan(emit=True)
                except Exception as e:
                    import traceback
                    print(f"[scan] CRASHED (monitor keeps running): {e}")
                    traceback.print_exc()
                last_scan = now
            local = self.utc_to_local(now)
            if local.hour == self.digest_hour and local.minute < 12 and local.date() != last_digest_date:
                try:
                    self.run_digest()
                except Exception as e:
                    print(f"[digest] failed (monitor keeps running): {e}")
                last_digest_date = local.date()
            # heartbeat sleep
            for _ in range(60):
                if self.stop:
                    break
                time.sleep(1)
        print("[monitor] stopping, flushing state ...")
        if self.server:
            self.server.shutdown()
        self.save_state(self.load_state())

    def request_stop(self):
        self.stop = True


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "monitor_config.json"))
    ap.add_argument("--once", action="store_true", help="单次扫描+推送后退出（测试用）")
    ap.add_argument("--test-notify", action="store_true", help="仅测试通知渠道")
    ap.add_argument("--no-emit", action="store_true", help="扫描但不推送信号（仅更新状态）")
    ap.add_argument("--sym-list", default=None, help="测试用：覆盖监控池，e.g. RAVEUSDT,LABUSDT,BTWUSDT")
    ap.add_argument("--web", action="store_true", help="启用网页看板 HTTP 服务")
    ap.add_argument("--no-web", action="store_true", help="强制关闭网页看板")
    ap.add_argument("--port", type=int, default=None, help="网页看板端口（覆盖配置）")
    ap.add_argument("--host", default=None, help="网页看板绑定地址（覆盖配置）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    notifier = Notifier(cfg["notification"])

    if args.test_notify:
        notifier.test()
        return

    # web overrides: CLI > env PORT > config
    web = cfg.setdefault("web", {})
    if args.no_web:
        web["enabled"] = False
    if args.web:
        web["enabled"] = True
    if args.host:
        web["host"] = args.host
    if args.port:
        web["port"] = args.port
    env_port = os.environ.get("PORT")
    if env_port:
        try:
            web["port"] = int(env_port)
        except ValueError:
            pass

    monitor = Monitor(cfg, notifier)
    if args.sym_list:
        monitor.universe = [s.strip().upper() for s in args.sym_list.split(",") if s.strip()]

    # graceful shutdown (Zeabur sends SIGTERM on redeploy/restart)
    def handle(signum, frame):
        print(f"[signal] received {signum}, stopping ...")
        monitor.request_stop()
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    if args.once:
        monitor.run_scan(emit=not args.no_emit)
        monitor.run_digest()
        if monitor.web_enabled:
            monitor.server = DashboardServer(monitor.web_host, monitor.web_port, monitor=monitor)
            monitor.server.start()
            print("[monitor] --once --web: serving dashboard, Ctrl-C to stop")
            try:
                while not monitor.stop:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            if monitor.server:
                monitor.server.shutdown()
        return
    monitor.loop()


if __name__ == "__main__":
    main()
