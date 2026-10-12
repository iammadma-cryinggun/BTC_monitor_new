"""
24h Memecoin 埋伏监控器 —— 部署到 Zeabur 云端长期运行。

它做什么（你手动交易，它只报警）:
  - 周期性扫描 Binance USD-M 合约妖币池（默认 exchange 全量合约，约 400 个；拉取失败回退 FUTURES_UNIVERSE ~82）
  - 用六阶段剧本检测器 (memecoin_playbook_scanner.detect) 判定每个币当前阶段
  - 跨扫描维护 state.json，只在「阶段/事件发生转移」时推送信号:
        🚀 ENTER   新启动 (S2 第一根巨量突破)  -> 进观察列表做 S3->S4
        💧 DIP     S3 洗盘 -> 可低吸
        🔥 AMBUSH  横盘蓄力 + 量能抬头 -> 加入埋伏观察，等启动
        ⚠️ TOP     4h 插针见顶 -> 至少减一半
        📉 MA25    放量跌破 4h MA25 -> 再减一半 (规则#6)
        🚪 MA99    跌破 4h MA99 -> 全部离场 (规则#7)
        🩸 CRASH   崩盘 -> 别接刀
        ↩️ BOUNCE  崩后巨量反弹 -> 阶段6不是底，别追
  - 每天本地 digest_hour 点推送「当日埋伏观察列表」(哪些币值得盯)

防封策略:
  - 只访问公开行情 klines（无需 API key，权重极低）
  - 每个币之间 sleep（默认 0.15s），整轮约 30~90s，远低于 Binance 限制
  - 多 host 轮换 + 失败指数退避（memecoin_scanner.fetch_klines 已实现）
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
from memecoin_scanner import score_coin, FUTURES_UNIVERSE, get_universe  # reuse detection
from notifier import Notifier
import dashboard
from dashboard import DashboardServer, append_signal, load_signals, write_dashboard

STATE_PATH = os.path.join(HERE, "data", "monitor_state.json")
DIGEST_PATH = os.path.join(HERE, "data", "daily_digest.html")
DATA = os.path.join(HERE, "data")

SIG_EMOJI = {"ENTER": "🚀", "DIP": "💧", "AMBUSH": "🔥", "TOP": "⚠️",
             "MA25": "📉", "MA99": "🚪", "CRASH": "🩸", "BOUNCE": "↩️"}
SIG_ORDER = ["ENTER", "AMBUSH", "DIP", "TOP", "MA25", "MA99", "CRASH", "BOUNCE"]

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


def event_sig(e):
    return f"{e.get('type')}@{e.get('ts')}"


def classify(sym, rec, prev):
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
            signals.append(("ENTER",
                f"{sym} 新启动 (S2)：横盘后第一根巨量突破（{note}）。\n"
                f"按剧本进观察列表，等待 S3 洗盘后做 S3→S4 主升。", e))
        elif t == "SHAKEOUT":
            signals.append(("DIP",
                f"{sym} 进入 S3 洗盘（{note}）。\n"
                f"这是剧本里的低吸区，用 4h 移动止损，别用固定止损。", e))
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
            signals.append(("BOUNCE",
                f"{sym} 崩后巨量反弹（{note}）。这是阶段6，不是底，别追。", e))
    # 埋伏★：COILING + 量能刚抬头（且之前不是这个状态）
    ambush_now = (rec["label"] == "COILING" and rec.get("vol_tick"))
    ambush_prev = bool(prev and prev.get("label") == "COILING" and prev.get("vol_tick"))
    if ambush_now and not ambush_prev:
        pv = rec.get("pv")
        pv_s = f"{pv*100:+.0f}%" if pv is not None else "n/a"
        signals.append(("AMBUSH",
            f"{sym} 埋伏★：横盘蓄力中量能抬头。\n"
            f"横盘 {rec.get('flat_run')} 根 4h，区间 {rec.get('range_w',0)*100:.1f}% 宽，"
            f"价 vs 4h MA99 = {pv_s}。\n加入观察列表，等第一根巨量突破再动手。", None))
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

    def next_scan_epoch(self, last_scan_ts=None):
        try:
            if last_scan_ts is None:
                last_scan_ts = self.last_scan_iso
            base = datetime.fromisoformat(last_scan_ts).timestamp()
        except Exception:
            base = time.time()
        return int(base + self.scan_interval * 60)

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
    def run_scan(self, emit=True):
        state = self.load_state()
        state_out = dict(state)
        ranked = []
        new_signals = []
        t0 = time.time()
        n_ok = 0
        skip_reasons = {}
        offline = os.environ.get("MONITOR_OFFLINE") == "1"
        for sym in self.universe:
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
            ranked.append(rec)
            prev = state.get(sym)
            signals, cur_sigs = classify(sym, rec, prev)
            state_out[sym] = {
                "label": rec["label"], "stage": rec["stage"], "score": rec["score"],
                "vol_tick": rec.get("vol_tick"), "price": rec["price"],
                "pv": rec.get("pv"), "ma99_1h": rec.get("ma99_1h"),
                "flat_run": rec.get("flat_run"), "range_w": rec.get("range_w"),
                "event_sigs": list(cur_sigs), "updated": now_iso(),
                "first_seen": (prev.get("first_seen") if prev else now_iso()),
            }
            if emit and prev is not None:   # skip signal emission on very first baseline
                for lvl, msg, ev in signals:
                    new_signals.append((lvl, sym, msg, ev))
            time.sleep(self.request_sleep)
        self.save_state(state_out)
        self.last_ranked = ranked
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
        if self.first_run:
            self.first_run = False
            if emit:
                self.run_digest()  # give an initial overview on first baseline
        if emit:
            for lvl, sym, msg, ev in new_signals:
                self.log_signal(lvl, sym, msg, ev=ev["type"] if isinstance(ev, dict) else None)
                self.notifier.send(f"{SIG_EMOJI.get(lvl,'')} {lvl} 信号 · {sym}", msg, level=lvl)
        self.write_dashboard()
        return ranked

    # ---- daily digest ----
    def run_digest(self):
        ranked = self.last_ranked or self.run_scan(emit=False) or []
        coiling = sorted([r for r in ranked if r["label"] == "COILING"], key=lambda r: -r["score"])
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
        lines.append(f"监控池 {src} · 池大小 {len(self.universe)} · 本轮有效 {len(ranked)} 币 | 埋伏候选 {len(coiling)} | 刚启动 {len(fresh)}")
        lines.append("")
        lines.append("🔥 埋伏观察列表（横盘蓄力，等第一根巨量启动）:")
        if coiling:
            for r in coiling[:18]:
                star = "★" if r.get("vol_tick") else " "
                pv = r.get("pv")
                pv_s = f"{pv*100:+.0f}%" if pv is not None else "n/a"
                m1 = r.get("ma99_1h")
                pv1 = (r["price"] / m1 - 1.0) if (isinstance(m1, (int, float)) and m1 > 0) else None
                pv1_s = f"{pv1*100:+.1f}%" if pv1 is not None else "—"
                lines.append(f"  {star} {r['sym']:<12} 分{r['score']:.0f}  价{r['price']:.6g}  "
                             f"区间{r['range_w']*100:.1f}%  量能{'抬头' if r.get('vol_tick') else '静'}  "
                             f"vsMA99 {pv_s}  1hM99 {pv1_s}")
        else:
            lines.append("  （暂无）")
        lines.append("")
        lines.append("⚡ 近期启动（做 S3→S4）:")
        if fresh:
            for r in fresh:
                m1 = r.get("ma99_1h")
                pv1 = (r["price"] / m1 - 1.0) if (isinstance(m1, (int, float)) and m1 > 0) else None
                pv1_s = f"1hM99 {pv1*100:+.1f}%  " if pv1 is not None else ""
                lines.append(f"  {r['sym']:<12} S{r['stage'][-1]}  价{r['price']:.6g}  {pv1_s}{r['reason']}")
        else:
            lines.append("  （暂无）")
        if launched:
            lines.append("")
            lines.append("👀 已启动动能衰减（按启动先后排列，可关注是否再洗盘）:")
            for r in launched[:8]:
                rec = r.get("launch_recency")
                rec_s = f"{rec}根4h前" if isinstance(rec, int) else "?"
                lines.append(f"  {r['sym']:<12} 价{r['price']:.6g}  启动于{rec_s}  {r['reason']}")
        if risk:
            lines.append("")
            lines.append("⚠️ 需警惕（已拉完/崩盘，别接刀；按近1日振幅排序）:")
            for r in risk[:10]:
                dm = r.get("day_move")
                dm_s = f"{dm*100:+.1f}%" if isinstance(dm, (int, float)) else "—"
                lines.append(f"  {r['sym']:<12} {r['label']} 价{r['price']:.6g}  ~1日{dm_s}")
        lines.append("")
        lines.append("信号会通过本渠道实时推送。手动交易，纪律第一。")
        text = "\n".join(lines)
        self.notifier.send(f"📅 每日妖币监控 · {date}", text, level="digest")
        self.log_signal("DIGEST", "", f"每日摘要 · 监控 {len(ranked)} 币 · 埋伏 {len(coiling)} · 刚启动 {len(fresh)}")
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
            if now - last_scan >= self.scan_interval * 60:
                self.run_scan(emit=True)
                last_scan = now
            local = self.utc_to_local(now)
            if local.hour == self.digest_hour and local.minute < 12 and local.date() != last_digest_date:
                self.run_digest()
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
