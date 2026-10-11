"""
Live web dashboard for the memecoin 埋伏 monitor.

Architecture (anti-cache, robust on PaaS / CDN edge):
  - The Python HTTP server generates the page SHELL fresh on every request
    (an inline HTML string + embedded JS), with `Cache-Control: no-store`.
  - The shell's JS fetches `/dashboard_data.json?<ts>` every 30s. That JSON is
    also served fresh from disk on every request with no-store.
  - There is NO static dashboard.html that a CDN can cache, so the page can
    never show a stale snapshot.

monitor.py writes `data/dashboard_data.json` after every scan/digest (and still
writes a static `dashboard.html` for offline inspection). This server serves
both: `/` -> live shell, `/dashboard_data.json` -> data, `/dashboard.html` ->
the static snapshot.

No Binance calls here — pure presentation.
"""
import os, json, time, threading
import http.server, socketserver
from datetime import datetime, timezone
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DASH_PATH = os.path.join(DATA, "dashboard.html")
DATA_JSON_PATH = os.path.join(DATA, "dashboard_data.json")
SIGNALS_PATH = os.path.join(DATA, "signals_log.json")

SIG_EMOJI = {
    "ENTER": "🚀", "DIP": "💧", "AMBUSH": "🔥", "TOP": "⚠️",
    "CRASH": "🩸", "BOUNCE": "↩️", "INFO": "ℹ️", "DIGEST": "📅",
}

# ----------------------------------------------------------------------------
# signals log (persisted so the web feed survives restarts)
def load_signals():
    if os.path.exists(SIGNALS_PATH):
        try:
            with open(SIGNALS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def append_signal(level, sym, msg, ev_type=None):
    sigs = load_signals()
    sigs.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": (level or "INFO").upper(),
        "sym": sym or "",
        "msg": msg or "",
        "ev": ev_type,
    })
    sigs = sigs[-200:]
    os.makedirs(DATA, exist_ok=True)
    with open(SIGNALS_PATH, "w", encoding="utf-8") as f:
        json.dump(sigs, f, indent=2, default=str)
    return sigs

def _ts_to_epoch(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0

# ----------------------------------------------------------------------------
def _spark_svg(vals, w=120, h=30, color="#3b82f6"):
    if not vals or len(vals) < 2:
        return ""
    v = np.array(vals, dtype=float)
    rng = float(v.max() - v.min())
    v = (v - v.min()) / (rng + 1e-12)
    pts = " ".join(f"{i/(len(v)-1)*w:.1f},{h-(x*h):.1f}" for i, x in enumerate(v))
    return (f'<svg width="{w}" height="{h}"><polyline points="{pts}" fill="none" '
            f'stroke="{color}" stroke-width="1.4"/></svg>')

# ----------------------------------------------------------------------------
def _disp_label(r):
    lab = r.get("label")
    if lab == "COILING":
        return "🔥埋伏★" if r.get("vol_tick") else "观察"
    return {"FRESH_LAUNCH": "⚡刚启动", "LAUNCHED": "已启动", "PUMPED": "已拉完",
            "CRASHING": "崩盘中", "LOW_VOL": "低波动", "STABLE": "稳定币",
            "STOCK": "代币股", "UNKNOWN": "未知"}.get(lab, str(lab))

LAB_COLOR = {"COILING": "#f59e0b", "FRESH_LAUNCH": "#22c55e", "LAUNCHED": "#0ea5e9",
             "PUMPED": "#ef4444", "CRASHING": "#6b7280", "LOW_VOL": "#4b5563",
             "STABLE": "#4b5563", "STOCK": "#4b5563", "UNKNOWN": "#9ca3af"}

# ----------------------------------------------------------------------------
def build_snapshot(ranked, signals, meta=None):
    """Build the JSON payload the live shell renders. Pure data, no HTML."""
    meta = meta or {}
    ranked = ranked or []
    n_coins = len(ranked)
    n_amb = sum(1 for r in ranked if r.get("label") == "COILING" and r.get("vol_tick"))
    n_fresh = sum(1 for r in ranked if r.get("label") == "FRESH_LAUNCH")
    try:
        cutoff = time.time() - 86400
        n_sig24 = sum(1 for s in signals if _ts_to_epoch(s.get("ts")) >= cutoff)
    except Exception:
        n_sig24 = len(signals)
    ss = meta.get("scan_summary") or {}
    return {
        "meta": {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "last_scan": meta.get("last_scan") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "next_scan_epoch": meta.get("next_scan_epoch"),
            "market": meta.get("market", "futures"),
            "scan_interval_minutes": meta.get("scan_interval_minutes", 120),
            "universe_source": meta.get("universe_source", "curated"),
            "universe_size": meta.get("universe_size", n_coins),
            "scan_summary": ss,
        },
        "kpis": {
            "pool_size": meta.get("universe_size", n_coins),
            "retest_ok": n_coins,
            "ambush": n_amb,
            "fresh": n_fresh,
            "sig24": n_sig24,
        },
        "ranked": ranked,
        "signals": signals or [],
    }

def write_dashboard(ranked, signals, meta=None, path=DASH_PATH):
    """Write both the static HTML snapshot (offline viewing) and the live
    dashboard_data.json (used by the web shell)."""
    meta = meta or {}
    # live JSON (primary source for the page)
    snap = build_snapshot(ranked, signals, meta)
    os.makedirs(DATA, exist_ok=True)
    tmp = DATA_JSON_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, DATA_JSON_PATH)
    # static HTML snapshot (for opening the file directly / archival)
    try:
        html = build_dashboard_html(ranked, signals, meta)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[dashboard] static html write skipped: {e}")
    return DATA_JSON_PATH

# ----------------------------------------------------------------------------
# Static HTML builder (kept for offline / archival use and email digests).
def build_dashboard_html(ranked, signals, meta=None):
    meta = meta or {}
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    last_scan = meta.get("last_scan") or gen
    next_epoch = meta.get("next_scan_epoch")
    market = meta.get("market", "futures")
    interval = meta.get("scan_interval_minutes", 120)
    ranked = ranked or []
    n_coins = len(ranked)
    n_amb = sum(1 for r in ranked if r.get("label") == "COILING" and r.get("vol_tick"))
    n_fresh = sum(1 for r in ranked if r.get("label") == "FRESH_LAUNCH")
    try:
        cutoff = time.time() - 86400
        n_sig24 = sum(1 for s in signals if _ts_to_epoch(s.get("ts")) >= cutoff)
    except Exception:
        n_sig24 = len(signals)

    ss = meta.get("scan_summary") or {}
    diag = ""
    if ss:
        attempted = ss.get("attempted", 0)
        ok = ss.get("ok", 0)
        skipped = ss.get("skipped", 0)
        reasons = ss.get("skip_reasons", {})
        rtxt = "  ".join(f"{k}:{v}" for k, v in reasons.items())
        color = "#f87171" if skipped > ok else "#9ca3af"
        diag = (f'<div class="note" style="color:{color}">'
                f'扫描诊断：尝试 {attempted} · 成功 {ok} · 跳过 {skipped}'
                + (f'（{rtxt}）' if rtxt else '')
                + ' ｜ 跳过多为 symbol 已被下架/不在该市场，或瞬时被限流（下一轮缓存命中即可恢复）</div>')

    watch = [r for r in ranked if r.get("label") in ("COILING", "FRESH_LAUNCH")]
    watch.sort(key=lambda r: -r.get("score", 0))
    others = [r for r in ranked if r.get("label") not in ("COILING", "FRESH_LAUNCH")]
    others.sort(key=lambda r: -r.get("score", 0))
    capped = len(others) > 150
    others_disp = others[:150] if capped else others
    ordered = watch + others_disp

    watch_rows = "".join(_row(r) for r in watch) or '<tr><td colspan="9" class="rs">（暂无埋伏候选）</td></tr>'
    full_rows = "".join(_row(r) for r in ordered) or '<tr><td colspan="9" class="rs">（无数据）</td></tr>'

    sig_rows = ""
    for s in signals[-30:][::-1]:
        emoji = SIG_EMOJI.get((s.get("level") or "").upper(), "•")
        ts = (s.get("ts") or "")[:16].replace("T", " ")
        sym = s.get("sym", "")
        msg = (s.get("msg") or "").replace("\n", " ").strip()
        if len(msg) > 90:
            msg = msg[:90] + "…"
        sig_rows += (f'<tr><td class="ts">{ts}</td><td class="em">{emoji}</td>'
                     f'<td class="ssym">{sym}</td><td class="smsg">{msg}</td></tr>')
    if not sig_rows:
        sig_rows = '<tr><td colspan="4" class="smsg">（暂无信号）</td></tr>'

    next_epoch_js = int(next_epoch) if isinstance(next_epoch, (int, float)) else 0
    u_src = meta.get("universe_source", "curated")
    u_size = meta.get("universe_size", n_coins)
    u_color = "#f87171" if u_src.startswith("curated") and u_size <= 90 else "var(--mut)"
    pool_line = f'监控池 <b style="color:{u_color}">{u_src}</b> · 池大小 {u_size} · 本轮有效 {n_coins}'

    health = ""
    feed_broken = bool(ss) and u_size and n_coins < max(1, int(u_size * 0.5))
    if feed_broken:
        rtxt = "  ".join(f"{k}:{v}" for k, v in ss.get("skip_reasons", {}).items())
        health = (f'<div class="note" style="color:#f87171;margin-top:8px">'
                  f'⚠️ <b>数据获取异常</b>：本轮回测仅成功 <b>{n_coins}/{u_size}</b> 个币。'
                  f'Binance 行情拉取很可能被部署区域（云服务器常见）封锁。<br>'
                  f'排查：① 设置 <b>HTTPS_PROXY</b> 代理（指向 Binance 友好区域）；'
                  f'② 把 Zeabur 区域切到 Binance 可访问区；'
                  f'③ 首轮失败会在缓存命中后自动恢复。<br>'
                  f'跳过原因：{rtxt or "网络/超时"}。</div>')

    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>妖币埋伏监控 · 实时看板</title>
<style>
:root{{--bg:#0f1115;--card:#161a22;--bd:#1f2937;--tx:#e5e7eb;--mut:#9ca3af}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--tx);margin:0;padding:20px}}
h1{{font-size:20px;margin:0 0 2px}} .sub{{color:var(--mut);font-size:12px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin-bottom:16px}}
.kpis{{display:flex;gap:10px;flex-wrap:wrap}}
.kpi{{background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:10px 14px;font-size:13px;color:var(--mut)}}
.kpi b{{display:block;font-size:20px;color:#fff;margin-top:2px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{padding:7px 9px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:middle}}
th{{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}}
.sym{{font-weight:700;color:#fff}} .tag{{color:#0f1115;font-weight:700;padding:2px 8px;border-radius:10px;font-size:11px;white-space:nowrap}}
.sc{{font-weight:700;font-size:15px}} .rs{{color:#cbd5e1;max-width:360px}}
.ts{{color:var(--mut);font-size:11px;white-space:nowrap}} .em{{font-size:15px}}
.ssym{{color:#fff;font-weight:600}} .smsg{{color:#cbd5e1}}
.h2{{font-size:15px;margin:0 0 10px;color:#fff}} .note{{color:var(--mut);font-size:11px;margin-top:8px}}
.cnt{{color:#f59e0b;font-weight:700}}
</style></head><body>
<h1>🐸 妖币埋伏监控 · 实时看板</h1>
<div class="sub">生成 {gen} ｜ 数据源 Binance USD-M 合约 ｜ 4h 六阶段 + 1h MA99 ｜ 时区 UTC ｜ 每 {interval} 分钟扫描一次（本页每 30 秒自动刷新）</div>
<div class="note" style="margin-bottom:12px">{pool_line}</div>

<div class="card">
  <div class="kpis">
    <div class="kpi">监控币数 <b>{u_size}</b></div>
    <div class="kpi">本轮回测有效 <b style="color:{'#f87171' if (u_size and n_coins < u_size*0.5) else '#fff'}">{n_coins}</b></div>
    <div class="kpi">🔥埋伏★ <b style="color:#f59e0b">{n_amb}</b></div>
    <div class="kpi">⚡刚启动 <b style="color:#22c55e">{n_fresh}</b></div>
    <div class="kpi">近24h信号 <b>{n_sig24}</b></div>
  </div>
  <div class="note">上次扫描：{last_scan} ｜ 距下次扫描：<span id="cd" class="cnt">—</span> ｜ 手动交易，纪律第一，本看板只呈现信号、不代下单。</div>
  {diag}
  {health}
</div>

<div class="card">
  <div class="h2">🔥 埋伏观察列表（横盘蓄力 / 刚启动，盯紧第一根巨量突破）</div>
  <table><tr><th>币种</th><th>标签</th><th>阶段</th><th>分</th><th>现价</th><th>vsMA99</th><th>~1日</th><th>4h走势</th><th>依据</th></tr>
  {watch_rows}
  </table>
</div>

<div class="card">
  <div class="h2">📊 全量监控（按埋伏分排序，显示前 {len(ordered)} / 共 {n_coins}）</div>
  <table><tr><th>币种</th><th>标签</th><th>阶段</th><th>分</th><th>现价</th><th>vsMA99</th><th>~1日</th><th>4h走势</th><th>依据</th></tr>
  {full_rows}
  </table>
  {('<div class="note">已按埋伏分截断显示前 150 行；全部 {0} 个币均已扫描，完整数据见 <code>data/monitor_state.json</code>。</div>'.format(len(others))) if capped else ''}
</div>

<div class="card">
  <div class="h2">🔔 信号流（最近 30 条）</div>
  <table><tr><th>时间(UTC)</th><th></th><th>币种</th><th>摘要</th></tr>
  {sig_rows}
  </table>
</div>

<div class="note">数据来自 Binance 公开行情（只读、无 API key）。看板与 Telegram 推送同源：同一套六阶段检测器，进入/埋伏/见顶/崩盘信号会同时在两者出现。</div>

<script>
var next = {next_epoch_js};
function tick(){{
  if(!next){{document.getElementById('cd').textContent='—';return;}}
  var d = Math.max(0, next - Math.floor(Date.now()/1000));
  var m = Math.floor(d/60), s = d%60;
  document.getElementById('cd').textContent = m+' 分 '+s+' 秒';
}}
tick(); setInterval(tick, 1000);
setTimeout(function(){{ location.reload(); }}, 30000);
</script>
</body></html>"""
    return html

def _row(r):
    c = LAB_COLOR.get(r.get("label"), "#9ca3af")
    tag = _disp_label(r)
    pv = r.get("pv")
    pv_s = f"{pv*100:+.0f}%" if isinstance(pv, (int, float)) else "n/a"
    dm = r.get("day_move")
    dm_s = f"{dm*100:+.1f}%" if isinstance(dm, (int, float)) else "—"
    spark = _spark_svg(r.get("spark", []), color=c)
    reason = (r.get("reason") or "").replace("\n", " ")
    return (f'<tr><td class="sym">{r.get("sym","")}</td>'
            f'<td><span class="tag" style="background:{c}">{tag}</span></td>'
            f'<td>S{(r.get("stage") or "?")[-1]}</td>'
            f'<td class="sc">{r.get("score",0):.0f}</td>'
            f'<td>{r.get("price"):.6g}</td>'
            f'<td>{pv_s}</td><td>{dm_s}</td>'
            f'<td>{spark}</td>'
            f'<td class="rs">{reason}</td></tr>')

# ----------------------------------------------------------------------------
# Live shell (served fresh on every request; JS fetches data from JSON).
def shell_html():
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>妖币埋伏监控 · 实时看板</title>
<style>
:root{--bg:#0f1115;--card:#161a22;--bd:#1f2937;--tx:#e5e7eb;--mut:#9ca3af}
*{box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--tx);margin:0;padding:20px}
h1{font-size:20px;margin:0 0 2px} .sub{color:var(--mut);font-size:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin-bottom:16px}
.kpis{display:flex;gap:10px;flex-wrap:wrap}
.kpi{background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:10px 14px;font-size:13px;color:var(--mut)}
.kpi b{display:block;font-size:20px;color:#fff;margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 9px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:middle}
th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}
.sym{font-weight:700;color:#fff} .tag{color:#0f1115;font-weight:700;padding:2px 8px;border-radius:10px;font-size:11px;white-space:nowrap}
.sc{font-weight:700;font-size:15px} .rs{color:#cbd5e1;max-width:360px}
.ts{color:var(--mut);font-size:11px;white-space:nowrap} .em{font-size:15px}
.ssym{color:#fff;font-weight:600} .smsg{color:#cbd5e1}
.h2{font-size:15px;margin:0 0 10px;color:#fff} .note{color:var(--mut);font-size:11px;margin-top:8px}
.cnt{color:#f59e0b;font-weight:700}
</style></head><body>
<h1>🐸 妖币埋伏监控 · 实时看板</h1>
<div class="sub" id="sub">加载中…</div>
<div class="note" id="pool" style="margin-bottom:12px"></div>
<div class="card">
  <div class="kpis" id="kpis"></div>
  <div class="note" id="scanline"></div>
  <div id="diag"></div>
  <div id="health"></div>
</div>
<div class="card">
  <div class="h2">🔥 埋伏观察列表（横盘蓄力 / 刚启动，盯紧第一根巨量突破）</div>
  <table><tr><th>币种</th><th>标签</th><th>阶段</th><th>分</th><th>现价</th><th>vsMA99</th><th>~1日</th><th>4h走势</th><th>依据</th></tr>
  <tbody id="watch"></tbody></table>
</div>
<div class="card">
  <div class="h2" id="fullh2">📊 全量监控</div>
  <table><tr><th>币种</th><th>标签</th><th>阶段</th><th>分</th><th>现价</th><th>vsMA99</th><th>~1日</th><th>4h走势</th><th>依据</th></tr>
  <tbody id="full"></tbody></table>
  <div id="fullnote" class="note"></div>
</div>
<div class="card">
  <div class="h2">🔔 信号流（最近 30 条）</div>
  <table><tr><th>时间(UTC)</th><th></th><th>币种</th><th>摘要</th></tr>
  <tbody id="sigs"></tbody></table>
</div>
<div class="note">数据来自 Binance 公开行情（只读、无 API key）。看板每 30 秒从 /dashboard_data.json 实时拉取，与 Telegram 推送同源。</div>
<script>
var NEXT = 0;
var LAB = {COILING:"#f59e0b",FRESH_LAUNCH:"#22c55e",LAUNCHED:"#0ea5e9",PUMPED:"#ef4444",CRASHING:"#6b7280",LOW_VOL:"#4b5563",STABLE:"#4b5563",STOCK:"#4b5563",UNKNOWN:"#9ca3af"};
function dispLabel(r){
  if(r.label==="COILING") return r.vol_tick?"🔥埋伏★":"观察";
  var m={FRESH_LAUNCH:"⚡刚启动",LAUNCHED:"已启动",PUMPED:"已拉完",CRASHING:"崩盘中",LOW_VOL:"低波动",STABLE:"稳定币",STOCK:"代币股",UNKNOWN:"未知"};
  return m[r.label]||r.label;
}
function fmtPct(x){ if(typeof x!=="number") return "—"; var s=(x>=0?"+":"")+(x*100).toFixed(x>-0.1&&x<0.1?1:0)+"%"; return s; }
function fmtPrice(p){ if(p==null) return "—"; if(p>=1) return p.toFixed(4); if(p>=0.01) return p.toFixed(5); if(p>0) return p.toPrecision(4); return p; }
function spark(vals){
  if(!vals||vals.length<2) return "";
  var mn=Math.min.apply(null,vals), mx=Math.max.apply(null,vals), rng=(mx-mn)||1, w=120,h=30;
  var pts=vals.map(function(v,i){ return (i/(vals.length-1)*w).toFixed(1)+","+(h-((v-mn)/rng)*h).toFixed(1); }).join(" ");
  return '<svg width="'+w+'" height="'+h+'"><polyline points="'+pts+'" fill="none" stroke="#3b82f6" stroke-width="1.4"/></svg>';
}
function row(r){
  var c=LAB[r.label]||"#9ca3af", tag=dispLabel(r);
  var rs=(r.reason||"").replace(/\\n/g," ");
  var sc=(r.score!=null)?Math.round(r.score):0;
  return '<tr><td class="sym">'+(r.sym||"")+'</td>'+
    '<td><span class="tag" style="background:'+c+'">'+tag+'</span></td>'+
    '<td>S'+((r.stage||"?").toString().slice(-1))+'</td>'+
    '<td class="sc">'+sc+'</td>'+
    '<td>'+fmtPrice(r.price)+'</td>'+
    '<td>'+fmtPct(r.pv)+'</td>'+
    '<td>'+fmtPct(r.day_move)+'</td>'+
    '<td>'+spark(r.spark)+'</td>'+
    '<td class="rs">'+rs+'</td></tr>';
}
function render(d){
  var meta=d.meta||{}, k=d.kpis||{}, ranked=d.ranked||[], sigs=d.signals||[];
  document.getElementById("sub").textContent =
    "生成 "+meta.generated+" ｜ 数据源 Binance USD-M 合约 ｜ 4h 六阶段 + 1h MA99 ｜ 时区 UTC ｜ 每 "+meta.scan_interval_minutes+" 分钟扫描一次（本页每 30 秒自动刷新）";
  var us=meta.universe_source||"curated", usz=meta.universe_size||ranked.length;
  var uc = (us.indexOf("curated")===0 && usz<=90)?"#f87171":"#9ca3af";
  document.getElementById("pool").innerHTML = '监控池 <b style="color:'+uc+'">'+us+'</b> · 池大小 '+usz+' · 本轮有效 '+ranked.length;
  // KPIs
  var broken = usz && ranked.length < Math.max(1, usz*0.5);
  document.getElementById("kpis").innerHTML =
    '<div class="kpi">监控币数 <b>'+usz+'</b></div>'+
    '<div class="kpi">本轮回测有效 <b style="color:'+(broken?"#f87171":"#fff")+'">'+ranked.length+'</b></div>'+
    '<div class="kpi">🔥埋伏★ <b style="color:#f59e0b">'+(k.ambush||0)+'</b></div>'+
    '<div class="kpi">⚡刚启动 <b style="color:#22c55e">'+(k.fresh||0)+'</b></div>'+
    '<div class="kpi">近24h信号 <b>'+(k.sig24||0)+'</b></div>';
  document.getElementById("scanline").textContent = "上次扫描："+meta.last_scan+" ｜ 距下次扫描："+
    '<span id="cd" class="cnt">—</span>'+" ｜ 手动交易，纪律第一，本看板只呈现信号、不代下单。";
  NEXT = meta.next_scan_epoch||0; tick();
  // diagnosis
  var ss=meta.scan_summary||{}, diag="";
  if(ss && ss.attempted){
    var rt=""; for(var kk in (ss.skip_reasons||{})) rt+=" "+kk+":"+ss.skip_reasons[kk];
    var dc = ss.skipped>ss.ok ? "#f87171":"#9ca3af";
    diag='<div class="note" style="color:'+dc+'">扫描诊断：尝试 '+ss.attempted+' · 成功 '+ss.ok+' · 跳过 '+ss.skipped+
      (rt?('（'+rt.trim()+'）'):'')+' ｜ 跳过多为 symbol 已被下架/不在该市场，或瞬时被限流（下一轮缓存命中即可恢复）</div>';
  }
  document.getElementById("diag").innerHTML = diag;
  // health
  var h="";
  if(broken){
    var rr=""; for(var k2 in (ss.skip_reasons||{})) rr+=" "+k2+":"+ss.skip_reasons[k2];
    h='<div class="note" style="color:#f87171;margin-top:8px">⚠️ <b>数据获取异常</b>：本轮回测仅成功 <b>'+ranked.length+'/'+usz+'</b> 个币。'+
      'Binance 行情拉取很可能被部署区域（云服务器常见）封锁。<br>排查：① 设置 <b>HTTPS_PROXY</b> 代理（指向 Binance 友好区域）；'+
      '② 把 Zeabur 区域切到 Binance 可访问区；③ 首轮失败会在缓存命中后自动恢复。<br>跳过原因：'+(rr.trim()||"网络/超时")+'。</div>';
  }
  document.getElementById("health").innerHTML = h;
  // tables
  var watch=ranked.filter(function(r){return r.label==="COILING"||r.label==="FRESH_LAUNCH";});
  watch.sort(function(a,b){return (b.score||0)-(a.score||0);});
  var others=ranked.filter(function(r){return r.label!=="COILING"&&r.label!=="FRESH_LAUNCH";});
  others.sort(function(a,b){return (b.score||0)-(a.score||0);});
  var capped=others.length>150, disp=watch.concat(others.slice(0,150));
  document.getElementById("watch").innerHTML = watch.map(row).join("") || '<tr><td colspan="9" class="rs">（暂无埋伏候选）</td></tr>';
  document.getElementById("full").innerHTML = disp.map(row).join("") || '<tr><td colspan="9" class="rs">（无数据）</td></tr>';
  document.getElementById("fullh2").textContent = "📊 全量监控（按埋伏分排序，显示前 "+disp.length+" / 共 "+ranked.length+"）";
  document.getElementById("fullnote").innerHTML = capped ? ("已按埋伏分截断显示前 150 行；全部 "+others.length+" 个币均已扫描，完整数据见 data/monitor_state.json。") : "";
  // signals
  var sg="";
  sigs.slice(-30).reverse().forEach(function(s){
    var em=s.level?({"ENTER":"🚀","DIP":"💧","AMBUSH":"🔥","TOP":"⚠️","CRASH":"🩸","BOUNCE":"↩️","INFO":"ℹ️","DIGEST":"📅"}[s.level.toUpperCase()]||"•"):"•";
    var ts=(s.ts||"").slice(0,16).replace("T"," ");
    var msg=(s.msg||"").replace(/\\n/g," ").trim(); if(msg.length>90) msg=msg.slice(0,90)+"…";
    sg+='<tr><td class="ts">'+ts+'</td><td class="em">'+em+'</td><td class="ssym">'+(s.sym||"")+'</td><td class="smsg">'+msg+'</td></tr>';
  });
  document.getElementById("sigs").innerHTML = sg || '<tr><td colspan="4" class="smsg">（暂无信号）</td></tr>';
}
function tick(){
  if(!NEXT){ var e=document.getElementById("cd"); if(e) e.textContent="—"; return; }
  var dd=Math.max(0, NEXT-Math.floor(Date.now()/1000)), m=Math.floor(dd/60), s=dd%60;
  var e=document.getElementById("cd"); if(e) e.textContent=m+" 分 "+s+" 秒";
}
function load(){
  fetch("/dashboard_data.json?_="+Date.now(), {cache:"no-store"}).then(function(r){return r.json();}).then(render).catch(function(e){
    document.getElementById("sub").textContent="加载失败: "+e+" （重试中…）";
  });
}
tick(); load(); setInterval(tick,1000); setInterval(load,30000);
</script>
</body></html>"""

# ----------------------------------------------------------------------------
class DashboardServer:
    """Threaded HTTP server that serves the data/ directory.

    `/`            -> live shell HTML (generated fresh, no-store)
    `/dashboard_data.json` -> the live data JSON (generated fresh, no-store)
    `/dashboard.html`      -> static snapshot (offline viewing)
    `/state.json` / `/signals.json` -> raw data files
    """
    def __init__(self, host="0.0.0.0", port=8080, data_dir=DATA):
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.httpd = None

    def _handler(self):
        data_dir = self.data_dir
        class _H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=data_dir, **k)
            def do_GET(self):
                # strip query string (cache-busters) before matching
                self.path = self.path.split("?", 1)[0]
                if self.path in ("/", "/index.html", "/dashboard", ""):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(shell_html().encode("utf-8"))
                    return
                elif self.path == "/dashboard_data.json":
                    self.path = "/dashboard_data.json"
                elif self.path == "/state.json":
                    self.path = "/monitor_state.json"
                elif self.path == "/signals.json":
                    self.path = "/signals_log.json"
                return super().do_GET()
            def end_headers(self):
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                super().end_headers()
            def log_message(self, *a):
                pass
        return _H

    def start(self):
        try:
            self.httpd = socketserver.ThreadingTCPServer((self.host, self.port), self._handler())
        except OSError as e:
            print(f"[web] FAILED to bind {self.host}:{self.port} -> {e}")
            return None
        self.httpd.daemon_threads = True
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        print(f"[web] dashboard serving on http://{self.host}:{self.port}  (dir={self.data_dir})")
        return t

    def shutdown(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    # smoke test: build a dummy dashboard and serve it
    import sys
    dummy = [dict(sym="BANKUSDT", label="COILING", vol_tick=True, stage="S1", score=72,
                  price=0.0286, pv=-0.07, day_move=0.01, spark=[1,2,1.5,3,2.5,4,3.5],
                  reason="coiling 4 bars, range 9.6% wide, vol_tick=True")]
    write_dashboard(dummy, [], {"last_scan": "test", "scan_interval_minutes": 120})
    srv = DashboardServer(port=8080)
    srv.start()
    print("serving... ctrl-c to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()
