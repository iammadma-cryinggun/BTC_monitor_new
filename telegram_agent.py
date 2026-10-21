"""
Telegram Q&A agent — rule-based, deterministic.

A daemon thread long-polls getUpdates and answers questions about the
monitor's own state. Every number comes from a structured store
(monitor.last_ranked / scan_summary / ambush_track.json) or a fresh
score_coin() fetch — no LLM in the loop, so replies can't hallucinate.

Each answer is a standalone method taking plain arguments and returning
plain text, so if an LLM front-end is ever bolted on, these become its
tool/function-call surface (the model would only parse intent and phrase;
facts stay tool-served).

Commands (slash or natural language):
  /help 帮助                  — this list
  /status 状态                — scan summary / pool / label counts
  /value 价值榜/值得埋谁       — EVIDENCE-ranked ambush board + aging verdicts
  /ambush [all] 可入场信号       — ACTIONABLE coins only (default);
                                   /ambush all = full raw watch list
  /track [SYM] 追踪            — ambush tracker stats, or one symbol's calls
  /review 复盘/胜率            — win-rate accountability report (outcome
                                   distribution, monthly trend, conditional
                                   re-entry rates — "验证程序是不是成功")
  /check SYM                  — live re-score + "还能埋伏吗" verdict
  "<SYM> 还能埋伏吗 / 怎么样" — same as /check
  "失效" (with or w/o SYM)    — "信号失效了吗": tracker entry status
"""
import os, re, json, time, threading, traceback
import urllib.request, urllib.error
from datetime import datetime, timezone

import ambush_tracker
import insights
from memecoin_scanner import score_coin, fetch_funding_all, fetch_oi_change

SYM_RE = re.compile(r"\b([A-Z0-9]{2,18}USDT)\b")

HELP = (
    "🤖 妖币监控 · 问答模式\n"
    "直接发消息问我（只读，不代下单）：\n\n"
    "/value 或 价值榜/值得埋谁 — 结论版：按历史战绩排序的价值榜+老化提醒\n"
    "/status 或 状态 — 扫描概况\n"
    "/ambush 或 可入场/埋伏 — 当前可入场信号（默认只显示量能抬头的行动币；无信号时直说）\n"
    "/ambush all — 全量观察列表（原始数据，均为观察态）\n"
    "/track 或 追踪 — 埋伏信号自检统计\n"
    "/track BANKUSDT — 该币历次埋伏追踪明细\n"
    "/review 或 复盘/胜率 — 胜率复盘报告：结局分布·按月趋势·条件胜率（验证这套程序成不成功）\n"
    "/check BANKUSDT — 实时重扫该币 + 还能埋伏吗判定\n"
    "或直接说：\"BANKUSDT 还能埋伏吗\" / \"BANKUSDT 信号失效了吗\" / \"BANKUSDT 怎么样\"\n\n"
    "判定口径全部来自六阶段状态机 + 追踪器，数据与看板/日报同源。"
)


def _split(s, n=3800):
    if len(s) <= n:
        return [s]
    out, cur = [], ""
    for line in s.split("\n"):
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        out.append(cur)
    return out


def _f(x):  # format a pct or '—'
    return f"{x*100:+.1f}%" if isinstance(x, (int, float)) else "—"


def _usd(x):  # compact USD, e.g. 1.2M / 350K
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{x/div:.1f}{suf}"
    return f"{x:.0f}"


def _age_days(ts_s):
    """Days since an entry ts (ISO string). Naive timestamps are read as UTC."""
    try:
        t = datetime.fromisoformat(str(ts_s))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).days
    except Exception:
        return None


class TelegramAgent:
    def __init__(self, notifier, monitor):
        self.notifier = notifier
        self.monitor = monitor
        self.token = notifier.bot_token
        try:
            self.chat_id = int(notifier.chat_id)
        except Exception:
            self.chat_id = None
        self.offset = 0
        self.thread = None

    # ------------------------------------------------------------------ plumbing
    def _api(self, method, **params):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        payload = json.dumps(params).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # surface Telegram's own error text (e.g. 409 Conflict)
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            raise RuntimeError(f"telegram {method} HTTP {e.code}: {body}") from e

    def _reply(self, text):
        # plain text, no parse_mode: symbols/percent signs go out verbatim
        for ch in _split(text):
            self._api("sendMessage", chat_id=self.chat_id, text=ch,
                      disable_web_page_preview=True)

    def start(self):
        if not (self.token and self.chat_id):
            print("[tg-agent] disabled: no telegram token/chat_id")
            return
        # a stale webhook would make every getUpdates fail with 409 —
        # clear it defensively (no-op when none is set)
        try:
            r = self._api("deleteWebhook", drop_pending_updates=False)
            print(f"[tg-agent] deleteWebhook: {r.get('ok')} {r.get('description', '')}")
        except Exception as e:
            print(f"[tg-agent] deleteWebhook failed: {e}")
        self.thread = threading.Thread(target=self._run, daemon=True, name="tg-agent")
        self.thread.start()

    def _run(self):
        print("[tg-agent] polling started")
        while not getattr(self.monitor, "stop", False):
            try:
                res = self._api("getUpdates", offset=self.offset, timeout=25,
                                allowed_updates=["message"])
                if not res.get("ok"):
                    raise RuntimeError(f"getUpdates not ok: {res}")
                for u in (res.get("result") or []):  # .result — the dict itself is NOT the list
                    self.offset = max(self.offset, u["update_id"] + 1)
                    try:
                        self._handle(u.get("message") or {})
                    except Exception:
                        print("[tg-agent] handler error:")
                        traceback.print_exc()
            except Exception as e:
                hint = ""
                if "409" in str(e):
                    hint = "（409：有 webhook 或另一个 getUpdates 轮询在抢占这个 bot）"
                print(f"[tg-agent] poll retry: {e} {hint}")
                time.sleep(5)
        print("[tg-agent] stopped")

    def _handle(self, msg):
        chat = (msg.get("chat") or {})
        if chat.get("id") != self.chat_id:
            return  # whitelist: only the configured chat may talk to the bot
        text = (msg.get("text") or "").strip()
        if not text:
            return
        try:
            reply = self.answer(text)
        except Exception as e:
            traceback.print_exc()
            reply = f"处理出错：{e}"
        if reply:
            self._reply(reply)

    # ------------------------------------------------------------------ router
    def answer(self, text):
        """Pure intent routing — no I/O. Returns reply text or None."""
        t = text.strip()
        low = t.lower()
        if low in ("/start", "/start@yaobi_monitor_bot"):
            return HELP
        if low.startswith("/help") or "帮助" in t or low in ("/?", "help"):
            return HELP
        # slash commands
        if t.startswith("/"):
            parts = t.split()
            cmd, args = parts[0].split("@")[0].lower(), parts[1:]
            if cmd == "/status":
                return self.ans_status()
            if cmd == "/value":
                return self.ans_value()
            if cmd == "/ambush":
                full = any(a.lower() in ("all", "全", "全量", "all列表") for a in args)
                return self.ans_ambush(full=full)
            if cmd == "/track":
                return self.ans_track(args[0].upper() if args else None)
            if cmd == "/review":
                return self.ans_review()
            if cmd == "/check":
                if not args:
                    return "用法：/check BANKUSDT"
                return self.ans_check(args[0].upper())
            return HELP
        # natural language
        m = SYM_RE.search(t)
        sym = m.group(1) if m else None
        if "失效" in t:
            return self.ans_invalid(sym)
        if sym:
            return self.ans_check(sym)
        if any(k in t for k in ("价值", "值得", "推荐", "结论", "埋谁", "该埋")):
            return self.ans_value()
        if any(k in t for k in ("复盘", "胜率")):
            return self.ans_review()
        if "追踪" in t or "自检" in t:
            return self.ans_track(None)
        if "埋伏" in t or "可入场" in t or "入场" in t:
            return self.ans_ambush()
        if "候选" in t:
            return self.ans_ambush(full=True)
        if "状态" in t or low == "status":
            return self.ans_status()
        if any(k in t for k in ("你好", "hi", "hello")):
            return "你好。我是妖币监控的问答端，发 /help 看我会什么。"
        return HELP

    # ------------------------------------------------------------------ answers
    def _ranked(self, sym=None):
        rk = self.monitor.last_ranked or []
        if sym:
            for r in rk:
                if r.get("sym") == sym:
                    return r
            return None
        return rk

    def ans_status(self):
        m = self.monitor
        ss = m.scan_summary or {}
        rk = m.last_ranked or []
        counts = {}
        for r in rk:
            counts[r.get("label", "?")] = counts.get(r.get("label", "?"), 0) + 1
        seg = " ｜ ".join(f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
        lines = [
            "📊 监控状态",
            f"监控池 {getattr(m, 'universe_source', '?')} · {len(m.universe)} 币",
            f"本轮有效 {ss.get('ok', '?')}/{ss.get('attempted', '?')} · 跳过 {ss.get('skipped', '?')}"
            + (f"（{ss.get('skip_reasons')}）" if ss.get("skip_reasons") else ""),
            f"上次扫描 {m.last_scan_iso} · 间隔 {m.scan_interval} 分钟",
            f"看板 https://yaobi.zeabur.app/",
        ]
        if seg:
            lines.append(f"标签分布：{seg}")
        if not rk:
            lines.append("（本轮还没有扫描数据）")
        return "\n".join(lines)

    def ans_value(self):
        """Conclusion board: EVIDENCE-ranked ambush candidates + aging
        verdicts — the "which targets are actually worth ambushing" answer,
        as opposed to the raw /ambush list."""
        rk = self.monitor.last_ranked or []
        if not rk:
            return "本轮还没有扫描数据，稍后再问（或发 /status 看扫描状态）。"
        entries = ambush_tracker.load()["entries"]
        lw = insights.launch_window_stats(entries)
        rows, n_cands = insights.value_board(rk, entries)
        if not rows:
            return "当前没有横盘蓄力（COILING）候选，价值榜为空。刚启动的币发 /ambush 看全量。"
        lines = insights.board_lines(rows, n_cands, lw)
        ag = insights.aging_lines(entries, lw=lw)
        if ag:
            lines.append("")
            lines += ag
        lines.append("")
        lines.append("全量观察列表：/ambush all；单个币判定：/check 币名；明细看板 https://yaobi.zeabur.app/")
        return "\n".join(lines)

    def _action_of(self, r):
        """Mirror of the dashboard action column: collapse volume-signal x
        15m-timing into ONE verdict. Returns (key, text) or (None, None)
        when the coin is not actionable right now."""
        lab = r.get("label")
        vt = bool(r.get("vol_tick"))
        st = r.get("vt_streak") if isinstance(r.get("vt_streak"), (int, float)) else 0
        t = r.get("timing") or {}
        has_t = t.get("ready") in (True, False)
        if lab == "COILING" and vt:
            if st >= 2:
                return ("go", "✅ 可入场") if (has_t and t.get("ready")) else ("wait", "⏳ 挂单等点位（等回踩企稳）")
            return ("chk", "👀 量能首现·等下轮确认")
        if lab == "COILING_LITE" and vt:
            if st >= 2:
                return ("go2", "✅ 次级可入场·半仓") if (has_t and t.get("ready")) else ("wait", "⏳ 次级·挂单等点位")
            return ("chk", "👀 次级·量能首现")
        return (None, None)

    def ans_ambush(self, full=False):
        """Action-first (2026-10-21 user review: "我只需要确切的入场信息").
        Default shows ONLY coins with volume ticking (the ambush-eligible
        slice) each carrying a one-line verdict; full=True keeps the legacy
        raw watch list for completeness."""
        def _evkey(r):
            if isinstance(r.get("ev_pts"), (int, float)):
                return -r["ev_pts"]
            p = r.get("potential")
            return -(p if isinstance(p, (int, float)) else (r.get("score") or 0))
        rk = sorted([r for r in (self.monitor.last_ranked or [])
                     if r.get("label") == "COILING"], key=_evkey)
        lite = sorted([r for r in (self.monitor.last_ranked or [])
                       if r.get("label") == "COILING_LITE"], key=_evkey)
        if not rk and not lite:
            return "当前没有 COILING（横盘蓄力）币。"

        if not full:
            act = [(self._action_of(r), r) for r in rk + lite]
            act = [(k, txt, r) for (k, txt), r in act if k]
            if not act:
                n_first = sum(1 for r in rk + lite if r.get("vol_tick"))
                return ("当前无埋伏信号——量能全部未抬头，无可入场标的。\n"
                        f"（观察池 {len(rk)} 蓄力 + {len(lite)} 次级；量能首现 {n_first} 个。"
                        "触发即推送 🔥 埋伏触发·可入场，无需盯盘。）\n"
                        "全量观察列表：/ambush all")
            order = {"go": 0, "go2": 1, "wait": 2, "chk": 3}
            act.sort(key=lambda x: (order.get(x[0], 9), _evkey(x[2])))
            lines = [f"🔥 可入场信号（{len(act)} 个，按动作优先级排序）："]
            for k, txt, r in act[:15]:
                fd, oc = r.get("funding"), r.get("oi_chg_24h")
                dv = []
                if isinstance(fd, (int, float)):
                    dv.append(f"费率{fd*100:+.3f}%")
                if isinstance(oc, (int, float)):
                    dv.append(f"OI{oc*100:+.0f}%")
                dv_s = ("·" + "·".join(dv)) if dv else ""
                ev_s = ""
                if isinstance(r.get("ev_pts"), (int, float)):
                    ev_s = f" 证据{r['ev_pts']:.0f}({r.get('ev_note', '')})"
                lines.append(f" {txt} {r['sym']:<13} 潜力{r.get('potential',0):.0f}{ev_s} "
                             f"价{r['price']:.6g} vsMA99 {_f(r.get('pv'))}{dv_s}")
            lines.append("✅=信号确认+15m点位共振，现在可打仓 · ⏳=信号确认但点位未就绪，挂单位等回踩 · "
                         "👀=量能首现（防闪烁需连续2扫确认），等下一轮扫描。"
                         "只盯 ✅ 打仓位。全量观察：/ambush all")
            return "\n".join(lines)

        lines = []
        if rk:
            lines.append(f"🔥 观察候选列表（{len(rk)} 个，按证据分排序，前 12；均为观察态）：")
            for r in rk[:12]:
                _st = r.get("vt_streak") if isinstance(r.get("vt_streak"), (int, float)) else 0
                star = "★" if (r.get("vol_tick") and _st >= 2) else ("＋" if r.get("vol_tick") else " ")
                tm = r.get("timing") or {}
                tmk = "⚡" if tm.get("ready") else ("✋" if tm else " ")
                fd, oc = r.get("funding"), r.get("oi_chg_24h")
                dv = []
                if isinstance(fd, (int, float)):
                    dv.append(f"费率{fd*100:+.3f}%")
                if isinstance(oc, (int, float)):
                    dv.append(f"OI{oc*100:+.0f}%")
                dv_s = ("·" + "·".join(dv)) if dv else ""
                m1 = r.get("ma99_1h")
                pv1 = (r["price"] / m1 - 1.0) if (isinstance(m1, (int, float)) and m1 > 0) else None
                ev_s = ""
                if isinstance(r.get("ev_pts"), (int, float)):
                    ev_s = f" 证据{r['ev_pts']:.0f}({r.get('ev_note', '')})"
                lines.append(f" {star}{tmk} {r['sym']:<13} 潜力{r.get('potential',0):.0f}{ev_s} 分{r['score']:.0f} 价{r['price']:.6g} "
                             f"vsMA99 {_f(r.get('pv'))} 1hM99 {_f(pv1)}{dv_s}")
            lines.append("★=埋伏确认（量能连续 2 扫抬头）· ＋=量能首现（等下轮扫描确认）· "
                         "⚡=15m 点位就绪（价>VWAP 且 EMA9>20）· ✋=未就绪。"
                         "证据分=潜力+历史战绩+逼空燃料+点位（/value 看结论版），潜力=弹性×蓄力×燃料，"
                         "分=信号质量。只盯 ⚡ 打仓位。完整列表看网页看板。")
        if lite:
            lines.append("")
            lines.append(f"🔵 次级蓄力（{len(lite)} 个，大盘币新周期横盘，按证据分前 6，仓位小于主档）：")
            for r in lite[:6]:
                _st = r.get("vt_streak") if isinstance(r.get("vt_streak"), (int, float)) else 0
                star = "★" if (r.get("vol_tick") and _st >= 2) else ("＋" if r.get("vol_tick") else " ")
                ev_s = ""
                if isinstance(r.get("ev_pts"), (int, float)):
                    ev_s = f" 证据{r['ev_pts']:.0f}({r.get('ev_note', '')})"
                lines.append(f" {star} {r['sym']:<13} 潜力{r.get('potential',0):.0f}{ev_s} 分{r['score']:.0f} 价{r['price']:.6g} "
                             f"横盘{r.get('flat_run')}根 vsMA99 {_f(r.get('pv'))} "
                             f"振幅{(r.get('swing') or 0)*100:.0f}%")
        return "\n".join(lines)

    def ans_track(self, sym=None):
        st = ambush_tracker.stats()
        if not st["total"]:
            return "追踪器还没有任何埋伏记录（首次部署后自动开始积累；本地缓存回填只算有偏样本）。"
        if sym:
            entries = [e for e in ambush_tracker.load()["entries"] if e["sym"] == sym]
            if not entries:
                cur = self._ranked(sym)
                base = (f"{sym} 当前 {cur.get('label')}/{cur.get('stage')}" if cur
                        else f"{sym} 不在本轮监控池里")
                return f"{sym} 没有埋伏追踪记录。\n{base}"
            lines = [f"🎯 {sym} 埋伏追踪明细（{len(entries)} 笔）："]
            for e in sorted(entries, key=lambda x: x.get("ts", ""), reverse=True)[:8]:
                status = ambush_tracker.STATUS_CN.get(e["status"], e["status"])
                day = (e.get("ts") or "")[:10]
                peak = e.get("peak_gain")
                pk = f" 峰值{_f(peak)}" if isinstance(peak, (int, float)) else ""
                src = "" if e.get("source") == "live" else "（回填）"
                lines.append(f"  {day} 入场{e.get('entry_price', 0):.6g} → {status}{pk}{src}")
            return "\n".join(lines)
        # overall stats
        lines = [f"🎯 埋伏信号自检 · 共 {st['total']} 笔（实盘 {st['live']} / 回填 {st['backfill']}）"]
        if st["evaluated"]:
            lr = f"{st['launch_rate']:.0%}" if st["launch_rate"] is not None else "—"
            w3 = f"{st['win30_rate']:.0%}" if st["win30_rate"] is not None else "—"
            ap = f"{st['avg_peak']:+.0%}" if st["avg_peak"] is not None else "—"
            lines.append(f"已结算 {st['evaluated']}：启动率 {lr} · 启动后+30%达标率 {w3} · 平均峰值 {ap}")
        tiers = st.get("tiers") or {}
        lt = tiers.get("lite") or {}
        if lt.get("total"):
            l_lr = f"{lt['launch_rate']:.0%}" if lt.get("launch_rate") is not None else "—"
            l_w3 = f"{lt['win30_rate']:.0%}" if lt.get("win30_rate") is not None else "—"
            lines.append(f"其中次级档（lite）：{lt['total']} 笔 · 已结算 {lt.get('evaluated', 0)} · "
                         f"启动率 {l_lr} · +30%达标率 {l_w3}")
        lines.append(f"在途：等待启动 {st['open_waiting']} · 已启动跟踪 {st['open_launched']}")
        store = ambush_tracker.load()["entries"]
        open_w = [e for e in store if e["status"] == "waiting"]
        if open_w:
            coins = len({e["sym"] for e in open_w})
            lines.append(f"（在途 {st['open_waiting']} 笔信号，涉及 {coins} 个币——同一横盘期可能有多笔）")
            open_w.sort(key=lambda e: e.get("ts", ""))   # oldest first
            lines.append("等待最久的（越接近 45 天窗越可能超时离场）：")
            for e in open_w[:5]:
                d = _age_days(e.get("ts"))
                d_s = f"，已等 {d} 天" if isinstance(d, int) else ""
                lines.append(f"  {e['sym']} （{(e.get('ts') or '')[:10]} 入场{d_s}）")
            new_today = [e for e in open_w if _age_days(e.get("ts")) == 0]
            if new_today:
                lines.append(f"今天新增等待：{', '.join(sorted({e['sym'] for e in new_today})[:10])}"
                             + ("…" if len({e['sym'] for e in new_today}) > 10 else ""))
        return "\n".join(lines)

    def ans_review(self):
        """胜率复盘报告（2026-10-21 用户指令：复盘是为了计算胜率，验证这套
        程序是不是成功）。四刀切：结局分布 / 整体胜率 / 按月趋势 / 条件胜率。
        数据直接来自当前 tracker，每次发问实时重算。"""
        try:
            rv = insights.win_rate_review(ambush_tracker.load()["entries"])
        except Exception as e:
            return f"复盘计算失败：{e}"
        if not rv["evaluated"]:
            return (f"追踪器共 {rv['total']} 笔（在途 {rv['open_waiting']}），"
                    "还没有已结算的完整轮次，胜率暂无可算——结算自动进行，"
                    "一段时间后再发 /review。")
        lines = [f"📊 胜率复盘 · 已结算 {rv['evaluated']} 轮（tracker 共 {rv['total']} 笔，"
                 f"在途等待 {rv['open_waiting']} / 已启动 {rv['open_launched']}）"]
        # 1. outcome distribution
        _CN = {"win_strong": "✅ 大赢(+80%)", "win": "✅ 赢(+30%)",
               "weak": "🟡 弱(+10%)", "miss": "⚪ 启动未走远",
               "pre_fail": "🩸 破位止损(-20%)", "timeout": "⏳ 45天超时"}
        seg = " ｜ ".join(f"{_CN[k]} {v}笔({v/rv['evaluated']:.0%})"
                         for k, v in sorted(rv["dist"].items(),
                                            key=lambda kv: -kv[1]))
        lines.append(f"结局分布：{seg}")
        # 2. overall + per-source
        ov = rv["overall"] or {}
        ap = f"{ov.get('avg_peak', 0):+.0%}" if ov.get("avg_peak") is not None else "—"
        lr = f"{ov.get('launch_rate', 0):.0%}" if ov.get("launch_rate") is not None else "—"
        lines.append(f"整体胜率 {ov.get('rate', 0):.0%}（赢+大赢 / 已结算）· "
                     f"平均峰值 {ap} · 启动率 {lr}"
                     + (f" · 平均启动延迟 {rv['avg_delay']}天" if rv.get("avg_delay") is not None else ""))
        for src, label in (("live", "实盘"), ("backfill", "回填")):
            c = (rv["by_src"] or {}).get(src)
            if c and c["n"]:
                cap = f"{c['rate']:.0%}"
                lines.append(f"  {label}：{c['n']} 轮 · 胜率 {cap}"
                             + (f" · 平均峰值 {c['avg_peak']:+.0%}"
                                if c.get("avg_peak") is not None else ""))
        # 3. monthly trend (last 8 months)
        mon = list((rv["monthly"] or {}).items())[-8:]
        if len(mon) >= 2:
            seg = " ｜ ".join(f"{m}: {d['rate']:.0%}(n={d['n']})"
                             for m, d in mon if d.get("rate") is not None)
            if seg:
                lines.append(f"按月胜率：{seg}")
        # 4. conditional rates — the re-entry scoring evidence
        cond = rv["cond"] or {}
        def _cs(k, label):
            d = cond.get(k) or {}
            if not d.get("n"):
                return None
            return f"{label} {d['rate']:.0%}（n={d['n']}）"
        parts = [x for x in (_cs("last_win", "上轮胜→"),
                             _cs("fresh", "无历史首战→"),
                             _cs("lose1", "上轮败→"),
                             _cs("lose2p", "连败≥2→")) if x]
        if parts:
            lines.append("条件胜率（重入评分依据）：" + " ｜ ".join(parts))
        # verdict: honest, numbers-first
        lw = (cond.get("last_win") or {}).get("rate")
        fr = (cond.get("fresh") or {}).get("rate")
        l2 = (cond.get("lose2p") or {}).get("rate")
        verdict = []
        if isinstance(lw, float) and isinstance(fr, float) and fr > 0:
            verdict.append(f"重入规则{'有效' if lw > fr + 0.1 else '失效'}"
                           f"（上轮胜 {lw:.0%} vs 首战 {fr:.0%}）")
        if isinstance(l2, float):
            verdict.append("连败降权规则有效" if l2 < 0.15 else "连败降权依据不足")
        if ov.get("avg_peak") is not None:
            verdict.append("平均峰值为正，策略期望为正" if ov["avg_peak"] > 0
                           else "⚠️ 平均峰值为负，策略当前期望为负")
        if verdict:
            lines.append("结论：" + "；".join(verdict)
                         + "。（口径提醒：胜=峰值曾达+30%，不保证满仓吃到；滑点会吃掉一部分）")
        lines.append("每日战报在日报里；单币明细 /track 币名。")
        return "\n".join(lines)

    def ans_invalid(self, sym):
        """'信号失效了吗' — tracker entry status, deterministic."""
        if not sym:
            store = ambush_tracker.load()["entries"]
            open_w = [e for e in store if e["status"] == "waiting"]
            if not open_w:
                return "当前没有在途（等待启动）的埋伏信号，谈不上失效。发 \"/check 币名\" 可实时判定单个币。"
            lines = ["在途埋伏（都未失效，仍在 45 天等待窗内，按等待时长排序）："]
            for e in sorted(open_w, key=lambda x: x.get("ts", ""))[:10]:
                d = _age_days(e.get("ts"))
                d_s = f"（已等 {d} 天）" if isinstance(d, int) else ""
                lines.append(f"  {e['sym']} {(e.get('ts') or '')[:10]} 入场{d_s}")
            return "\n".join(lines)
        entries = [e for e in ambush_tracker.load()["entries"] if e["sym"] == sym]
        if not entries:
            return f"{sym} 没有埋伏追踪记录，不存在\"失效\"与否。发 /check {sym} 可实时判定当前能否埋伏。"
        e = sorted(entries, key=lambda x: x.get("ts", ""))[-1]
        status = e["status"]
        cn = ambush_tracker.STATUS_CN.get(status, status)
        if status == "waiting":
            cur = self._ranked(sym)
            extra = ""
            if cur:
                extra = (f"\n当前状态：{cur.get('label')}/{cur.get('stage')}，"
                         f"价 {cur.get('price'):.6g}（入场 {e.get('entry_price', 0):.6g}）。")
            return (f"{sym} 最近一笔埋伏（{(e.get('ts') or '')[:10]}）仍在等待启动，未失效。"
                    f"{extra}\n口径：45 天内启动=有效；启动前跌破 -20%=破位失效。")
        if status == "launched":
            return (f"{sym} 的埋伏信号已启动（第 {e.get('launch_delay_d')} 天触发），未失效——"
                    f"现在跟踪的是启动后 20 天峰值（当前峰值 {_f(e.get('peak_gain'))}）。")
        if status == "pre_fail":
            return f"{sym} 最近一笔埋伏已失效：启动前跌破 -20%（破位失败）。等它重新横盘会自动再评估。"
        if status == "timeout":
            return f"{sym} 最近一笔埋伏已失效：45 天未启动（超时）。横盘是死水不是蓄力。"
        # finalized post-launch outcomes
        return (f"{sym} 最近一笔埋伏已走完：{cn}"
                + (f"，峰值 {_f(e.get('peak_gain'))}" if isinstance(e.get("peak_gain"), (int, float)) else "")
                + "。复盘口径：启动后20天峰值 +80%/+30%/+10% = 大赢/赢/弱。")

    def ans_check(self, sym):
        """Live re-score + deterministic verdict for '还能埋伏吗'."""
        rec, err = score_coin(sym, market=self.monitor.market, force_fetch=True)
        stale = False
        if rec is None:
            rec = self._ranked(sym)
            stale = bool(rec)
            if rec is None:
                return (f"{sym} 拉不到数据（{err}），也不在本轮扫描结果里。"
                        f"\n确认币名拼写（如 BANKUSDT，含 USDT 后缀）。")
        # derivatives context (best-effort)
        try:
            fd = fetch_funding_all().get(sym)
            if fd is not None:
                rec["funding"] = fd
        except Exception:
            pass
        if rec.get("label") in ("COILING", "COILING_LITE", "FRESH_LAUNCH", "LAUNCHED"):
            # Coinalyze first (batched source, also serves liquidations + LSR
            # that Binance doesn't), Binance per-coin as backstop
            try:
                import coinalyze
                if coinalyze.has_key():
                    om = coinalyze.fetch_oi_batch([sym]) or {}
                    if isinstance(om.get(sym), (int, float)):
                        rec["oi_chg_24h"] = round(om[sym], 4)
                    lm = coinalyze.fetch_liq_batch([sym]) or {}
                    if lm.get(sym):
                        rec["liq_long_24h"], rec["liq_short_24h"] = lm[sym]
                    rm = coinalyze.fetch_lsr_batch([sym]) or {}
                    if isinstance(rm.get(sym), (int, float)):
                        rec["lsr"] = round(rm[sym], 2)
            except Exception:
                pass
            if "oi_chg_24h" not in rec:
                try:
                    oc = fetch_oi_change(sym)
                    if oc is not None:
                        rec["oi_chg_24h"] = oc
                except Exception:
                    pass

        label = rec.get("label")
        verdicts = {
            "COILING_VOL_TICK": "✅ 接近可埋伏：横盘蓄力 + 量能抬头。\n⚠️ 注意：/check 是单次快照，正式埋伏信号需量能连续 2 扫确认（系统自动判定并推送 🔥 埋伏触发·可入场）。\n打法：横盘区内即可分批入场（-20%破位止损，45天等待窗）；\n或等确认推送 / 🚀ENTER（第一根巨量突破）再入——更稳但入场价更高。",
            "COILING": "🟡 可观察但未触发：在横盘，但量能还没抬头。把它放进观察列表等量变。",
            "COILING_LITE_VOL_TICK": "✅ 次级接近可埋伏：大盘币新周期横盘 + 量能抬头（单次快照，双扫确认以推送为准）。振幅未达妖币门槛，仓位应小于主档。等巨量突破。",
            "COILING_LITE": "🔵 次级蓄力可观察：大盘币新周期横盘（振幅未达妖币门槛），量能还没抬头。小仓位观察，等量变。",
            "FRESH_LAUNCH": "🚀 埋伏窗口已过：已启动（S2）。剧本动作是等 S3 洗盘 30-60% 低吸，做 S3→S4。",
            "LAUNCHED": "👀 已启动且动能可能衰减（>8根4h）。关注是否再洗盘，别追高。",
            "PUMPED": "⚠️ 不可埋伏：已在主升/顶部段（S4/S5）。剧本：太晚/高危，等下一轮。",
            "CRASHING": "🩸 不可埋伏：崩盘中（S6）。别接刀，等重新横盘。",
            "LOW_VOL": "⚪ 死水：波动太低（20日振幅<130% 门槛），不是妖币标的。",
            "STABLE": "⚪ 稳定币，非标的。",
            "STOCK": "⚪ 代币化股票，非标的。",
        }
        vt = bool(rec.get("vol_tick"))
        key = ("COILING_VOL_TICK" if (label == "COILING" and vt)
               else "COILING_LITE_VOL_TICK" if (label == "COILING_LITE" and vt)
               else label)
        verdict = verdicts.get(key, f"当前标签 {label} / 阶段 {rec.get('stage')}。")
        try:
            from memecoin_scanner import potential_of
            if label in ("COILING", "COILING_LITE"):
                rec["potential"] = potential_of(rec)
        except Exception:
            pass
        pot = rec.get("potential")
        pot_s = f" · 潜力 {pot:.0f}" if isinstance(pot, (int, float)) else ""
        m1 = rec.get("ma99_1h")
        pv1 = (rec["price"] / m1 - 1.0) if (isinstance(m1, (int, float)) and m1 > 0 and rec.get("price")) else None
        fd, oc = rec.get("funding"), rec.get("oi_chg_24h")
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
        lines = [
            f"🔎 {sym} 实时判定" + ("（离线数据，可能陈旧）" if stale else ""),
            f"阶段 {rec.get('stage')} · 标签 {rec.get('label')} · 分 {rec.get('score', 0):.0f}{pot_s}",
            f"价 {rec.get('price'):.6g} · vsMA99 {_f(rec.get('pv'))} · 1hM99 {_f(pv1)}",
            rec.get("reason") or "",
        ]
        if dv:
            squeeze = (isinstance(fd, (int, float)) and fd < 0
                       and isinstance(oc, (int, float)) and oc > 0.10)
            lines.append("·".join(dv) + ("（逼空燃料充足）" if squeeze else ""))
        # entry timing gate (15m VWAP + 9/20 EMA) — best-effort, live fetch
        try:
            import timing as _timing
            _mk = getattr(self.monitor, "market", "futures")
            t = _timing.analyze(sym, market=_mk)
            env = _timing.env_regime(market=_mk) or {}
            rg = env.get("r") or _timing.regime(market=_mk)
        except Exception:
            t, rg, env = None, None, {}
        if t:
            vd = t.get("vwap_dist")
            vd_s = f"{vd*100:+.1f}%" if isinstance(vd, (int, float)) else "—"
            rg_s = {"bullish": "多头", "bearish": "空头", "neutral": "中性"}.get(rg, "—")
            gate = "就绪 ⚡（价>VWAP 且 EMA9>20）" if t.get("ready") else "未就绪 ✋"
            warn = "（大盘逆风：新仓减半或推迟）" if rg == "bearish" else ""
            legs = ""
            if env.get("r4h") or env.get("r1d"):
                legs = f"（4h {env.get('r4h') or '—'}/1d {env.get('r1d') or '—'}）"
            lines.append(f"📍 点位（15m）：{gate} · 价 vs VWAP {vd_s} · "
                         f"EMA{'多头' if t.get('ema_bull') else '空头'} · 大盘环境 4h/1d {rg_s}{legs}{warn}")
        lines.append("")
        lines.append(verdict)
        # tracker context if present
        entries = [e for e in ambush_tracker.load()["entries"] if e["sym"] == sym]
        if entries:
            e = sorted(entries, key=lambda x: x.get("ts", ""))[-1]
            cn = ambush_tracker.STATUS_CN.get(e["status"], e["status"])
            lines.append(f"追踪：{(e.get('ts') or '')[:10]} 那笔 → {cn}")
        return "\n".join(lines)


if __name__ == "__main__":
    # offline smoke: intent routing only (no network, no telegram)
    class FakeNotifier:
        bot_token, chat_id = "x", "1"
    class FakeMonitor:
        stop = True
        last_ranked, scan_summary = [], {}
        universe, market, scan_interval, last_scan_iso = [], "futures", 120, "-"
        universe_source = "test"
    ag = TelegramAgent(FakeNotifier(), FakeMonitor())
    # hermetic: stub the network calls ans_check would make (Binance is
    # unreachable from some networks; the smoke must stay offline)
    _real_score, _real_fund = score_coin, fetch_funding_all
    score_coin = lambda sym, market=None, force_fetch=False: (
        {"sym": sym, "label": "COILING", "stage": "S1", "score": 80,
         "price": 0.001, "reason": "smoke", "pv": 0.10, "vol_tick": True}, None)
    fetch_funding_all = lambda: {sym: -0.001}
    # hermetic part 2: ans_check also touches fetch_oi_change / coinalyze /
    # timing (15m VWAP gate) — all network. Stub all three or a blocked
    # network can HANG the smoke (connection blackhole, not fast-fail).
    _real_oi = fetch_oi_change
    fetch_oi_change = lambda s: None
    import coinalyze as _cz
    _real_cz_key = _cz.has_key
    _cz.has_key = lambda: False
    import timing as _t
    _real_t_an, _real_t_rg = _t.analyze, _t.regime
    _t.analyze = lambda sym, market="futures": None
    _t.regime = lambda market="futures": None
    for q in ["/help", "/HELP", "状态", "埋伏列表", "追踪", "BANKUSDT 还能埋伏吗",
              "BANKUSDT 信号失效了吗", "失效", "/check", "你好"]:
        r = ag.answer(q)
        print(f"--- {q!r} ->\n{r}\n" if r else f"--- {q!r} -> None\n")
    score_coin, fetch_funding_all = _real_score, _real_fund
    fetch_oi_change = _real_oi
    _cz.has_key = _real_cz_key
    _t.analyze, _t.regime = _real_t_an, _real_t_rg
    # regression test for the poll-loop bug: _api returns the FULL dict
    # {"ok":..., "result":[...]}; iterating the dict itself yields keys and
    # crashed with TypeError on u["update_id"] — updates were never handled.
    import ambush_tracker as _at
    _at.TRACK_PATH = "data/ambush_track_smoke.json"
    seen = []
    ag2 = TelegramAgent(FakeNotifier(), FakeMonitor())
    FakeMonitor.stop = False  # let the poll loop enter; fake_api flips it back
    def fake_api(method, **params):
        if method == "getUpdates":
            FakeMonitor.stop = True  # one iteration then exit
            return {"ok": True, "result": [{
                "update_id": 42,
                "message": {"chat": {"id": 1}, "text": "/HELP"},
            }]}
        return {"ok": True}
    def fake_handle(msg):
        seen.append(msg.get("text"))
    ag2._api = fake_api
    ag2._handle = fake_handle
    ag2._run()
    assert seen == ["/HELP"], f"poll loop did not handle the update: {seen}"
    print("poll-loop regression OK: update handled through full response dict")
    print("routing smoke OK")
