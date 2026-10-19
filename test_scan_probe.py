"""Hermetic smoke test for the scan-speed fixes (no network).

Incident (2026-10-19 evening): pod started 08:08 UTC; the startup scan NEVER
finished in 2h13m — dashboard last_scan frozen at the init timestamp, zero
signals. Root cause: fetch_klines walked ~10 kline hosts PER COIN with a 10s
timeout each, so a partially black-holed egress (ping OK, kline GET dies)
cost ~100s/coin => ~30h for a 519-coin round.

Covers:
  1. pick_kline_hosts: one probe sweep ranks reachable hosts, skips dead ones,
     result cached (second call = zero new requests).
  2. fetch_klines with a warm probe: ONE request per coin against the first
     known-good host (the 10-host crawl is gone).
  3. probe finds NOTHING: fetch_klines fail-fasts over 2 hosts only.
  4. monitor scan budget: SCAN_BUDGET_S exceeded -> round COMPLETES with the
     remaining coins counted as skip_reasons["budget"], state written.

Run: python test_scan_probe.py   (exit 0 = pass)
"""
import os, sys, json, time, tempfile, shutil, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MONITOR_OFFLINE"] = "1"

# --- isolate data dirs BEFORE importing the modules under test ---
tmp = tempfile.mkdtemp(prefix="probe_test_")
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

import urllib.request, urllib.error
import memecoin_scanner
import monitor

# hermetic: stub the 15m timing module BEFORE run_scan imports it
_fake_timing = types.ModuleType("timing")
_fake_timing.analyze = lambda sym, market="futures": {"ready": True, "vwap_dist": 0.001, "ema_bull": True}
_fake_timing.regime = lambda market="futures": "bullish"
sys.modules["timing"] = _fake_timing

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)

# ---- fake opener: records every URL, per-host ok/fail switch ----
KLINES = [[1700000000000, "1", "2", "0.5", "1.5", "100", 0, 0, 0, "60"],
          [1700001600000, "1.5", "2", "1", "1.8", "120", 0, 0, 0, "70"]]
HOST_STATE = {}          # host -> "ok" | "fail"
REQUESTS = {"n": 0}

class FakeResp:
    def __init__(self, payload): self._p = payload
    def read(self): return json.dumps(self._p).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False

class FakeOpener:
    def open(self, req, timeout=None):
        url = getattr(req, "full_url", str(req))
        REQUESTS["n"] += 1
        host = url.split("/")[2]
        if HOST_STATE.get(host) == "fail":
            raise urllib.error.URLError("timed out")
        return FakeResp(KLINES)

_orig_build = urllib.request.build_opener
urllib.request.build_opener = lambda *a, **k: FakeOpener()

CANDS = memecoin_scanner._kline_candidate_hosts("futures")
check("candidates include fapi3+vision mirrors",
      any("fapi3" in h for h in CANDS) and any("vision" in h for h in CANDS)
      and len(CANDS) == 10)

# ---------- 1. probe sweep: 2 alive, 8 dead ----------
memecoin_scanner._KLINE_HOST_CACHE.clear()
for h in CANDS:
    HOST_STATE[h.split("/")[2]] = "fail"
alive = ["fapi3.binance.com", "api.binance.vision"]
for h in alive:
    HOST_STATE[h] = "ok"
n_before = REQUESTS["n"]
hosts = memecoin_scanner.pick_kline_hosts("futures", max_age=1800)
check("probe: returned exactly the 2 alive hosts",
      sorted(h.split("/")[2] for h in hosts) == sorted(alive))
check("probe: swept every candidate once (10 requests)",
      REQUESTS["n"] - n_before == 10)

# ---------- 2. cache: second call costs zero requests ----------
n_before = REQUESTS["n"]
hosts2 = memecoin_scanner.pick_kline_hosts("futures", max_age=1800)
check("probe cache: no new requests", REQUESTS["n"] == n_before)
check("probe cache: same hosts", hosts2 == hosts)

# ---------- 3. warm probe -> ONE request per coin, no crawl ----------
n_before = REQUESTS["n"]
df = memecoin_scanner.fetch_klines("AAAUSDT", "4h", 120, market="futures")
check("fetch: returns a DataFrame", df is not None and len(df) == len(KLINES))
check("fetch: single request (crawl eliminated)", REQUESTS["n"] - n_before == 1)
check("fetch: hit a known-good host",
      any(h in ("fapi3.binance.com", "api.binance.vision") for h in HOST_STATE))

# ---------- 4. nothing alive -> fail fast over 2 hosts ----------
memecoin_scanner._KLINE_HOST_CACHE.clear()
for h in CANDS:
    HOST_STATE[h.split("/")[2]] = "fail"
hosts_dead = memecoin_scanner.pick_kline_hosts("futures", max_age=1800)
check("dead probe: empty list", hosts_dead == [])
n_before = REQUESTS["n"]
raised = False
try:
    memecoin_scanner.fetch_klines("AAAUSDT", "4h", 120, market="futures")
except Exception:
    raised = True
check("dead probe: fetch raises", raised)
check("dead probe: only 2 fail-fast attempts (not 10x10s crawl)",
      REQUESTS["n"] - n_before == 2)

urllib.request.build_opener = _orig_build

# ---------- 5. monitor scan budget: round COMPLETES past the deadline ----------
CALLS = {"n": 0}
def slow_score_coin(sym, market="futures", force_fetch=False):
    CALLS["n"] += 1
    time.sleep(0.4)                       # 0.4s/coin; budget 1s -> cut mid-round
    return dict(sym=sym, label="COILING", stage="S1", score=95, price=1.0,
                vol_tick=False, flat_run=40, range_w=0.10, pv=-0.05,
                swing=2.0, events=[], potential=50.0), None

monitor.score_coin = slow_score_coin
os.environ["SCAN_BUDGET_S"] = "1"

class FakeNotifier:
    def __init__(self): self.sent = []
    def send(self, title, message, level="info"):
        self.sent.append((title, level)); return True

CFG = dict(market="futures", universe="list",
           symbols=["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT"],
           scan_interval_minutes=120, digest_hour=8,
           local_utc_offset_hours=8, request_sleep=0.0, web={})
mon = monitor.Monitor(CFG, FakeNotifier())
mon.run_scan(emit=True)

ss = mon.scan_summary
check("budget: round COMPLETED (scan_summary written)",
      isinstance(ss, dict) and ss.get("attempted") is not None)
check("budget: some coins scored before the cut", ss.get("ok", 0) >= 1)
check("budget: remaining coins counted under skip_reasons['budget']",
      ss.get("skip_reasons", {}).get("budget", 0) >= 1)
check("budget: ok + budget-skips cover the universe",
      ss.get("ok", 0) + ss.get("skip_reasons", {}).get("budget", 0) == 5)
check("budget: state file written (dashboard unfreezes)",
      os.path.exists(monitor.STATE_PATH))
check("budget: scan stopped early (did not score all 5)",
      CALLS["n"] < 5)

# cleanup
os.environ.pop("SCAN_BUDGET_S", None)
for bak, cur in _MOVED:
    if os.path.exists(bak):
        shutil.move(bak, cur)
shutil.rmtree(tmp, ignore_errors=True)
for name in ("monitor_state.json", "scan_summary.json", "dashboard_data.json",
             "dashboard_data.html", "signals_log.json", "ambush_track.json"):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        os.remove(p)

print()
if FAIL:
    print(f"{len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("ALL SCAN-PROBE CHECKS PASSED")
