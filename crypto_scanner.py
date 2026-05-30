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
import json, logging, re, requests, time
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from config import (BASE, BALANCE, MAX_BET, MIN_BET, MIN_EV, PAPER_MODE,
                    BTC_LIVE_MODE, SOL_LIVE_MODE,
                    PRIVATE_KEY, POLYMARKET_PROXY_WALLET,
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
SIGNAL_WINDOW = 2     # consecutive readings required for a signal
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
BTC_MIN_BET = 2.0
BTC_MAX_BET = 2.0
BTC_BASE    = 2.0   # maximum bet before confidence/spread/time multipliers

# ── Binance public klines (no API key required) ────────────────
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# ── Hard limits ───────────────────────────────────────────────
MAX_TRADES_PER_HOUR    = 8      # max entries in any rolling 60-minute window
MAX_EXPOSURE_USD       = 10.0   # max total $ open across all BTC 5m positions
MAX_CONSECUTIVE_LOSSES = 5      # halt today after this many losses in a row

BTC_TRADES_FILE    = BASE / "data" / "btc_5m_trades.json"
MID_TRADE_CSV      = BASE / "data" / "mid_trade_prices.csv"
BINANCE_TICKER     = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
COINGECKO_URL      = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
PROFIT_ALERT_PRICE = 0.65   # alert when position price hits this or above

# Fallback floor used when market close time can only be read from API (non-ET-title markets)
MIN_MINUTES_REMAINING = 1

# Trades that have already triggered the profit alert — reset on process restart
_profit_alerted: set = set()

# ── SOL 5-minute scanner constants ────────────────────────────
SOL_MIN_BET  = 2.0
SOL_MAX_BET  = 2.0
SOL_BASE     = 2.0

MAX_SOL_TRADES_PER_HOUR    = 5
MAX_SOL_EXPOSURE_USD       = 10.0
MAX_SOL_CONSECUTIVE_LOSSES = 5

SOL_TRADES_FILE    = BASE / "data" / "sol_5m_trades.json"
COINGECKO_SOL_URL  = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
BINANCE_SOL_TICKER = "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"

# SOL module-level rolling state
_sol_prices:            List[tuple]     = []
_sol_last_fetch_mono:   float           = 0.0
_sol_prices_loaded:     bool            = False
_sol_candles:           list            = []
_sol_last_candles_mono: float           = 0.0
_prev_sol_oi:           Optional[float] = None




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
    # Primary: Binance (real-time)
    try:
        r = requests.get(BINANCE_TICKER, timeout=8)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"Binance ticker failed: {e} — falling back to CoinGecko")
    # Fallback: CoinGecko (3-5 min delayed but reliable)
    try:
        r = requests.get(COINGECKO_URL, timeout=10)
        r.raise_for_status()
        return float(r.json()["bitcoin"]["usd"])
    except Exception as e:
        log.warning(f"CoinGecko fallback failed: {e}")
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
            timeout=15,
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
    import re as _re
    # Exact 5-min slug: "btc-updown-5m-{ts}".  Must use "-5m-" not "5m" to
    # avoid matching "15m" or "30m" variants (substring false-positives).
    if "-5m-" in slug:
        return True
    # Explicit 5-minute mention in title
    has_5    = "5" in title
    has_unit = any(kw in title for kw in ["minute", "min", "5m"])
    if has_5 and has_unit:
        return True
    # Time-range format: "9:00AM-9:05AM ET" — verify window is exactly 5 min.
    # Avoids matching 15m ("1:30PM-1:45PM") or 30m ranges.
    _tr = _re.search(
        r'(\d{1,2}):(\d{2})\s*[AP]M\s*-\s*(\d{1,2}):(\d{2})\s*[AP]M',
        title, _re.IGNORECASE)
    if _tr:
        mins1 = int(_tr.group(1)) * 60 + int(_tr.group(2))
        mins2 = int(_tr.group(3)) * 60 + int(_tr.group(4))
        if (mins2 - mins1) % (24 * 60) == 5:
            return True
    return False


def _fetch_5m_market_by_slug(ts: int) -> Optional[dict]:
    """Fetch a specific 5-minute market by its Unix timestamp slug."""
    slug = f"btc-updown-5m-{ts}"
    try:
        resp = requests.get(f"{POLY_API}/events", params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        events = data if isinstance(data, list) else data.get("data", [])
        if events:
            return events[0]
    except Exception:
        pass
    return None


def _find_btc_5m_market() -> Optional[dict]:
    """
    Find the current active BTC 5-minute market.

    Primary: calculate current/next window timestamps (slug=btc-updown-5m-{ts})
    and fetch directly — guaranteed to find the right market without search noise.
    Fallback: broad API search for cases where slug pattern changes.

    Returns {"title", "yes_price", "no_price", "market_id", "token_id"} or None.
    Selects the candidate with the most extreme pricing (lowest min(yes,no)).
    """
    today_iso    = datetime.now(timezone.utc).date().isoformat()
    tomorrow_iso = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

    searches = [
        # Targeted crypto search
        {"tag_slug": "crypto", "active": "true", "closed": "false",
         "end_date_min": today_iso, "end_date_max": tomorrow_iso, "limit": 100},
        # Broad search with higher limit to catch 5-min markets
        {"active": "true", "closed": "false",
         "end_date_min": today_iso, "end_date_max": tomorrow_iso, "limit": 500},
        # Search without date filter — 5-min markets may have very near end_date
        {"tag_slug": "crypto", "active": "true", "closed": "false", "limit": 500},
    ]

    # ── Primary: direct slug lookup by calculated timestamp ──────
    # Polymarket 5-min slugs = "btc-updown-5m-{ts}" where ts % 300 == 0.
    # Check current window + next 3 windows (up to 15 min ahead).
    now_ts = int(datetime.now(timezone.utc).timestamp())
    current_window = (now_ts // 300) * 300
    direct_events: List[dict] = []
    for i in range(4):
        ts  = current_window + i * 300
        ev  = _fetch_5m_market_by_slug(ts)
        if ev:
            log.info(f"[BTC DIRECT] Found slug btc-updown-5m-{ts}: {ev.get('title')}")
            direct_events.append(ev)

    seen_ids: set = set()
    btc_candidates: List[dict] = []

    # Add directly-fetched events first
    for ev in direct_events:
        eid = ev.get("id")
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            btc_candidates.append(ev)

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

    # Apply 5m filter — collect all valid market candidates, pick best at end
    valid_markets: List[dict] = []
    for event in btc_candidates:
        title = (event.get("title") or "").lower()
        slug  = (event.get("slug")  or "").lower()

        if not _is_5m(title, slug):
            continue

        eid  = event.get("id", "?")
        name = event.get("title", "?")

        # Slug-based expiry: btc-updown-5m-{ts} — ts is window start, closes at ts+300.
        # This is the definitive check — no title regex or timezone parsing needed.
        _slug_ts_m = re.search(r'btc-updown-5m-(\d+)', slug)
        if _slug_ts_m:
            _window_end = int(_slug_ts_m.group(1)) + 300
            _now_ts_chk = int(datetime.now(timezone.utc).timestamp())
            if _now_ts_chk >= _window_end:
                log.info(f"  [5m candidate {eid}] SKIP — window closed "
                         f"{_now_ts_chk - _window_end}s ago (slug): {name}")
                continue

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
        # 5-min markets: need at least 1 min remaining to be worth entering
        # Hourly markets: need at least 30 min remaining
        now_utc = datetime.now(timezone.utc)

        title_close: Optional[datetime] = None

        # Try 5-minute market format first: "9:05AM-9:10AM ET" — close = second time
        _tm5 = _re.search(
            r'\d{1,2}:\d{2}\s*[AP]M\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s+ET',
            name, _re.IGNORECASE)
        if _tm5:
            try:
                hr, mn, ampm = int(_tm5.group(1)), int(_tm5.group(2)), _tm5.group(3).upper()
                if ampm == "PM" and hr != 12:
                    hr += 12
                elif ampm == "AM" and hr == 12:
                    hr = 0
                close_utc = hr * 60 + mn + 4 * 60   # EDT = UTC-4
                today = now_utc.date()
                title_close = datetime(today.year, today.month, today.day,
                                       (close_utc // 60) % 24, close_utc % 60, 0,
                                       tzinfo=timezone.utc)
            except Exception:
                pass

        # Fallback: hourly format "9AM ET" — close = start + 1 hour
        if title_close is None:
            _tm = _re.search(r'(\d{1,2})(AM|PM)\s+ET', name, _re.IGNORECASE)
            if _tm:
                try:
                    hr   = int(_tm.group(1))
                    ampm = _tm.group(2).upper()
                    if ampm == "PM" and hr != 12:
                        hr += 12
                    elif ampm == "AM" and hr == 12:
                        hr = 0
                    close_utc_hr = (hr + 4 + 1) % 24
                    today        = now_utc.date()
                    title_close  = datetime(today.year, today.month, today.day,
                                            close_utc_hr, 0, 0, tzinfo=timezone.utc)
                except Exception:
                    pass

        if title_close is not None:
            mins_left = (title_close - now_utc).total_seconds() / 60
            # 5-min market (detected by minutes in close time) needs ≥1 min
            # Hourly market needs ≥30 min
            is_5min_market = _tm5 is not None
            min_floor = 1 if is_5min_market else 30
            if mins_left <= 0:
                log.info(f"  [5m candidate {eid}] SKIP — trading window closed "
                         f"{-mins_left:.0f} min ago ({title_close.strftime('%H:%M')} UTC): {name}")
                continue
            if mins_left < min_floor:
                log.info(f"  [5m candidate {eid}] SKIP — only {mins_left:.1f} min "
                         f"remaining (< {min_floor} min floor): {name}")
                continue
            closes_at = title_close.isoformat()
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
                    continue
            else:
                log.info(f"  [5m candidate {eid}] SKIP — no close time determinable: {name}")
                continue

        valid_markets.append({
            "title":        event.get("title", ""),
            "yes_price":    yes_price,
            "no_price":     no_price,
            "market_id":    m.get("id"),
            "token_id":     token_id,
            "no_token_id":  no_token_id,
            "closes_at":    closes_at,
            "is_5min":      _tm5 is not None,   # True = real 5-min market, False = hourly
        })

    if not valid_markets:
        log.info("No active BTC 5m market found on Polymarket")
        return None

    # Pick the market with the most extreme pricing (lowest min(yes,no) = best payout).
    # Near-50/50 markets have thin payout; 0-30¢ markets have asymmetric upside.
    best = min(valid_markets, key=lambda c: min(c["yes_price"], c["no_price"]))
    log.info(f"[BTC MARKET SELECT] {len(valid_markets)} candidates — "
             f"picked '{best['title']}' YES={best['yes_price']:.3f} NO={best['no_price']:.3f} "
             f"(best payout potential)")
    return best


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
        price_r   = round(min(price + 0.03, 0.97), 4)  # +3¢ slippage to cross the spread
        usdc_size = round(size, 2)   # guaranteed 2-decimal maker amount

        log.info(
            f"[BTC LIVE] Step 1 — token_id={token_id} "
            f"signal={price:.4f} limit={price_r} usdc=${usdc_size:.2f} (market FOK)"
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


def _query_poly_resolution(yes_token_id: str) -> Optional[bool]:
    """
    Query Polymarket CLOB midpoint for the YES token.
    Returns True (YES won / UP resolved), False (NO won / DOWN resolved),
    or None if market not yet resolved.
    """
    try:
        resp = requests.get(
            "https://clob.polymarket.com/midpoint",
            params={"token_id": yes_token_id},
            timeout=5,
        )
        if resp.status_code == 200:
            mid = float(resp.json().get("mid", 0.5))
            if mid >= 0.99:
                return True    # YES won
            elif mid <= 0.01:
                return False   # NO won
    except Exception:
        pass
    return None   # not resolved yet / query failed


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

            # LIVE: use Polymarket oracle. Wait until market_closes_at has passed,
            # then query YES token midpoint (1.00 = YES won, 0.00 = NO won).
            # Keep the trade open if oracle hasn't resolved yet.
            if BTC_LIVE_MODE:
                mca = t.get("market_closes_at")
                if mca:
                    try:
                        mca_dt = datetime.fromisoformat(mca)
                        if now_utc < mca_dt:
                            remaining = (mca_dt - now_utc).total_seconds()
                            log.info(f"[BTC LIVE] Awaiting Polymarket close in {remaining:.0f}s")
                            continue
                    except Exception:
                        pass
                yes_token_id = t.get("yes_token_id")
                if yes_token_id:
                    yes_won = _query_poly_resolution(yes_token_id)
                    if yes_won is None:
                        log.info("[BTC LIVE] Oracle not resolved yet — rechecking next scan")
                        continue
                    won = yes_won if direction == "UP" else not yes_won
                    log.info(f"[BTC LIVE] Oracle resolved: YES={'WON' if yes_won else 'LOST'} "
                             f"→ trade {'WON' if won else 'LOST'}")
                else:
                    # No token ID stored — fall back to BTC price comparison
                    won = ((direction == "UP"   and current_btc_price > btc_entry) or
                           (direction == "DOWN" and current_btc_price < btc_entry))
            else:
                # PAPER: compare BTC price at 5-min TTL expiry
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
            "date":           (trade.get("opened_at") or "")[:10],
            "opened_at":      trade.get("opened_at"),
            "market_open_at": trade.get("market_open_at"),
            "direction":      trade["direction"],
            "entry_price":    trade["entry_price"],
            "btc_entry":      trade["btc_entry"],
            "btc_exit":       trade.get("btc_exit"),
            "won":            trade.get("won"),
            "pnl":            trade.get("pnl"),
            "size":           trade["size"],
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

    if price > 0.50:
        log.info(f"[BTC PRICE GATE] entry={price:.2f} > 0.50 max — skip")
        return

    ev = calc_ev(win_prob, price)
    if ev < 0.05:
        log.info(f"[BTC EV GATE] entry={price:.2f} EV={ev:.4f} < 0.05 min — skip")
        return
    log.info(f"[BTC EV GATE] entry={price:.2f} EV={ev:.4f} >= 0.05 — ENTER")

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

    # Internal settlement uses 5-min TTL (tracks momentum signal accuracy).
    # market_closes_at is the real Polymarket end time shown in the alert.
    closes_at        = (datetime.now(timezone.utc) + timedelta(seconds=TRADE_TTL)).isoformat()
    market_closes_at = market.get("closes_at") or closes_at
    mode_tag         = "LIVE" if BTC_LIVE_MODE else "PAPER"

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
        f"  Break-even WR: {round(price * 100, 1)}%\n"
        f"  trend={p_trend:.3f}  level={p_level:.3f}  momentum={p_momentum:.3f}\n"
        f"  funding={p_funding:.3f}  oi={p_oi:.3f}\n"
        f"  spread={spread:.3f}  confidence={confidence:.3f}  tf={tf:.2f}\n"
        f"  Market: {market.get('title', 'N/A')}\n"
        f"  Polymarket resolves: {market_closes_at[:19]} UTC\n"
        f"  DO NOT sell manually — wait for oracle auto-payout"
    )
    log.info(msg)
    send_alert(msg, priority="high")

    opened_at      = datetime.now(timezone.utc).isoformat()
    is_5min_market = market.get("is_5min", False)
    try:
        _mc = datetime.fromisoformat(market_closes_at)
        _window = timedelta(minutes=5) if is_5min_market else timedelta(hours=1)
        market_open_at = (_mc - _window).isoformat()
    except Exception:
        market_open_at = None

    trade = {
        "id":             f"btc_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "opened_at":      opened_at,
        "market_open_at": market_open_at,
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
        "market_id":       market.get("market_id"),
        "market_title":    market.get("title", ""),
        "yes_token_id":    market.get("token_id"),
        "market_closes_at": market_closes_at,
        "poly_order_id":   poly_order_id,   # empty string in paper mode
        "status":          "open",
        "won":            None,
        "pnl":            None,
    }
    trades = _load_trades()
    trades.append(trade)
    _save_trades(trades)


# ── Mid-trade price monitoring ────────────────────────────────
import csv as _csv

def _fetch_poly_midpoint(yes_token_id: str) -> Optional[float]:
    """Current YES midpoint price from Polymarket CLOB."""
    try:
        r = requests.get(
            "https://clob.polymarket.com/midpoint",
            params={"token_id": yes_token_id},
            timeout=5,
        )
        if r.status_code == 200:
            return float(r.json().get("mid", 0.5))
    except Exception:
        pass
    return None


def _log_mid_trade_csv(trade_id: str, timestamp: str, entry_price: float,
                       current_price: float, seconds_since_entry: float):
    write_header = not MID_TRADE_CSV.exists()
    try:
        with open(MID_TRADE_CSV, "a", newline="") as f:
            w = _csv.writer(f)
            if write_header:
                w.writerow(["trade_id", "timestamp", "entry_price",
                            "current_price", "seconds_since_entry"])
            w.writerow([trade_id, timestamp,
                        round(entry_price, 4), round(current_price, 4),
                        round(seconds_since_entry, 1)])
    except Exception as e:
        log.warning(f"CSV log failed: {e}")


def monitor_btc_open_trades():
    """Called every 30 seconds from the main loop.
    Checks current Polymarket price for each open trade, logs to CSV,
    and sends a Telegram alert when the position hits 65¢ or above.
    """
    global _profit_alerted
    trades = _load_trades()
    open_trades = [t for t in trades if t.get("status") == "open"]
    if not open_trades:
        return

    now = datetime.now(timezone.utc)

    for trade in open_trades:
        trade_id     = trade.get("id", "?")
        yes_token_id = trade.get("yes_token_id")
        if not yes_token_id:
            continue

        direction   = trade.get("direction", "UP")
        entry_price = trade.get("entry_price", 0.5)

        yes_mid = _fetch_poly_midpoint(yes_token_id)
        if yes_mid is None:
            continue

        # Current value of our position
        current_price = yes_mid if direction == "UP" else round(1.0 - yes_mid, 4)

        try:
            seconds_since_entry = (
                now - datetime.fromisoformat(trade.get("opened_at", now.isoformat()))
            ).total_seconds()
        except Exception:
            seconds_since_entry = 0.0

        _log_mid_trade_csv(trade_id, now.isoformat(), entry_price,
                           current_price, seconds_since_entry)

        log.info(f"[MID-TRADE] {trade_id} {direction} entry={entry_price:.2f} "
                 f"current={current_price:.2f} ({seconds_since_entry:.0f}s in)")

        if current_price >= PROFIT_ALERT_PRICE and trade_id not in _profit_alerted:
            _profit_alerted.add(trade_id)
            msg = (
                f"[BTC PROFIT OPPORTUNITY]\n"
                f"  entry={entry_price:.2f}  current={current_price:.2f}\n"
                f"  Trade: {trade_id} | Direction: {direction}\n"
                f"  manual sell available on Polymarket"
            )
            log.info(msg)
            send_alert(msg, priority="high")


# ════════════════════════════════════════════════════════════════
# SOL 5-MINUTE SCANNER
# ════════════════════════════════════════════════════════════════

# ── SOL persistence ───────────────────────────────────────────
def _load_sol_store() -> dict:
    if SOL_TRADES_FILE.exists():
        try:
            raw = json.loads(SOL_TRADES_FILE.read_text())
            if isinstance(raw, list):
                return {"trades": raw, "halt_until": None}
            return raw
        except Exception:
            pass
    return {"trades": [], "halt_until": None}


def _save_sol_store(store: dict):
    SOL_TRADES_FILE.write_text(json.dumps(store, indent=2))


def _load_sol_trades() -> List[dict]:
    return _load_sol_store()["trades"]


def _save_sol_trades(trades: List[dict]):
    store = _load_sol_store()
    store["trades"] = trades
    _save_sol_store(store)


def _sol_is_halted() -> bool:
    halt_until = _load_sol_store().get("halt_until")
    if not halt_until:
        return False
    try:
        return datetime.fromisoformat(halt_until) > datetime.now(timezone.utc)
    except Exception:
        return False


def _sol_set_halt():
    store = _load_sol_store()
    store["halt_until"] = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    _save_sol_store(store)


def _sol_reset_daily_session(today_str: str) -> bool:
    store = _load_sol_store()
    if store.get("last_session_date") == today_str:
        return False
    store["last_session_date"] = today_str
    _save_sol_store(store)
    return True


def _sol_trades_this_hour(trades: List[dict]) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return sum(1 for t in trades if t.get("opened_at", "") >= cutoff)


def _sol_current_exposure(trades: List[dict]) -> float:
    return sum(t["size"] for t in trades if t.get("status") == "open")


def _sol_consecutive_losses(trades: List[dict], since_date: Optional[str] = None) -> int:
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


# ── SOL price fetching ────────────────────────────────────────
def _fetch_sol_price() -> Optional[float]:
    # Primary: CoinGecko
    try:
        r = requests.get(COINGECKO_SOL_URL, timeout=10)
        r.raise_for_status()
        return float(r.json()["solana"]["usd"])
    except Exception as e:
        log.warning(f"CoinGecko SOL failed: {e} — falling back to Binance")
    # Fallback: Binance (real-time)
    try:
        r = requests.get(BINANCE_SOL_TICKER, timeout=8)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"Binance SOL fallback failed: {e}")
        return None


def _update_sol_price() -> Optional[float]:
    global _sol_prices, _sol_last_fetch_mono, _sol_prices_loaded
    now_mono = time.monotonic()

    if not _sol_prices_loaded:
        _sol_prices_loaded = True
        try:
            saved = _load_sol_store().get("price_history", [])
            if saved:
                _sol_prices = [(ts, float(p)) for ts, p in saved]
                log.info(f"[SOL] Restored {len(_sol_prices)} price readings from disk")
        except Exception:
            pass

    if now_mono - _sol_last_fetch_mono < FETCH_TTL:
        return _sol_prices[-1][1] if _sol_prices else None

    price = _fetch_sol_price()
    if price is None:
        return _sol_prices[-1][1] if _sol_prices else None

    _sol_prices.append((datetime.now(timezone.utc).isoformat(), price))
    _sol_prices = _sol_prices[-MAX_HISTORY:]
    _sol_last_fetch_mono = now_mono

    try:
        store = _load_sol_store()
        store["price_history"] = list(_sol_prices)
        _save_sol_store(store)
    except Exception as e:
        log.warning(f"[SOL] Failed to persist price history: {e}")

    log.info(f"SOL ${price:,.2f} | history={len(_sol_prices)} readings")
    return price


# ── SOL candle fetching ───────────────────────────────────────
def _fetch_sol_candles(limit: int = 30) -> list:
    global _sol_candles, _sol_last_candles_mono
    now_mono = time.monotonic()
    if now_mono - _sol_last_candles_mono < CANDLES_TTL and _sol_candles:
        return _sol_candles
    try:
        r = requests.get(
            BINANCE_KLINES,
            params={"symbol": "SOLUSDT", "interval": "5m", "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        _sol_candles = [
            {"open": float(c[1]), "high": float(c[2]),
             "low":  float(c[3]), "close": float(c[4]), "volume": float(c[5])}
            for c in r.json()
        ]
        _sol_last_candles_mono = now_mono
        log.info(f"[SOL CANDLES] fetched {len(_sol_candles)} 5m candles from Binance")
    except Exception as e:
        log.warning(f"[SOL CANDLES] Binance fetch failed: {e}")
        if _sol_prices:
            _sol_candles = [
                {"open": p, "high": p, "low": p, "close": p, "volume": 1.0}
                for _, p in _sol_prices
            ]
            log.info(f"[SOL CANDLES] using {len(_sol_candles)} synthetic candles")
    return _sol_candles


# ── SOL signal modules ────────────────────────────────────────
def _module_sol_trend(candles: list) -> float:
    if len(candles) < 5:
        log.info("[SOL TREND] insufficient candles — neutral 0.500")
        return 0.5
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    ema5   = _ema(closes, 5)
    ema20  = _ema(closes, 20)
    vwap   = _vwap(candles)
    if vwap is None:
        return 0.5
    vwap_dev    = (price - vwap) / price
    vwap_signal = 0.5 + max(-0.5, min(0.5, vwap_dev * 50))
    if ema5 is None or ema20 is None:
        p_trend = round(vwap_signal, 3)
        log.info(f"[SOL TREND] VWAP-only ({len(candles)} candles) p_trend={p_trend:.3f}")
        return p_trend
    ema_margin = (ema5 - ema20) / price
    ema_signal = 0.5 + max(-0.5, min(0.5, ema_margin * 250))
    p_trend    = round(0.5 * ema_signal + 0.5 * vwap_signal, 3)
    log.info(f"[SOL TREND] EMA5={ema5:.2f} EMA20={ema20:.2f} VWAP={vwap:.2f} "
             f"price={price:.2f} → p_trend={p_trend:.3f}")
    return p_trend


def _module_sol_levels(price: float, candles: list) -> float:
    if not candles:
        return 0.5
    window  = candles[-12:] if len(candles) >= 12 else candles
    high_1h = max(c["high"] for c in window)
    low_1h  = min(c["low"]  for c in window)
    rng     = high_1h - low_1h
    pos_in_range  = ((price - low_1h) / rng) if rng > 0 else 0.5
    # SOL: $10 round-number boundaries (e.g. 150, 160, 170)
    nearest_round = round(price / 10) * 10
    dist_pct      = abs(price - nearest_round) / price
    above_round   = price >= nearest_round
    if dist_pct < 0.01:
        p_round = 0.5
    elif above_round:
        p_round = 0.62
    else:
        p_round = 0.38
    p_level = round(0.6 * pos_in_range + 0.4 * p_round, 3)
    log.info(f"[SOL LEVELS] 1h_hi={high_1h:.2f} 1h_lo={low_1h:.2f} "
             f"pos={pos_in_range:.2f} round={nearest_round:.0f} "
             f"above={above_round} p_level={p_level:.3f}")
    return p_level


def _module_sol_momentum(signal: str) -> float:
    if signal == "UP":
        p = MOMENTUM_P
    elif signal == "DOWN":
        p = round(1.0 - MOMENTUM_P, 4)
    else:
        p = 0.5
    log.info(f"[SOL MOMENTUM] signal={signal} p_momentum={p:.3f}")
    return p


def _module_sol_funding(direction: str) -> tuple:
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/futures/usdt/funding_rate",
            params={"contract": "SOL_USDT", "limit": 1},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        rate = float(data[0]["r"]) if data else 0.0
    except Exception as e:
        log.warning(f"[SOL FUNDING gateio] fetch failed: {e} — neutral 0.500")
        return 0.5, 0.0
    rate_pct = rate * 100
    if rate > 0.0005 and direction == "UP":
        p_funding, funding_adj, bias_tag = 0.45, -0.05, " -> bearish bias applied"
    elif rate < -0.0005 and direction == "DOWN":
        p_funding, funding_adj, bias_tag = 0.55, -0.05, " -> bullish bias applied"
    else:
        p_funding, funding_adj, bias_tag = 0.50, 0.0, ""
    log.info(f"[SOL FUNDING gateio] SOL_USDT rate={rate_pct:+.4f}%{bias_tag}")
    return p_funding, funding_adj


def _module_sol_oi(direction: str) -> tuple:
    global _prev_sol_oi
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/futures/usdt/tickers",
            params={"contract": "SOL_USDT"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        oi_current = float(data[0]["total_size"]) if data else None
        if oi_current is None:
            raise ValueError("empty ticker list")
    except Exception as e:
        log.warning(f"[SOL OI gateio] fetch failed: {e} — neutral 0.500")
        return 0.5, 0.0
    if _prev_sol_oi is None:
        _prev_sol_oi = oi_current
        log.info(f"[SOL OI gateio] first reading {oi_current:,.0f} — neutral 0.500")
        return 0.5, 0.0
    oi_delta     = oi_current - _prev_sol_oi
    _prev_sol_oi = oi_current
    if oi_delta == 0:
        log.info(f"[SOL OI gateio] unchanged {oi_current:,.0f} — neutral 0.500")
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
    log.info(f"[SOL OI gateio] OI={oi_current:,.0f} delta={oi_delta:+,.0f} ({oi_dir}) | {tag}")
    return p_oi, conf_adj


def _sol_momentum_signal() -> str:
    if len(_sol_prices) < SIGNAL_WINDOW:
        log.info(f"SOL momentum: NONE (need {SIGNAL_WINDOW}, have {len(_sol_prices)})")
        return "NONE"
    recent = [x[1] for x in _sol_prices[-SIGNAL_WINDOW:]]
    if all(recent[i + 1] > recent[i] for i in range(len(recent) - 1)):
        pct = (recent[-1] - recent[0]) / recent[0] * 100
        log.info(f"SOL momentum: UP   ${recent[0]:,.2f}→${recent[-1]:,.2f} ({pct:+.3f}%)")
        return "UP"
    if all(recent[i + 1] < recent[i] for i in range(len(recent) - 1)):
        pct = (recent[-1] - recent[0]) / recent[0] * 100
        log.info(f"SOL momentum: DOWN ${recent[0]:,.2f}→${recent[-1]:,.2f} ({pct:+.3f}%)")
        return "DOWN"
    log.info("SOL momentum: NONE (mixed)")
    return "NONE"


# ── SOL Polymarket market finder ──────────────────────────────
def _is_sol(title: str, slug: str) -> bool:
    return "solana" in title.lower() or "sol-updown" in slug.lower()


def _find_sol_5m_market() -> Optional[dict]:
    today_iso    = datetime.now(timezone.utc).date().isoformat()
    tomorrow_iso = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

    searches = [
        {"tag_slug": "crypto", "active": "true", "closed": "false",
         "end_date_min": today_iso, "end_date_max": tomorrow_iso, "limit": 100},
        {"active": "true", "closed": "false",
         "end_date_min": today_iso, "end_date_max": tomorrow_iso, "limit": 500},
        {"tag_slug": "crypto", "active": "true", "closed": "false", "limit": 500},
    ]

    # Primary: direct slug lookup by calculated window timestamp
    now_ts         = int(datetime.now(timezone.utc).timestamp())
    current_window = (now_ts // 300) * 300
    direct_events: List[dict] = []
    for i in range(4):
        ts   = current_window + i * 300
        slug = f"sol-updown-5m-{ts}"
        try:
            resp = requests.get(f"{POLY_API}/events", params={"slug": slug}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            evs  = data if isinstance(data, list) else data.get("data", [])
            if evs:
                log.info(f"[SOL DIRECT] Found {slug}: {evs[0].get('title')}")
                direct_events.append(evs[0])
        except Exception:
            pass

    seen_ids: set = set()
    sol_candidates: List[dict] = []

    for ev in direct_events:
        eid = ev.get("id")
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            sol_candidates.append(ev)

    for params in searches:
        try:
            events = _fetch_events(params)
        except Exception as e:
            log.warning(f"SOL market fetch failed (params={params}): {e}")
            continue
        log.info(f"[SOL DEBUG] API returned {len(events)} events (params={params})")
        for event in events:
            eid   = event.get("id")
            title = event.get("title") or ""
            slug  = event.get("slug")  or ""
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            if _is_sol(title, slug):
                log.info(f"  [SOL DEBUG] id={eid}  title={title!r}  slug={slug!r}")
                sol_candidates.append(event)

    if not sol_candidates:
        log.info("[SOL DEBUG] No Solana events found on Polymarket")
        return None

    log.info(f"[SOL DEBUG] {len(sol_candidates)} event(s) passed _is_sol filter")

    valid_markets: List[dict] = []
    for event in sol_candidates:
        title = (event.get("title") or "").lower()
        slug  = (event.get("slug")  or "").lower()
        eid   = event.get("id", "?")
        name  = event.get("title", "?")

        if not _is_5m(title, slug):
            continue

        # Slug-based expiry: sol-updown-5m-{ts} — window closes at ts+300
        _slug_ts_m = re.search(r'sol-updown-5m-(\d+)', slug)
        if _slug_ts_m:
            _window_end = int(_slug_ts_m.group(1)) + 300
            _now_ts_chk = int(datetime.now(timezone.utc).timestamp())
            if _now_ts_chk >= _window_end:
                log.info(f"  [SOL 5m {eid}] SKIP — window closed "
                         f"{_now_ts_chk - _window_end}s ago: {name}")
                continue

        mkts = event.get("markets", [])
        if not mkts:
            log.info(f"  [SOL 5m {eid}] SKIP — no markets: {name}")
            continue

        m      = mkts[0]
        prices = _parse_prices(m.get("outcomePrices", "[]"))

        if len(prices) < 2:
            try:
                detail = requests.get(f"{POLY_API}/events/{eid}", timeout=10).json()
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
                log.warning(f"  [SOL 5m {eid}] detail fetch failed: {e}")

        if len(prices) < 2:
            log.info(f"  [SOL 5m {eid}] SKIP — no price data: {name}")
            continue

        yes_price = float(prices[0])
        no_price  = float(prices[1])

        if not (0.05 <= yes_price <= 0.95):
            log.info(f"  [SOL 5m {eid}] SKIP — degenerate prices YES={yes_price}: {name}")
            continue

        if m.get("liquidityNum", 0) == 0 and float(m.get("volume", 0) or 0) == 0:
            log.info(f"  [SOL 5m {eid}] SKIP — no liquidity: {name}")
            continue

        raw_ids     = (m.get("clobTokenIds") or "").split(",")
        token_id    = raw_ids[0].strip('"[] ') if len(raw_ids) > 0 else None
        no_token_id = raw_ids[1].strip('"[] ') if len(raw_ids) > 1 else None

        log.info(
            f"[SOL 5m MARKET IDENTIFIED]\n"
            f"  Title : {event.get('title')}\n"
            f"  Slug  : {event.get('slug')}\n"
            f"  YES={yes_price:.3f}  NO={no_price:.3f}"
        )

        import re as _re
        now_utc     = datetime.now(timezone.utc)
        title_close: Optional[datetime] = None

        _tm5 = _re.search(
            r'\d{1,2}:\d{2}\s*[AP]M\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s+ET',
            name, _re.IGNORECASE)
        if _tm5:
            try:
                hr, mn, ampm = int(_tm5.group(1)), int(_tm5.group(2)), _tm5.group(3).upper()
                if ampm == "PM" and hr != 12:
                    hr += 12
                elif ampm == "AM" and hr == 12:
                    hr = 0
                close_utc = hr * 60 + mn + 4 * 60
                today = now_utc.date()
                title_close = datetime(today.year, today.month, today.day,
                                       (close_utc // 60) % 24, close_utc % 60, 0,
                                       tzinfo=timezone.utc)
            except Exception:
                pass

        if title_close is None:
            _tm = _re.search(r'(\d{1,2})(AM|PM)\s+ET', name, _re.IGNORECASE)
            if _tm:
                try:
                    hr   = int(_tm.group(1))
                    ampm = _tm.group(2).upper()
                    if ampm == "PM" and hr != 12:
                        hr += 12
                    elif ampm == "AM" and hr == 12:
                        hr = 0
                    close_utc_hr = (hr + 4 + 1) % 24
                    today        = now_utc.date()
                    title_close  = datetime(today.year, today.month, today.day,
                                            close_utc_hr, 0, 0, tzinfo=timezone.utc)
                except Exception:
                    pass

        if title_close is not None:
            mins_left   = (title_close - now_utc).total_seconds() / 60
            is_5min_mkt = _tm5 is not None
            min_floor   = 1 if is_5min_mkt else 30
            if mins_left <= 0:
                log.info(f"  [SOL 5m {eid}] SKIP — window closed {-mins_left:.0f} min ago: {name}")
                continue
            if mins_left < min_floor:
                log.info(f"  [SOL 5m {eid}] SKIP — only {mins_left:.1f} min remaining: {name}")
                continue
            closes_at = title_close.isoformat()
        else:
            closes_at = (m.get("endDateIso") or m.get("endDate")
                         or event.get("endDateIso") or event.get("endDate"))
            if closes_at:
                try:
                    closes_dt = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
                    mins_left = (closes_dt - now_utc).total_seconds() / 60
                    if mins_left <= 0:
                        log.info(f"  [SOL 5m {eid}] SKIP — already expired: {name}")
                        continue
                    if mins_left < MIN_MINUTES_REMAINING:
                        log.info(f"  [SOL 5m {eid}] SKIP — only {mins_left:.0f} min remaining: {name}")
                        continue
                except Exception:
                    continue
            else:
                log.info(f"  [SOL 5m {eid}] SKIP — no close time determinable: {name}")
                continue

        valid_markets.append({
            "title":        event.get("title", ""),
            "yes_price":    yes_price,
            "no_price":     no_price,
            "market_id":    m.get("id"),
            "token_id":     token_id,
            "no_token_id":  no_token_id,
            "closes_at":    closes_at,
            "is_5min":      _tm5 is not None,
        })

    if not valid_markets:
        log.info("No active SOL 5m market found on Polymarket")
        return None

    best = min(valid_markets, key=lambda c: min(c["yes_price"], c["no_price"]))
    log.info(f"[SOL MARKET SELECT] {len(valid_markets)} candidates — "
             f"picked '{best['title']}' YES={best['yes_price']:.3f} NO={best['no_price']:.3f}")
    return best


# ── SOL live order ────────────────────────────────────────────
def _sol_live_order(direction: str, market: dict, price: float, size: float) -> str:
    """Submit a live SOL 5m FOK order via the shared Polymarket CLOB client.
    Returns order_id on success, empty string on any failure.
    Reuses _get_clob_client() — same wallet and credentials as BTC.
    """
    token_id = (market.get("token_id")
                if direction == "UP"
                else market.get("no_token_id"))
    if not token_id:
        log.warning(f"[SOL LIVE] No token_id for direction={direction} — skipping")
        return ""

    client = _get_clob_client()
    if client is None:
        return ""

    try:
        from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType

        price_r   = round(min(price + 0.03, 0.97), 4)
        usdc_size = round(size, 2)

        log.info(
            f"[SOL LIVE] Step 1 — token_id={token_id} "
            f"signal={price:.4f} limit={price_r} usdc=${usdc_size:.2f} (market FOK)"
        )
        order_args = MarketOrderArgsV2(
            token_id=token_id,
            amount=usdc_size,
            side="BUY",
            price=price_r,
            order_type=OrderType.FOK,
        )
        log.info("[SOL LIVE] Step 2 — calling create_market_order() …")
        signed_order = client.create_market_order(order_args)
        log.info("[SOL LIVE] Step 3 — calling post_order() FOK …")
        resp = client.post_order(signed_order, OrderType.FOK)
        log.info(f"[SOL LIVE] Step 4 — full response: {resp}")
    except Exception as e:
        err_str = str(e)
        if "fully filled" in err_str or "FOK" in err_str:
            log.info(f"[SOL LIVE] FOK kill — no liquidity at {round(price, 4)}, trade skipped")
            return ""
        log.error(f"[SOL LIVE] Order submission failed: {e}")
        send_alert(f"[SOL LIVE] LIVE ORDER FAILED: {e}", priority="high")
        return ""

    if not isinstance(resp, dict):
        log.error(f"[SOL LIVE] Unexpected response type {type(resp).__name__}: {resp}")
        send_alert(f"[SOL LIVE] LIVE ORDER FAILED — unexpected response: {resp}",
                   priority="high")
        return ""

    order_id  = resp.get("orderID") or resp.get("id") or ""
    success   = resp.get("success")
    error_msg = resp.get("errorMsg") or resp.get("error") or ""
    status    = resp.get("status", "")

    if success is False:
        reason = error_msg or f"status={status}" or "unknown"
        log.error(f"[SOL LIVE] ORDER REJECTED — {reason} | resp={resp}")
        send_alert(f"[SOL LIVE] ORDER REJECTED\n{reason}", priority="high")
        return ""

    if error_msg:
        log.error(f"[SOL LIVE] ORDER ERROR — {error_msg} | resp={resp}")
        send_alert(f"[SOL LIVE] ORDER ERROR\n{error_msg}", priority="high")
        return ""

    if not order_id and success is not True:
        log.info(f"[SOL LIVE] FOK unmatched — no liquidity at {price:.4f}, skipping")
        return ""

    log.info(f"[SOL LIVE] Order CONFIRMED — direction={direction} order_id={order_id} "
             f"token={token_id[:8]}... price={price:.4f} usdc=${usdc_size:.2f}")
    return order_id


# ── SOL trade lifecycle ───────────────────────────────────────
def _close_expired_sol_trades(current_sol_price: float):
    trades  = _load_sol_trades()
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
                log.info(f"SOL trade still open — {remaining:.0f}s remaining")
                continue

            direction = t["direction"]
            sol_entry = t["sol_entry"]

            if SOL_LIVE_MODE:
                # Wait for Polymarket oracle before settling
                mca = t.get("market_closes_at")
                if mca:
                    try:
                        mca_dt = datetime.fromisoformat(mca)
                        if now_utc < mca_dt:
                            remaining = (mca_dt - now_utc).total_seconds()
                            log.info(f"[SOL LIVE] Awaiting Polymarket close in {remaining:.0f}s")
                            continue
                    except Exception:
                        pass
                yes_token_id = t.get("yes_token_id")
                if yes_token_id:
                    yes_won = _query_poly_resolution(yes_token_id)
                    if yes_won is None:
                        log.info("[SOL LIVE] Oracle not resolved yet — rechecking next scan")
                        continue
                    won = yes_won if direction == "UP" else not yes_won
                    log.info(f"[SOL LIVE] Oracle resolved: YES={'WON' if yes_won else 'LOST'} "
                             f"→ trade {'WON' if won else 'LOST'}")
                else:
                    won = ((direction == "UP"   and current_sol_price > sol_entry) or
                           (direction == "DOWN" and current_sol_price < sol_entry))
            else:
                won = ((direction == "UP"   and current_sol_price > sol_entry) or
                       (direction == "DOWN" and current_sol_price < sol_entry))

            entry_price = t["entry_price"]
            size        = t["size"]
            pnl = round(size * (1.0 / entry_price - 1.0), 2) if won else -size
            t["sol_exit"]  = current_sol_price
            t["closed_at"] = now_utc.isoformat()
            t["won"]       = won
            t["pnl"]       = pnl
            t["status"]    = "won" if won else "lost"
            pct    = (current_sol_price - sol_entry) / sol_entry * 100
            payout = round(size / entry_price, 2) if won else 0.0
            mode_tag = "SOL LIVE" if SOL_LIVE_MODE else "SOL 5m"
            msg = (f"[{mode_tag}] {'WIN' if won else 'LOSS'} | {direction} | "
                   f"${sol_entry:,.2f}→${current_sol_price:,.2f} ({pct:+.3f}%) | "
                   f"Payout:${payout:.2f} | PnL:${pnl:+.2f}")
            log.info(msg)
            send_alert(msg, priority="normal" if won else "high")
            changed = True
        except Exception as e:
            log.warning(f"[SOL] Failed to close trade {t.get('id','?')}: {e}")
    if changed:
        _save_sol_trades(trades)


def _enter_sol_trade(direction: str, market: dict, sol_price: float, candles: list):
    # Five-module conflict detector (SOL-specific modules)
    p_trend    = _module_sol_trend(candles)
    p_level    = _module_sol_levels(sol_price, candles)
    p_momentum = _module_sol_momentum(direction)
    p_funding, funding_adj = _module_sol_funding(direction)
    p_oi, oi_conf_adj      = _module_sol_oi(direction)

    p_trend    = round(max(0.0, min(1.0, p_trend    + funding_adj)), 3)
    p_momentum = round(max(0.0, min(1.0, p_momentum + funding_adj)), 3)

    all_p  = (p_trend, p_level, p_momentum, p_funding, p_oi)
    spread = round(max(all_p) - min(all_p), 3)

    if spread > 0.40:
        log.info(
            f"[SOL CONFLICT] {direction} spread={spread:.3f} > 0.40 "
            f"(trend={p_trend:.3f} level={p_level:.3f} momentum={p_momentum:.3f} "
            f"funding={p_funding:.3f} oi={p_oi:.3f}) — skip"
        )
        return

    confidence_base = round(
        0.30 * p_trend + 0.25 * p_level + 0.25 * p_momentum
        + 0.10 * p_funding + 0.10 * p_oi,
        3,
    )
    confidence = round(max(0.0, min(1.0, confidence_base + oi_conf_adj)), 3)
    win_prob   = confidence if direction == "UP" else round(1.0 - confidence, 3)

    price = market["yes_price"] if direction == "UP" else market["no_price"]

    if price > 0.50:
        log.info(f"[SOL PRICE GATE] entry={price:.2f} > 0.50 max — skip")
        return

    # Tiered EV: cheap entries (≤0.30) need stronger edge than mid-range entries
    ev_min = 0.15 if price <= 0.30 else 0.08
    ev = calc_ev(win_prob, price)
    if ev < ev_min:
        log.info(f"[SOL EV GATE] entry={price:.2f} EV={ev:.4f} < {ev_min:.2f} (tier) — skip")
        return
    log.info(f"[SOL EV GATE] entry={price:.2f} EV={ev:.4f} >= {ev_min:.2f} — ENTER")

    tf = _time_factor(market.get("closes_at"))

    raw_size      = SOL_BASE * confidence * (1.0 - spread) * tf
    exposure_room = MAX_SOL_EXPOSURE_USD - _sol_current_exposure(_load_sol_trades())
    size = round(min(max(raw_size, SOL_MIN_BET), SOL_MAX_BET, exposure_room), 2)

    closes_at        = (datetime.now(timezone.utc) + timedelta(seconds=TRADE_TTL)).isoformat()
    market_closes_at = market.get("closes_at") or closes_at
    mode_tag         = "SOL LIVE" if SOL_LIVE_MODE else "SOL 5m"

    # Live mode: confirm with Polymarket BEFORE alerting or recording
    poly_order_id = ""
    if SOL_LIVE_MODE:
        poly_order_id = _sol_live_order(direction, market, price, size)
        if not poly_order_id:
            log.warning("[SOL LIVE] Order not confirmed — OPEN alert suppressed, "
                        "trade not recorded")
            return

    msg = (
        f"[{mode_tag}] OPEN | {direction} @ ${sol_price:,.2f}\n"
        f"  Poly price:{price:.3f} | win_p:{win_prob:.3f} | EV:{ev:.4f} | Size:${size:.2f}\n"
        f"  EV tier: {'≤0.30 → min 0.15' if price <= 0.30 else '≤0.50 → min 0.08'}\n"
        f"  Break-even WR: {round(price * 100, 1)}%\n"
        f"  trend={p_trend:.3f}  level={p_level:.3f}  momentum={p_momentum:.3f}\n"
        f"  funding={p_funding:.3f}  oi={p_oi:.3f}\n"
        f"  spread={spread:.3f}  confidence={confidence:.3f}  tf={tf:.2f}\n"
        f"  Market: {market.get('title', 'N/A')}\n"
        f"  Polymarket resolves: {market_closes_at[:19]} UTC\n"
        f"  DO NOT sell manually — wait for oracle auto-payout"
    )
    log.info(msg)
    send_alert(msg, priority="high")

    opened_at      = datetime.now(timezone.utc).isoformat()
    is_5min_market = market.get("is_5min", False)
    try:
        _mc        = datetime.fromisoformat(market_closes_at)
        _window    = timedelta(minutes=5) if is_5min_market else timedelta(hours=1)
        market_open_at = (_mc - _window).isoformat()
    except Exception:
        market_open_at = None

    trade = {
        "id":              f"sol_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "opened_at":       opened_at,
        "market_open_at":  market_open_at,
        "closes_at":       closes_at,
        "direction":       direction,
        "entry_price":     price,
        "sol_entry":       sol_price,
        "sol_exit":        None,
        "p":               win_prob,
        "p_trend":         p_trend,
        "p_level":         p_level,
        "p_momentum":      p_momentum,
        "p_funding":       p_funding,
        "p_oi":            p_oi,
        "spread":          spread,
        "confidence":      confidence,
        "time_factor":     round(tf, 3),
        "ev":              round(ev, 4),
        "ev_min":          ev_min,
        "size":            size,
        "market_id":       market.get("market_id"),
        "market_title":    market.get("title", ""),
        "yes_token_id":    market.get("token_id"),
        "market_closes_at": market_closes_at,
        "poly_order_id":   poly_order_id,
        "status":          "open",
        "won":             None,
        "pnl":             None,
    }
    trades = _load_sol_trades()
    trades.append(trade)
    _save_sol_trades(trades)


# ── SOL entry point ───────────────────────────────────────────
def run_sol_scan():
    """Called from main.py sequentially after run_crypto_scan() each cycle."""
    log.info("--- SOL 5m Scan ---")

    sol_price = _update_sol_price()
    if sol_price is None:
        log.warning("SOL 5m: no price available — skipping scan")
        return

    _close_expired_sol_trades(sol_price)

    trades    = _load_sol_trades()
    today_str = datetime.now(timezone.utc).date().isoformat()

    if _sol_reset_daily_session(today_str):
        log.info(f"[SOL] New UTC day ({today_str}) — session date updated")

    if _sol_is_halted():
        halt_until = _load_sol_store().get("halt_until", "")[:19]
        log.info(f"SOL 5m: HALTED until {halt_until} UTC — loss-streak limit")
        return

    streak = _sol_consecutive_losses(trades, since_date=today_str)
    if streak >= MAX_SOL_CONSECUTIVE_LOSSES:
        _sol_set_halt()
        halt_until = _load_sol_store().get("halt_until", "")[:19]
        msg = (f"[SOL 5m] HALTED for 8h (until {halt_until} UTC) — "
               f"{MAX_SOL_CONSECUTIVE_LOSSES} consecutive losses reached.")
        log.warning(msg)
        send_alert(msg, priority="high")
        return

    hourly = _sol_trades_this_hour(trades)
    if hourly >= MAX_SOL_TRADES_PER_HOUR:
        log.info(f"SOL 5m: hourly cap {hourly}/{MAX_SOL_TRADES_PER_HOUR} — skip")
        return

    exposure = _sol_current_exposure(trades)
    if exposure >= MAX_SOL_EXPOSURE_USD:
        log.info(f"SOL 5m: exposure ${exposure:.2f} >= ${MAX_SOL_EXPOSURE_USD:.2f} — skip")
        return

    open_count = sum(1 for t in trades if t.get("status") == "open")
    if open_count >= 3:
        log.info(f"SOL 5m: {open_count} trades already open — no new entry")
        return

    candles = _fetch_sol_candles(30)
    market  = _find_sol_5m_market()
    if market is None:
        return

    signal = _sol_momentum_signal()
    if signal != "NONE":
        _enter_sol_trade(signal, market, sol_price, candles)


# ── BTC entry point ───────────────────────────────────────────
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

    # Max 3 concurrent open trades (exposure cap is the secondary backstop)
    open_count = sum(1 for t in trades if t.get("status") == "open")
    if open_count >= 3:
        log.info(f"BTC 5m: {open_count} trades already open — no new entry")
        return

    # Fetch market and candles once — shared by both strategies
    candles = _fetch_btc_candles(30)
    market  = _find_btc_5m_market()
    if market is None:
        return

    signal = _momentum_signal()
    if signal != "NONE":
        _enter_trade(signal, market, btc_price, candles)
