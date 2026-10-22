# -*- coding: utf-8 -*-
"""模拟持仓（纸面组合）— 2026-10-22 用户指令：
"在页面上增加一个板块，模拟持仓：如果你是交易员，正常入场/持仓/止损/止盈，
虚拟本金 1 万 USDT，仓位你自己安排"。

纪律与给用户的《交易员操作流程》完全一致（不自相矛盾）：
  入场  只买「✅可入场」= COILING 主池 + 埋伏★确认（vol_confirmed，
        含 flat_run>=2 / range<=20% 成熟度门槛）+ ⚡点位共振（timing.ready）。
        ⏳挂单等点位 / 量能首现·等确认 一律不买。
  过滤  ⚠️连败>=2 的币不买（复盘：连败2+ 条件胜率 7.5%）。
  仓位  单笔 = 当前净值的 10%；最多 5 仓；每轮扫描最多新开 2 仓。
  止损  入场价 -20%，扫描触及即按扫描价平仓（2h 粒度的真实滑点）。
  止盈  浮动 +30% 先卖一半；尾仓止损线上移到保本（入场价），
        回落触保本线即平；若继续涨则一直拿到保本线或 45 天窗。
  超时  45 天无达标 → 按现价平仓（对齐 tracker 的 timeout 口径）。
  其他  同一币只持一仓不加仓；平仓后需重新走完整信号才算新仓
        （边沿触发：信号先离开 ✅可入场 态、再回来才允许新开仓；
        满仓/现金不足期间错过的触发不追——宁可错过，等下一轮）。

状态存 data/paper.json（部署清卷时随 export/restore 闭环走 sibling
"paper" 键，合并策略：updated_at 较新者胜——纸面组合是单 JSON 状态，
不存在条目级合并问题）。
"""
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_PATH = os.path.join(HERE, "data", "paper.json")

INITIAL_CAPITAL = 10000.0
POS_PCT = 0.10          # 单笔 = 当前净值 10%
MAX_POS = 5             # 最多 5 仓（≤50% 资金在场，留现金扛止损潮）
MAX_NEW_PER_SCAN = 2    # 每轮最多新开 2 仓
STOP_PCT = 0.20         # -20% 无条件止损
TARGET_PCT = 0.30       # +30% 减半
TIMEOUT_DAYS = 45

RULES_NOTE = ("纪律：只买「✅可入场」（埋伏★确认+⚡点位共振）；⚠️连败≥2 不买；"
              "单笔=净值10%，最多5仓，每轮最多新开2仓；-20% 无条件止损；"
              "+30% 先卖一半、尾仓保本止损；45天超时平仓。纸面模拟，非投资建议。")


def _now_iso(now=None):
    return (now or datetime.now(timezone.utc)).isoformat()


def _default_state(now=None):
    return {
        "initial_capital": INITIAL_CAPITAL,
        "cash": INITIAL_CAPITAL,
        "positions": [],
        "closed": [],
        "started_at": _now_iso(now),
        "updated_at": _now_iso(now),
    }


def load(path=PAPER_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and "cash" in st and "positions" in st:
            return st
    except Exception:
        pass
    return _default_state()


def save(state, path=PAPER_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def _equity(state, prices):
    pos_v = 0.0
    for p in state["positions"]:
        # 现价优先；本轮不在 ranked（掉出观察池/拉取失败）时退回上次已知价
        price = prices.get(p["sym"])
        if not (isinstance(price, (int, float)) and price > 0):
            price = p.get("last_price")
        if isinstance(price, (int, float)) and price > 0:
            pos_v += p["qty"] * price
    return state["cash"] + pos_v


def _close(state, p, price, now, reason):
    """平掉剩余仓位并入 closed（含此前止盈减半那一腿）。"""
    legs = []
    tp = p.get("tp_leg")
    if tp:
        legs.append({"qty": round(tp["qty"], 10), "price": tp["price"],
                     "pnl": round(tp["pnl"], 6)})
    tail_pnl = p["qty"] * (price - p["entry"])
    legs.append({"qty": round(p["qty"], 10), "price": price,
                 "pnl": round(tail_pnl, 6)})
    realized = sum(l["pnl"] for l in legs)
    state["cash"] += p["qty"] * price
    qty_in = p.get("qty_in") or (p["qty"] + (tp["qty"] if tp else 0))
    invested = qty_in * p["entry"]
    state["closed"].append({
        "sym": p["sym"],
        "entry_ts": p["entry_ts"],
        "exit_ts": _now_iso(now),
        "entry": p["entry"],
        "exit": price,
        "qty_in": round(qty_in, 10),
        "realized": round(realized, 6),
        "pnl_pct": round(realized / invested, 4) if invested > 0 else None,
        "reason": reason,
        "peak": p.get("peak") or p["entry"],
        "legs": legs,
    })


def _process_exits(state, prices, now):
    """每轮扫描先处理离场：止损 / +30% 减半 / 保本线 / 45 天超时。"""
    events = []
    remaining = []
    for p in state["positions"]:
        price = prices.get(p["sym"])
        if not (isinstance(price, (int, float)) and price > 0):
            remaining.append(p)      # 行情缺失（下架/拉取失败）：跳过不判
            continue
        p["last_price"] = price      # 供下轮权益计算的价格回退
        p["peak"] = max(p.get("peak") or p["entry"], price)
        entry = p["entry"]
        # 1) +30% 先卖一半（只做一次），尾仓止损线上移到保本
        if not p.get("half_taken") and price >= entry * (1 + TARGET_PCT):
            sell_qty = p["qty"] / 2.0
            state["cash"] += sell_qty * price
            p["tp_leg"] = {"qty": sell_qty, "price": price,
                           "pnl": sell_qty * (price - entry)}
            p["qty"] = round(p["qty"] - sell_qty, 10)
            p["half_taken"] = True
            p["stop"] = entry        # 保本止损
            events.append(("tp_half", p["sym"], round(p["tp_leg"]["pnl"], 2)))
            remaining.append(p)
            continue
        # 2) 止损（整仓 -20%，或减半后的保本线）
        stop = p.get("stop")
        if not isinstance(stop, (int, float)):
            stop = entry * (1 - STOP_PCT)
        if price <= stop:
            reason = ("止盈+30%减半·尾仓保本离场" if p.get("half_taken")
                      else "止损-20%")
            _close(state, p, price, now, reason)
            events.append(("exit", p["sym"], reason))
            continue
        # 3) 45 天超时
        try:
            age_d = (now - datetime.fromisoformat(p["entry_ts"])).days
        except Exception:
            age_d = 0
        if age_d >= TIMEOUT_DAYS:
            _close(state, p, price, now, "45天超时")
            events.append(("exit", p["sym"], "45天超时"))
            continue
        remaining.append(p)
    state["positions"] = remaining
    return events


def _is_eligible(r):
    """✅可入场 的纯信号判定（不含持有/连败过滤——那两层在别处）。"""
    if r.get("label") != "COILING" or not r.get("vol_tick"):
        return False
    t = r.get("timing") or {}
    if t.get("ready") is not True:
        return False
    import insights
    if not insights.vol_confirmed(r):
        return False
    price = r.get("price")
    return isinstance(price, (int, float)) and price > 0


def _candidates(ranked, tracker_entries):
    """✅可入场 候选：埋伏★确认 + ⚡点位 + 未持有 + 连败<2。
    优先级：上轮胜 > 新面孔 > 上轮败；同级按证据分（ev_pts→potential→score）。"""
    import insights
    out = []
    for r in ranked or []:
        if not _is_eligible(r):
            continue
        out.append((r, r["price"]))
    # 连败过滤（coin_history 现按 final_ts 排序，上轮判定可信）
    try:
        filtered = []
        for r, price in out:
            h = insights.coin_history(tracker_entries or [], r["sym"])
            if (h.get("lose_streak") or 0) >= 2:
                continue
            r = dict(r)
            r["_hist"] = h
            filtered.append((r, price))
        out = filtered
    except Exception:
        pass

    def sort_key(item):
        r, _ = item
        h = r.get("_hist") or {}
        lw = h.get("last_win")
        tier = 0 if lw is True else (1 if lw is None else 2)
        pts = r.get("ev_pts")
        if not isinstance(pts, (int, float)):
            pts = r.get("potential") if isinstance(r.get("potential"), (int, float)) else (r.get("score") or 0)
        return (tier, -pts)

    out.sort(key=sort_key)
    return out


def on_scan(ranked, now=None, path=PAPER_PATH, tracker_entries=None):
    """每轮扫描调用：先离场后入场，返回 (state, events)。"""
    now = now or datetime.now(timezone.utc)
    state = load(path)
    if tracker_entries is None:
        try:
            import ambush_tracker
            tracker_entries = ambush_tracker.load()["entries"]
        except Exception:
            tracker_entries = []
    prices = {r.get("sym"): r.get("price") for r in (ranked or [])
              if isinstance(r.get("price"), (int, float))}
    events = _process_exits(state, prices, now)
    # 边沿触发纪律（2026-10-22 测试抓到的 bug：AAA 止损平仓后，同一轮扫描
    # 因为信号仍是 ✅可入场 被立即回补成僵尸仓）。平仓后必须等信号先离开
    # 可入场态（量能灭/确认丢/点位关），再重新走到 ✅ 才算新触发——
    # 与《交易员操作流程》"平仓后等它重新横盘、双扫确认再埋"一致。
    # last_elig 只记录"最近一次被观测到"的 eligible 布尔值：不在本轮
    # ranked 里的币保持旧值（没观测到 ≠ 信号熄灭，防掉榜误判重置）。
    last_elig = dict(state.get("last_elig") or {})
    held = {p["sym"] for p in state["positions"]}
    n_new = 0
    for r, price in _candidates(ranked, tracker_entries):
        if len(state["positions"]) >= MAX_POS or n_new >= MAX_NEW_PER_SCAN:
            break
        if r["sym"] in held:
            continue
        if last_elig.get(r["sym"]) is True:
            # 上一轮观测到它就已是 ✅（不是新触发）：刚平仓的回补、
            # 以及满仓期间错过的信号都不追——宁可错过，等下一轮触发
            continue
        equity = _equity(state, prices)
        budget = equity * POS_PCT
        if state["cash"] < budget * 0.5:
            break                      # 现金吃紧：本轮不硬开
        budget = min(budget, state["cash"])
        qty = budget / price
        state["cash"] = round(state["cash"] - budget, 6)
        state["positions"].append({
            "sym": r["sym"],
            "qty": round(qty, 10),
            "qty_in": round(qty, 10),
            "entry": price,
            "last_price": price,
            "entry_ts": _now_iso(now),
            "stop": round(price * (1 - STOP_PCT), 10),
            "target": round(price * (1 + TARGET_PCT), 10),
            "half_taken": False,
            "peak": price,
            "score": r.get("score"),
            "ev_note": r.get("ev_note") or "",
        })
        held.add(r["sym"])
        n_new += 1
        events.append(("enter", r["sym"], round(budget, 2)))
    # 观测更新：只改本轮出现在 ranked 里的币（缺席保持旧值）
    for r in (ranked or []):
        last_elig[r["sym"]] = _is_eligible(r)
    state["last_elig"] = last_elig
    state["updated_at"] = _now_iso(now)
    save(state, path)
    return state, events


def snapshot(ranked=None, path=PAPER_PATH):
    """面板视图：净值/现金/持仓/已平仓/统计 + 纪律说明。"""
    state = load(path)
    prices = {r.get("sym"): r.get("price") for r in (ranked or [])
              if isinstance(r.get("price"), (int, float))}
    equity = _equity(state, prices)
    positions = []
    for p in state["positions"]:
        price = prices.get(p["sym"])
        if not (isinstance(price, (int, float)) and price > 0):
            price = p.get("last_price")   # 掉出观察池时退回上次已知价
        float_pct = (price / p["entry"] - 1) if (isinstance(price, (int, float)) and price > 0) else None
        positions.append({
            "sym": p["sym"],
            "entry_ts": p["entry_ts"],
            "entry": p["entry"],
            "price": price,
            "float_pct": (round(float_pct, 4) if float_pct is not None else None),
            "peak_pct": (round((p.get("peak", p["entry"]) / p["entry"] - 1), 4)),
            "weight": (round(p["qty"] * price / equity, 4)
                       if (isinstance(price, (int, float)) and equity > 0) else None),
            "stop": p.get("stop"),
            "target": p.get("target"),
            "half_taken": bool(p.get("half_taken")),
            "ev_note": p.get("ev_note") or "",
        })
    closed = sorted(state["closed"],
                    key=lambda c: c.get("exit_ts") or "", reverse=True)[:20]
    n = len(state["closed"])
    wins = [c for c in state["closed"]
            if isinstance(c.get("pnl_pct"), (int, float)) and c["pnl_pct"] >= 0.10]
    pcts = [c["pnl_pct"] for c in state["closed"]
            if isinstance(c.get("pnl_pct"), (int, float))]
    return {
        "initial": state.get("initial_capital", INITIAL_CAPITAL),
        "cash": round(state["cash"], 2),
        "equity": round(equity, 2),
        "total_return": round(equity / state.get("initial_capital", INITIAL_CAPITAL) - 1, 4),
        "n_pos": len(state["positions"]),
        "max_pos": MAX_POS,
        "positions": positions,
        "closed": closed,
        "stats": {
            "n": n,
            "win_rate": round(len(wins) / n, 3) if n else None,
            "avg_pnl_pct": round(sum(pcts) / len(pcts), 4) if pcts else None,
        },
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "rules": RULES_NOTE,
    }
