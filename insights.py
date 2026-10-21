"""
Conclusion engine — turn the ambush tracker's accumulated EVIDENCE into
actionable conclusions, so the user gets "which targets are worth ambushing
and why" instead of an endless firehose of raw recommendations.

The 2026-10-20 feedback: "我需要类似的结论，而不是大量的推荐——这样我才能
找到有价值的标的埋伏". The retrospective findings that resonated:
  * live signals all LAUNCHED within <=3 days of entry  -> fresh entries are
    the golden window; old waiting entries are statistically dead water
  * same-coin re-entries compounded the biggest wins (ONE x3) -> a coiling
    coin with prior winning episodes is worth more than a pretty new face
  * score <75 never launches; 90+ adds nothing over 75-89 -> score is a gate,
    not a ranking

This module turns those findings into live output:
  launch_window_stats(entries)  delay distribution of historical launches
  coin_history(entries, sym)    prior episodes / wins / best peak for a coin
  value_board(ranked, entries)  TOP-N coiling candidates ranked by EVIDENCE
                                (potential + re-entry record + squeeze fuel
                                + entry-timing), each with a reason line
  aging_lines(entries, lw)      golden-window / aging split of open live
                                calls, with the historical launch share as
                                the culling criterion
  launch_window_note(lw)        one-line historical conclusion appended to
                                AMBUSH pushes ("X% launch within 3 days")

All functions are pure over their arguments (entries defaults to the
tracker store) so tests can feed synthetic histories.
"""
from datetime import datetime, timezone

WIN = 0.30           # prior episode counts as a win at >= +30% peak
GOLDEN_D = 3         # golden launch window (days from ambush call)
AGING_D = 7          # beyond this, historical launches have mostly happened
MIN_LW_SAMPLE = 20   # launch-window note needs a real sample, not 3 entries


def _fmt_pct(g):
    return f"{g*100:+.0f}%" if isinstance(g, (int, float)) else "—"


def _pct(x):
    return f"{x*100:.0f}%" if isinstance(x, (int, float)) else "?"


def _entries_or(entries):
    if entries is None:
        import ambush_tracker
        entries = ambush_tracker.load()["entries"]
    return entries or []


def _age_days(ts_s, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        t = datetime.fromisoformat(str(ts_s))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).days
    except Exception:
        return None


# ----------------------------------------------------------------------------
# historical launch-delay distribution: the "3-day golden window" evidence
# ----------------------------------------------------------------------------
def launch_window_stats(entries=None):
    """Share of historical launches that happened within 1/3/7 days of the
    ambush call. Only entries that actually launched carry launch_delay_d,
    so this is the distribution CONDITIONAL on launching — exactly the right
    conditional for "my open call is N days old, is it still alive?"."""
    launched = [e for e in _entries_or(entries)
                if isinstance(e.get("launch_delay_d"), (int, float))]
    n = len(launched)
    out = {"n": n, "p1": None, "p3": None, "p7": None, "median": None,
           "live_n": 0}
    if not n:
        return out
    ds = sorted(float(e["launch_delay_d"]) for e in launched)
    out["live_n"] = sum(1 for e in launched if e.get("source") == "live")

    def share(x):
        return round(sum(1 for d in ds if d <= x) / n, 3)
    mid = ds[n // 2] if n % 2 else (ds[n // 2 - 1] + ds[n // 2]) / 2
    out.update(p1=share(1), p3=share(3), p7=share(7), median=round(mid, 1))
    return out


def launch_window_note(lw):
    """One-line historical conclusion appended to AMBUSH pushes. Empty when
    the sample is too small to mean anything."""
    if not lw or lw.get("n", 0) < MIN_LW_SAMPLE:
        return ""
    p3 = lw.get("p3")
    if not isinstance(p3, (int, float)):
        return ""
    return (f"\n📊 历史结论：{p3*100:.0f}% 的埋伏启动发生在入场 ≤{GOLDEN_D} 天内"
            f"（{lw['n']} 笔样本）——{GOLDEN_D} 天没动静就该当死水警惕，"
            f"跌破 -20% 无条件撤。")


# ----------------------------------------------------------------------------
# vol_confirmed: the single source of truth for "埋伏★ 已触发" across the
# dashboard / telegram / KPI counts. monitor sets a time-aware vt_confirmed on
# every ranked rec (streak>=2 AND persisted >= CONFIRM_MIN_H); older callers
# and test fixtures that only carry vt_streak fall back to streak>=2.
# ----------------------------------------------------------------------------
def vol_confirmed(r):
    v = r.get("vt_confirmed")
    if v is not None:
        return bool(v)
    return (r.get("vt_streak") or 0) >= 2


# ----------------------------------------------------------------------------
# per-coin re-entry record: the "same-coin compounding" evidence
# ----------------------------------------------------------------------------
def coin_history(entries, sym):
    """Prior FINALIZED episodes of a coin, PLUS the recency structure that
    actually predicts (2026-10-20 review of 1140 round-transitions):

      P(next round +30% | last round won)   ~= 70%   (streak length irrelevant)
      P(next round +30% | no history)       ~= 39%
      P(next round +30% | last round lost)  ~= 12%   (aggregate wins in the
        coin's deeper history add NOTHING once the last round lost: 9.6%)

    So the edge is RECENCY, not the aggregate: a win-win-lose coin behaves
    like a loser (9.6%), not like a double winner. Returns rounds/wins/best
    for display plus last_win / lose_streak for scoring.
    """
    fins = [e for e in _entries_or(entries)
            if e.get("sym") == sym and e.get("status") in
            ("win_strong", "win", "weak", "miss", "pre_fail", "timeout")]
    wins = [e for e in fins if (e.get("peak_gain") or 0) >= WIN]
    peaks = [e.get("peak_gain") for e in fins
             if isinstance(e.get("peak_gain"), (int, float))]
    last_win = None
    lose_streak = 0
    if fins:
        # entries are appended chronologically; scan from the end, skipping
        # nothing (all fins are finalized rounds)
        last_win = (fins[-1].get("peak_gain") or 0) >= WIN
        if not last_win:
            for e in reversed(fins):
                if (e.get("peak_gain") or 0) >= WIN:
                    break
                lose_streak += 1
    return {"rounds": len(fins), "wins": len(wins),
            "best": round(max(peaks), 3) if peaks else None,
            "last_win": last_win, "lose_streak": lose_streak}


# ----------------------------------------------------------------------------
# win-rate review: the accountability report ("复盘是为了计算胜率，验证这套
# 程序是不是成功"). One pass over the tracker, four cuts:
#   1. outcome distribution — where the settled rounds actually end up
#   2. overall win rate    — win+win_strong / evaluated (the headline number)
#   3. monthly trend       — is the edge decaying or compounding?
#   4. conditional rates   — the recency structure that drives re-entry
#      scoring (P(win | last round won) vs P(win | losing streak)), computed
#      live from the current tracker so the numbers self-update.
# ----------------------------------------------------------------------------
def win_rate_review(entries):
    FINAL = ("win_strong", "win", "weak", "miss", "pre_fail", "timeout")
    WINS = ("win_strong", "win")
    ent = _entries_or(entries)
    fins = [e for e in ent if e.get("status") in FINAL]

    def _is_win(e):
        return e.get("status") in WINS

    # 1. outcome distribution
    dist = {}
    for e in fins:
        dist[e["status"]] = dist.get(e["status"], 0) + 1

    # 2. overall + by source
    def _cut(pool):
        if not pool:
            return None
        w = sum(1 for e in pool if _is_win(e))
        g = [e.get("peak_gain") for e in pool
             if isinstance(e.get("peak_gain"), (int, float))]
        launched = [e for e in pool if e.get("status") not in
                    ("pre_fail", "timeout")]
        return dict(n=len(pool), wins=w, rate=round(w / len(pool), 3),
                    avg_peak=round(sum(g) / len(g), 3) if g else None,
                    launch_rate=round(len(launched) / len(pool), 3) if pool else None)

    overall = _cut(fins)
    by_src = {s: _cut([e for e in fins if e.get("source") == s])
              for s in ("live", "backfill")}

    # 3. monthly trend (settled count + win rate per entry month)
    monthly = {}
    for e in fins:
        m = (e.get("ts") or "")[:7]
        if not m:
            continue
        d = monthly.setdefault(m, dict(n=0, wins=0))
        d["n"] += 1
        if _is_win(e):
            d["wins"] += 1
    for m, d in monthly.items():
        d["rate"] = round(d["wins"] / d["n"], 3) if d["n"] else None

    # 4. conditional: per coin, walk settled rounds in ts order; the outcome
    # of round i-1 conditions round i. This recomputes the exact numbers the
    # 2026-10-20 review found (70% / 39% / 12%) from whatever the tracker
    # holds today, so the scoring rules stay accountable over time.
    by_coin = {}
    for e in fins:
        by_coin.setdefault(e.get("sym"), []).append(e)
    cond = dict(last_win=dict(n=0, wins=0), lose1=dict(n=0, wins=0),
                lose2p=dict(n=0, wins=0), fresh=dict(n=0, wins=0))
    for sym, rounds in by_coin.items():
        rounds.sort(key=lambda x: x.get("ts") or "")
        for i, e in enumerate(rounds):
            if i == 0:
                bucket = "fresh"
            else:
                prev = rounds[i - 1]
                if _is_win(prev):
                    bucket = "last_win"
                else:
                    streak = 1
                    j = i - 2
                    while j >= 0 and not _is_win(rounds[j]):
                        streak += 1
                        j -= 1
                    bucket = "lose1" if streak == 1 else "lose2p"
            cond[bucket]["n"] += 1
            if _is_win(e):
                cond[bucket]["wins"] += 1
    for d in cond.values():
        d["rate"] = round(d["wins"] / d["n"], 3) if d["n"] else None

    delays = [e["launch_delay_d"] for e in ent
              if isinstance(e.get("launch_delay_d"), (int, float))]
    return dict(
        total=len(ent),
        open_waiting=sum(1 for e in ent if e.get("status") == "waiting"),
        open_launched=sum(1 for e in ent if e.get("status") == "launched"),
        evaluated=len(fins), dist=dist, overall=overall, by_src=by_src,
        monthly=dict(sorted(monthly.items())),
        cond=cond,
        avg_delay=round(sum(delays) / len(delays), 1) if delays else None,
    )


# ----------------------------------------------------------------------------
# the value board: rank the current coiling candidates by EVIDENCE
# ----------------------------------------------------------------------------
def value_board(ranked, entries=None, top=5):
    """Rank currently-coiling candidates by evidence, not by raw potential.

    points = potential (0-100, elasticity x duration x fuel)
           + 30 last-round WIN re-entry (flat — win streaks don't compound:
              1/2/3-in-a-row all win the next round ~70% of the time)
           - 15 last-round loss; -25 when the losing streak >= 2
              (next-round win rate decays 12% -> 10% -> 4.9% with streak
              length, and deeper WINS add nothing once the last round lost)
           + 10 squeeze fuel (funding<0 AND OI 24h>+10%)
           +  8 15m entry timing ready
           +  5 volume ticking (first tick; the ambush signal itself
              needs the 2-scan confirm)
    Each row carries its evidence as human conclusion strings.

    Returns (rows, n_candidates). rows are sorted best-first, capped at top.
    """
    entries = _entries_or(entries)
    cands = [r for r in (ranked or [])
             if r.get("label") in ("COILING", "COILING_LITE")]
    hist = {}
    for r in cands:
        s = r["sym"]
        if s not in hist:
            hist[s] = coin_history(entries, s)
    rows = []
    for r in cands:
        h = hist.get(r["sym"]) or {"rounds": 0, "wins": 0, "best": None,
                                   "last_win": None, "lose_streak": 0}
        pot = r.get("potential")
        pts = float(pot if isinstance(pot, (int, float)) else (r.get("score") or 0))
        reasons = []
        if h.get("last_win"):
            pts += 30.0
            reasons.append(f"第{h['rounds']+1}轮重入：前 {h['wins']}/{h['rounds']} 轮"
                           f"峰值达 +30%（最佳 {_fmt_pct(h['best'])}），上一轮刚赢——"
                           f"历史 70% 的此类重入下一轮再达标，同币复利是历史最大赢面")
        elif h.get("rounds"):
            ls = h.get("lose_streak") or 1
            pts -= (25.0 if ls >= 2 else 15.0)
            base_rate = "12%" if ls < 2 else "5%"
            reasons.append(f"第{h['rounds']+1}轮重入：连败 {ls} 轮"
                           f"（最佳 {_fmt_pct(h['best'])}）——历史复盘：此类重入"
                           f"下一轮仅 {base_rate} 达标，已降权，建议回避")
        fd, oc = r.get("funding"), r.get("oi_chg_24h")
        if (isinstance(fd, (int, float)) and fd < 0
                and isinstance(oc, (int, float)) and oc > 0.10):
            pts += 10
            reasons.append("空头付费+仓位堆积（若启动，逼空燃料充足）")
        tm = r.get("timing") or {}
        if tm.get("ready"):
            pts += 8
            reasons.append("15m 点位就绪（价>VWAP·EMA9>20）——仅点位条件满足，"
                           "入场仍须埋伏★量能双扫确认")
        elif tm:
            vd = tm.get("vwap_dist")
            vd_s = f"{vd*100:+.1f}%" if isinstance(vd, (int, float)) else "?"
            reasons.append(f"15m 未就绪（价 vs VWAP {vd_s}）——等回上 VWAP 再打")
        if r.get("vol_tick"):
            pts += 5
            reasons.append("量能抬头中（待双扫确认＝埋伏触发条件）")
        if not reasons:
            reasons.append(f"新面孔：潜力 {pts:.0f}（弹性×蓄力×燃料），无历史战绩可查")
        note_parts = []
        if h.get("last_win"):
            note_parts.append(f"{h['wins']}/{h['rounds']}轮胜·上轮胜·最佳{_fmt_pct(h['best'])}")
        elif h.get("rounds"):
            note_parts.append(f"⚠️连败{h.get('lose_streak') or 1}轮·最佳{_fmt_pct(h['best'])}")
        else:
            note_parts.append("新面孔·无战绩")
        if any("逼空燃料" in s for s in reasons):
            note_parts.append("逼空燃料")
        rows.append(dict(sym=r["sym"], pts=round(pts, 1), reasons=reasons,
                         note="·".join(note_parts),
                         potential=pot, score=r.get("score"),
                         price=r.get("price"),
                         tier=("lite" if r.get("label") == "COILING_LITE" else "main")))
    rows.sort(key=lambda x: -x["pts"])
    return rows[:top], len(cands)


def attach_evidence(ranked, entries=None):
    """Attach ev_pts / ev_note to every COILING candidate IN PLACE.

    The monitor calls this once per scan right after the timing badges, so
    the SAME evidence ordering flows everywhere: dashboard_data.json (live
    web tables), the static HTML snapshot, snapshot_from_monitor (the HTTP
    endpoint), and the Telegram /ambush list. One source, one order — the
    value board's. Recs keep their raw potential/score untouched.
    """
    rows, _ = value_board(ranked, entries, top=10 ** 6)
    bysym = {r["sym"]: r for r in rows}
    for rec in (ranked or []):
        if rec.get("label") not in ("COILING", "COILING_LITE"):
            continue
        row = bysym.get(rec.get("sym"))
        if row:
            rec["ev_pts"] = row["pts"]
            rec["ev_note"] = row["note"]
    return ranked


def board_lines(rows, n_total, lw=None):
    """Format the value board as message lines (digest / /value share these)."""
    if not rows:
        return []
    lines = [f"🎯 候选价值榜 · 当前横盘观察候选 {n_total} 个（均为观察态），只列前 {len(rows)} 个证据最硬的："]
    for i, r in enumerate(rows, 1):
        tag = "[次级] " if r["tier"] == "lite" else ""
        pot = r.get("potential")
        pot_s = (f"潜力{pot:.0f}" if isinstance(pot, (int, float))
                 else f"分{r.get('score') or 0:.0f}")
        lines.append(f"  {i}. {tag}{r['sym']} {pot_s} —— " + "；".join(r["reasons"]))
    if lw and lw.get("n"):
        lines.append(f"  排序口径 = 潜力(0-100) + 重入战绩/逼空燃料/点位加成。"
                     f"历史 {lw['n']} 笔样本：{_pct(lw.get('p3'))} 启动在 ≤{GOLDEN_D} 天、"
                     f"{_pct(lw.get('p7'))} 在 ≤{AGING_D} 天。")
    return lines


# ----------------------------------------------------------------------------
# aging split of open live calls: the culling conclusion
# ----------------------------------------------------------------------------
def aging_lines(entries=None, lw=None, now=None):
    """Split open LIVE ambush calls by age and attach the historical launch
    share as the culling criterion. Backfill waiting entries are replay
    artifacts, not the user's calls — excluded."""
    now = now or datetime.now(timezone.utc)
    live_w = [e for e in _entries_or(entries)
              if e.get("source") == "live" and e.get("status") == "waiting"]
    if not live_w:
        return []
    # dedupe by sym: a coin can carry several waiting rows (re-entry or
    # flicker artifacts) — that put SOLUSDT twice in one bucket and
    # OGUSDT in two buckets at once. Keep the LATEST call per coin so
    # the age verdict reflects the most recent signal.
    by_sym = {}
    for e in live_w:
        s = e.get("sym")
        if s not in by_sym or (e.get("ts") or "") > (by_sym[s].get("ts") or ""):
            by_sym[s] = e
    live_w = list(by_sym.values())
    aged = [(_age_days(e.get("ts"), now), e) for e in live_w]
    fresh = sorted(e["sym"] for a, e in aged if a is not None and a <= GOLDEN_D)
    mid = sorted(e["sym"] for a, e in aged if a is not None and GOLDEN_D < a <= AGING_D)
    aging = sorted(((a, e) for a, e in aged if a is not None and a > AGING_D),
                   key=lambda x: -(x[0]))
    lines = [f"⏳ 在途埋伏老化结论（真实信号 {len(live_w)} 笔在等启动）："]
    if fresh:
        lines.append(f"  🟢 黄金窗（≤{GOLDEN_D}天）：{', '.join(fresh)} —— "
                     f"历史 {_pct((lw or {}).get('p3'))} 的启动发生在这里，这批最值得盯。")
    if mid:
        lines.append(f"  🟡 观察窗（{GOLDEN_D+1}-{AGING_D}天）：{', '.join(mid)} —— "
                     f"尚在正常窗口，但优先级低于黄金窗。")
    if aging:
        parts = [f"{e['sym']}（已等{a}天）" for a, e in aging]
        lines.append(f"  🔴 老化（>{AGING_D}天）：{'、'.join(parts)} —— "
                     f"历史 {_pct((lw or {}).get('p7'))} 的启动在 ≤{AGING_D} 天内已发生，"
                     f"这批大概率是死水，别再加仓等（45 天超时会自动撤）。")
    return lines
