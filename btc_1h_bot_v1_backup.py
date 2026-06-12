"""
btc_1h_bot.py  —  BTC 1-Hour Up/Down Fair Value Bot

Architecture
------------
Single signal: fair_yes_probability vs Polymarket YES price.
Edge = price gap of 8c or more between fair value and market price.

Fair value is derived from:
  1. BTC price delta vs hourly open (strike)
  2. Realized volatility from the last 60 minutes of 1-min price history
  3. Gate.io funding-rate adjustment (+/-0.03)
  4. Gate.io open-interest delta adjustment (+/-0.02)

No module signals (p_trend / p_level / p_momentum etc). Diagnostic data
showed those provided zero predictive edge at the 5-minute timeframe.
The 1-hour framework gives the probability calculation real information to
work with: price drift over 30+ minutes is meaningful; over 5 minutes it
is noise.

Usage
-----
  python main.py 1h             # live scan loop (BTC_1H_LIVE_MODE=false = paper)
  python main.py 1h-paper-test  # validate math on current markets, no trades
"""

import json, logging, math, os, re, requests, statistics, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List

from config import BASE, PAPER_MODE
from state  import send_alert

log = logging.getLogger("weatherbet")

# ── Configuration ──────────────────────────────────────────────────────────────
MIN_GAP                = 0.08    # minimum fair-value vs market-price gap to enter
MIN_TIME_REMAINING_MIN = 15      # skip if < 15 min left in window
MAX_TIME_REMAINING_MIN = 50      # skip if > 50 min left (too early, vol estimate weak)
MIN_TIME_ELAPSED_MIN   = 10      # require at least 10 min elapsed for price context
MIN_ENTRY_PRICE        = 0.10    # no entry below 10c
MAX_ENTRY_PRICE        = 0.70    # no entry above 70c
MAX_OPEN_TRADES        = 3       # concurrent open 1h positions
MAX_DAILY_TRADES       = 18      # per-UTC-day cap
TRADE_SIZE_USD         = 2.00
SCAN_INTERVAL_SEC      = 60      # main.py calls run_1h_scan() every 60 s
DEFAULT_VOL_PCT        = 0.45    # fallback hourly vol when history is thin (%)

# ── API endpoints ──────────────────────────────────────────────────────────────
POLY_API       = "https://gamma-api.polymarket.com"
COINGECKO_URL  = ("https://api.coingecko.com/api/v3/simple/price"
                  "?ids=bitcoin&vs_currencies=usd")
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
GATE_FUNDING   = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate"
GATE_CONTRACTS = "https://api.gateio.ws/api/v4/futures/usdt/contracts"

# ── Credentials / live mode ────────────────────────────────────────────────────
BTC_1H_LIVE_MODE = os.getenv("BTC_1H_LIVE_MODE", "false").lower() == "true"
_PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")
_PROXY_WALLET    = os.getenv("POLYMARKET_PROXY_WALLET", "")
_POLY_KEY        = os.getenv("POLYMARKET_API_KEY", "")
_POLY_SECRET     = os.getenv("POLYMARKET_SECRET", "")
_POLY_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")

# ── Module-level rolling price history ────────────────────────────────────────
_1h_prices: List[tuple] = []   # [(iso_ts, price_float), ...]  — max 120 readings
_prev_oi:   Optional[float] = None

TRADES_FILE    = BASE / "data" / "btc_1h_trades.json"
MAX_PX_HISTORY = 120   # keep 2 h; vol calc uses last 60


# ══════════════════════════════════════════════════════════════════════════════
# Maths
# ══════════════════════════════════════════════════════════════════════════════

def _norm_cdf(z: float) -> float:
    """Standard normal CDF via math.erf — no scipy required."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _realized_hourly_vol() -> float:
    """
    Hourly realized volatility in % from the last 60 one-minute price readings.

    Method:
      - Collect up to 60 most-recent consecutive price readings
      - Calculate 1-minute % returns: (p[i] - p[i-1]) / p[i-1] * 100
      - Per-minute vol = stdev(returns)
      - Hourly vol = per_min_vol * sqrt(60)   [square-root-of-time scaling]

    Returns DEFAULT_VOL_PCT if fewer than 5 readings are available.
    """
    window = [p for _, p in _1h_prices[-60:]]
    if len(window) < 5:
        log.info(f"[1H VOL] insufficient history ({len(window)} readings) "
                 f"— using default {DEFAULT_VOL_PCT}%")
        return DEFAULT_VOL_PCT
    returns = [(window[i] - window[i-1]) / window[i-1] * 100
               for i in range(1, len(window))]
    if len(returns) < 2:
        return DEFAULT_VOL_PCT
    per_min_vol = statistics.stdev(returns)
    hourly_vol  = per_min_vol * math.sqrt(60)
    log.info(f"[1H VOL] {len(window)} readings  per_min={per_min_vol:.4f}%  "
             f"hourly={hourly_vol:.4f}%")
    return hourly_vol


def _calc_fair_yes(current_price: float,
                   strike_price:  float,
                   remaining_sec: float,
                   hourly_vol_pct: float) -> float:
    """
    P(BTC_close > strike_price) using a simplified GBM/diffusion model.

    remaining_vol = hourly_vol * sqrt(remaining_hours)
    z_score       = price_delta_pct / remaining_vol
    fair_yes      = norm_cdf(z_score)

    Clamped to [0.05, 0.95] so the model is never fully certain.
    """
    if remaining_sec <= 0 or hourly_vol_pct <= 0:
        return 1.0 if current_price > strike_price else 0.0

    remaining_hours = remaining_sec / 3600.0
    remaining_vol   = hourly_vol_pct * math.sqrt(remaining_hours)

    if remaining_vol < 0.001:
        return 1.0 if current_price > strike_price else 0.0

    price_delta_pct = (current_price - strike_price) / strike_price * 100.0
    z_score         = price_delta_pct / remaining_vol

    fair_yes = _norm_cdf(z_score)
    return max(0.05, min(0.95, fair_yes))


# ══════════════════════════════════════════════════════════════════════════════
# Price feeds
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_btc_price() -> Optional[float]:
    """Fetch current BTC/USD price from CoinGecko; fall back to Binance ticker."""
    try:
        r = requests.get(COINGECKO_URL, timeout=8)
        r.raise_for_status()
        price = float(r.json()["bitcoin"]["usd"])
        _record_price(price)
        return price
    except Exception:
        pass
    try:
        r = requests.get(BINANCE_TICKER, timeout=6)
        r.raise_for_status()
        price = float(r.json()["price"])
        _record_price(price)
        return price
    except Exception as e:
        log.warning(f"[1H BOT] BTC price fetch failed: {e}")
        return None


def _record_price(price: float):
    global _1h_prices
    ts = datetime.now(timezone.utc).isoformat()
    _1h_prices.append((ts, price))
    _1h_prices = _1h_prices[-MAX_PX_HISTORY:]


def _fetch_strike_price(open_ts: datetime) -> Optional[float]:
    """
    Fetch the Binance 1-hour kline open price for the candle starting at open_ts.
    This is the BTC price at market open — the strike for the Up/Down bet.
    """
    ts_ms = int(open_ts.timestamp() * 1000)
    try:
        r = requests.get(BINANCE_KLINES, params={
            "symbol": "BTCUSDT", "interval": "1h",
            "startTime": ts_ms, "limit": 1,
        }, timeout=8)
        r.raise_for_status()
        data = r.json()
        if data:
            strike = float(data[0][1])   # index 1 = open price
            log.info(f"[1H STRIKE] open_ts={open_ts.strftime('%H:%M')}Z  "
                     f"strike=${strike:,.2f}")
            return strike
    except Exception as e:
        log.warning(f"[1H BOT] Binance kline fetch failed: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# External signal adjustments (Gate.io)
# ══════════════════════════════════════════════════════════════════════════════

def _funding_adjustment() -> tuple:
    """
    Fetch BTC perpetual funding rate from Gate.io.
    Returns (rate_pct, prob_adjustment).
      Positive funding (longs pay shorts) → market overcrowded long → bearish adj −0.03
      Negative funding (shorts pay longs) → market overcrowded short → bullish adj +0.03
    """
    try:
        r = requests.get(GATE_FUNDING,
                         params={"contract": "BTC_USDT", "limit": 1},
                         timeout=8)
        r.raise_for_status()
        data = r.json()
        if not data:
            return 0.0, 0.0
        rate_pct = float(data[0]["r"]) * 100.0
        if rate_pct > 0.05:
            adj = -0.03
        elif rate_pct < -0.05:
            adj = +0.03
        else:
            adj = 0.0
        log.info(f"[1H FUNDING] rate={rate_pct:+.4f}%  adj={adj:+.2f}")
        return rate_pct, adj
    except Exception as e:
        log.warning(f"[1H BOT] Gate.io funding fetch failed: {e} — neutral 0.0")
        return 0.0, 0.0


def _oi_adjustment(current_price: float) -> float:
    """
    Fetch BTC_USDT perpetual open interest from Gate.io.
    Compare to previous reading; return probability adjustment.
      OI rising + price rising  → real trend confirmed   → +0.02
      OI rising + price falling → real selling pressure  → −0.02
      OI falling or flat        → noise                  → 0.0
    """
    global _prev_oi
    try:
        r = requests.get(GATE_CONTRACTS,
                         params={"contract": "BTC_USDT"},
                         timeout=8)
        r.raise_for_status()
        data = r.json()
        contracts = data if isinstance(data, list) else [data]
        btc_contract = next(
            (c for c in contracts if c.get("name") == "BTC_USDT"), None
        )
        if not btc_contract:
            return 0.0
        oi = float(btc_contract.get("open_interest", 0) or 0)
    except Exception as e:
        log.warning(f"[1H BOT] Gate.io OI fetch failed: {e} — neutral")
        return 0.0

    adj = 0.0
    if _prev_oi is not None and _prev_oi > 0:
        oi_delta = oi - _prev_oi
        if oi_delta > 0:
            # OI rising: determine direction from price vs previous price reading
            if len(_1h_prices) >= 2:
                prev_p = _1h_prices[-2][1]
                if current_price > prev_p:
                    adj = +0.02
                elif current_price < prev_p:
                    adj = -0.02
        log.info(f"[1H OI] oi={oi:,.0f}  prev={_prev_oi:,.0f}  "
                 f"delta={oi-_prev_oi:+,.0f}  adj={adj:+.2f}")
    _prev_oi = oi
    return adj


# ══════════════════════════════════════════════════════════════════════════════
# Market detection
# ══════════════════════════════════════════════════════════════════════════════

def _parse_prices(price_str) -> List[float]:
    try:
        if isinstance(price_str, list):
            return [float(p) for p in price_str]
        return [float(p) for p in json.loads(price_str)]
    except Exception:
        return []


def _is_1h_btc(title: str, slug: str) -> bool:
    """True for BTC Up/Down markets — excludes 5-minute ranges."""
    tl = title.lower()
    sl = slug.lower()
    if "bitcoin" not in tl and "btc" not in tl:
        return False
    if "up" not in tl or "down" not in tl:
        return False
    # Exclude 5-minute slug
    if "-5m-" in sl:
        return False
    # Exclude 5-min title format "9:05AM-9:10AM" — colon-minute time ranges
    if re.search(r'\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M', title, re.IGNORECASE):
        return False
    # Exclude explicit "5 minute" / "5m" in title
    if re.search(r'\b5[\s-]?(min|minute|m)\b', tl):
        return False
    # Must contain an "NAM / NPM ET" hourly timestamp — filters out daily/weekly markets
    if not re.search(r'\d{1,2}[AP]M\s+ET', title, re.IGNORECASE):
        return False
    return True


def _parse_market_open(title: str, close_ts: datetime) -> Optional[datetime]:
    """
    Derive market open time from title + known close time.
    "Bitcoin Up or Down - June 1, 7PM ET" → close = close_ts → open = close - 1h
    """
    return close_ts - timedelta(hours=1)


_MONTH_MAP = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

def _parse_close_from_title(title: str) -> Optional[datetime]:
    """
    Parse close time (UTC) from title like "Bitcoin Up or Down - June 2, 7PM ET".

    Strategy:
      1. Parse the date from the title ("June 2" → June 2 of current year).
      2. Parse the hour ("7PM ET" → 19 in 24h).
      3. Apply EDT offset (UTC-4); close is 1 hour after the title hour.
      4. If raw_close_utc >= 24 the window crosses UTC midnight — use title_date + 1 day.
      5. If the resulting close_ts is already in the past → return None (market closed).

    Note: hardcodes EDT = UTC-4. In winter (EST = UTC-5) this offset needs updating.
    """
    now_utc = datetime.now(timezone.utc)

    # ── 1. Parse hour ────────────────────────────────────────────
    tm = re.search(r'(?<!\d)(?<!:)\b(\d{1,2})(AM|PM)\s+ET', title, re.IGNORECASE)
    if not tm:
        return None
    try:
        hr   = int(tm.group(1))
        ampm = tm.group(2).upper()
        if hr > 12:
            return None
        if ampm == "PM" and hr != 12:
            hr += 12
        elif ampm == "AM" and hr == 12:
            hr = 0
    except Exception:
        return None

    # ── 2. Parse date from title ("June 2", "May 31", …) ─────────
    dm = re.search(
        r'(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d{1,2})',
        title, re.IGNORECASE,
    )
    try:
        if dm:
            month = _MONTH_MAP[dm.group(1).lower()]
            day   = int(dm.group(2))
            year  = now_utc.year
            title_date = datetime(year, month, day, tzinfo=timezone.utc).date()
        else:
            title_date = now_utc.date()
    except (ValueError, KeyError):
        return None

    # ── 3. Compute close UTC time ─────────────────────────────────
    # EDT = UTC-4; close = title_hour + 1 (the market covers one full hour)
    EDT_OFFSET    = 4
    raw_close_utc = hr + EDT_OFFSET + 1   # un-modded, to detect midnight crossing
    close_utc_hr  = raw_close_utc % 24

    # ── 4. Midnight rollover ──────────────────────────────────────
    # If raw_close_utc >= 24 the closing falls in the next UTC calendar day
    close_date = title_date
    if raw_close_utc >= 24:
        close_date = (datetime(title_date.year, title_date.month, title_date.day,
                               tzinfo=timezone.utc) + timedelta(days=1)).date()

    close_ts = datetime(close_date.year, close_date.month, close_date.day,
                        close_utc_hr, 0, 0, tzinfo=timezone.utc)

    # ── 5. Skip already-closed markets ───────────────────────────
    if close_ts <= now_utc:
        return None

    return close_ts


def _find_1h_markets() -> List[dict]:
    """
    Query Gamma API for active BTC 1-hour Up/Down markets with 15–50 minutes
    remaining in the trading window.

    Returns a list of dicts:
      title, market_id, close_ts, open_ts, elapsed_sec, remaining_sec,
      yes_price, no_price, yes_token_id, no_token_id
    """
    now_utc  = datetime.now(timezone.utc)
    today    = now_utc.date().isoformat()
    tomorrow = (now_utc.date() + timedelta(days=1)).isoformat()

    search_params = [
        {"tag_slug": "crypto", "active": "true", "closed": "false",
         "end_date_min": today, "end_date_max": tomorrow, "limit": 100},
        {"tag_slug": "crypto", "active": "true", "closed": "false", "limit": 200},
    ]

    seen_ids: set = set()
    candidates: List[dict] = []

    for params in search_params:
        try:
            r = requests.get(f"{POLY_API}/events", params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            events = data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            log.warning(f"[1H BOT] Gamma API search failed: {e}")
            continue

        for ev in events:
            eid   = ev.get("id")
            title = ev.get("title") or ""
            slug  = ev.get("slug")  or ""

            if eid in seen_ids:
                continue
            seen_ids.add(eid)

            if not _is_1h_btc(title, slug):
                continue

            # Derive close time from title
            close_ts = _parse_close_from_title(title)
            if close_ts is None:
                log.info(f"[1H BOT] skip {title!r} — could not parse close time")
                continue

            open_ts       = _parse_market_open(title, close_ts)
            elapsed_sec   = (now_utc - open_ts).total_seconds()
            remaining_sec = (close_ts - now_utc).total_seconds()

            # Time gates
            if elapsed_sec < MIN_TIME_ELAPSED_MIN * 60:
                log.info(f"[1H BOT] skip {title!r} — only {elapsed_sec/60:.1f} min elapsed")
                continue
            if remaining_sec < MIN_TIME_REMAINING_MIN * 60:
                log.info(f"[1H BOT] skip {title!r} — only {remaining_sec/60:.1f} min remaining")
                continue
            if remaining_sec > MAX_TIME_REMAINING_MIN * 60:
                log.info(f"[1H BOT] skip {title!r} — {remaining_sec/60:.1f} min remaining (too early)")
                continue

            # Get prices from nested markets
            mkts = ev.get("markets", [])
            if not mkts:
                # Try full event detail fetch
                try:
                    detail = requests.get(f"{POLY_API}/events/{eid}", timeout=10).json()
                    mkts   = detail.get("markets", [])
                except Exception:
                    pass
            if not mkts:
                continue

            m      = mkts[0]
            prices = _parse_prices(m.get("outcomePrices", "[]"))

            if len(prices) < 2:
                try:
                    detail = requests.get(f"{POLY_API}/events/{eid}", timeout=10).json()
                    dm     = (detail.get("markets") or [None])[0]
                    if dm:
                        prices = _parse_prices(dm.get("outcomePrices", "[]"))
                        if len(prices) < 2:
                            ba = dm.get("bestAsk")
                            if ba is not None:
                                ya = float(ba)
                                prices = [ya, round(1.0 - ya, 4)]
                        m = dm
                except Exception:
                    pass

            if len(prices) < 2:
                continue

            yes_price = float(prices[0])
            no_price  = float(prices[1])

            if not (0.05 <= yes_price <= 0.95):
                continue

            # Liquidity check
            if m.get("liquidityNum", 0) == 0 and float(m.get("volume", 0) or 0) == 0:
                log.info(f"[1H BOT] skip {title!r} — no liquidity")
                continue

            raw_ids      = (m.get("clobTokenIds") or "").split(",")
            yes_token_id = raw_ids[0].strip('"[] ') if raw_ids else ""
            no_token_id  = raw_ids[1].strip('"[] ') if len(raw_ids) > 1 else ""

            candidates.append({
                "title":         title,
                "market_id":     m.get("id", eid),
                "open_ts":       open_ts,
                "close_ts":      close_ts,
                "elapsed_sec":   elapsed_sec,
                "remaining_sec": remaining_sec,
                "yes_price":     yes_price,
                "no_price":      no_price,
                "yes_token_id":  yes_token_id,
                "no_token_id":   no_token_id,
            })

    log.info(f"[1H BOT] Found {len(candidates)} qualifying 1-hour market(s)")
    return candidates


# ══════════════════════════════════════════════════════════════════════════════
# Trade state
# ══════════════════════════════════════════════════════════════════════════════

def _load_trades() -> List[dict]:
    if not TRADES_FILE.exists():
        return []
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("trades", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_trades(trades: List[dict]):
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2, default=str)


def _open_count() -> int:
    return sum(1 for t in _load_trades() if t.get("status") == "open")


def _daily_count() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(
        1 for t in _load_trades()
        if (t.get("opened_at") or "")[:10] == today
    )


def _resolve_trades(current_price: float):
    """
    Close any open 1h trades whose close_ts has passed.
    Resolution: BTC current price vs strike price.
      YES wins if current_price > strike_price
      NO  wins if current_price <= strike_price
    """
    trades  = _load_trades()
    now_utc = datetime.now(timezone.utc)
    changed = False

    for t in trades:
        if t.get("status") != "open":
            continue
        close_ts_str = t.get("close_ts")
        if not close_ts_str:
            continue
        try:
            close_ts = datetime.fromisoformat(str(close_ts_str).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now_utc < close_ts:
            continue

        strike    = t.get("strike_price", 0.0)
        direction = t.get("direction", "UP")
        won       = (current_price > strike) if direction == "YES" else (current_price <= strike)
        entry     = t.get("entry_price", 0.5)
        size      = t.get("size", TRADE_SIZE_USD)
        pnl       = round(size * (1.0 / entry - 1) if won else -size, 2)

        t.update({
            "status":     "won" if won else "lost",
            "won":        won,
            "pnl":        pnl,
            "btc_exit":   current_price,
            "closed_at":  now_utc.isoformat(),
        })

        mode_tag = "1H LIVE" if BTC_1H_LIVE_MODE else "1H PAPER"
        msg = (f"[{mode_tag}] {'WIN' if won else 'LOSS'} | "
               f"{direction} | strike=${strike:,.2f} exit=${current_price:,.2f} | "
               f"PnL:${pnl:+.2f}")
        log.info(msg)
        send_alert(msg, priority="normal" if won else "high")
        changed = True

    if changed:
        _save_trades(trades)


# ══════════════════════════════════════════════════════════════════════════════
# Live order (Polymarket CLOB)
# ══════════════════════════════════════════════════════════════════════════════

def _btc_1h_live_order(direction: str, market: dict,
                       price: float, size: float) -> str:
    """
    Place a FOK limit order on Polymarket CLOB.
    direction: "YES" or "NO"
    Returns order_id string on success, "" on failure.
    """
    if not _PRIVATE_KEY:
        log.warning("[1H LIVE] PRIVATE_KEY not set — cannot place order")
        return ""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
        from py_clob_client.constants import POLYGON

        creds  = ApiCreds(api_key=_POLY_KEY, api_secret=_POLY_SECRET,
                          api_passphrase=_POLY_PASSPHRASE)
        client = ClobClient("https://clob.polymarket.com", key=_PRIVATE_KEY,
                            chain_id=POLYGON, creds=creds,
                            signature_type=3, funder=_PROXY_WALLET)
        client.set_api_creds(client.create_or_derive_api_creds())

        token_id = market["yes_token_id"] if direction == "YES" else market["no_token_id"]
        args     = OrderArgs(token_id=token_id, price=price,
                             size=round(size / price, 4), side="BUY")
        resp     = client.create_and_post_order(args, OrderType.FOK)
        oid      = (resp or {}).get("orderID") or (resp or {}).get("id") or ""
        if oid:
            log.info(f"[1H LIVE] Order placed: {oid}")
        else:
            log.warning(f"[1H LIVE] Order response had no ID: {resp}")
        return oid or ""
    except Exception as e:
        log.error(f"[1H LIVE] Order placement failed: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# Core scan
# ══════════════════════════════════════════════════════════════════════════════

def _evaluate_market(market: dict, current_price: float,
                     hourly_vol: float, paper_test: bool = False) -> Optional[dict]:
    """
    Run the full fair-value calculation for one market.
    Returns a result dict (always) — caller decides whether to enter.
    """
    title         = market["title"]
    yes_price     = market["yes_price"]
    no_price      = market["no_price"]
    open_ts       = market["open_ts"]
    elapsed_sec   = market["elapsed_sec"]
    remaining_sec = market["remaining_sec"]

    # Strike price: BTC open price for this 1-hour candle
    strike = _fetch_strike_price(open_ts)
    if strike is None:
        log.warning(f"[1H BOT] Could not get strike for {title!r} — skip")
        return None

    # Base fair value
    base_fair_yes = _calc_fair_yes(current_price, strike, remaining_sec, hourly_vol)

    # Adjustments
    funding_rate, funding_adj = _funding_adjustment()
    oi_adj                    = _oi_adjustment(current_price)
    total_adj                 = funding_adj + oi_adj

    fair_yes = max(0.05, min(0.95, base_fair_yes + total_adj))
    gap      = round(fair_yes - yes_price, 4)

    if gap > 0:
        direction    = "YES"
        entry_price  = yes_price
        token_id_key = "yes_token_id"
    else:
        direction    = "NO"
        entry_price  = no_price
        token_id_key = "no_token_id"

    # Detailed log
    log.info(
        f"[1H BOT] {title}\n"
        f"  strike=${strike:,.2f}  current=${current_price:,.2f}  "
        f"elapsed={elapsed_sec/60:.0f}min  remaining={remaining_sec/60:.0f}min\n"
        f"  delta=${current_price-strike:+,.2f} ({(current_price-strike)/strike*100:+.3f}%)  "
        f"hourly_vol={hourly_vol:.3f}%  "
        f"remaining_vol={hourly_vol*math.sqrt(remaining_sec/3600):.3f}%\n"
        f"  z_score={(current_price-strike)/strike*100 / (hourly_vol*math.sqrt(remaining_sec/3600)):.3f}  "
        f"base_fair_yes={base_fair_yes:.4f}\n"
        f"  funding={funding_rate:+.4f}%  funding_adj={funding_adj:+.2f}  "
        f"oi_adj={oi_adj:+.2f}\n"
        f"  final_fair_yes={fair_yes:.4f}\n"
        f"  polymarket_yes={yes_price:.3f}  polymarket_no={no_price:.3f}\n"
        f"  gap={gap:+.4f}  {'-> ENTER ' + direction + ' @ ' + f'{entry_price:.3f}' if abs(gap) >= MIN_GAP else '-> NO TRADE (gap < ' + str(MIN_GAP) + ')'}"
    )

    return {
        "title":           title,
        "market_id":       market["market_id"],
        "direction":       direction,
        "entry_price":     entry_price,
        "token_id":        market[token_id_key],
        "strike_price":    strike,
        "btc_entry":       current_price,
        "yes_price":       yes_price,
        "no_price":        no_price,
        "fair_yes":        fair_yes,
        "base_fair_yes":   base_fair_yes,
        "gap":             gap,
        "hourly_vol":      hourly_vol,
        "funding_rate":    funding_rate,
        "funding_adj":     funding_adj,
        "oi_adj":          oi_adj,
        "elapsed_min":     round(elapsed_sec / 60, 1),
        "remaining_min":   round(remaining_sec / 60, 1),
        "close_ts":        market["close_ts"].isoformat(),
        "open_ts":         market["open_ts"].isoformat(),
    }


def run_1h_scan(paper_test: bool = False):
    """
    Main entry point — called every SCAN_INTERVAL_SEC from main.py.

    1. Fetch current BTC price (updates rolling history)
    2. Resolve any expired open trades
    3. Find active 1-hour markets in the 15–50 min window
    4. For each: calculate fair value, check gap, enter if qualifies
    """
    current_price = _fetch_btc_price()
    if current_price is None:
        log.warning("[1H BOT] No BTC price — skipping scan")
        return

    log.info(f"[1H BOT] BTC=${current_price:,.2f}  history={len(_1h_prices)} readings")

    # Resolve settled trades
    _resolve_trades(current_price)

    # Gate checks
    today_str = datetime.now(timezone.utc).date().isoformat()
    if not paper_test:
        if _open_count() >= MAX_OPEN_TRADES:
            log.info(f"[1H BOT] {MAX_OPEN_TRADES} trades open — skip scan")
            return
        if _daily_count() >= MAX_DAILY_TRADES:
            log.info(f"[1H BOT] {MAX_DAILY_TRADES} trades today — skip scan")
            return

    all_trades = _load_trades()

    # token_ids with an open position — primary dedup guard (survives restarts)
    open_token_ids: set = {
        t["token_id"] for t in all_trades
        if t.get("status") == "open" and t.get("token_id")
    }

    # market_ids already entered today — one entry per market per UTC day
    traded_market_ids: set = {
        t["market_id"] for t in all_trades
        if t.get("opened_at", "")[:10] == today_str and t.get("market_id")
    }

    hourly_vol = _realized_hourly_vol()
    markets    = _find_1h_markets()

    if not markets:
        log.info("[1H BOT] No qualifying 1-hour markets found this scan")
        return

    for market in markets:
        if not paper_test and market["market_id"] in traded_market_ids:
            log.info(f"[1H BOT] skip {market['title']!r} — already traded today")
            continue

        result = _evaluate_market(market, current_price, hourly_vol,
                                  paper_test=paper_test)
        if result is None:
            continue

        gap          = result["gap"]
        entry_price  = result["entry_price"]
        direction    = result["direction"]
        token_id     = result["token_id"]

        if abs(gap) < MIN_GAP:
            continue
        if not (MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE):
            log.info(f"[1H BOT] SKIP {direction} @ {entry_price:.3f} — "
                     f"outside [{MIN_ENTRY_PRICE}, {MAX_ENTRY_PRICE}] entry range")
            continue

        if paper_test:
            log.info(f"[1H PAPER-TEST] Would enter: {direction} @ {entry_price:.3f}  "
                     f"fair_yes={result['fair_yes']:.4f}  gap={gap:+.4f}")
            continue

        # token_id dedup — block if we already hold this position
        if token_id in open_token_ids:
            log.info(f"[1H BOT] Skip — already have open position on this market "
                     f"(token_id={token_id[:12]}...)")
            continue

        # Hard gate: recheck limits after evaluate
        if _open_count() >= MAX_OPEN_TRADES:
            log.info("[1H BOT] Open trade limit reached — stop entering")
            break
        if _daily_count() >= MAX_DAILY_TRADES:
            log.info("[1H BOT] Daily trade limit reached — stop entering")
            break

        _enter_trade(result)
        # Update local sets so further iterations in this scan see the new state
        open_token_ids.add(token_id)
        traded_market_ids.add(market["market_id"])


def _enter_trade(result: dict):
    """Record and (if live) execute a 1-hour trade."""
    direction   = result["direction"]
    entry_price = result["entry_price"]
    title       = result["title"]
    size        = TRADE_SIZE_USD

    mode_tag    = "1H LIVE" if BTC_1H_LIVE_MODE else "1H PAPER"
    poly_order_id = ""

    if BTC_1H_LIVE_MODE:
        market_stub = {
            "yes_token_id": result["token_id"] if direction == "YES" else "",
            "no_token_id":  result["token_id"] if direction == "NO"  else "",
        }
        poly_order_id = _btc_1h_live_order(direction, market_stub, entry_price, size)
        if not poly_order_id:
            log.warning("[1H LIVE] Order not confirmed — trade not recorded")
            return

    now       = datetime.now(timezone.utc).isoformat()
    trade_id  = f"btc1h_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    trade = {
        "id":             trade_id,
        "opened_at":      now,
        "close_ts":       result["close_ts"],
        "direction":      direction,
        "entry_price":    entry_price,
        "strike_price":   result["strike_price"],
        "btc_entry":      result["btc_entry"],
        "btc_exit":       None,
        "fair_yes":       result["fair_yes"],
        "base_fair_yes":  result["base_fair_yes"],
        "gap":            result["gap"],
        "yes_price_at_entry": result["yes_price"],
        "no_price_at_entry":  result["no_price"],
        "hourly_vol":     result["hourly_vol"],
        "funding_rate":   result["funding_rate"],
        "funding_adj":    result["funding_adj"],
        "oi_adj":         result["oi_adj"],
        "elapsed_min":    result["elapsed_min"],
        "remaining_min":  result["remaining_min"],
        "size":           size,
        "market_id":      result["market_id"],
        "market_title":   title,
        "token_id":       result["token_id"],
        "poly_order_id":  poly_order_id,
        "status":         "open",
        "won":            None,
        "pnl":            None,
    }

    trades = _load_trades()
    trades.append(trade)
    _save_trades(trades)

    msg = (
        f"{mode_tag} OPEN | {direction} @ ${result['btc_entry']:,.0f}\n"
        f"  Market: {title}\n"
        f"  Entry: {entry_price:.3f}  Strike: ${result['strike_price']:,.2f}\n"
        f"  Fair YES: {result['fair_yes']:.4f}  Market YES: {result['yes_price']:.3f}  "
        f"Gap: {result['gap']:+.4f}\n"
        f"  Vol: {result['hourly_vol']:.3f}%/h  "
        f"{result['elapsed_min']:.0f}min elapsed / {result['remaining_min']:.0f}min remaining\n"
        f"  Size: ${size:.2f}  Break-even WR: {entry_price*100:.1f}%"
    )
    log.info(msg)
    send_alert(msg, priority="high")


# ══════════════════════════════════════════════════════════════════════════════
# Diagnostic / paper-test mode
# ══════════════════════════════════════════════════════════════════════════════

def run_1h_paper_test(max_markets: int = 20):
    """
    Diagnostic mode: evaluate up to max_markets 1-hour markets without
    recording any trades. Logs fair value vs market price and whether
    the bot would enter.

    Run via: python main.py 1h-paper-test
    """
    log.info("=" * 60)
    log.info("[1H PAPER-TEST] Starting diagnostic scan")
    log.info(f"  MIN_GAP={MIN_GAP}  TIME_WINDOW={MIN_TIME_REMAINING_MIN}-"
             f"{MAX_TIME_REMAINING_MIN}min  ELAPSED_MIN={MIN_TIME_ELAPSED_MIN}min")
    log.info("=" * 60)

    current_price = _fetch_btc_price()
    if current_price is None:
        log.error("[1H PAPER-TEST] Cannot fetch BTC price — aborting")
        return

    hourly_vol = _realized_hourly_vol()
    markets    = _find_1h_markets()

    log.info(f"[1H PAPER-TEST] BTC=${current_price:,.2f}  "
             f"hourly_vol={hourly_vol:.3f}%  markets={len(markets)}")

    if not markets:
        log.info("[1H PAPER-TEST] No qualifying markets found. "
                 "Check time-of-day (markets exist at each full hour ET).")
        return

    results  = []
    would_enter = 0
    for i, market in enumerate(markets[:max_markets], 1):
        log.info(f"\n--- Market {i}/{min(len(markets), max_markets)} ---")
        result = _evaluate_market(market, current_price, hourly_vol, paper_test=True)
        if result is None:
            continue
        results.append(result)
        if abs(result["gap"]) >= MIN_GAP:
            if MIN_ENTRY_PRICE <= result["entry_price"] <= MAX_ENTRY_PRICE:
                would_enter += 1

    # Summary table
    log.info("\n" + "=" * 60)
    log.info("[1H PAPER-TEST] SUMMARY")
    log.info(f"  Markets evaluated : {len(results)}")
    log.info(f"  Would enter       : {would_enter}")
    log.info(f"  BTC price         : ${current_price:,.2f}")
    log.info(f"  Hourly vol        : {hourly_vol:.3f}%")
    log.info("")
    log.info(f"  {'Title':<42}  {'Fair':>6}  {'MktYES':>6}  {'Gap':>7}  {'Action'}")
    log.info("  " + "-" * 78)
    for r in results:
        action = (f"ENTER {r['direction']} @ {r['entry_price']:.3f}"
                  if abs(r["gap"]) >= MIN_GAP and
                     MIN_ENTRY_PRICE <= r["entry_price"] <= MAX_ENTRY_PRICE
                  else "skip")
        short_title = r["title"][-42:] if len(r["title"]) > 42 else r["title"]
        log.info(f"  {short_title:<42}  {r['fair_yes']:>6.4f}  "
                 f"{r['yes_price']:>6.3f}  {r['gap']:>+7.4f}  {action}")
    log.info("=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# Standalone run loop (called from main.py)
# ══════════════════════════════════════════════════════════════════════════════

def print_1h_status():
    """
    Print a clean status report for all 1h trades.
    Called via: python main.py 1h-status
    """
    from datetime import datetime, timezone
    trades = _load_trades()
    if not trades:
        print("No 1h trades recorded yet.")
        return

    now      = datetime.now(timezone.utc)
    settled  = [t for t in trades if t.get("won") is not None]
    open_t   = [t for t in trades if t.get("status") == "open"]
    wins     = [t for t in settled if t["won"]]
    losses   = [t for t in settled if not t["won"]]
    total_pnl = sum(t.get("pnl", 0) or 0 for t in settled)
    wr        = len(wins) / len(settled) * 100 if settled else 0

    print("=" * 65)
    print("  BTC 1-HOUR BOT — STATUS")
    print("=" * 65)
    print(f"  Total trades  : {len(trades)}  "
          f"(settled={len(settled)}  open={len(open_t)})")
    print(f"  Win / Loss    : {len(wins)} / {len(losses)}  ({wr:.1f}% WR)")
    print(f"  Total P&L     : ${total_pnl:+.2f}")
    if settled:
        print(f"  Per-trade avg : ${total_pnl/len(settled):+.2f}")
    print()

    if open_t:
        print("  OPEN POSITIONS")
        print(f"  {'Opened':16s}  {'Dir':3s}  {'Entry':5s}  {'Strike':>10s}  {'Closes':16s}  {'Fair':6s}  {'Gap':>7s}")
        print("  " + "-" * 72)
        for t in open_t:
            close_str = str(t.get("close_ts",""))[:16].replace("T"," ")
            print(f"  {str(t['opened_at'])[:16].replace('T',' '):16s}  "
                  f"{t['direction']:3s}  {t['entry_price']:.3f}  "
                  f"${t.get('strike_price',0):>9,.0f}  "
                  f"{close_str:16s}  "
                  f"{t.get('fair_yes',0):.4f}  "
                  f"{t.get('gap',0):>+7.4f}")
        print()

    print("  SETTLED TRADES")
    print(f"  {'Opened':16s}  {'Dir':3s}  {'Entry':5s}  {'Strike':>10s}  "
          f"{'BTC In':>8s}  {'BTC Out':>8s}  {'Gap':>7s}  {'Result':6s}  {'PnL':>7s}")
    print("  " + "-" * 95)
    today = now.date().isoformat()
    for t in settled:
        date_str  = str(t["opened_at"])[:16].replace("T", " ")
        is_today  = str(t["opened_at"])[:10] == today
        tag       = " <today>" if is_today else ""
        result    = "WIN " if t["won"] else "LOSS"
        pnl_str   = f"${t.get('pnl',0):+.2f}"
        btc_in    = f"${t.get('btc_entry',0):,.0f}"
        btc_out   = f"${t.get('btc_exit',0):,.0f}" if t.get("btc_exit") else "  —  "
        print(f"  {date_str:16s}  {t['direction']:3s}  {t['entry_price']:.3f}  "
              f"${t.get('strike_price',0):>9,.0f}  "
              f"{btc_in:>8s}  {btc_out:>8s}  "
              f"{t.get('gap',0):>+7.4f}  {result}  {pnl_str:>7s}{tag}")

    print()
    print(f"  Mode: {'LIVE' if BTC_1H_LIVE_MODE else 'PAPER'}  |  "
          f"Trades file: {TRADES_FILE}")
    print("=" * 65)


def run_1h_bot_loop(paper_test: bool = False):
    """
    Blocking run loop. main.py calls this for `python main.py 1h` and
    `python main.py 1h-paper-test`.
    """
    mode = "PAPER-TEST" if paper_test else ("LIVE" if BTC_1H_LIVE_MODE else "PAPER")
    log.info(f"[1H BOT] Starting — mode={mode}  interval={SCAN_INTERVAL_SEC}s")

    if paper_test:
        run_1h_paper_test()
        return

    try:
        while True:
            try:
                run_1h_scan()
            except Exception as e:
                log.error(f"[1H BOT] Scan error: {e}", exc_info=True)
            time.sleep(SCAN_INTERVAL_SEC)
    except KeyboardInterrupt:
        log.info("[1H BOT] Stopped by user (Ctrl+C)")
        print_1h_status()
