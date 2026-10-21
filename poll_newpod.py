# -*- coding: utf-8 -*-
"""Poll live dashboard until the new pod (new code) is up and first scan done."""
import json, urllib.request, time, sys

PROXY = "http://localhost:15236"
BASE = "https://yaobi.zeabur.app/dashboard_data.json"

op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

def fetch():
    for i in range(8):
        try:
            return json.load(op.open(BASE, timeout=90))
        except Exception:
            time.sleep(5)
    return None

for loop in range(20):
    d = fetch()
    if not d:
        print("[%s] loop %d fetch failed" % (time.strftime("%H:%M:%S"), loop), flush=True)
        time.sleep(30)
        continue
    m = d.get("meta") or {}
    amb = d.get("ambush") or {}
    st = amb.get("stats") or {}
    rf = amb.get("recent_finals") or []
    rk = d.get("ranked") or []
    conf = sum(1 for r in rk if r.get("vt_confirmed"))
    print("[%s] last_scan=%s total=%s live=%s finals_rows=%d confirmed=%d"
          % (time.strftime("%H:%M:%S"), str(m.get("last_scan"))[:19],
             st.get("total"), st.get("live"), len(rf), conf), flush=True)
    # new pod signature: new code renders 20 finals rows, or tracker dropped to
    # first-scan backfill size (fresh pod, recovery not yet done)
    if st.get("total") and st.get("total") >= 2000:
        print(">>> RECOVERY DONE (total=%s)" % st.get("total"), flush=True)
        break
    time.sleep(30)
print("POLL DONE", flush=True)
