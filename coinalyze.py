"""Coinalyze v1 API client — batched derivatives data (OI / liquidations / LSR).

Why this exists: Binance's openInterestHist is one HTTP call per coin, so the
scan could only afford ~80/round allocated by *alphabetical scan order* — a
coin got OI because of its name, not its merit. Coinalyze batches up to 20
symbols per request, covers Binance perps (exchange code "A", internal symbol
e.g. ARKUSDT_PERP.A), and exposes two datasets Binance does not serve
historically: liquidations and the accounts long/short ratio.

Free tier: 40 API calls per minute per key, and each symbol inside a batched
request consumes one call — so the limiter counts symbols, not requests.

Key: coinalyze.net -> account/api-key -> put "coinalyze_key" in
monitor_config.json (gitignored) or set the COINALYZE_KEY env var.

Fail-soft by design: every public fetch returns {} on error so the monitor
can fall back to its Binance per-coin path.
"""

import json
import os
import time
import urllib.request
import urllib.parse

BASE = "https://api.coinalyze.net/v1"
BINANCE = "A"                        # Coinalyze exchange code for Binance
CACHE = os.path.join("data", "coinalyze_markets.json")
CACHE_TTL = 7 * 86400
BATCH = 20                           # max symbols per request (API limit)
RATE = 40                            # calls per minute (each symbol = 1 call)

_calls = []  # monotonic timestamps of consumed calls, for rate limiting


def _key():
    k = os.environ.get("COINALYZE_KEY")
    if k:
        return k
    try:
        with open("monitor_config.json", encoding="utf-8") as f:
            k = (json.load(f) or {}).get("coinalyze_key")
    except Exception:
        return None
    return k or None


def has_key():
    return bool(_key())


def _throttle(n):
    """Each symbol inside a batched request costs one API call."""
    now = time.monotonic()
    while _calls and now - _calls[0] > 60:
        _calls.pop(0)
    if len(_calls) + n > RATE and _calls:
        wait = 60 - (now - _calls[0]) + 0.5
        if wait > 0:
            print(f"[coinalyze] rate limit: sleeping {wait:.0f}s "
                  f"({len(_calls)} calls in window)")
            time.sleep(wait)
        now = time.monotonic()
        while _calls and now - _calls[0] > 60:
            _calls.pop(0)
    _calls.extend([time.monotonic()] * n)


def _get(path, params):
    p = dict(params)
    p["api_key"] = _key()
    url = f"{BASE}/{path}?{urllib.parse.urlencode(p)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read() or b"null")


def _filter_markets(rows):
    """Binance USDT perpetuals only: {'ARKUSDT': 'ARKUSDT_PERP.A'}."""
    return {row["symbol_on_exchange"]: row["symbol"] for row in rows
            if row.get("exchange") == BINANCE and row.get("is_perpetual")
            and row.get("quote_asset") == "USDT"}


def market_map(force=False):
    """Binance-USDT-perp mapping, cached for a week under data/."""
    if not force and os.path.exists(CACHE):
        try:
            if time.time() - os.path.getmtime(CACHE) < CACHE_TTL:
                with open(CACHE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
    m = _filter_markets(_get("future-markets", {}) or [])
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(m, f)
    except Exception:
        pass
    return m


def _chunks(syms):
    for i in range(0, len(syms), BATCH):
        yield syms[i:i + BATCH]


def _mapped(syms):
    """Map Binance symbols to Coinalyze symbols -> (query_list, inverse_map)."""
    try:
        mm = market_map()
    except Exception as e:
        print(f"[coinalyze] market map failed: {e}")
        return [], {}
    inv = {}
    for s in syms:
        c = mm.get(s)
        if c:
            inv[c] = s
    return list(inv.keys()), inv


def _oi_change_from_history(h, now):
    """24h OI change from hourly points; baseline = latest point >= 24h old."""
    if not h or len(h) < 2:
        return None
    last = h[-1]
    base = None
    for pt in h:
        if pt.get("t", 0) <= now - 24 * 3600:
            base = pt
    if base is None or not base.get("c") or not last.get("c"):
        return None
    return last["c"] / base["c"] - 1.0


def fetch_oi_batch(syms):
    """{binance_sym: 24h OI change (fraction)} for the given symbols."""
    q, inv = _mapped(syms)
    out = {}
    now = int(time.time())
    for chunk in _chunks(q):
        _throttle(len(chunk))
        try:
            rows = _get("open-interest-history", {
                "symbols": ",".join(chunk), "interval": "1hour",
                "from": now - 26 * 3600, "to": now,
                "convert_to_usd": "true"}) or []
        except Exception as e:
            print(f"[coinalyze] oi history failed: {e}")
            continue
        for row in rows:
            s = inv.get(row.get("symbol"))
            chg = _oi_change_from_history(row.get("history") or [], now)
            if s and chg is not None:
                out[s] = chg
    return out


def fetch_liq_batch(syms):
    """{binance_sym: (long_usd_24h, short_usd_24h)} — summed hourly liquidations."""
    q, inv = _mapped(syms)
    out = {}
    now = int(time.time())
    for chunk in _chunks(q):
        _throttle(len(chunk))
        try:
            rows = _get("liquidation-history", {
                "symbols": ",".join(chunk), "interval": "1hour",
                "from": now - 25 * 3600, "to": now,
                "convert_to_usd": "true"}) or []
        except Exception as e:
            print(f"[coinalyze] liq history failed: {e}")
            continue
        for row in rows:
            s = inv.get(row.get("symbol"))
            h = row.get("history") or []
            if not s or not h:
                continue
            lg = sum(pt.get("l") or 0 for pt in h)
            sh = sum(pt.get("s") or 0 for pt in h)
            if lg or sh:
                out[s] = (round(lg), round(sh))
    return out


def fetch_lsr_batch(syms):
    """{binance_sym: accounts long/short ratio (latest hourly point)}."""
    q, inv = _mapped(syms)
    out = {}
    now = int(time.time())
    for chunk in _chunks(q):
        _throttle(len(chunk))
        try:
            rows = _get("long-short-ratio-history", {
                "symbols": ",".join(chunk), "interval": "1hour",
                "from": now - 3 * 3600, "to": now}) or []
        except Exception as e:
            print(f"[coinalyze] lsr history failed: {e}")
            continue
        for row in rows:
            s = inv.get(row.get("symbol"))
            h = row.get("history") or []
            if s and h and isinstance(h[-1].get("r"), (int, float)):
                out[s] = h[-1]["r"]
    return out


if __name__ == "__main__":
    import sys
    # ---- offline selftest: the two pure functions, no network ----
    now = 1791822634
    h = [{"t": now - 25 * 3600, "c": 100.0},
         {"t": now - 24 * 3600, "c": 110.0},
         {"t": now - 3600, "c": 132.0},
         {"t": now, "c": 143.0}]
    chg = _oi_change_from_history(h, now)
    assert chg is not None and abs(chg - 0.3) < 1e-9, chg      # 143/110 - 1
    assert _oi_change_from_history([{"t": 1, "c": 5.0}], now) is None
    assert _oi_change_from_history(
        [{"t": now - 3600, "c": 5.0}, {"t": now, "c": 6.0}], now) is None  # no 24h base
    rows = [
        {"symbol": "ARKUSDT_PERP.A", "symbol_on_exchange": "ARKUSDT", "exchange": "A",
         "is_perpetual": True, "quote_asset": "USDT"},
        {"symbol": "BTCUSDT_PERP.6", "symbol_on_exchange": "BTCUSDT", "exchange": "6",
         "is_perpetual": True, "quote_asset": "USDT"},          # not Binance
        {"symbol": "ETHUSDC_PERP.A", "symbol_on_exchange": "ETHUSDC", "exchange": "A",
         "is_perpetual": True, "quote_asset": "USDC"},          # not USDT quote
        {"symbol": "XYZUSDT_PERP.A", "symbol_on_exchange": "XYZUSDT", "exchange": "A",
         "is_perpetual": False, "quote_asset": "USDT"},         # not perp
    ]
    m = _filter_markets(rows)
    assert m == {"ARKUSDT": "ARKUSDT_PERP.A"}, m
    print("coinalyze offline selftest OK")

    # ---- optional live check: python coinalyze.py ARKUSDT TAKEUSDT ----
    if len(sys.argv) > 1:
        if not has_key():
            print("no api key (monitor_config.json coinalyze_key / COINALYZE_KEY)")
            sys.exit(0)
        for syms in (sys.argv[1:],):
            print("live OI :", {k: f"{v*100:+.1f}%" for k, v in fetch_oi_batch(syms).items()})
            print("live liq:", fetch_liq_batch(syms))
            print("live lsr:", fetch_lsr_batch(syms))
