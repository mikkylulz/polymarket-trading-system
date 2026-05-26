"""
crypto_scanner.py — BTC 5-minute Up/Down trading

Called from main.py every 4 minutes via run_crypto_scan().

Momentum logic
  Price is fetched from CoinGecko at most once per 60 s and stored in a
  module-level rolling list (up to 10 readings, process-lifetime only).
  Signal = UP   if last 3 readings are strictly increasing.
  Signal = DOWN if last 3 readings are strictly decreasing.
  Signal = NONE otherwise (mixed or not enough history yet).

Entry conditions
  - Clear UP or DOWN momentum signal
  - Active Polymarket BTC 5-minute market found (YES = higher, NO = lower)
  - EV >= MIN_EV using MOMENTUM_P as our estimated win probability
  - No BTC 5m trade currently open (max 1 at a time)

Hard limits (checked in order before any new entry)
  1. Max 3 trades opened within the last rolling 60 minutes.
  2. Max $10 total open exposure across all BTC 5m positions.
  3. If the last 5 settled trades are all losses, halt for 8 hours from the
     trigger time. halt_until is persisted in btc_5m_trades.json so it
     survives restarts. Expires automatically on its own schedule.

Trade lifecycle
  Written to data/btc_5m_trades.json with a UTC closes_at timestamp.
  Next scan after that timestamp resolves the trade by comparing BTC
  entry price vs current price, records PnL, and feeds learning.json.
"""
import json, logging, requests, time
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from config import (BASE, BALANCE, MAX_BET, MIN_BET, MIN_EV, PAPER_MODE,
                    BTC_LIVE_MODE, PRIVATE_KEY, POLYMARKET_PROXY_WALLET,
                    POLY_KEY, POLY_SECRET, POLY_PASSPHRASE)
from state import send_alert
from signals import calc_ev, calc_kelly, POLY_API
from learning import load_learning, save_learning

log = logging.getLogger("weatherbet")

# ── Module-level rolling price history ────────────────────────
# Each entry: (utc_iso: str, price: float)
_btc_prices:      List[tuple] = []
_last_fetch_mono: float       = 0.0
_prices_loaded:   bool        = False   # loaded from disk once per process

# ── Binance candle cache (Modules 1 & 2) ──────────────────────
_btc_candles:       list  = []
_last_candles_mono: float = 0.0
CANDLES_TTL = 60   # seconds; one refresh per minute is enough for 5m signals

# ── Open Interest state (Module 5) ────────────────────────────
_prev_oi: Optional[float] = None

FETCH_TTL     = 60    # min seconds between CoinGecko calls
MAX_HISTORY   = 10    # rolling window size
SIGNAL_WINDOW = 3     # consecutive readings required for a signal
TRADE_TTL     = 300   # 5 minutes

# Estimated win probability when momentum is clearly directional.
# Conservative prior: liquid-market momentum wins 51-54%; update from learning.json once 10+ trades settle.
MOMENTUM_P = 0.53

# ── BTC-specific bet sizing (overrides global MIN_BET/MAX_BET) ───────────────
# Live small-size mode: flat $2 per trade.
# BTC_BASE drives raw_size = BTC_BASE × confidence × (1−spread) × tf.
# With confidence ~0.53 and typical spread, raw_size < BTC_MIN_BET,
# so the floor always applies — effective size is always $2.
# BTC_MAX_BET hard-caps at $2 as an additional guard.
BTC_MIN_BET = 3.0
BTC_MAX_BET = 3.0
BTC_BASE    = 3.0   # maximum bet before confidence/spread/time multipliers

# ── Binance public klines (no API key required) ────────────────
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# ── Hard limits ───────────────────────────────────────────────
MAX_TRADES_PER_HOUR    = 3      # max entries in any rolling 60-minute window
MAX_EXPOSURE_USD       = 10.0   # max total $ open across all BTC 5m positions
MAX_CONSECUTIVE_LOSSES = 5      # halt today after this many losses in a row

BTC_TRADES_FILE = BASE / "data" / "btc_5m_trades.json"
COINGECKO_URL   = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"



# ── Persistence ───────────────────────────────────────────────
# File format: {"trades": [...], "halt_date": null | "YYYY-MM-DD"}
# Old bare-list format is migrated transparently on first read.

def _load_store() -> dict:
    if BTC_TRADES_FILE.exists():
        try:
            raw = json.loads(BTC_TRADES_FILE.read_text())
            if isinstance(raw, list):
                return {"trades": raw, "halt_date": None}
            return raw
        except Exception:
            pass
    return {"trades": [], "halt_date": None}


def _save_store(store: dict):
    BTC_TRADES_FILE.write_text(json.dumps(store, indent=2))


def _load_trades() -> List[dict]:
    return _load_store()["trades"]


def _save_trades(trades: List[dict]):
    store = _load_store()
    store["trades"] = trades
    _save_store(store)


def _is_halted() -> bool:
    """Return True if the loss-streak halt is still active (time-based, 8h TTL)."""
    halt_until = _load_store().get("halt_until")
    if not halt_until:
        return False
    try:
        return datetime.fromisoformat(halt_until) > datetime.now(timezone.utc)
    except Exception:
        return False


def _set_halt():
    """Engage the loss-streak halt for 8 hours from now."""
    store = _load_store()
    store["halt_until"] = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    _save_store(store)


def _reset_daily_session(today_str: str) -> bool:
    """
    Called once per scan. Updates last_session_date when the UTC date rolls over.
    Returns True when a reset actually happened (so the caller can log it).

    Note: the loss-streak halt is time-based (halt_until timestamp, 8h TTL)
    and is NOT cleared here — it expires on its own schedule regardless of
    UTC midnight, preventing the previous date-keyed bypass.
    """
    store = _load_store()
    if store.get("last_session_date") == today_str:
        return False   # already reset today
    store["last_session_date"] = today_str
    _save_store(store)
    return True


# ── Hard-limit guards ─────────────────────────────────────────
def _trades_this_hour(trades: List[dict]) -> int:
    """Count trades opened within the last 60 minutes (open or settled)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return sum(1 for t in trades if t.get("opened_at", "") >= cutoff)


def _current_exposure(trades: List[dict]) -> float:
    """Sum of size for all currently open BTC 5m positions."""
    return sum(t["size"] for t in trades if t.get("status") == "open")


def _consecutive_losses(trades: List[dict], since_date: Optional[str] = None) -> int:
    """Return the length of the current loss streak (most-recent settled first).

    since_date (YYYY-MM-DD): when provided, only trades opened on or after that
    date are considered.  This prevents a streak from a previous UTC day from
    bleeding into a new day and re-triggering the halt indefinitely.
    """
    candidates = [t for t in trades if t.get("won") is not None]
    if since_date:
        candidates = [t for t in candidates
                      if (t.get("opened_at") or "")[:10] >= since_date]
    settled = sorted(candidates, key=lambda t: t.get("opened_at", ""))
    streak = 0
    for t in reversed(settled):
        if t["won"]:
            break
        streak += 1
    return streak


# ── Price fetching ────────────────────────────────────────────
def _fetch_btc_price() -> Optional[float]:
    try:
        r = requests.get(COINGECKO_URL, timeout=10)
        r.raise_for_status()
        return float(r.json()["bitcoin"]["usd"])
    except Exception as e:
        log.warning(f"CoinGecko fetch failed: {e}")
        return None


def _update_btc_price() -> Optional[float]:
    """Fetch at most once per FETCH_TTL seconds. Returns latest known price."""
    global _btc_prices, _last_fetch_mono, _prices_loaded
    now_mono = time.monotonic()

    # Restore persisted price history once per process startup so history
    # accumulates correctly across bot restarts (not just within one run).
    if not _prices_loaded:
        _prices_loaded = True
        try:
            saved = _load_store().get("price_history", [])
            if saved:
                _btc_prices = [(ts, float(p)) for ts, p in saved]
                log.info(f"[BTC] Restored {len(_btc_prices)} price readings from disk")
        except Exception:
            pass

    if now_mono - _last_fetch_mono < FETCH_TTL:
        return _btc_prices[-1][1] if _btc_prices else None

    price = _fetch_btc_price()
    if price is None:
        return _btc_prices[-1][1] if _btc_prices else None

    _btc_prices.append((datetime.now(timezone.utc).isoformat(), price))
    _btc_prices = _btc_prices[-MAX_HISTORY:]
    _last_fetch_mono = now_mono

    # Persist so history survives the next restart
    try:
        store = _load_store()
        store["price_history"] = list(_btc_prices)
        _save_store(store)
    except Exception as e:
        log.warning(f"[BTC] Failed to persist price history: {e}")

    log.info(f"BTC ${price:,.2f} | history={len(_btc_prices)} readings")
    return price


# ── Candle helpers ────────────────────────────────────────────
def _fetch_btc_candles(limit: int = 30) -> list:
    """Fetch recent 5m OHLCV candles from Binance (no auth required).
    Cached for CANDLES_TTL seconds. Returns list of dicts.
    """
    global _btc_candles, _last_candles_mono
    now_mono = time.monotonic()
    if now_mono - _last_candles_mono < CANDLES_TTL and _btc_candles:
        return _btc_candles
    try:
        r = requests.get(
            BINANCE_KLINES,
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        _btc_candles = [
            {"open": float(c[1]), "high": float(c[2]),
             "low":  float(c[3]), "close": float(c[4]), "volume": float(c[5])}
            for c in r.json()
        ]
        _last_candles_mono = now_mono
        log.info(f"[BTC CANDLES] fetched {len(_btc_candles)} 5m candles from Binance")
    except Exception as e:
        log.warning(f"Binance candles fetch failed: {e}")
        # Synthesize pseudo-candles from CoinGecko price history as fallback.
        # These have flat OHLC (open=high=low=close=price) so VWAP works but
        # EMA20 won't — _module_trend() degrades to VWAP-only automatically.
        if _btc_prices:
            _btc_candles = [
                {"open": p, "high": p, "low": p, "close": p, "volume": 1.0}
                for _, p in _btc_prices
            ]
            log.info(f"[BTC CANDLES] using {len(_btc_candles)} synthetic candles "
                     f"from CoinGecko price history (Binance unavailable)")
    return _btc_candles


def _ema(values: list, period: int) -> Optional[float]:
    """Exponential moving average of a list of floats."""
    if len(values) < period:
        return None
    k   = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _vwap(candles: list) -> Optional[float]:
    """VWAP (typical-price × volume weighted) over the supplied candles."""
    pv = v = 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        pv += typical * c["volume"]
        v  += c["volume"]
    return pv / v if v > 0 else None


# ── Three-module conflict detector ────────────────────────────
def _module_trend(candles: list) -> float:
    """
    Module 1 — Trend.
    Returns p_trend ∈ [0, 1]: probability of price going UP.
    0.5 = neutral; >0.5 = bullish; <0.5 = bearish.

    Components:
      EMA cross   (EMA5 vs EMA20 on 5m closes) — 50% weight
      VWAP offset (current price vs session VWAP) — 50% weight

    Margins are normalised so ±0.2% EMA gap or ±1% VWAP offset → full ±0.5 swing.
    """
    if len(candles) < 5:
        log.info("[BTC TREND] insufficient candles — neutral 0.500")
        return 0.5

    closes = [c["close"] for c in candles]
    price  = closes[-1]
    ema5   = _ema(closes, 5)
    ema20  = _ema(closes, 20)   # None when < 20 candles (e.g. synthetic fallback)
    vwap   = _vwap(candles)

    if vwap is None:
        return 0.5

    vwap_dev    = (price - vwap) / price          # fraction; ±0.01 → ±0.5
    vwap_signal = 0.5 + max(-0.5, min(0.5, vwap_dev * 50))

    if ema5 is None or ema20 is None:
        # Degraded mode — Binance unavailable or fewer than 20 candles.
        # Use VWAP-only rather than returning a hard neutral.
        p_trend = round(vwap_signal, 3)
        log.info(f"[BTC TREND] VWAP-only (EMA20 unavailable, {len(candles)} candles) "
                 f"VWAP={vwap:.0f} price={price:.0f} "
                 f"vwap_sig={vwap_signal:.3f} p_trend={p_trend:.3f}")
        return p_trend

    ema_margin   = (ema5 - ema20) / price          # fraction; ±0.002 → ±0.5
    ema_signal   = 0.5 + max(-0.5, min(0.5, ema_margin * 250))

    p_trend = round(0.5 * ema_signal + 0.5 * vwap_signal, 3)
    log.info(f"[BTC TREND] EMA5={ema5:.0f} EMA20={ema20:.0f} VWAP={vwap:.0f} "
             f"price={price:.0f} → ema_sig={ema_signal:.3f} "
             f"vwap_sig={vwap_signal:.3f} p_trend={p_trend:.3f}")
    return p_trend


def _module_levels(price: float, candles: list) -> float:
    """
    Module 2 — Levels.
    Returns p_level ∈ [0, 1]: probability of price going UP.

    Components:
      1h range position  (price vs recent 12-candle = 60min high/low) — 60% weight
      Round-number bias  ($1 000 boundaries for BTC)                  — 40% weight

    Interpretation: being at the top of the 1h range is bullish (momentum continuation);
    having just broken above a round number is also bullish.
    """
    if not candles:
        return 0.5

    window   = candles[-12:] if len(candles) >= 12 else candles
    high_1h  = max(c["high"] for c in window)
    low_1h   = min(c["low"]  for c in window)
    rng      = high_1h - low_1h

    pos_in_range = ((price - low_1h) / rng) if rng > 0 else 0.5

    nearest_round = round(price / 1000) * 1000
    dist_pct      = abs(price - nearest_round) / price
    above_round   = price >= nearest_round

    if dist_pct < 0.003:          # within 0.3% — sitting on the level, neutral
        p_round = 0.5
    elif above_round:
        p_round = 0.62            # above round = broke through → slight bullish
    else:
        p_round = 0.38            # below round = failed to break → slight bearish

    p_level = round(0.6 * pos_in_range + 0.4 * p_round, 3)
    log.info(f"[BTC LEVELS] 1h_hi={high_1h:.0f} 1h_lo={low_1h:.0f} "
             f"pos={pos_in_range:.2f} round={nearest_round:.0f} "
             f"above={above_round} dist={dist_pct:.4f} p_level={p_level:.3f}")
    return p_level


def _module_momentum(signal: str) -> float:
    """
    Module 3 — Momentum.
    Wraps the existing MOMENTUM_P into a directional probability of going UP.
      UP signal   → p = MOMENTUM_P   (e.g. 0.53)
      DOWN signal → p = 1−MOMENTUM_P (e.g. 0.47)
      NONE        → p = 0.50
    """
    if signal == "UP":
        p = MOMENTUM_P
    elif signal == "DOWN":
        p = round(1.0 - MOMENTUM_P, 4)
    else:
        p = 0.5
    log.info(f"[BTC MOMENTUM] signal={signal} p_momentum={p:.3f}")
    return p


def _module_funding(direction: str) -> tuple:
    """
    Module 4 — Funding Rate via Gate.io public API (no key required).
    Endpoint: GET /api/v4/futures/usdt/funding_rate?contract=BTC_USDT&limit=1
    High positive funding (> +0.05%) = bearish pressure; high negative (< -0.05%) = bullish.
    Returns (p_funding, funding_adj) where funding_adj is applied to p_trend and p_momentum.
    """
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/futures/usdt/funding_rate",
            params={"contract": "BTC_USDT", "limit": 1},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        rate = float(data[0]["r"]) if data else 0.0
    except Exception as e:
        log.warning(f"[BTC FUNDING gateio] fetch failed: {e} — neutral 0.500")
        return 0.5, 0.0

    rate_pct = rate * 100

    if rate > 0.0005 and direction == "UP":
        p_funding, funding_adj, bias_tag = 0.45, -0.05, " -> bearish bias applied"
    elif rate < -0.0005 and direction == "DOWN":
        p_funding, funding_adj, bias_tag = 0.55, -0.05, " -> bullish bias applied"
    else:
        p_funding, funding_adj, bias_tag = 0.50, 0.0, ""

    log.info(f"[BTC FUNDING gateio] rate={rate_pct:+.4f}%{bias_tag}")
    return p_funding, funding_adj


def _module_oi(direction: str) -> tuple:
    """
    Module 5 — Open Interest direction via Gate.io public ticker (no key required).
    Endpoint: GET /api/v4/futures/usdt/tickers?contract=BTC_USDT → total_size field.
    Compares current OI to previous scan. Matching direction adds conviction;
    divergence (squeeze) reduces it.
    Returns (p_oi, conf_adj).
    """
    global _prev_oi

    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/futures/usdt/tickers",
            params={"contract": "BTC_USDT"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        oi_current = float(data[0]["total_size"]) if data else None
        if oi_current is None:
            raise ValueError("empty ticker list")
    except Exception as e:
        log.warning(f"[BTC OI gateio] fetch failed: {e} — neutral 0.500")
        return 0.5, 0.0

    if _prev_oi is None:
        _prev_oi = oi_current
        log.info(f"[BTC OI gateio] first reading {oi_current:,.0f} — neutral 0.500")
        return 0.5, 0.0

    oi_delta = oi_current - _prev_oi
    _prev_oi = oi_current

    if oi_delta == 0:
        log.info(f"[BTC OI gateio] unchanged {oi_current:,.0f} — neutral 0.500")
        return 0.5, 0.0

    oi_dir = "UP" if oi_delta > 0 else "DOWN"
    match  = (oi_dir == direction)

    if match:
        if direction == "UP":
            p_oi, conf_adj, tag = 0.55, +0.05, "OI confirms UP — +0.05 conviction"
        else:
            p_oi, conf_adj, tag = 0.45, +0.05, "OI confirms DOWN — +0.05 conviction"
    else:
        if direction == "UP":
            p_oi, conf_adj, tag = 0.45, -0.05, "squeeze detected — reducing confidence"
        else:
            p_oi, conf_adj, tag = 0.55, -0.05, "OI rising vs DOWN — reducing confidence"

    log.info(f"[BTC OI gateio] OI={oi_current:,.0f} delta={oi_delta:+,.0f} ({oi_dir}) | {tag}")
    return p_oi, conf_adj


def _time_factor(closes_at_iso: Optional[str]) -> float:
    """
    Scale bet size by how much time remains in the 5m market window.
      ≥ 4 min remaining → 0.7  (early; market price less settled)
      < 1 min remaining → 1.3  (late; outcome almost determined)
      1–4 min           → linear interpolation
    """
    if not closes_at_iso:
        return 1.0
    try:
        closes_at = datetime.fromisoformat(closes_at_iso.replace("Z", "+00:00"))
        mins_left = (closes_at - datetime.now(timezone.utc)).total_seconds() / 60
        if mins_left >= 4.0:
            return 0.7
        if mins_left < 1.0:
            return 1.3
        # Linear: 0.7 at 4 min → 1.3 at 1 min, slope = 0.6 / 3 = 0.2 per min
        return round(0.7 + (4.0 - mins_left) * 0.2, 3)
    except Exception:
        return 1.0


# ── Momentum signal ───────────────────────────────────────────
def _momentum_signal() -> str:
    """Return 'UP', 'DOWN', or 'NONE'."""
    if len(_btc_prices) < SIGNAL_WINDOW:
        log.info(f"BTC momentum: NONE (need {SIGNAL_WINDOW}, have {len(_btc_prices)})")
        return "NONE"

    recent = [x[1] for x in _btc_prices[-SIGNAL_WINDOW:]]

    if all(recent[i + 1] > recent[i] for i in range(len(recent) - 1)):
        pct = (recent[-1] - recent[0]) / recent[0] * 100
        log.info(f"BTC momentum: UP   {recent[0]:,.0f}→{recent[-1]:,.0f} ({pct:+.3f}%)")
        return "UP"

    if all(recent[i + 1] < recent[i] for i in range(len(recent) - 1)):
        pct = (recent[-1] - recent[0]) / recent[0] * 100
        log.info(f"BTC momentum: DOWN {recent[0]:,.0f}→{recent[-1]:,.0f} ({pct:+.3f}%)")
        return "DOWN"

    log.info("BTC momentum: NONE (mixed)")
    return "NONE"


# ── Polymarket market lookup ──────────────────────────────────
def _parse_prices(price_str) -> List[float]:
    try:
        if isinstance(price_str, list):
            return [float(p) for p in price_str]
        return [float(p) for p in json.loads(price_str)]
    except Exception:
        return []


def _fetch_events(params: dict) -> List[dict]:
    """Single GET to /events; normalises list vs paginated-dict response."""
    r = requests.get(f"{POLY_API}/events", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("data", [])


def _is_btc(title: str, slug: str) -> bool:
    return "bitcoin" in title or "btc" in title


def _is_5m(title: str, slug: str = "") -> bool:
    # Slug is the reliable signal: Polymarket uses "btc-updown-5m-<timestamp>"
    if "5m" in slug or "updown" in slug:
        return True
    # Title fallback for alternate market formats
    if "up or down" in title:
        return True
    has_5    = "5" in title
    has_unit = any(kw in title for kw in ["minute", "min", "5m"])
    return has_5 and has_unit


def _find_btc_5m_market() -> Optional[dict]:
    """
    Search Polymarket gamma API for an active BTC 5-minute market.

    Strategy (most-specific → broadest):
      1. /events?tag_slug=crypto   — scoped to crypto category
      2. /events (no category)     — full scan, deduplicated

    All bitcoin/btc events found are logged so the caller can see exactly
    what Polymarket is returning before the 5m filter is applied.
    The slug changes every 5 minutes, so filtering is title-only.

    Returns {"title", "yes_price", "no_price", "market_id", "token_id"} or None.
    """
    today_iso    = datetime.now(timezone.utc).date().isoformat()
    tomorrow_iso = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

    searches = [
        {"tag_slug": "crypto", "active": "true", "closed": "false",
         "end_date_min": today_iso, "end_date_max": tomorrow_iso, "limit": 100},
        {"active": "true", "closed": "false",
         "end_date_min": today_iso, "end_date_max": tomorrow_iso, "limit": 200},
    ]

    seen_ids: set = set()
    btc_candidates: List[dict] = []

    for params in searches:
        try:
            events = _fetch_events(params)
        except Exception as e:
            log.warning(f"BTC market fetch failed (params={params}): {e}")
            continue

        log.info(f"[BTC DEBUG] API returned {len(events)} events (params={params})")
        for event in events:
            eid   = event.get("id")
            title = event.get("title") or ""
            slug  = event.get("slug")  or ""
            log.info(f"  [BTC DEBUG] id={eid}  title={title!r}  slug={slug!r}")

            if eid in seen_ids:
                continue
            seen_ids.add(eid)

            if _is_btc(title.lower(), slug.lower()):
                btc_candidates.append(event)

    # Summary: every event that passed _is_btc, before the 5m filter
    if btc_candidates:
        log.info(f"[BTC DEBUG] {len(btc_candidates)} event(s) passed _is_btc filter:")
        for ev in btc_candidates:
            t = (ev.get("title") or "").lower()
            s = (ev.get("slug")  or "").lower()
            passes_5m = _is_5m(t, s)
            log.info(f"  [{ev.get('id','?')}] {ev.get('title')!r}  slug={ev.get('slug')!r}  5m={passes_5m}")
    else:
        log.info("[BTC DEBUG] No events passed _is_btc filter — check titles/slugs above")

    # Apply 5m filter and return the first valid match
    for event in btc_candidates:
        title = (event.get("title") or "").lower()
        slug  = (event.get("slug")  or "").lower()

        if not _is_5m(title, slug):
            continue

        eid  = event.get("id", "?")
        name = event.get("title", "?")

        mkts = event.get("markets", [])
        if not mkts:
            log.info(f"  [5m candidate {eid}] SKIP — no markets array: {name}")
            continue

        m      = mkts[0]
        prices = _parse_prices(m.get("outcomePrices", "[]"))

        # The bulk /events list returns shallow market objects without
        # outcomePrices. Fetch the full event when prices are missing.
        # outcomePrices is only populated on funded/liquid markets.
        # Fall back to bestAsk (YES ask price) when it is absent.
        if len(prices) < 2:
            try:
                detail = requests.get(f"{POLY_API}/events/{eid}",
                                      timeout=10).json()
                detail_mkts = detail.get("markets", [])
                if detail_mkts:
                    m      = detail_mkts[0]
                    prices = _parse_prices(m.get("outcomePrices", "[]"))
                    if len(prices) < 2:
                        best_ask = m.get("bestAsk")
                        if best_ask is not None:
                            ya = float(best_ask)
                            prices = [ya, round(1.0 - ya, 4)]
            except Exception as e:
                log.warning(f"  [5m candidate {eid}] detail fetch failed: {e}")

        if len(prices) < 2:
            log.info(f"  [5m candidate {eid}] SKIP — no price data: {name}")
            continue

        yes_price = float(prices[0])
        no_price  = float(prices[1])

        if not (0.05 <= yes_price <= 0.95):
            log.info(f"  [5m candidate {eid}] SKIP — degenerate prices "
                     f"YES={yes_price} NO={no_price}: {name}")
            continue

        # Skip markets with no liquidity — unfunded/template markets
        if m.get("liquidityNum", 0) == 0 and float(m.get("volume", 0) or 0) == 0:
            log.info(f"  [5m candidate {eid}] SKIP — no liquidity (unfunded): {name}")
            continue

        raw_ids     = (m.get("clobTokenIds") or "").split(",")
        token_id    = raw_ids[0].strip('"[] ') if len(raw_ids) > 0 else None
        no_token_id = raw_ids[1].strip('"[] ') if len(raw_ids) > 1 else None

        log.info(
            f"[BTC 5m MARKET IDENTIFIED]\n"
            f"  Title : {event.get('title')}\n"
            f"  Slug  : {event.get('slug')}\n"
            f"  YES={yes_price:.3f}  NO={no_price:.3f}"
        )

        # Determine the TRADING window close time.
        # For hourly "Up or Down" markets the title says e.g. "8AM ET" meaning the
        # trading window runs 8AM–9AM ET (12:00–13:00 UTC with EDT=UTC-4).
        # The API's endDate/endDateIso is the oracle RESOLUTION time (hours later)
        # and must NOT be used to decide whether the book is still open.
        # We always derive the trading close from the title when it contains "AM/PM ET",
        # and fall back to the API date only for non-hourly markets.
        import re as _re
        MIN_MINUTES_REMAINING = 30
        now_utc = datetime.now(timezone.utc)

        title_close: Optional[datetime] = None
        _tm = _re.search(r'(\d{1,2})(AM|PM)\s+ET', name, _re.IGNORECASE)
        if _tm:
            try:
                hr   = int(_tm.group(1))
                ampm = _tm.group(2).upper()
                if ampm == "PM" and hr != 12:
                    hr += 12
                elif ampm == "AM" and hr == 12:
                    hr = 0
                # Trading window end = start + 1 hour, EDT = UTC-4
                close_utc_hr = (hr + 4 + 1) % 24   # +4 for EDT, +1 for end-of-hour
                today        = now_utc.date()
                title_close  = datetime(today.year, today.month, today.day,
                                        close_utc_hr, 0, 0, tzinfo=timezone.utc)
            except Exception:
                pass

        if title_close is not None:
            # Title-based close is authoritative for hourly markets
            mins_left = (title_close - now_utc).total_seconds() / 60
            if mins_left <= 0:
                log.info(f"  [5m candidate {eid}] SKIP — trading window closed "
                         f"{-mins_left:.0f} min ago ({title_close.strftime('%H:%M')} UTC): {name}")
                continue
            if mins_left < MIN_MINUTES_REMAINING:
                log.info(f"  [5m candidate {eid}] SKIP — only {mins_left:.0f} min "
                         f"remaining (< {MIN_MINUTES_REMAINING} min floor): {name}")
                continue
            closes_at = title_close.isoformat()   # use for _time_factor()
        else:
            # Non-hourly market: use the API's closes_at
            closes_at = (m.get("endDateIso") or m.get("endDate")
                         or event.get("endDateIso") or event.get("endDate"))
            if closes_at:
                try:
                    closes_dt = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
                    mins_left = (closes_dt - now_utc).total_seconds() / 60
                    if mins_left <= 0:
                        log.info(f"  [5m candidate {eid}] SKIP — already expired: {name}")
                        continue
                    if mins_left < MIN_MINUTES_REMAINING:
                        log.info(f"  [5m candidate {eid}] SKIP — only {mins_left:.0f} min "
                                 f"remaining: {name}")
                        continue
                except Exception:
                    pass
            else:
                log.info(f"  [5m candidate {eid}] SKIP — no close time determinable: {name}")
                continue

        return {
            "title":       event.get("title", ""),
            "yes_price":   yes_price,
            "no_price":    no_price,
            "market_id":   m.get("id"),
            "token_id":    token_id,    # YES token — used for UP trades
            "no_token_id": no_token_id, # NO token  — used for DOWN trades (live only)
            "closes_at":   closes_at,
        }

    log.info("No active BTC 5m market found on Polymarket")
    return None


# ── Live order client (lazy-initialized, only when BTC_LIVE_MODE=True) ───────
_clob_client = None   # cached after first successful init


def _get_clob_client():
    """Lazily initialize and cache the Polymarket CLOB client.
    Only called when BTC_LIVE_MODE=True.  Returns None on any failure.

    Uses py_clob_client_v2 (Polymarket's current client — auto-detects v1/v2
    order format from the server).  signature_type=1 (POLY_PROXY): EOA signs,
    proxy wallet is the funder that holds the USDC.
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    try:
        from py_clob_client_v2.client import ClobClient   # pip install py-clob-client-v2
        from py_clob_client_v2.constants import POLYGON
    except ImportError:
        log.error("[BTC LIVE] py-clob-client-v2 not installed — "
                  "run: pip install py-clob-client-v2")
        return None
    if not PRIVATE_KEY:
        log.error("[BTC LIVE] PRIVATE_KEY not set in .env — cannot sign orders")
        return None
    # Strip whitespace, quotes, and newlines that .env parsers sometimes leave
    clean_key = PRIVATE_KEY.strip().strip('"').strip("'").strip()
    key_len   = len(clean_key)
    key_preview = f"{clean_key[:6]}...{clean_key[-4:]}" if key_len >= 10 else "(too short)"

    log.info(f"[BTC LIVE] Key format: {key_preview} ({key_len} chars)")
    if key_len != 66 or not clean_key.startswith("0x"):
        log.error(
            f"[BTC LIVE] PRIVATE_KEY looks malformed — expected 66-char hex starting "
            f"with 0x, got {key_len} chars starting with {clean_key[:4]!r}"
        )
        return None

    if not POLYMARKET_PROXY_WALLET:
        log.error("[BTC LIVE] POLYMARKET_PROXY_WALLET not set in .env — "
                  "set it to the proxy wallet address shown on your Polymarket profile")
        return None
    proxy_wallet  = POLYMARKET_PROXY_WALLET.strip().strip('"').strip("'").strip()
    proxy_preview = f"{proxy_wallet[:6]}...{proxy_wallet[-4:]}" if len(proxy_wallet) >= 10 else "(too short)"
    if not proxy_wallet.startswith("0x") or len(proxy_wallet) != 42:
        log.error(f"[BTC LIVE] POLYMARKET_PROXY_WALLET malformed: {proxy_preview}")
        return None
    log.info(f"[BTC LIVE] Proxy wallet (funder): {proxy_preview}")

    try:
        log.info(
            f"[BTC LIVE] Initializing ClobClient — "
            f"chain_id={POLYGON} (137=Polygon mainnet) "
            f"key={key_preview} funder={proxy_preview} signature_type=3 (POLY_1271)"
        )
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=clean_key,
            signature_type=3,        # POLY_1271: ERC-1271 deposit wallet (new Polymarket accounts)
            funder=proxy_wallet,
        )
        # Suppress the library's own ERROR logs for expected 400 responses
        # (create-api-key fallback and FOK kills). Real failures still surface
        # via our own exception handlers.
        logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)

        client.set_api_creds(client.create_or_derive_api_key())
        _clob_client = client
        log.info("[BTC LIVE] CLOB client initialized successfully")
        return _clob_client
    except Exception as e:
        log.error(f"[BTC LIVE] CLOB client init failed: {e}")
        return None


def test_clob_credentials():
    """
    Diagnostic: verify whether the POLYMARKET_API_KEY / SECRET / PASSPHRASE in .env
    match the wallet derived from PRIVATE_KEY.

    Step 1 — load the existing .env credentials into a fresh ClobClient and call
             get_api_keys().  A 200 result means the credentials are valid.
             A 400/401 means they don't match the wallet and need to be regenerated.

    Step 2 — regardless of Step 1, call create_or_derive_api_creds() and print what
             the wallet actually derives.  Copy these values into .env if Step 1 failed.

    Call this once from the terminal:
        python -c "import logging; logging.basicConfig(level=logging.INFO); from crypto_scanner import test_clob_credentials; test_clob_credentials()"
    """
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
        from py_clob_client_v2.constants import POLYGON
    except ImportError:
        print("[CRED TEST] py-clob-client-v2 not installed — pip install py-clob-client-v2")
        return

    if not PRIVATE_KEY:
        print("[CRED TEST] PRIVATE_KEY not set in .env")
        return

    clean_key = PRIVATE_KEY.strip().strip('"').strip("'").strip()
    key_preview = f"{clean_key[:6]}...{clean_key[-4:]}"
    print(f"[CRED TEST] Wallet key: {key_preview} ({len(clean_key)} chars)")

    # ── Step 1: test existing .env credentials ────────────────────
    print("\n[CRED TEST] Step 1 — testing existing .env credentials against wallet …")
    if not POLY_KEY:
        print("[CRED TEST]   POLYMARKET_API_KEY is empty — skip Step 1")
    else:
        try:
            client_env = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=POLYGON,
                key=clean_key,
                signature_type=2,
                funder=clean_key,
            )
            client_env.set_api_creds(ApiCreds(
                api_key=POLY_KEY,
                api_secret=POLY_SECRET,
                api_passphrase=POLY_PASSPHRASE,
            ))
            result = client_env.get_api_keys()
            print(f"[CRED TEST]   get_api_keys() OK — credentials are VALID for this wallet")
            print(f"[CRED TEST]   Response: {result}")
        except Exception as e:
            print(f"[CRED TEST]   get_api_keys() FAILED: {e}")
            print(f"[CRED TEST]   -> .env credentials do NOT match this wallet — regenerate (see Step 2)")

    # ── Step 2: derive fresh credentials from the wallet ─────────
    print("\n[CRED TEST] Step 2 — deriving fresh credentials from wallet …")
    try:
        client_fresh = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=clean_key,
            signature_type=2,
            funder=clean_key,
        )
        creds = client_fresh.create_or_derive_api_key()
        print(f"[CRED TEST]   Derived credentials — copy these into .env:")
        print(f"  POLYMARKET_API_KEY={creds.api_key}")
        print(f"  POLYMARKET_SECRET={creds.api_secret}")
        print(f"  POLYMARKET_PASSPHRASE={creds.api_passphrase}")
    except Exception as e:
        print(f"[CRED TEST]   create_or_derive_api_key() FAILED: {e}")
        print(f"[CRED TEST]   -> wallet may not be registered with Polymarket CLOB yet")


def _btc_live_order(direction: str, market: dict, price: float, size: float) -> str:
    """Submit a live BTC 5m FOK order to Polymarket via py-clob-client.

    direction : "UP"  → buy YES token  (price goes up)
                "DOWN" → buy NO token   (price goes down)
    price     : Polymarket price in [0, 1] for the chosen side
    size      : dollar amount to spend; converted to shares internally

    Returns the order_id string on success, empty string on any failure.
    Trade is only recorded to btc_5m_trades.json after a successful submission.
    FOK (Fill or Kill): fills immediately or cancels — no resting GTC orders
    that could overlap with the next trade cycle.
    """
    token_id = (market.get("token_id")
                if direction == "UP"
                else market.get("no_token_id"))
    if not token_id:
        log.warning(f"[BTC LIVE] No token_id for direction={direction} — skipping")
        return ""

    client = _get_clob_client()
    if client is None:
        return ""

    try:
        from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType

        # Use a market order (amount = USDC to spend).
        # The library rounds amount with round_down(amount, 2) before building the
        # signed order, so maker_amount is always an exact 2-decimal USDC value.
        # Limit orders (OrderArgsV2) compute maker = shares × price internally,
        # which produces floating-point drift (e.g. 5.8252 × 0.515 = 2.9999779…)
        # and the API rejects amounts with > 2 decimal places.
        price_r   = round(price, 4)
        usdc_size = round(size, 2)   # guaranteed 2-decimal maker amount

        # Ensure the USDC amount buys at least 5 shares (Polymarket minimum).
        # This only triggers at prices above ~0.60 which our EV gate normally
        # filters out anyway, but guard it explicitly just in case.
        if usdc_size / price_r < 5.0:
            usdc_size = round(5.0 * price_r + 0.005, 2)  # round up to next cent
            log.info(f"[BTC LIVE] Bumped to ${usdc_size:.2f} to cover 5-share minimum")

        log.info(
            f"[BTC LIVE] Step 1 — token_id={token_id} "
            f"price={price_r} usdc=${usdc_size:.2f} (market FOK)"
        )
        order_args = MarketOrderArgsV2(
            token_id=token_id,
            amount=usdc_size,
            side="BUY",
            price=price_r,
            order_type=OrderType.FOK,
        )
        log.info("[BTC LIVE] Step 2 — calling create_market_order() …")
        signed_order = client.create_market_order(order_args)
        log.info("[BTC LIVE] Step 3 — calling post_order() FOK …")
        resp = client.post_order(signed_order, OrderType.FOK)
        log.info(f"[BTC LIVE] Step 4 — full response: {resp}")
    except Exception as e:
        err_str = str(e)
        if "fully filled" in err_str or "FOK" in err_str:
            log.info(f"[BTC LIVE] FOK kill — no liquidity at {round(price, 4)}, "
                     f"trade skipped (no position opened)")
            return ""
        log.error(f"[BTC LIVE] Order submission failed: {e}")
        send_alert(f"[BTC LIVE] LIVE ORDER FAILED: {e}", priority="high")
        return ""

    # post_order() does not raise on exchange-level rejection — inspect the response.
    if not isinstance(resp, dict):
        log.error(f"[BTC LIVE] LIVE ORDER FAILED — unexpected response type "
                  f"{type(resp).__name__}: {resp}")
        send_alert(f"[BTC LIVE] LIVE ORDER FAILED — unexpected response: {resp}",
                   priority="high")
        return ""

    order_id  = resp.get("orderID") or resp.get("id") or ""
    success   = resp.get("success")
    error_msg = resp.get("errorMsg") or resp.get("error") or ""
    status    = resp.get("status", "")

    if success is False:
        reason = error_msg or f"status={status}" or "unknown"
        log.error(f"[BTC LIVE] LIVE ORDER REJECTED — {reason} | resp={resp}")
        send_alert(f"[BTC LIVE] LIVE ORDER REJECTED\n{reason}", priority="high")
        return ""

    if error_msg:
        log.error(f"[BTC LIVE] LIVE ORDER ERROR — {error_msg} | resp={resp}")
        send_alert(f"[BTC LIVE] LIVE ORDER ERROR\n{error_msg}", priority="high")
        return ""

    # FOK with no order_id and no explicit success=True — unmatched / no liquidity
    if not order_id and success is not True:
        log.info(f"[BTC LIVE] FOK unmatched — no liquidity at {price:.4f}, skipping")
        return ""

    log.info(f"[BTC LIVE] Order CONFIRMED — direction={direction} order_id={order_id} "
             f"token={token_id[:8]}... price={price:.4f} usdc=${usdc_size:.2f}")
    return order_id


# ── Trade lifecycle ───────────────────────────────────────────
def _close_expired_trades(current_btc_price: float):
    """Resolve any open trades whose 5-minute window has elapsed."""
    trades  = _load_trades()
    now_utc = datetime.now(timezone.utc)
    changed = False

    for t in trades:
        if t.get("status") != "open":
            continue
        try:
            closes_str = t.get("closes_at")
            if not closes_str:
                continue

            closes_at = datetime.fromisoformat(closes_str)
            if now_utc < closes_at:
                remaining = (closes_at - now_utc).total_seconds()
                log.info(f"BTC trade still open — {remaining:.0f}s remaining")
                continue

            direction = t["direction"]
            btc_entry = t["btc_entry"]

            won = ((direction == "UP"   and current_btc_price > btc_entry) or
                   (direction == "DOWN" and current_btc_price < btc_entry))

            entry_price = t["entry_price"]
            size        = t["size"]
            pnl = round(size * (1.0 / entry_price - 1.0), 2) if won else -size

            t["btc_exit"]  = current_btc_price
            t["closed_at"] = now_utc.isoformat()
            t["won"]       = won
            t["pnl"]       = pnl
            t["status"]    = "won" if won else "lost"

            pct    = (current_btc_price - btc_entry) / btc_entry * 100
            payout = round(size / entry_price, 2) if won else 0.0
            msg = (f"BTC 5m {'WIN' if won else 'LOSS'} | {direction} | "
                   f"${btc_entry:,.0f}→${current_btc_price:,.0f} ({pct:+.3f}%) | "
                   f"Payout:${payout:.2f} | PnL:${pnl:+.2f}")
            log.info(msg)
            send_alert(msg, priority="normal" if won else "high")

            _record_to_learning(t)
            changed = True
        except Exception as e:
            log.warning(f"[BTC] Failed to close trade {t.get('id','?')}: {e}")

    if changed:
        _save_trades(trades)


def _record_to_learning(trade: dict):
    """Append resolved BTC 5m result to learning.json['btc_5m']."""
    try:
        data = load_learning()
        btc  = data.setdefault("btc_5m", {
            "trades":          [],
            "win_rates":       {"7d": None, "30d": None, "all": None},
            "kelly_multiplier": 1.0,
        })

        btc["trades"].append({
            "date":        (trade.get("opened_at") or "")[:10],
            "direction":   trade["direction"],
            "entry_price": trade["entry_price"],
            "btc_entry":   trade["btc_entry"],
            "btc_exit":    trade.get("btc_exit"),
            "won":         trade.get("won"),
            "pnl":         trade.get("pnl"),
            "size":        trade["size"],
        })

        now  = datetime.now(timezone.utc)
        c7d  = (now - timedelta(days=7)).date().isoformat()
        c30d = (now - timedelta(days=30)).date().isoformat()
        done = [t for t in btc["trades"] if t.get("won") is not None]
        t7d  = [t for t in done if t.get("date", "") >= c7d]
        t30d = [t for t in done if t.get("date", "") >= c30d]

        def _wr(lst):
            return round(sum(1 for t in lst if t["won"]) / len(lst), 4) if lst else None

        btc["win_rates"] = {
            "7d":       _wr(t7d),  "count_7d":  len(t7d),
            "30d":      _wr(t30d), "count_30d": len(t30d),
            "all":      _wr(done), "count_all": len(done),
        }

        # Kelly hysteresis — require 5 trades before adjusting
        wr7 = btc["win_rates"]["7d"]
        n7  = btc["win_rates"]["count_7d"]
        if wr7 is not None and n7 >= 5:
            if wr7 < 0.30:
                btc["kelly_multiplier"] = 0.5
            elif wr7 >= 0.40:
                btc["kelly_multiplier"] = 1.0

        save_learning(data)
        log.info(f"[LEARNING-BTC] won={trade.get('won')} "
                 f"pnl=${trade.get('pnl'):+.2f} "
                 f"7d_wr={btc['win_rates']['7d']}")
    except Exception as e:
        log.warning(f"BTC learning record failed: {e}")


def _enter_trade(direction: str, market: dict, btc_price: float, candles: list):
    """Open a paper (or live) BTC 5m trade using the five-module conflict detector."""

    # ── Five-module conflict detector ─────────────────────────────
    p_trend    = _module_trend(candles)
    p_level    = _module_levels(btc_price, candles)
    p_momentum = _module_momentum(direction)

    # Modules 4 & 5: funding rate and open interest
    p_funding, funding_adj = _module_funding(direction)
    p_oi, oi_conf_adj      = _module_oi(direction)

    # Apply funding adjustment to trend and momentum before spread calculation
    p_trend    = round(max(0.0, min(1.0, p_trend    + funding_adj)), 3)
    p_momentum = round(max(0.0, min(1.0, p_momentum + funding_adj)), 3)

    all_p  = (p_trend, p_level, p_momentum, p_funding, p_oi)
    spread = round(max(all_p) - min(all_p), 3)

    if spread > 0.40:
        log.info(
            f"[BTC CONFLICT] {direction} spread={spread:.3f} > 0.40 "
            f"(trend={p_trend:.3f} level={p_level:.3f} momentum={p_momentum:.3f} "
            f"funding={p_funding:.3f} oi={p_oi:.3f}) — skip"
        )
        return

    # Weighted confidence: probability of price going UP (weights sum to 1.0)
    confidence_base = round(
        0.30 * p_trend + 0.25 * p_level + 0.25 * p_momentum
        + 0.10 * p_funding + 0.10 * p_oi,
        3,
    )
    # OI post-adjustment: squeeze reduces, confirmation adds conviction
    confidence = round(max(0.0, min(1.0, confidence_base + oi_conf_adj)), 3)

    # win_prob is directional: for UP we want prob_up; for DOWN we want prob_down
    win_prob = confidence if direction == "UP" else round(1.0 - confidence, 3)

    price = market["yes_price"] if direction == "UP" else market["no_price"]
    ev    = calc_ev(win_prob, price)
    if ev < MIN_EV:
        log.info(f"BTC 5m {direction}: EV={ev:.4f} < MIN_EV={MIN_EV} — skip")
        return

    tf = _time_factor(market.get("closes_at"))

    kelly_mult = 1.0
    try:
        kelly_mult = load_learning().get("btc_5m", {}).get("kelly_multiplier", 1.0)
    except Exception:
        pass
    if kelly_mult < 1.0:
        log.info("[LEARNING-BTC] Kelly 0.5x active (7d win rate < 30%)")

    # Size formula: BTC_BASE × confidence × (1 − spread) × time_factor × kelly
    raw_size      = BTC_BASE * confidence * (1.0 - spread) * tf
    exposure_room = MAX_EXPOSURE_USD - _current_exposure(_load_trades())
    size = round(
        min(max(raw_size * kelly_mult, BTC_MIN_BET), BTC_MAX_BET, exposure_room),
        2,
    )

    closes_at = (datetime.now(timezone.utc) + timedelta(seconds=TRADE_TTL)).isoformat()
    mode_tag  = "LIVE" if BTC_LIVE_MODE else "PAPER"

    # Live mode: confirm with Polymarket BEFORE sending the OPEN alert or recording
    # anything locally. _btc_live_order returns the order_id on success, "" on failure.
    poly_order_id = ""
    if BTC_LIVE_MODE:
        poly_order_id = _btc_live_order(direction, market, price, size)
        if not poly_order_id:
            log.warning("[BTC LIVE] Order not confirmed — OPEN alert suppressed, "
                        "trade not recorded")
            return

    msg = (
        f"{mode_tag} BTC 5m OPEN | {direction} @ ${btc_price:,.0f}\n"
        f"  Poly price:{price:.3f} | win_p:{win_prob:.3f} | EV:{ev:.4f} | Size:${size:.2f}\n"
        f"  trend={p_trend:.3f}  level={p_level:.3f}  momentum={p_momentum:.3f}\n"
        f"  funding={p_funding:.3f}  oi={p_oi:.3f}\n"
        f"  spread={spread:.3f}  confidence={confidence:.3f}  tf={tf:.2f}\n"
        f"  Market: {market.get('title', 'N/A')}\n"
        f"  Auto-closes: {closes_at[:19]} UTC"
    )
    log.info(msg)
    send_alert(msg, priority="high")

    trade = {
        "id":             f"btc_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "opened_at":      datetime.now(timezone.utc).isoformat(),
        "closes_at":      closes_at,
        "direction":      direction,
        "entry_price":    price,
        "btc_entry":      btc_price,
        "btc_exit":       None,
        "p":              win_prob,
        "p_trend":        p_trend,
        "p_level":        p_level,
        "p_momentum":     p_momentum,
        "p_funding":      p_funding,
        "p_oi":           p_oi,
        "spread":         spread,
        "confidence":     confidence,
        "time_factor":    round(tf, 3),
        "ev":             round(ev, 4),
        "size":           size,
        "market_id":      market.get("market_id"),
        "market_title":   market.get("title", ""),
        "poly_order_id":  poly_order_id,   # empty string in paper mode
        "status":         "open",
        "won":            None,
        "pnl":            None,
    }
    trades = _load_trades()
    trades.append(trade)
    _save_trades(trades)


# ── Entry point ───────────────────────────────────────────────
def run_crypto_scan():
    """
    Main entry point. Called from main.py every 4 minutes.
    Steps: fetch price → close expired trades → check hard limits →
           evaluate signal → maybe enter.
    """
    log.info("--- BTC 5m Scan ---")

    btc_price = _update_btc_price()
    if btc_price is None:
        log.warning("BTC 5m: no price available — skipping scan")
        return

    # Close any trades whose 5-minute window has passed (updates trade states
    # in the file before we apply limits that read those states).
    _close_expired_trades(btc_price)

    trades    = _load_trades()
    today_str = datetime.now(timezone.utc).date().isoformat()

    # ── UTC midnight session reset ────────────────────────────
    # Updates last_session_date when the UTC date rolls over.
    # Halt is time-based (halt_until, 8h TTL) — NOT cleared here.
    if _reset_daily_session(today_str):
        log.info(f"[BTC] New UTC day ({today_str}) — session date updated")

    # ── Hard limit 3: loss-streak halt (24h time-based) ──────
    # Checked BEFORE signal evaluation. Persists across UTC midnight.
    if _is_halted():
        halt_until = _load_store().get("halt_until", "")[:19]
        log.info(f"BTC 5m: HALTED until {halt_until} UTC — 5 consecutive losses limit")
        return

    # Re-evaluate streak using only today's trades.
    streak = _consecutive_losses(trades, since_date=today_str)
    if streak >= MAX_CONSECUTIVE_LOSSES:
        _set_halt()
        halt_until = _load_store().get("halt_until", "")[:19]
        msg = (f"BTC 5m HALTED for 8h (until {halt_until} UTC) — "
               f"{MAX_CONSECUTIVE_LOSSES} consecutive losses reached.")
        log.warning(msg)
        send_alert(msg, priority="high")
        return

    # ── Hard limit 1: max 3 trades per rolling hour ───────────
    hourly = _trades_this_hour(trades)
    if hourly >= MAX_TRADES_PER_HOUR:
        log.info(f"BTC 5m: hourly cap {hourly}/{MAX_TRADES_PER_HOUR} — skip")
        return

    # ── Hard limit 2: max $10 open exposure ───────────────────
    exposure = _current_exposure(trades)
    if exposure >= MAX_EXPOSURE_USD:
        log.info(f"BTC 5m: exposure ${exposure:.2f} >= ${MAX_EXPOSURE_USD:.2f} — skip")
        return

    # Max 1 open trade at a time (separate from exposure — keeps logic explicit)
    if any(t.get("status") == "open" for t in trades):
        log.info("BTC 5m: trade already open — no new entry")
        return

    signal = _momentum_signal()
    if signal == "NONE":
        return

    # Fetch 5m candles for Module 1 (trend) and Module 2 (levels); cached 60s
    candles = _fetch_btc_candles(30)

    market = _find_btc_5m_market()
    if market is None:
        return

    _enter_trade(signal, market, btc_price, candles)
