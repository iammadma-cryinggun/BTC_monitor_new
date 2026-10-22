# -*- coding: utf-8 -*-
"""卷丢失一键恢复 v2（2026-10-21）：tracker + regime_log 双资产闭环。

用法（等新版部署完、首扫回填 >1000 后执行）：
  python do_recover.py
流程：
  1. POST /restore_track?merge=1 —— body = 本地 tracker 备份 + 注入本地
     regime_log 备份（服务端分开落盘：tracker 按三元组去重合并、
     regime_log 按 ts 去重合并）。
  2. GET /export_track?with_regime=1 —— 拉最新快照（tracker + regime_log），
     本地两个备份文件分别滚动更新（.prev.json）。
"""
import json, urllib.request, urllib.error, shutil, os, time
PROXY = "http://localhost:15236"
BASE = "https://yaobi.zeabur.app"
TOKEN = "yaobi1024"
TRACK = "C:/Users/Martin/Downloads/ambush_track.json"
REGIME = "C:/Users/Martin/Downloads/regime_log.json"
PAPER = "C:/Users/Martin/Downloads/paper_state.json"   # 2026-10-22: 纸面组合状态备份
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

# ---- step 1: merge inject (tracker + regime_log + paper in one body) ----
store = json.load(open(TRACK, encoding="utf-8"))
n_local = len(store.get("entries", []))
if os.path.exists(REGIME):
    try:
        rows = json.load(open(REGIME, encoding="utf-8"))
        if isinstance(rows, list) and rows:
            store["regime_log"] = rows
            print("local regime_log rows:", len(rows))
    except Exception as e:
        print("regime backup unreadable, tracker-only restore:", e)
# 纸面组合：部署清卷后 data/paper.json 会重置为空，用本地备份回填
if os.path.exists(PAPER):
    try:
        paper_state = json.load(open(PAPER, encoding="utf-8"))
        if isinstance(paper_state, dict) and ("positions" in paper_state or "cash" in paper_state):
            store["paper"] = paper_state
            print("local paper state injected (positions=%d, closed=%d)" % (
                len(paper_state.get("positions", [])), len(paper_state.get("closed", []))))
    except Exception as e:
        print("paper backup unreadable, skipping paper restore:", e)
body = json.dumps(store, ensure_ascii=False).encode("utf-8")
print("local tracker entries:", n_local)
req = urllib.request.Request(BASE + "/restore_track?merge=1", data=body, method="POST")
req.add_header("X-Restore-Token", TOKEN)
req.add_header("Content-Type", "application/json")
for attempt in range(3):
    try:
        with opener.open(req, timeout=60) as r:
            resp = json.load(r)
        break
    except urllib.error.HTTPError as e:
        print("MERGE_HTTP_ERR:", e.code, e.read().decode("utf-8", "replace")[:400])
        raise SystemExit(1)
    except Exception as e:
        print("merge attempt %d err: %s" % (attempt + 1, e))
        if attempt == 2:
            raise SystemExit(1)
        time.sleep(5)
print("MERGE_RESP:", json.dumps(resp, ensure_ascii=False)[:300])
if not resp.get("ok"):
    raise SystemExit(1)

# ---- step 2: export fresh snapshot (tracker + regime) ----
time.sleep(3)
req2 = urllib.request.Request(BASE + "/export_track?with_regime=1")
req2.add_header("X-Restore-Token", TOKEN)
with opener.open(req2, timeout=90) as r:
    snap = json.load(r)
n_new = len(snap.get("entries", []))
rows_new = snap.pop("regime_log", None)
paper_new = snap.pop("paper", None)   # 2026-10-22: 同步备份纸面组合状态
print("exported entries:", n_new, "| regime rows:", len(rows_new) if isinstance(rows_new, list) else 0,
      "| paper:", "yes" if isinstance(paper_new, dict) else "no")

def rotate(path, data_bytes):
    bak = path.replace(".json", ".prev.json")
    if os.path.exists(path):
        shutil.copy2(path, bak)
        print("rotated old ->", bak)
    with open(path, "wb") as f:
        f.write(data_bytes)
    print("saved ->", path, "(%.1f KB)" % (len(data_bytes) / 1024))

rotate(TRACK, json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8"))
if isinstance(rows_new, list) and rows_new:
    rotate(REGIME, json.dumps(rows_new, ensure_ascii=False, indent=2).encode("utf-8"))
if isinstance(paper_new, dict):
    rotate(PAPER, json.dumps(paper_new, ensure_ascii=False, indent=2).encode("utf-8"))
print("RECOVERY_DONE")
