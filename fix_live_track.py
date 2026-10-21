# -*- coding: utf-8 -*-
"""Surgically restore the finals that the 23:57 recovery merge rolled back.

The 23:39 pod's backfill replay re-derived entries as `waiting`; the old-code
merge kept the pod's version, so every entry finalized between the replay and
the pre-push snapshot (BIRB's 15:10Z stop-out, PTB's 7 pre_fails, ...) got
resurrected as waiting — while Telegram had already announced them.

Fix: union(live_tracker, prev_snapshot) with the FIXED collision policy
(finalized beats waiting), then POST ?force=1 to overwrite the live tracker.
"""
import json, urllib.request, time

PROXY = "http://localhost:15236"
BASE = "https://yaobi.zeabur.app"
TOKEN = "yaobi1024"
PREV = "C:/Users/Martin/Downloads/ambush_track.prev.json"

op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

def get_json(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.load(op.open(req, timeout=90))
        except Exception as e:
            print("retry", i, e)
            time.sleep(5)
    raise SystemExit("fetch failed: " + url)

# 1. current live tracker + regime rows
live = get_json(BASE + "/export_track?with_regime=1&token=" + TOKEN)
live_entries = live.get("entries") or []
regime_rows = live.get("regime_log") or []
print("live entries:", len(live_entries), "| regime rows:", len(regime_rows))

# 2. pre-push snapshot (has the announced finals)
prev = json.load(open(PREV, encoding="utf-8"))
prev_entries = prev.get("entries") or []
print("prev entries:", len(prev_entries))

# 3. fixed merge policy: union keyed by (sym, ts, source);
#    finalized snapshot entry beats waiting live replay
key = lambda e: (e.get("sym", ""), e.get("ts", ""), e.get("source", ""))
by_key = {}
for e in live_entries:
    by_key[key(e)] = e
added = upgraded = 0
for e in prev_entries:
    k = key(e)
    cur = by_key.get(k)
    if cur is None:
        by_key[k] = e
        added += 1
    elif e.get("final_ts") and not cur.get("final_ts"):
        by_key[k] = e
        upgraded += 1
merged = list(by_key.values())
print("merged:", len(merged), "| added:", added, "| upgraded:", upgraded)

# sanity: BIRB must have A final (either re-stopped by the evaluator at
# 16:00Z — observed live — or upgraded from prev), PTB finals restored
birb_f = [e for e in merged if e.get("sym") == "BIRBUSDT" and e.get("final_ts")]
assert birb_f, "BIRB final not restored!"
ptb_f = [e for e in merged if e.get("sym") == "PTBUSDT" and e.get("final_ts")]
assert len(ptb_f) >= 7, "PTB finals not restored: %d" % len(ptb_f)
print("BIRB final present:", birb_f[0].get("status"),
      birb_f[0].get("final_ts"), birb_f[0].get("peak_gain"))
print("PTB finals in merged:", len(ptb_f))

# 4. force-write back to the live server
body = json.dumps({"entries": merged, "regime_log": regime_rows},
                  ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(BASE + "/restore_track?force=1", data=body,
                             method="POST",
                             headers={"Content-Type": "application/json",
                                      "X-Restore-Token": TOKEN,
                                      "User-Agent": "Mozilla/5.0"})
out = json.load(op.open(req, timeout=90))
print("restore response:", out)

# 5. verify from a fresh export
time.sleep(3)
check = get_json(BASE + "/export_track?with_regime=1&token=" + TOKEN)
ce = check.get("entries") or []
print("post-restore entries:", len(ce))
birb2 = [e for e in ce if e.get("sym") == "BIRBUSDT" and e.get("final_ts")]
print("VERIFY BIRB final on live:", "OK" if birb2 else "MISSING",
      birb2[0].get("final_ts") if birb2 else "")
ptb2 = [e for e in ce if e.get("sym") == "PTBUSDT" and e.get("final_ts")]
print("VERIFY PTB finals on live:", len(ptb2), "entries (need >=7)",
      "OK" if len(ptb2) >= 7 else "MISSING")

# 6. write the corrected snapshot back to local Downloads
json.dump(check, open("C:/Users/Martin/Downloads/ambush_track.json", "w",
                      encoding="utf-8"), ensure_ascii=False, indent=2)
print("local snapshot refreshed")
