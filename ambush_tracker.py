"""
Ambush signal outcome tracker — the self-learning loop.

Every time the monitor fires an AMBUSH signal (COILING + vol_tick transition),
snapshot the call (price, score, coil metrics) and track what actually
happened, so the system can measure its own hit rate and tune the scoring
weights from evidence instead of intuition.

Outcome model (from the playbook):
  waiting  -> launched   (stage left S1 / ENTER fired)              within 45d
  waiting  -> pre_fail   (price broke -20% BEFORE any launch)
  waiting  -> timeout    (no launch within 45 days)
  launched -> win_strong (post-launch peak >= +80% vs entry price)
           -> win        (peak >= +30%)
           -> weak       (peak >= +10%)
           -> miss       (peak < +10%)
  A launched entry finalizes when the coin reaches S6/CRASHING or 20 days
  pass after launch.

Store: data/ambush_track.json (survives restarts on the /app/data volume).
Backfill: replays cached 4h CSVs through the same state machine to bootstrap
historical stats. NOTE: the cached set is a curated/biased sample — read the
backfill-only numbers with care; live-tracked entries are the clean sample.

Usage:
  python ambush_tracker.py --backfill     # bootstrap from cached klines
  python ambush_tracker.py --stats        # print current stats
"""
import os, json, glob
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TRACK_PATH = os.path.join(DATA, "ambush_track.json")

WAIT_DAYS = 45          # max days to wait for a launch after the ambush call
POST_DAYS = 20          # days after launch to measure the peak
PRE_FAIL = -0.20        # broke -20% before launching -> failed call
WIN_STRONG, WIN, WEAK = 0.80, 0.30, 0.10
REENTRY_COOLDOWN_D = 10  # min days between two ambush calls on the same coin
STORE_LIMIT = 3000  # cap on stored entries (backfill full-history can exceed 1.5k)

FINAL = ("win_strong", "win", "weak", "miss", "pre_fail", "timeout")
STATUS_CN = {
    "waiting": "等待启动", "launched": "已启动·跟踪中",
    "win_strong": "大赢(+80%)", "win": "赢(+30%)", "weak": "弱(+10%)",
    "miss": "未达标", "pre_fail": "破位失败", "timeout": "超时未启动",
}


def _now():
    return datetime.now(timezone.utc)


def load():
    if os.path.exists(TRACK_PATH):
        try:
            with open(TRACK_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"entries": []}


def save(store):
    os.makedirs(DATA, exist_ok=True)
    tmp = TRACK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, TRACK_PATH)


def record_entry(rec, now=None, source="live", score=None, tier="main"):
    """Called when an AMBUSH signal actually fires. rec = score_coin result.
    tier: "main" (妖币横盘) | "lite" (次级蓄力，大盘币新周期) — tracked
    separately so the self-learning loop can measure each tier's edge."""
    store = load()
    now = now or _now()
    sym = rec["sym"]
    # dedup: one active track per coin
    for e in store["entries"]:
        if e["sym"] == sym and e["status"] not in FINAL:
            return None
    # cooldown after a finalized call
    for e in reversed(store["entries"]):
        if e["sym"] == sym and e.get("final_ts"):
            ft = datetime.fromisoformat(e["final_ts"])
            if (now - ft).days < REENTRY_COOLDOWN_D:
                return None
    entry = dict(
        sym=sym, ts=now.isoformat(), source=source, tier=tier,
        entry_price=float(rec["price"]),
        score=(score if score is not None else rec.get("score")),
        flat_run=rec.get("flat_run"), range_w=rec.get("range_w"),
        pv=rec.get("pv"), ma99_1h=rec.get("ma99_1h"),
        status="waiting", launched_ts=None, launched_price=None,
        launch_delay_d=None, max_price=float(rec["price"]),
        min_price=float(rec["price"]), peak_gain=None,
        final_ts=None, last_price=float(rec["price"]), last_seen=now.isoformat(),
    )
    store["entries"].append(entry)
    store["entries"] = store["entries"][-STORE_LIMIT:]
    save(store)
    return entry


def _is_launched_stage(stage):
    try:
        return str(stage)[-1] in "23456"
    except Exception:
        return False


def update(ranked, now=None):
    """Called after EVERY scan with the full ranked list: refresh prices,
    apply status transitions, finalize outcomes. Returns the store."""
    store = load()
    bysym = {r["sym"]: r for r in (ranked or [])}
    now = now or _now()
    for e in store["entries"]:
        if e["status"] in FINAL:
            continue
        rec = bysym.get(e["sym"])
        t0 = datetime.fromisoformat(e["ts"])
        if rec is not None and rec.get("price"):
            p = float(rec["price"])
            e["last_price"] = p
            e["last_seen"] = now.isoformat()
            e["max_price"] = max(e.get("max_price") or p, p)
            e["min_price"] = min(e.get("min_price") or p, p)
        ep = e["entry_price"]
        if e["status"] == "waiting":
            launched = rec is not None and (
                _is_launched_stage(rec.get("stage"))
                or rec.get("label") in ("FRESH_LAUNCH", "LAUNCHED", "PUMPED", "CRASHING"))
            if launched:
                e["status"] = "launched"
                e["launched_ts"] = now.isoformat()
                if rec.get("price"):
                    e["launched_price"] = float(rec["price"])
                e["launch_delay_d"] = round((now - t0).total_seconds() / 86400, 1)
            elif e.get("min_price") is not None and e["min_price"] <= ep * (1 + PRE_FAIL):
                e["status"] = "pre_fail"
                e["peak_gain"] = round(e["min_price"] / ep - 1, 4)
                e["final_ts"] = now.isoformat()
            elif (now - t0).days > WAIT_DAYS:
                e["status"] = "timeout"
                e["peak_gain"] = round((e.get("max_price") or ep) / ep - 1, 4)
                e["final_ts"] = now.isoformat()
        elif e["status"] == "launched":
            tl = datetime.fromisoformat(e["launched_ts"])
            crashed = rec is not None and (rec.get("label") == "CRASHING"
                                           or str(rec.get("stage"))[-1] == "6")
            if crashed or (now - tl).days > POST_DAYS:
                peak = e.get("max_price") or ep
                g = peak / ep - 1
                e["peak_gain"] = round(g, 4)
                e["status"] = ("win_strong" if g >= WIN_STRONG else
                               "win" if g >= WIN else
                               "weak" if g >= WEAK else "miss")
                e["final_ts"] = now.isoformat()
    save(store)
    return store


def stats(store=None):
    """Aggregate outcome stats, including per-score-bucket hit rates —
    the evidence base for tuning the scoring weights."""
    store = store or load()
    ent = store["entries"]
    final = [e for e in ent if e["status"] in FINAL]
    launched_final = [e for e in final if e["status"] in
                      ("win_strong", "win", "weak", "miss")]
    open_waiting = [e for e in ent if e["status"] == "waiting"]
    open_launched = [e for e in ent if e["status"] == "launched"]

    def _r(a, b):
        return round(len(a) / len(b), 3) if b else None

    win30 = [e for e in launched_final if (e.get("peak_gain") or 0) >= WIN]
    win80 = [e for e in launched_final if (e.get("peak_gain") or 0) >= WIN_STRONG]
    delays = [e["launch_delay_d"] for e in ent
              if e.get("launch_delay_d") is not None]
    gains = [e["peak_gain"] for e in launched_final if e.get("peak_gain") is not None]
    out = dict(
        total=len(ent),
        live=sum(1 for e in ent if e.get("source") == "live"),
        backfill=sum(1 for e in ent if e.get("source") == "backfill"),
        evaluated=len(final),
        launched_n=len(launched_final),
        launch_rate=_r(launched_final, final),
        win30_n=len(win30), win30_rate=_r(win30, launched_final),
        win80_n=len(win80), win80_rate=_r(win80, launched_final),
        avg_delay=round(sum(delays) / len(delays), 1) if delays else None,
        avg_peak=round(sum(gains) / len(gains), 3) if gains else None,
        open_waiting=len(open_waiting), open_launched=len(open_launched),
        buckets={},
    )
    # per-score-bucket hit rates (the tuning evidence)
    for name, lo, hi in (("90-98", 90, 99), ("75-89", 75, 90), ("<75", 0, 75)):
        b = [e for e in final if isinstance(e.get("score"), (int, float))
             and lo <= e["score"] < hi]
        bl = [e for e in b if e["status"] in ("win_strong", "win", "weak", "miss")]
        bg = [e["peak_gain"] for e in bl if e.get("peak_gain") is not None]
        out["buckets"][name] = dict(
            n=len(b), launched_n=len(bl), launch_rate=_r(bl, b),
            avg_peak=round(sum(bg) / len(bg), 3) if bg else None)
    # per-tier hit rates: main (妖币) vs lite (次级蓄力) — lets the data decide
    # whether the lite tier earns its place. Old entries have no tier -> main.
    def _tier(tier):
        t_ent = [e for e in ent if e.get("tier", "main") == tier]
        t_final = [e for e in final if e.get("tier", "main") == tier]
        t_lf = [e for e in t_final if e["status"] in ("win_strong", "win", "weak", "miss")]
        t30 = [e for e in t_lf if (e.get("peak_gain") or 0) >= WIN]
        tg = [e["peak_gain"] for e in t_lf if e.get("peak_gain") is not None]
        return dict(
            total=len(t_ent), evaluated=len(t_final), launched_n=len(t_lf),
            launch_rate=_r(t_lf, t_final), win30_rate=_r(t30, t_lf),
            avg_peak=round(sum(tg) / len(tg), 3) if tg else None,
            open_waiting=sum(1 for e in t_ent if e["status"] == "waiting"))
    out["tiers"] = {"main": _tier("main"), "lite": _tier("lite")}
    return out


def recent(limit=30):
    """Newest-first entries for display."""
    ent = sorted(load()["entries"], key=lambda e: e.get("ts") or "", reverse=True)
    return ent[:limit]


# ----------------------------------------------------------------------------
def backfill(market="futures", quiet=False):
    """Replay cached 4h CSVs through the state machine to bootstrap historical
    ambush calls + outcomes. Replicates score_coin's S1/COILING+vol_tick
    trigger formulas on the detect() output (all causal columns)."""
    import numpy as np
    from pandas import Timestamp as pd_ts
    from memecoin_playbook_scanner import load_csv, detect

    d = os.path.join(DATA, "scan_futures" if market == "futures" else "scan_spot")
    csvs = sorted(glob.glob(os.path.join(d, "*_4h.csv")))
    store = load()
    existing = store["entries"]
    added = 0
    for p4 in csvs:
        sym = os.path.basename(p4).replace("_4h.csv", "")
        try:
            dfr, events, levels = detect(load_csv(p4), None)
        except Exception as e:
            if not quiet:
                print(f"  skip {sym}: {e}")
            continue
        close = dfr["close"].values; volr = dfr["vol_ratio"].values
        stage = dfr["stage"].values; ma99 = dfr["ma99"].values
        consol = dfr["in_consol"].values
        high = dfr["high"].values; low = dfr["low"].values
        n = len(dfr)
        ts = dfr["ts"]

        def tick(i):
            if i < 30:
                return False
            a, b = volr[i - 2:i + 1], volr[i - 27:i - 3]
            if not len(a) or not len(b) or np.isnan(a).all() or np.isnan(b).all():
                return False
            vr3 = float(np.nanmean(a))
            base = float(np.nanmean(b))
            return (vr3 > 1.15) and (vr3 > base * 1.15)

        def swing(i):
            w = close[max(0, i - 119):i + 1]
            return (w.max() / w.min()) if len(w) and w.min() > 0 else 1.0

        def coiling(i):
            return str(stage[i]) == "S1" and swing(i) >= 1.30

        def score_at(i):
            fr = 0
            k = i
            while k >= 0 and consol[k]:
                fr += 1; k -= 1
            w = close[max(0, i - 14):i + 1]
            med = float(np.mean(w))
            rw = (high[max(0, i - 14):i + 1].max() - low[max(0, i - 14):i + 1].min()) / med if med > 0 else 1.0
            tight = 1.0 - rw
            pv = (close[i] / ma99[i] - 1.0) if ma99[i] == ma99[i] and ma99[i] > 0 else None
            s = 35 + min(22, fr * 1.6) + max(0.0, (tight - 0.80)) * 120 + 28
            if pv is not None and -0.15 <= pv <= 0.45:
                s += 8
            return min(98, s)

        i = 120
        while i < n - 1:
            if coiling(i) and tick(i) and not (coiling(i - 1) and tick(i - 1)):
                # dedup against existing entries for this coin (±5 days)
                t0 = ts.iloc[i]
                dup = any(e["sym"] == sym and abs((pd_ts(e["ts"]) - t0).days) < 5
                          for e in existing)
                if not dup:
                    j = i + 1
                    while j < n and str(stage[j]) == "S1":
                        j += 1
                    ep = float(close[i])
                    e = dict(
                        sym=sym, ts=t0.isoformat(), source="backfill",
                        entry_price=ep, score=round(score_at(i), 1),
                        flat_run=None, range_w=None, pv=None, ma99_1h=None,
                        status="waiting", launched_ts=None, launched_price=None,
                        launch_delay_d=None, max_price=ep, min_price=ep,
                        peak_gain=None, final_ts=None,
                        last_price=float(close[-1]), last_seen=str(ts.iloc[-1]),
                    )
                    if j >= n:      # still S1 at the end of cached data
                        if (ts.iloc[-1] - t0).days > WAIT_DAYS:
                            e["status"] = "timeout"
                            e["peak_gain"] = round(close[i:].max() / ep - 1, 4)
                            e["final_ts"] = str(ts.iloc[-1])
                        existing.append(e); added += 1
                    else:
                        t_launch = ts.iloc[j]
                        pre_min = close[i:j].min()
                        if pre_min <= ep * (1 + PRE_FAIL):
                            e["status"] = "pre_fail"
                            e["peak_gain"] = round(pre_min / ep - 1, 4)
                        elif (t_launch - t0).days > WAIT_DAYS:
                            e["status"] = "timeout"
                            e["peak_gain"] = round(close[i:j].max() / ep - 1, 4)
                        else:
                            e["status"] = "launched"
                            e["launched_ts"] = t_launch.isoformat()
                            e["launched_price"] = float(close[j])
                            e["launch_delay_d"] = round((t_launch - t0).total_seconds() / 86400, 1)
                            end = min(j + int(POST_DAYS * 6), n)  # 6 bars/day
                            peak = close[j:end].max()
                            e["max_price"] = float(max(peak, ep))
                            g = peak / ep - 1
                            e["peak_gain"] = round(g, 4)
                            e["status"] = ("win_strong" if g >= WIN_STRONG else
                                           "win" if g >= WIN else
                                           "weak" if g >= WEAK else "miss")
                        e["final_ts"] = t_launch.isoformat() if e["status"] != "waiting" else None
                        existing.append(e); added += 1
                i += 12  # skip ~2 days to avoid overlapping calls on one coil
                continue
            i += 1
    store["entries"] = sorted(existing, key=lambda e: e.get("ts") or "")[-STORE_LIMIT:]
    save(store)
    if not quiet:
        print(f"[backfill] added {added} entries, total {len(store['entries'])}")
    return added


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.backfill:
        backfill()
    if a.stats or not a.backfill:
        print(json.dumps(stats(), indent=2, ensure_ascii=False))
