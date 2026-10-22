# -*- coding: utf-8 -*-
"""8d5e66b 部署后自动恢复：等新 pod（push 后新扫描 + regime_log 行数<8）+
首扫回填 >1000，然后自动跑 do_recover.py（tracker + regime_log 双资产）。"""
import json, time, urllib.request, subprocess, sys
PROXY = "http://localhost:15236"
BASE = "https://yaobi.zeabur.app"
PUSH_AFTER = "2026-10-22T07:20"  # UTC, 本轮 push 时间（paper 板块部署）
OLD_REGIME_ROWS = 23             # 老 pod 备份时的 regime_log 行数（07:20 导出=23）
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

def fetch(url, timeout=30):
    with opener.open(url, timeout=timeout) as r:
        return json.load(r)

deadline = time.time() + 40 * 60
n = 0
while time.time() < deadline:
    n += 1
    try:
        d = fetch(BASE + "/dashboard_data.json")
        amb = (d.get("ambush") or {})
        st = amb.get("stats") or {}
        ls = ((d.get("meta") or {}).get("last_scan") or "")[:16]
        total = st.get("total") or 0
        # 老 pod 在 03:37 还会再扫一轮，用 regime_log 行数区分新老 pod
        try:
            rl = fetch(BASE + "/regime_log.json", timeout=15)
            regime_n = len(rl) if isinstance(rl, list) else -1
        except Exception:
            regime_n = -1
        print("poll %d: total=%s last_scan=%s regime_rows=%s" % (n, total, ls, regime_n), flush=True)
        new_pod = ls >= PUSH_AFTER and regime_n < OLD_REGIME_ROWS
        if new_pod and total > 1000:
            print("NEW_POD_LIVE_AND_BACKFILLED", flush=True)
            time.sleep(30)  # 让首扫完全落盘再合并
            r = subprocess.run([sys.executable, "D:/memecoin-monitor/do_recover.py"],
                               capture_output=True, text=True, timeout=300)
            print(r.stdout, flush=True)
            if r.returncode != 0:
                print("RECOVER_SCRIPT_STDERR:", r.stderr[:800], flush=True)
                sys.exit(1)
            print("ALL_DONE", flush=True)
            break
    except Exception as e:
        print("poll %d err: %s" % (n, str(e)[:120]), flush=True)
    time.sleep(90)
else:
    print("TIMEOUT", flush=True)
    sys.exit(1)
