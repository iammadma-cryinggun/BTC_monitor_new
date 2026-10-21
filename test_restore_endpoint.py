"""Hermetic smoke test for the volume-loss recovery tooling (no network).

2026-10-19 second incident: a manual restart rescheduled the pod onto a node
whose data volume came up EMPTY — monitor_state / ambush_track / scan_summary
all gone. The user keeps local downloads of ambush_track.json; this tests the
path that puts them back.

Covers:
  1. POST /restore_track: token auth (missing/wrong -> 403, disabled when env
     unset), valid backup -> 200 + file written, non-empty clobber guard
     (409 without ?force=1, 200 with), garbage body -> 400.
  2. monitor._restore_dashboard_state() seeds _feed_was_ok=False when the
     persisted scan_summary says ok==0, so the first successful scan after a
     restart sends the 🟢 recovery note.

Run: python test_restore_endpoint.py  (exit 0 = pass)
"""
import os, sys, json, time, tempfile, shutil, http.client, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

# isolate data dir BEFORE import
tmp = tempfile.mkdtemp(prefix="restore_test_")
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
_MOVED = []
for name in ("ambush_track.json", "signals_log.json", "monitor_state.json",
             "scan_summary.json", "dashboard_data.json", "dashboard_data.html"):
    cur = os.path.join(DATA, name)
    if os.path.exists(cur):
        bak = os.path.join(tmp, name + ".bak")
        shutil.move(cur, bak)
        _MOVED.append((bak, cur))

import dashboard
import monitor as monitor_mod

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)

# ---------- part 1: restore endpoint ----------
os.environ["RESTORE_TOKEN"] = "sekrit-token-123"
srv = dashboard.DashboardServer(host="127.0.0.1", port=0, data_dir=tmp, monitor=None)
# bind an ephemeral port
import socketserver
srv.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), srv._handler())
srv.httpd.daemon_threads = True
threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
PORT = srv.httpd.server_address[1]

BACKUP = {"entries": [dict(sym="AAAUSDT", ts="2026-10-18T00:00:00+00:00", status="waiting",
                           entry_price=1.0, source="live", tier="main"),
                      dict(sym="BBBUSDT", ts="2026-10-18T01:00:00+00:00", status="win",
                           entry_price=2.0, source="backfill", tier="main")]}

def post(path, body, token="sekrit-token-123"):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    hdr = {"Content-Type": "application/json"}
    if token is not None:
        hdr["X-Restore-Token"] = token
    conn.request("POST", path, body=body, headers=hdr)
    r = conn.getresponse()
    out = json.loads(r.read().decode("utf-8"))
    conn.close()
    return r.status, out

BODY = json.dumps(BACKUP)
st, out = post("/restore_track", BODY, token=None)
check("no token -> 403", st == 403 and not out.get("ok"))
st, out = post("/restore_track", BODY, token="wrong")
check("wrong token -> 403", st == 403 and not out.get("ok"))
st, out = post("/restore_track", "{not json")
check("garbage body -> 400", st == 400 and not out.get("ok"))
st, out = post("/other_route", BODY)
check("unknown POST route -> 404", st == 404)

st, out = post("/restore_track", BODY)
check("valid backup -> 200 ok, 2 entries", st == 200 and out.get("ok") and out.get("restored") == 2)
restored = json.load(open(os.path.join(tmp, "ambush_track.json"), encoding="utf-8"))
check("file on disk matches backup", len(restored.get("entries") or []) == 2)

st, out = post("/restore_track", BODY)
check("clobber guard -> 409 without force", st == 409 and "force=1" in out.get("error", ""))
st, out = post("/restore_track?force=1", BODY)
check("force=1 -> 200 overwrite", st == 200 and out.get("ok"))

# ---------- part 1b: export endpoint (symmetric online backup pull) ----------
def get(path, token="sekrit-token-123"):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    hdr = {}
    if token is not None:
        hdr["X-Restore-Token"] = token
    conn.request("GET", path, headers=hdr)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    return r.status, raw

st, raw = get("/export_track", token=None)
check("export: no token -> 403", st == 403)
st, raw = get("/export_track", token="wrong")
check("export: wrong token -> 403", st == 403)
st, raw = get("/export_track")
try:
    ex_body = json.loads(raw.decode("utf-8"))
except Exception:
    ex_body = {}
check("export: valid token -> 200 full tracker (2 entries)",
      st == 200 and len(ex_body.get("entries") or []) == 2)
st, raw = get("/export_track?token=sekrit-token-123", token=None)
check("export: ?token= query also accepted", st == 200)
check("export: exported file equals what restore wrote",
      json.loads(raw.decode("utf-8")).get("entries") ==
      json.load(open(os.path.join(tmp, "ambush_track.json"), encoding="utf-8")).get("entries"))

# ---------- part 1c: regime_log rides along (2026-10-21) ----------
REGIME = {"entries": BACKUP["entries"],
          "regime_log": [dict(ts="2026-10-21T01:00:00+00:00", n=513, btc="neutral",
                              coiling=0.61, lite=0.17),
                         dict(ts="2026-10-21T03:00:00+00:00", n=513, btc="bullish",
                              coiling=0.63, lite=0.16)]}
st, out = post("/restore_track?force=1", json.dumps(REGIME))
check("restore with regime_log -> 200, 2 rows folded",
      st == 200 and out.get("ok") and out.get("regime_log") == 2)
pure = json.load(open(os.path.join(tmp, "ambush_track.json"), encoding="utf-8"))
check("tracker file stays pure (no regime_log key)", "regime_log" not in pure)
rl = json.load(open(os.path.join(tmp, "regime_log.json"), encoding="utf-8"))
check("regime_log.json written with 2 rows", isinstance(rl, list) and len(rl) == 2)
st, out = post("/restore_track?force=1", json.dumps(REGIME))
check("re-restore dedupes regime rows (0 new)",
      st == 200 and out.get("regime_log") == 0)
st, raw = get("/export_track?with_regime=1")
ex = json.loads(raw.decode("utf-8"))
check("export ?with_regime=1 carries regime_log (2 rows)",
      st == 200 and len(ex.get("regime_log") or []) == 2
      and len(ex.get("entries") or []) == 2)
st, raw = get("/export_track")
check("plain export stays pure (no regime_log key)",
      "regime_log" not in json.loads(raw.decode("utf-8")))

del os.environ["RESTORE_TOKEN"]
st, out = post("/restore_track", BODY, token=None)
check("env unset -> endpoint disabled 403", st == 403 and "RESTORE_TOKEN" in out.get("error", ""))
st, raw = get("/export_track", token=None)
check("export: env unset -> disabled 403", st == 403 and b"RESTORE_TOKEN" in raw)
os.environ["RESTORE_TOKEN"] = "sekrit-token-123"
srv.shutdown()

# ---------- part 2: _feed_was_ok seeding across restart ----------
CFG = dict(market="futures", universe="list", symbols=["AAAUSDT"],
           scan_interval_minutes=120, digest_hour=8,
           local_utc_offset_hours=8, request_sleep=0.0, web={})

class FakeNotifier:
    def send(self, *a, **k): return True

# persisted snapshot says last round was a TOTAL failure
snap = {"meta": {"scan_summary": {"attempted": 519, "ok": 0, "skipped": 519},
                 "last_scan": "2026-10-19T07:00:00+00:00"},
        "ranked": []}
with open(os.path.join(DATA, "dashboard_data.json"), "w", encoding="utf-8") as f:
    json.dump(snap, f)
mon = monitor_mod.Monitor(CFG, FakeNotifier())
mon._restore_dashboard_state()
check("ok==0 snapshot -> _feed_was_ok False", mon._feed_was_ok is False)
check("scan_summary seeded", mon.scan_summary.get("ok") == 0)

snap["meta"]["scan_summary"] = {"attempted": 519, "ok": 400, "skipped": 119}
with open(os.path.join(DATA, "dashboard_data.json"), "w", encoding="utf-8") as f:
    json.dump(snap, f)
mon2 = monitor_mod.Monitor(CFG, FakeNotifier())
mon2._restore_dashboard_state()
check("ok>0 snapshot -> _feed_was_ok stays True", mon2._feed_was_ok is True)

# ---------- cleanup ----------
os.remove(os.path.join(DATA, "dashboard_data.json"))
for bak, cur in _MOVED:
    if os.path.exists(bak):
        shutil.move(bak, cur)
shutil.rmtree(tmp, ignore_errors=True)
for name in ("monitor_state.json", "signals_log.json", "ambush_track.json",
             "scan_summary.json", "dashboard_data.html"):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        os.remove(p)

print()
if FAIL:
    print(f"{len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("ALL RESTORE-ENDPOINT CHECKS PASSED")
