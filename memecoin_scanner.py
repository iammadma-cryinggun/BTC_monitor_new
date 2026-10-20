"""
Memecoin "埋伏" (ambush) scanner.

Goal: across a universe of Binance meme/low-cap USDT pairs, find coins that
match the user's 6-stage pump-and-dump playbook and rank them by how good a
*target to stake out early* they are.

Two actionable buckets:
  COILING     -> currently in long tight consolidation (S1). Best "埋伏" candidates:
                 flat + quiet, possibly with early volume pick-up. Pump may be near.
  FRESH_LAUNCH-> just broke out of consolidation (S2/S3) within the last few bars.
                 Per playbook rule #1: add to watchlist immediately, ride S3->S4.

Non-actionable (low priority, shown for context):
  PUMPED  -> already in markup/top (S4/S5): too late / high risk.
  CRASHING-> in crash phase (S6): do not catch the falling knife.

Reuses detect()/add_indicators() from memecoin_playbook_scanner.py.

Usage:
  python memecoin_scanner.py                 # scan curated universe, build report
  python memecoin_scanner.py --top 12        # show top N in console
  python memecoin_scanner.py --sym-list DOGEUSDT,PEPEUSDT
"""
import os, sys, json, argparse, time
import urllib.request, urllib.error, http.client
import pandas as pd, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
sys.path.insert(0, HERE)
from memecoin_playbook_scanner import detect, add_indicators  # reuse

# ---- curated Binance meme / low-cap universe (scanner skips any 404) ----
UNIVERSE = [
    "BANKUSDT",                                  # validated reference coin
    "DOGEUSDT","SHIBUSDT","PEPEUSDT","WIFUSDT","BOMEUSDT","FLOKIUSDT","BONKUSDT",
    "MEMEUSDT","NEIROUSDT","TURBOUSDT","MOODENGUSDT","GOATUSDT","PNUTUSDT","ACTUSDT",
    "BRETTUSDT","POPCATUSDT","MUMUUSDT","FWOGUSDT","SUNUSDT","MOGUSDT","MEWUSDT",
    "DADDYUSDT","LADYSUSDT","WOJAKUSDT","APUUSDT","CATUSDT","ELONUSDT","SAMOUSDT",
    "HARAMBEUSDT","BIAOQINGUSDT","MYROUSDT","PONKEUSDT","WHYUSDT","RETARDIOUSDT",
    "CHILLGUYUSDT","GIGAUSDT","SPXUSDT","AIDOGEUSDT","LUNCUSDT","BTTUSDT","BABYDOGEUSDT",
    "KISHUUSDT","CATEUSDT","TOMIUSDT","BULLYUSDT","SLERFUSDT","BODENUSDT","TREMPUSDT",
    "LOUISUSDT","MOTHERUSDT","GROKUSDT","AISUSDT","XUSDT","DEVUSDT","WENUSDT",
]

# ---- tokens that are NOT 妖币 targets: stablecoins + tokenized stocks ----
STABLE_SET = {"BFUSD","XUSD","EURI","FDUSD","USDC","USDT","TUSD","DAI","BUSD","PYUSD",
              "USDP","USD1","USDD","FRAX","USDE","USDG","USDS"}
STOCK_SET = {"GOOGL","MSFT","ASML","TSLA","BABA","AMZN","NVDA","AMD","META","NFLX","CRM",
             "SMH","DJT","GPRO","WDC","AMC","GME","COIN","PLTR","NKE","BA","DIS","INTC",
             "QCOM","ORCL","ADBE","PYPL","UBER","SHOP","SQ","ROKU","SPY","QQQ","DIA",
             "IWM","VXX","GLD","SLV","USO","UNG","AAPL","MSTR","KO","PFE","XOM","JPM",
             "GS","CAT","GE","IBM","SAP","CSCO","TXN","AVGO","MRK","UNH","V","MA","COST",
             "WMT","HD","BAC","C","T","VZ","PG","PEP","KO","MCD","SBUX","ABBV","LLY","TSM",
             "AMGN","HON","LOW","MDT","NEE","BMV","ITW","DE","ADP","NOW","INTU","AMAT",
             "LRCX","KLAC","SNPS","CDNS","PANW","CRWD","ZS","OKTA","DDOG","NET","MDB",
             "SNOW","HUBS","TWLO","DOCU","ESTC","TEAM","WDAY","ASAN","PATH","BBY","BKNG",
             "EXPE","MAR","HLT","RCL","CCL","NCLH","UAA","GPS","RH","ETSY","WAYFAIR",
             "CHWY","FIVE","DLTR","DG","KR","SJM","MKC","CPB","KHC","MO","PM","TAP",
             "STZ","BF","DEO","MNST","KDP","CAH","COR","MCK","CI","HUM","CNC","THC",
             "UHS","DVA","EBAY","EBA","VRSK","EXPD","CHRW","JBHT","LSTR","ODFL","SAIA",
             "XPO","GXO","FWRD"}
SWING_GATE = 1.30   # 20-day max/min ratio must exceed this to be a real move candidate
# --- secondary tier: coiling large-caps in a FRESH cycle (never 妖过) ---
# main tier requires swing >= 1.30 (a prior pump/dump inside 20d). Coins below
# that gate used to be LOW_VOL. COILING_LITE catches "W/STRK-style" big caps
# grinding at lows: amplitude is NOT enough, so coil quality must compensate
# (longer run, tighter range, glued to MA99) and the score is capped below
# the main tier so it can never outrank a real 妖币 candidate.
LITE_GATE = 1.12        # 20d swing must at least reach this (12%)
LITE_MIN_FLAT = 20      # must coil >= 20 4h bars (~3.3 days), stricter than main tier
LITE_MAX_RANGE = 0.25   # last-15-bar range <= 25%
LITE_PV_WINDOW = (-0.25, 0.15)  # price vs MA99: hugging/below it (grinding at lows)

# curated Binance USD-M FUTURES meme / low-cap perpetual universe
FUTURES_UNIVERSE = [
    # user's real 妖币 (only on futures, not spot)
    "RAVEUSDT","LABUSDT","AKEUSDT","BTWUSDT","BANKUSDT",
    # popular meme perpetuals
    "DOGEUSDT","SHIBUSDT","PEPEUSDT","WIFUSDT","BOMEUSDT","FLOKIUSDT","BONKUSDT",
    "MEMEUSDT","NEIROUSDT","TURBOUSDT","MOODENGUSDT","GOATUSDT","PNUTUSDT","ACTUSDT",
    "BRETTUSDT","POPCATUSDT","MUMUUSDT","FWOGUSDT","SUNUSDT","MOGUSDT","MEWUSDT",
    "DADDYUSDT","LADYSUSDT","WOJAKUSDT","APUUSDT","CATUSDT","ELONUSDT","SAMOUSDT",
    "HARAMBEUSDT","MYROUSDT","PONKEUSDT","WHYUSDT","RETARDIOUSDT","CHILLGUYUSDT",
    "GIGAUSDT","SPXUSDT","AIDOGEUSDT","LUNCUSDT","BTTUSDT","BABYDOGEUSDT","KISHUUSDT",
    "CATEUSDT","SLERFUSDT","BODENUSDT","TREMPUSDT","LOUISUSDT","MOTHERUSDT","GROKUSDT",
    "AISUSDT","XUSDT","DEVUSDT","WENUSDT","BLAZUSDT","ODOSUSDT","SXPUsdt".upper(),
    # extra small-cap perps often with 妖币-like behavior
    "CETUSUSDT","AAVEUSDT","SUIUSDT","SEIUSDT","TIAUSDT","WUSDT","ARKMUSDT","AIXBTUSDT",
    "THEUSDT","ZKUSDT","LISTAUSDT","MOVEUSDT","KAIAUSDT","ROSEUSDT","DUSKUSDT",
    "CELOUSDT","USUALUSDT","SHELLUSDT","OPENUSDT","VINEUSDT","DARKUSDT","SNAIUSDT",
]

SPOT_KLINE_HOSTS = [
    "https://api.binance.com/api/v3/klines",
    "https://api1.binance.com/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
]
FUT_KLINE_HOSTS = [
    "https://fapi.binance.com/fapi/v1/klines",
    "https://fapi1.binance.com/fapi/v1/klines",
    "https://fapi2.binance.com/fapi/v1/klines",
]
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

def _proxy_opener():
    """Build an urllib opener that routes through HTTPS_PROXY/BINANCE_PROXY if set.

    Cloud hosts (Zeabur, Render, Fly, AWS, GCP, ...) are frequently geo-blocked by
    Binance, so a proxy in a Binance-friendly region is the most reliable fix. When
    no proxy is configured we still return a real opener (with .open) so callers can
    uniformly use opener.open(req, timeout=...).
    """
    p = None
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "BINANCE_PROXY"):
        v = os.environ.get(k)
        if v:
            p = v.strip()
            break
    if not p:
        return urllib.request.build_opener()
    try:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p, "https": p}))
    except Exception:
        return urllib.request.build_opener()

def _kline_candidate_hosts(market="spot"):
    """All hosts fetch_klines MAY use, in nominal preference order."""
    if market == "futures":
        return (FUT_KLINE_HOSTS
                + ["https://fapi3.binance.com/fapi/v1/klines",
                   "https://fapi4.binance.com/fapi/v1/klines",
                   "https://fapi5.binance.com/fapi/v1/klines"]
                + SPOT_KLINE_HOSTS
                + ["https://api.binance.vision/api/v3/klines"])
    return (SPOT_KLINE_HOSTS
            + ["https://api.binance.vision/api/v3/klines"]
            + FUT_KLINE_HOSTS)

# probed host order, cached per market (~one sweep per scan round)
_KLINE_HOST_CACHE = {}

def pick_kline_hosts(market="spot", max_age=1800, probe_sym="BTCUSDT"):
    """Probe every candidate kline host ONCE per round (not per coin!) with a tiny
    request; return reachable hosts, fastest-first. Cached ~30 min.

    Why this exists (2026-10-19 incident): fetch_klines used to walk ~10 hosts
    per COIN with a 10s timeout each. On an egress where the connectivity ping
    succeeds but kline GETs black-hole (DPI / large-payload filtering), one coin
    cost ~100s and a 519-coin round ~30 HOURS — the scan never finished, the
    dashboard froze at its init timestamp and no signal was ever emitted.
    Probing once costs <= ~40s worst case; afterwards every coin is ONE request
    against a known-good host. Returns [] when nothing answers.
    """
    import urllib.request, time as _time
    now = _time.time()
    c = _KLINE_HOST_CACHE.get(market)
    if c and now - c["ts"] < max_age:
        return list(c["hosts"])
    opener = _proxy_opener()
    cands = _kline_candidate_hosts(market)
    scored = []
    for host in cands:
        t0 = _time.time()
        try:
            url = f"{host}?symbol={probe_sym}&interval=4h&limit=5"
            req = urllib.request.Request(url, headers=UA)
            with opener.open(req, timeout=4) as resp:
                raw = json.loads(resp.read())
            if raw:
                scored.append((_time.time() - t0, host))
        except Exception:
            pass
    scored.sort()
    hosts = [h for _, h in scored]
    _KLINE_HOST_CACHE[market] = {"ts": now, "hosts": hosts}
    tag = market
    names = ", ".join(h.split("//")[1].split("/")[0].split(".")[0] for h in hosts)
    print(f"[kline-hosts] {tag}: probed {len(cands)} hosts, "
          f"{len(hosts)} reachable -> {names or 'NONE'}")
    return hosts

def fetch_klines(sym, interval, limit, market="spot"):
    import urllib.request, http.client, urllib.error
    opener = _proxy_opener()
    # ONE probe sweep per ~30 min decides the host order for every coin, so a
    # partially-blocked egress costs ~40s per ROUND instead of ~100s per COIN.
    # If the probe found nothing, fail fast (2 hosts, short timeout) instead of
    # crawling 10 hosts x 10s per coin — the outage alerting takes it from there.
    probed = pick_kline_hosts(market)
    if probed:
        hosts = probed
        timeout = 10
    else:
        hosts = _kline_candidate_hosts(market)[:2]
        timeout = 5
    last_err = None
    for host in hosts:
        try:
            url = f"{host}?symbol={sym}&interval={interval}&limit={limit}"
            req = urllib.request.Request(url, headers=UA)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
            raw = json.loads(raw)
            if not raw:
                last_err = Exception("empty klines")
                continue
            rows = [dict(ts_ms=int(k[0]), open=float(k[1]), high=float(k[2]),
                         low=float(k[3]), close=float(k[4]), volume=float(k[5]),
                         tbuy=(float(k[9]) if len(k) > 9 and k[9] not in (None, "") else 0.0))
                    for k in raw]
            return pd.DataFrame(rows)
        except (http.client.IncompleteRead, urllib.error.URLError, Exception) as e:
            last_err = e
            time.sleep(0.3)
    raise last_err

# ----------------------------------------------------------------------------
# Derivatives context (OI / funding) — the "what is the house doing" radar.
# ----------------------------------------------------------------------------
FUNDING_HOSTS = [
    "https://fapi.binance.com/fapi/v1/premiumIndex",
    "https://fapi1.binance.com/fapi/v1/premiumIndex",
    "https://fapi2.binance.com/fapi/v1/premiumIndex",
]
OI_HIST_HOSTS = [
    "https://fapi.binance.com/futures/data/openInterestHist",
    "https://fapi1.binance.com/futures/data/openInterestHist",
    "https://fapi2.binance.com/futures/data/openInterestHist",
]

def fetch_funding_all():
    """ONE bulk call -> {SYMBOL: lastFundingRate} for every USD-M perp.

    premiumIndex without a symbol param returns the whole board, so attaching
    the current funding rate to all ~500 coins costs a single request per scan.
    Returns {} on failure (callers treat missing as 'no data').
    """
    import urllib.request, urllib.error, http.client
    opener = _proxy_opener()
    for host in FUNDING_HOSTS:
        try:
            req = urllib.request.Request(host, headers=UA)
            with opener.open(req, timeout=12) as resp:
                raw = json.loads(resp.read())
            out = {}
            for d in raw:
                sym = d.get("symbol", "")
                if sym.endswith("USDT"):
                    try:
                        out[sym] = float(d["lastFundingRate"])
                    except (KeyError, TypeError, ValueError):
                        pass
            if out:
                return out
        except Exception:
            time.sleep(0.3)
    return {}

def fetch_oi_change(sym):
    """24h open-interest change (fraction) from the 4h OI history.

    OI up = positions piling in (fuel / accumulation); OI down after a pump =
    covering / distribution. None on failure or thin history.
    """
    import urllib.request, urllib.error, http.client
    opener = _proxy_opener()
    for host in OI_HIST_HOSTS:
        try:
            url = f"{host}?symbol={sym}&period=4h&limit=42"
            req = urllib.request.Request(url, headers=UA)
            with opener.open(req, timeout=10) as resp:
                raw = json.loads(resp.read())
            if not raw or len(raw) < 8:
                return None
            ois = []
            for r in raw:
                try:
                    ois.append(float(r["sumOpenInterest"]))
                except (KeyError, TypeError, ValueError):
                    return None
            base = ois[-7] if len(ois) >= 7 else ois[0]   # ~24h ago (6 bars back)
            if base <= 0:
                return None
            return ois[-1] / base - 1.0
        except Exception:
            time.sleep(0.2)
    return None

def get_universe(market="spot"):
    """All TRADING USDT pairs on Binance (spot or futures perpetuals).

    Results are cached to data/scan_<market>/universe_cache.json (24h TTL).
    On fetch failure we reuse a fresh cache if present, otherwise fall back to
    the curated FUTURES_UNIVERSE / UNIVERSE AND emit a clear warning (no silent
    shrink to the small list).
    """
    import urllib.request, time as _time
    cache_dir = os.path.join(DATA, f"scan_{market}")
    cache_path = os.path.join(cache_dir, "universe_cache.json")
    ttl = 24 * 3600
    def _read_cache():
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    c = json.load(f)
                if _time.time() - c.get("ts", 0) < ttl and c.get("symbols"):
                    return c["symbols"]
        except Exception:
            pass
        return None
    def _write_cache(syms):
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"ts": int(_time.time()), "symbols": syms}, f)
        except Exception:
            pass

    if market == "futures":
        hosts = ["https://fapi.binance.com/fapi/v1/exchangeInfo",
                 "https://fapi1.binance.com/fapi/v1/exchangeInfo",
                 "https://fapi2.binance.com/fapi/v1/exchangeInfo",
                 "https://fapi3.binance.com/fapi/v1/exchangeInfo",
                 "https://fapi4.binance.com/fapi/v1/exchangeInfo",
                 "https://fapi5.binance.com/fapi/v1/exchangeInfo"]
        skip = {"USDC","USDT","DAI","TUSD","FDUSD","EUR","BUSD","PYUSD","USDP","BTC","ETH","BNB"}
    else:
        hosts = ["https://api.binance.com/api/v3/exchangeInfo",
                 "https://api1.binance.com/api/v3/exchangeInfo",
                 "https://data-api.binance.vision/api/v3/exchangeInfo"]
        skip = {"USDC","USDT","DAI","TUSD","FDUSD","EUR","BUSD","PYUSD","USDP","BTC","ETH","BNB"}

    opener = _proxy_opener()
    fetched = None
    for host in hosts:
        try:
            req = urllib.request.Request(host, headers=UA)
            with opener.open(req, timeout=25) as resp:
                info = json.loads(resp.read())
            syms = []
            for s in info["symbols"]:
                if market == "futures":
                    if (s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
                            and s.get("quoteAsset") == "USDT" and s["baseAsset"] not in skip):
                        b = s["baseAsset"]
                        if b.endswith(("UP","DOWN","BULL","BEAR")):
                            continue
                        syms.append(s["symbol"])
                else:
                    if (s["status"] == "TRADING" and s.get("isSpotTradingAllowed")
                            and s["quoteAsset"] == "USDT" and s["baseAsset"] not in skip):
                        b = s["baseAsset"]
                        if b.endswith(("UP","DOWN","BULL","BEAR")):
                            continue
                        syms.append(s["symbol"])
            fetched = sorted(set(syms))
            break
        except Exception:
            continue

    if fetched:
        _write_cache(fetched)
        return fetched

    # fetch failed — try cache, then warn loudly instead of silently shrinking
    cached = _read_cache()
    if cached:
        print(f"[universe] WARNING: live exchangeInfo fetch failed; reusing cached "
              f"universe ({len(cached)} symbols, up to 24h old)")
        return cached
    fallback = FUTURES_UNIVERSE if market == "futures" else UNIVERSE
    print(f"[universe] WARNING: exchangeInfo fetch FAILED and no cache; "
          f"falling back to curated list ({len(fallback)} symbols). "
          f"Set MONITOR_UNIVERSE=curated to acknowledge, or fix network/region.")
    return fallback

def connectivity_check(market="futures"):
    """Quick reachability probe for the Binance endpoints we depend on.

    Returns a dict {endpoint_name: HTTP status or 'ERR:...'}. Lets the monitor log
    (and tell the user) whether Binance is reachable from this deployment, and which
    fallback (fapi / vision mirror / proxy) actually works.
    """
    import urllib.request, urllib.error
    opener = _proxy_opener()
    probes = [
        ("fapi", "https://fapi.binance.com/fapi/v1/ping"),
        ("fapi3", "https://fapi3.binance.com/fapi/v1/ping"),
        ("vision_spot", "https://api.binance.vision/api/v3/ping"),
        ("data_api_vision", "https://data-api.binance.vision/api/v3/ping"),
    ]
    out = {}
    for name, url in probes:
        try:
            req = urllib.request.Request(url, headers=UA)
            with opener.open(req, timeout=8) as r:
                out[name] = r.status
        except Exception as e:
            out[name] = f"ERR:{type(e).__name__}"
    return out

def load_or_fetch(sym, interval, limit, market="spot", force_fetch=False):
    scan_dir = os.path.join(DATA, f"scan_{market}")
    os.makedirs(scan_dir, exist_ok=True)
    path = os.path.join(scan_dir, f"{sym}_{interval}.csv")
    if (not force_fetch) and os.path.exists(path):
        df = pd.read_csv(path)
        if len(df) >= limit - 5:
            df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
            return df.sort_values("ts").reset_index(drop=True)
    df = fetch_klines(sym, interval, limit, market=market)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    os.makedirs(DATA, exist_ok=True)
    df.to_csv(path, index=False)
    return df

def potential_of(rec):
    """Upside-potential score (0-100) for a coil candidate.

    score answers "how textbook is the coil" (signal quality); potential
    answers "IF it fires, how big can it get" (elasticity x fuel), which
    is what should drive WHICH candidates get watched / badged / sized:

      elasticity 0-30 : 20d swing — last cycle's 妖性 repeats (beta)
      duration   0-20 : flat_run — stored energy (0.35/bar, ~57 bars caps)
      tightness  0-15 : narrow range = sellers exhausted (30% wide -> 0)
      low-entry  0-15 : price hugging/below MA99 = not pre-pumped
      flow      0-12 : taker-buy share >= 0.55 (+6) + 7d CVD > 0 (+6)
      squeeze    0-8 : negative funding (+4) + OI 24h > +10% (+4)
                        (funding/OI are attached by the monitor AFTER the
                        scan — pass an enriched rec to get these points)
      LITE tier x0.8  : big caps simply move less

    Not a prediction — a ranking heuristic. The ambush tracker's per-bucket
    peak_gain stats are the calibration loop that will tune these weights.
    """
    swing = rec.get("swing") or 1.0
    flat = rec.get("flat_run") or 0
    range_w = rec.get("range_w")
    pv = rec.get("pv")
    tbr = rec.get("tbuy_ratio")
    cvd = rec.get("cvd_7d")
    fd = rec.get("funding")
    oc = rec.get("oi_chg_24h")

    p = 0.0
    p += min(30.0, max(0.0, (swing - 1.12) * 10.0))   # swing 4.1+ -> 30
    p += min(20.0, flat * 0.35)                         # ~57 bars -> 20
    if isinstance(range_w, (int, float)):
        p += max(0.0, 15.0 * (1.0 - range_w / 0.30))    # 15% wide -> 7.5
    if pv is None:
        p += 5.0
    elif pv <= -0.35:
        p += 5.0                                       # too deep, may be dead
    elif pv <= 0.05:
        p += 15.0                                      # ideal grinding zone
    elif pv <= 0.45:
        p += 15.0 - (pv - 0.05) / 0.40 * 13.0
    else:
        p += 2.0                                       # already stretched
    if isinstance(tbr, (int, float)) and tbr >= 0.55:
        p += 6.0
    if isinstance(cvd, (int, float)) and cvd > 0.0:
        p += 6.0
    if isinstance(fd, (int, float)) and fd < 0:
        p += 4.0
    if isinstance(oc, (int, float)) and oc > 0.10:
        p += 4.0
    if rec.get("label") == "COILING_LITE":
        p *= 0.8
    return round(min(100.0, p), 1)


def score_coin(sym, market="spot", force_fetch=False):
    try:
        df4h = load_or_fetch(sym, "4h", 400, market=market, force_fetch=force_fetch)
        df1h = load_or_fetch(sym, "1h", 240, market=market, force_fetch=force_fetch)
    except Exception as e:
        return None, f"fetch fail: {e}"
    if len(df4h) < 100:
        return None, "not enough 4h bars"
    df, events, levels = detect(df4h, df1h)
    s = levels["stage"]
    close = df["close"].values
    volr = df["vol_ratio"].values
    consol = df["in_consol"].values
    n = len(df)

    # trailing consolidation run (count of recent bars flagged in_consol)
    flat_run = 0
    for j in range(n - 1, -1, -1):
        if consol[j]:
            flat_run += 1
        else:
            break
    # volume tick: recent 3-bar avg vs prior ~24-bar baseline — is volume waking up?
    vol_recent = float(np.mean(volr[-3:])) if n >= 3 else float(volr[-1])
    vol_base = float(np.mean(volr[-27:-3])) if n >= 30 else float(np.mean(volr[:max(1, n-3)]))
    vol_tick = (vol_recent > 1.15) and (vol_recent > vol_base * 1.15)
    # tightness of last 15 bars
    hi = float(np.max(df["high"].values[-15:]))
    lo = float(np.min(df["low"].values[-15:]))
    med = float(np.mean(df["close"].values[-15:]))
    range_w = (hi - lo) / med if med > 0 else 1.0
    tightness = 1.0 - range_w
    # price vs 4h MA99 (should be near, not already pumped)
    ma99 = levels["ma99_4h"]
    pv = (close[-1] / ma99 - 1.0) if (ma99 and ma99 > 0) else None
    # launch recency
    launch_recency = None
    for e in reversed(events):
        if e["type"] == "LAUNCH":
            launch_recency = (n - 1) - e["i"]
            break
    # ~1 day move from 4h (last 6 bars)
    day_move = (close[-1] / close[-7] - 1.0) if n >= 7 else 0.0

    # CVD proxy from taker-buy volume (klines index 9). tbuy_ratio = share of
    # recent volume that was AGGRESSIVE BUYING (>0.5 = buyers paying up, i.e.
    # real hands / squeeze fuel, not passive distribution). cvd_7d is the
    # 7-day cumulative delta normalized by volume (-1..+1). Old cached CSVs
    # lack the tbuy column -> both None (shown as n/a).
    tbuy_ratio = None; cvd_7d = None
    if "tbuy" in df.columns and n >= 10:
        try:
            tb = df["tbuy"].values; vv = df["volume"].values
            sv3 = float(np.sum(vv[-3:])); st3 = float(np.sum(tb[-3:]))
            if sv3 > 0:
                tbuy_ratio = st3 / sv3
            w = min(42, n)
            svw = float(np.sum(vv[-w:])); stw = float(np.sum(tb[-w:]))
            if svw > 0:
                cvd_7d = 2.0 * stw / svw - 1.0
        except Exception:
            pass

    # 20-day (120 4h bars) swing ratio — gates out stablecoins / tokenized stocks / dead coins
    win = close[-120:]
    swing = (float(np.max(win)) / float(np.min(win))) if len(win) and np.min(win) > 0 else 1.0
    base = sym[:-4] if sym.endswith("USDT") else sym
    stable = (base in STABLE_SET) or (0.90 <= close[-1] <= 1.10)
    stock = base in STOCK_SET

    label, score, reason = "UNKNOWN", 0, ""
    if stable:
        label, score, reason = "STABLE", 0, "stablecoin — not a 妖币 target"
    elif stock:
        label, score, reason = "STOCK", 0, "tokenized stock — not a 妖币 target"
    elif s in ("S4", "S5"):
        label, score, reason = "PUMPED", 5, "already in markup/top — too late / high risk"
    elif s == "S6":
        label, score, reason = "CRASHING", 0, "in crash phase — do not catch"
    elif s in ("S2", "S3"):
        if launch_recency is not None and launch_recency <= 8:
            score = max(60, 95 - launch_recency * 4)
            label = "FRESH_LAUNCH"
            reason = f"broke out {launch_recency} bars ago (S{s}); add to watch, ride S3->S4"
        else:
            label, score = "LAUNCHED", 38
            reason = f"launched {launch_recency} bars ago; momentum may be fading"
    elif s == "S1":
        if swing >= SWING_GATE:
            label = "COILING"
            score = 35
            score += min(22, flat_run * 1.6)                 # longer coil = better
            score += max(0.0, (tightness - 0.80)) * 120      # tighter range = better
            if vol_tick:
                score += 28                                  # early volume pick-up = about to go (埋伏★)
            if pv is not None and -0.15 <= pv <= 0.45:
                score += 8                                   # near MA99, not already pumped
            score = min(98, score)
            reason = (f"coiling {flat_run} bars, range {range_w*100:.1f}% wide, "
                      f"vol_tick={vol_tick}, 20d swing {swing:.0%}, price vs MA99={pv*100:+.0f}%" if pv is not None
                      else f"coiling {flat_run} bars, range {range_w*100:.1f}% wide, vol_tick={vol_tick}")
        elif (swing >= LITE_GATE and flat_run >= LITE_MIN_FLAT
              and range_w <= LITE_MAX_RANGE and pv is not None
              and LITE_PV_WINDOW[0] <= pv <= LITE_PV_WINDOW[1]):
            # secondary tier: amplitude below the 妖币 gate, so require MORE
            # coil quality and cap the score below every main-tier candidate
            label = "COILING_LITE"
            score = 25
            score += min(18, flat_run * 1.2)                 # longer grind = better
            score += min(12.0, max(0.0, (tightness - 0.80)) * 120)
            if vol_tick:
                score += 20                                  # 次级埋伏★
            score += 8                                       # near MA99 is REQUIRED here
            score = min(78, score)
            reason = (f"lite coiling {flat_run} bars, range {range_w*100:.1f}% wide, "
                      f"vol_tick={vol_tick}, 20d swing {swing:.0%} (below main gate), "
                      f"price vs MA99={pv*100:+.0f}%")
        else:
            label, score, reason = "LOW_VOL", 0, f"dead-flat / low-vol (20d swing {swing:.0%}) - skip"

    spark = [round(float(x), 6) for x in close[-120:]]
    # base potential (no funding/OI yet — the monitor recomputes after enrich)
    _pot_src = dict(sym=sym, label=label, swing=swing, flat_run=flat_run,
                    range_w=range_w, pv=pv, tbuy_ratio=tbuy_ratio, cvd_7d=cvd_7d)
    return dict(
        sym=sym, label=label, stage=s, score=round(score, 1), reason=reason,
        potential=potential_of(_pot_src),
        price=levels["price"],
        ma25_4h=levels["ma25_4h"], ma99_4h=ma99,
        ma99_1h=levels["ma99_1h"],
        flat_run=flat_run, range_w=round(range_w, 4), vol_tick=vol_tick, swing=round(swing, 3),
        pv=(round(pv, 4) if pv is not None else None),
        launch_recency=launch_recency, day_move=round(day_move, 4),
        tbuy_ratio=(round(tbuy_ratio, 3) if tbuy_ratio is not None else None),
        cvd_7d=(round(cvd_7d, 3) if cvd_7d is not None else None),
        events=[{k: e[k] for k in ("type", "ts", "price", "note")} for e in events[-6:]],
        spark=spark,
        bars=int(n), range_start=str(df["ts"].iloc[0]), range_end=str(df["ts"].iloc[-1]),
    ), None

def run(universe, market="spot"):
    out = []
    skipped = []
    for i, sym in enumerate(universe):
        try:
            rec, err = score_coin(sym, market=market)
        except Exception as e:
            rec, err = None, f"err: {e}"
        if rec is None:
            skipped.append((sym, err)); continue
        out.append(rec)
        print(f"  {sym:12s} {rec['label']:12s} score={rec['score']:5.1f} stage={rec['stage']} "
              f"price={rec['price']:.6g}  {rec['reason']}")
        time.sleep(0.12)
    # rank
    actionable = [r for r in out if r["label"] in ("COILING", "COILING_LITE", "FRESH_LAUNCH")]
    actionable.sort(key=lambda r: r["score"], reverse=True)
    others = [r for r in out if r["label"] not in ("COILING", "COILING_LITE", "FRESH_LAUNCH")]
    others.sort(key=lambda r: r["score"], reverse=True)
    return actionable + others, out, skipped

def build_html(ranked, skipped, top_n, market="spot"):
    from datetime import datetime, timezone
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src_label = ("Binance USD-M 合约 (真实妖币在此)" if market == "futures"
                 else "Binance 现货 (仅上币所，真妖币多不在)")
    def spark_svg(vals, w=120, h=28, color="#3b82f6"):
        if not vals: return ""
        v = np.array(vals, dtype=float)
        v = (v - v.min()) / (v.max() - v.min() + 1e-12)
        pts = " ".join(f"{i/(len(v)-1)*w:.1f},{h-(x*h):.1f}" for i, x in enumerate(v))
        return (f'<svg width="{w}" height="{h}"><polyline points="{pts}" fill="none" '
                f'stroke="{color}" stroke-width="1.4"/></svg>')
    def disp_label(r):
        if r["label"] == "COILING":
            return ("埋伏★" if r.get("vol_tick") else "观察")
        if r["label"] == "COILING_LITE":
            return ("次级★" if r.get("vol_tick") else "次级蓄力")
        return {"FRESH_LAUNCH":"刚启动","LAUNCHED":"已启动","PUMPED":"已拉完",
                "CRASHING":"崩盘中","LOW_VOL":"低波动","STABLE":"稳定币",
                "STOCK":"代币股","UNKNOWN":"未知"}.get(r["label"], r["label"])
    lab_color = {"COILING":"#f59e0b","COILING_LITE":"#38bdf8","FRESH_LAUNCH":"#22c55e","LAUNCHED":"#0ea5e9",
                 "PUMPED":"#ef4444","CRASHING":"#6b7280","LOW_VOL":"#4b5563",
                 "STABLE":"#4b5563","STOCK":"#4b5563","UNKNOWN":"#9ca3af"}
    rows = []
    for r in ranked[:top_n]:
        c = lab_color.get(r["label"], "#9ca3af")
        tag = disp_label(r)
        ev = "; ".join(f"{e['type']}@{e['ts'][:10]}" for e in r["events"]) or "—"
        rows.append(f"""<tr>
<td class="rk">{ranked.index(r)+1}</td>
<td class="sym">{r['sym']}</td>
<td><span class="tag" style="background:{c}">{tag}</span></td>
<td>S{r['stage'][-1]}</td>
<td class="sc">{r['score']:.0f}</td>
<td>{r['price']:.6g}</td>
<td>{r['day_move']*100:+.1f}%</td>
<td>{spark_svg(r['spark'], color=c)}</td>
<td class="rs">{r['reason']}</td>
<td class="ev">{ev}</td>
</tr>""")
    skip_html = "".join(f"<li>{s} — {e}</li>" for s, e in skipped) or "<li>none</li>"
    n_act = sum(1 for r in ranked if r["label"] in ("COILING","FRESH_LAUNCH"))
    n_amb = sum(1 for r in ranked if r["label"]=="COILING" and r.get("vol_tick"))
    n_fresh = sum(1 for r in ranked if r["label"]=="FRESH_LAUNCH")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Memecoin 埋伏扫描器</title>
<style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;background:#0f1115;color:#e5e7eb;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#9ca3af;font-size:13px;margin-bottom:18px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{padding:8px 10px;border-bottom:1px solid #1f2937;text-align:left;vertical-align:middle}}
th{{color:#9ca3af;font-weight:600;position:sticky;top:0;background:#0f1115}}
.rk{{color:#6b7280;width:28px}} .sym{{font-weight:700;color:#fff}}
.tag{{color:#0f1115;font-weight:700;padding:2px 8px;border-radius:10px;font-size:11px}}
.sc{{font-weight:700;font-size:15px}} .rs{{color:#cbd5e1;max-width:340px}} .ev{{color:#94a3b8;font-size:11px}}
.legend{{margin:14px 0;font-size:12px;color:#cbd5e1}}
.legend b{{color:#fff}} .card{{background:#161a22;border:1px solid #1f2937;border-radius:10px;padding:16px;margin-bottom:18px}}
ul{{font-size:12px;color:#94a3b8}}
.kpi{{display:inline-block;background:#0f1115;border:1px solid #1f2937;border-radius:8px;padding:8px 14px;margin:0 8px 8px 0;font-size:13px}}
.kpi b{{font-size:18px;color:#fff}}
</style></head><body>
<h1>🐸 Memecoin 埋伏扫描器</h1>
<div class="sub">生成时间 {gen} · 数据源 {src_label} · 4h 六阶段 + 1h MA99 · 时区均为 UTC（Binance K线原生时区）</div>
<div class="card">
<div class="kpi">扫描 <b>{len(ranked)}</b> 个币</div>
<div class="kpi">观察候选 <b>{n_act}</b></div>
<div class="kpi">🔥量能抬头 <b style="color:#f59e0b">{n_amb}</b></div>
<div class="kpi">⚡刚启动 <b style="color:#22c55e">{n_fresh}</b></div>
</div>
<div class="card">
<div class="legend">
<b style="color:#f59e0b">埋伏★</b> 横盘蓄力 + 量能刚抬头 = 最该盯的信号（等第一根巨量启动） &nbsp;·&nbsp;
<b style="color:#22c55e">刚启动</b> 刚放量突破 ≤8 根 = 进观察列表做 S3→S4 &nbsp;·&nbsp;
<b style="color:#f59e0b">观察</b> 横盘蓄力中、量未动 = 加入观察、等放量 &nbsp;·&nbsp;
<b style="color:#0ea5e9">已启动</b> 早启动动能衰减 &nbsp;·&nbsp;
<b style="color:#ef4444">已拉完</b> 主升/见顶（太晚） &nbsp;·&nbsp;
<b style="color:#6b7280">崩盘中/低波动/稳定币/代币股</b> 已排除
</div>
<table>
<tr><th>#</th><th>币种</th><th>标签</th><th>阶段</th><th>埋伏分</th><th>现价</th><th>~1日</th><th>4h走势</th><th>埋伏依据</th><th>近期事件</th></tr>
{''.join(rows)}
</table>
</div>
<div class="card"><b>扫描跳过 / 失败 ({len(skipped)})</b><ul>{skip_html}</ul></div>
</body></html>"""
    return html

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--sym-list", default=None, help="comma sep override, e.g. DOGEUSDT,PEPEUSDT")
    ap.add_argument("--curated", action="store_true", help="scan curated meme list instead of all USDT pairs")
    ap.add_argument("--market", choices=["spot","futures"], default="spot",
                   help="data source: Binance spot or USD-M futures (real 妖币 live here)")
    a = ap.parse_args()
    market = a.market
    if a.sym_list:
        universe = a.sym_list.split(",")
    elif a.curated:
        universe = FUTURES_UNIVERSE if market == "futures" else UNIVERSE
    else:
        print(f"[scan] fetching Binance {market} USDT universe ...")
        universe = get_universe(market=market)
        print(f"[scan] universe={len(universe)} {market} pairs")
    print(f"[scan] scanning {len(universe)} {market} coins ...")
    ranked, all_out, skipped = run(universe, market=market)
    # save JSON
    payload = dict(generated=__import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat(),
        market=market, universe=universe,
        skipped=[{"sym":s,"err":e} for s,e in skipped],
        ranked=ranked)
    with open(os.path.join(HERE, f"memecoin_scan_{market}.json"), "w") as f:
        json.dump(payload, f, indent=2, default=str)
    html = build_html(ranked, skipped, a.top, market=market)
    with open(os.path.join(HERE, f"Memecoin_Ambush_Scanner_{market}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[done] {len(all_out)} scanned, {len(skipped)} skipped -> "
          f"Memecoin_Ambush_Scanner_{market}.html")

if __name__ == "__main__":
    main()
