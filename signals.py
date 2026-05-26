import json, logging, math, re, requests, time
from typing import Dict, List, Optional
from config import (LOCATIONS, MONTH_NAMES, MARKET_CORRELATIONS, CFG, REGIME_CONFIG,
                    MIN_EV, MIN_PRICE, MAX_PRICE, MIN_BET, MAX_BET, KELLY_FRACTION,
                    MAX_BUCKET_DISTANCE, MIN_VOLUME, MIN_ENTRY_PRICE)
from state import get_recent_pattern

log = logging.getLogger("weatherbet")

POLY_API = "https://gamma-api.polymarket.com"
WEATHER_TAG_ID = "84"

# ── Trade finders ────────────────────────────────────────────
def parse_bucket(bucket_str):
    try:
        c = str(bucket_str).replace("°C", "").replace("°F", "")
        c = c.replace("or higher", "").replace("or lower", "").strip()
        nums = re.findall(r'-?\d+', c)
        return int(nums[0]) if nums else None
    except:
        return None

def _f_to_c(temp_f: float) -> float:
    return (temp_f - 32) * 5 / 9

def _bucket_celsius(bucket_str, raw_value) -> float:
    """Return bucket temperature in °C, converting from °F when the unit is present.
    raw_value is the already-parsed numeric value from parse_bucket()."""
    if "°F" in str(bucket_str):
        return _f_to_c(raw_value)
    return float(raw_value)

def _bucket_label(bucket_str, raw_value: int) -> str:
    """Return display label preserving original unit: '82°F' or '29°C'."""
    unit = "°F" if "°F" in str(bucket_str) else "°C"
    return f"{raw_value}{unit}"

# ── Probability and EV ───────────────────────────────────────
def bucket_probability(target, best, sigma):
    if best is None or not sigma:
        return 0.20
    lo = (target - 0.5 - best) / sigma
    hi = (target + 0.5 - best) / sigma
    def phi(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return max(0.01, min(0.99, phi(hi) - phi(lo)))

def get_sigma(city, calibration):
    # Prefer learned sigma (best_temp std dev) over ECMWF MAE, then location default
    for key in (f"{city}_best", f"{city}_ecmwf"):
        if key in calibration:
            return calibration[key].get("sigma", LOCATIONS[city].get("sigma", 2.0))
    return LOCATIONS[city].get("sigma", 2.0)

def calc_ev(p, price):
    if price <= 0 or price >= 1:
        return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price, balance):
    if price <= 0 or price >= 1:
        return MIN_BET
    b = 1.0 / price - 1.0
    f = max(0.0, (p * b - (1.0 - p)) / b) * KELLY_FRACTION
    bet = min(f * balance, MAX_BET)
    return round(max(bet, MIN_BET), 2)

# ── Regime and seasonality ───────────────────────────────────
def get_regime_multiplier(city, month):
    for regime, cfg in REGIME_CONFIG.get(city, {}).items():
        if month in cfg["months"]:
            return cfg["size"], cfg.get("note", "")
    return 0.5, "Default"

# ── Recency bias detector ────────────────────────────────────
def detect_recency_bias(city: str, market_outcomes: List[Dict], best_temp: float) -> Dict:
    """
    Detect if market is anchored to stale temperatures.
    Disabled by default (recency_bias_enabled=false in config) — help_rate was 0.0
    from 1 application; heuristic has no empirical basis.
    """
    if not CFG.get("recency_bias_enabled", True):
        return {"bias_detected": False, "adjustment": 0}

    pattern = get_recent_pattern(city, days=3)

    if pattern["avg"] is None:
        return {"bias_detected": False, "adjustment": 0}

    # Find the most liquid bucket near recent average
    recent_avg = pattern["avg"]
    market_mid = None

    for o in market_outcomes:
        bucket = parse_bucket(o["bucket"])
        if bucket and abs(bucket - recent_avg) <= 1:
            market_mid = o["price"]
            break

    if market_mid is None:
        return {"bias_detected": False, "adjustment": 0}

    # Simple heuristic: if forecast is >2°C from recent but market hasn't moved
    forecast_diff = best_temp - recent_avg
    if abs(forecast_diff) > 2:
        if market_mid > 0.3:
            log.info(f"RECENCY BIAS detected: recent_avg={recent_avg}, forecast={best_temp}, market_mid_price={market_mid}")
            return {
                "bias_detected": True,
                "adjustment": forecast_diff * 0.5,
                "recent_avg": recent_avg,
                "market_mid": market_mid,
            }

    return {"bias_detected": False, "adjustment": 0}

# ── Correlation exposure check ───────────────────────────────
def check_correlation_exposure(city: str, state: Dict) -> float:
    """
    NEW: Check current exposure to correlated markets.
    Returns recommended size multiplier (0.5 if overexposed).
    """
    exposures = state.get("market_exposures", {})
    current_exposure = sum(1 for v in exposures.values() if v > 0)

    if current_exposure == 0:
        return 1.0

    corr = MARKET_CORRELATIONS.get(city, {})
    correlated_exposure = sum(
        exposures.get(c, 0) * weight
        for c, weight in corr.items()
    )

    if correlated_exposure > 1.5:
        log.info(f"Correlation exposure high for {city}: {correlated_exposure:.2f}")
        return 0.5  # Reduce size

    return 1.0

# ── Polymarket API ───────────────────────────────────────────
def _build_event_slug(city, month, day, year, temp_type="highest"):
    prefix = LOCATIONS[city]["slug_prefix"]
    month_name = MONTH_NAMES[month]
    return f"{temp_type}-temperature-in-{prefix}-on-{month_name}-{day}-{year}"

def _parse_outcome_prices(price_str):
    try:
        if isinstance(price_str, list):
            return [float(p) for p in price_str]
        return json.loads(price_str)
    except:
        return []

def find_polymarket_event(city, month, day, year):
    loc = LOCATIONS[city]
    month_name = MONTH_NAMES[month]

    # Build list of slug prefixes to try — primary first, then alts
    primary = loc["slug_prefix"]
    alts    = loc.get("slug_prefix_alts", [])
    prefixes = list(dict.fromkeys([primary] + alts))  # deduplicated, order preserved

    for prefix in prefixes:
        slug = f"highest-temperature-in-{prefix}-on-{month_name}-{day}-{year}"
        log.info(f"Trying slug lookup: {slug}")
        try:
            r = requests.get(
                f"{POLY_API}/events",
                params={"slug": slug, "active": "true", "closed": "false"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                log.info(f"FOUND by slug: {data[0].get('title', '')}")
                return _normalize_event(data[0])

            r2 = requests.get(f"{POLY_API}/events/slug/{slug}", timeout=15)
            if r2.status_code == 200:
                event = r2.json()
                log.info(f"FOUND by slug path: {event.get('title', '')}")
                return _normalize_event(event)
        except Exception as e:
            log.warning(f"Slug lookup failed ({slug}): {e}")

    # Fallback 1: weather tag scan (title match)
    try:
        r = requests.get(
            f"{POLY_API}/events",
            params={"tag_id": WEATHER_TAG_ID, "active": "true", "closed": "false", "limit": 200},
            timeout=15,
        )
        data = r.json()
        events = data if isinstance(data, list) else data.get("data", [])

        city_name = loc["name"].lower()
        for event in events:
            title = (event.get("title") or "").lower()
            if city_name in title and month_name in title and str(day) in title:
                log.info(f"FOUND by tag scan: {event.get('title', '')}")
                return _normalize_event(event)
    except Exception as e:
        log.warning(f"Tag discovery failed: {e}")

    # Fallback 2: city-specific title search (catches slug mismatches like new york)
    search_terms = loc.get("title_search_terms", [loc["name"].lower()])
    try:
        r = requests.get(
            f"{POLY_API}/events",
            params={"active": "true", "closed": "false", "limit": 200},
            timeout=15,
        )
        data = r.json()
        events = data if isinstance(data, list) else data.get("data", [])
        for event in events:
            title = (event.get("title") or "").lower()
            if any(term in title for term in search_terms):
                if month_name in title and str(day) in title:
                    log.info(f"FOUND by title search: {event.get('title', '')}")
                    return _normalize_event(event)
    except Exception as e:
        log.warning(f"Title search fallback failed: {e}")

    log.info(f"No event found: {city} {month_name} {day}")
    return None

def _normalize_event(event):
    markets = event.get("markets", [])
    total_volume = sum(float(m.get("volume", 0) or 0) for m in markets)

    tokens = []
    for m in markets:
        prices = _parse_outcome_prices(m.get("outcomePrices", "[]"))
        outcomes = _parse_outcome_prices(m.get("outcomes", "[]"))
        bucket = m.get("groupItemTitle", "")
        if not bucket and outcomes:
            bucket = outcomes[0] if len(outcomes) > 0 else ""

        yes_price = prices[0] if len(prices) > 0 else 0

        tokens.append({
            "outcome": bucket,
            "price": yes_price,
            "token_id": m.get("clobTokenIds", "").split(",")[0].strip('"[] ') if m.get("clobTokenIds") else None,
            "market_id": m.get("id"),
        })

    event["tokens"] = tokens
    event["volume"] = total_volume
    event["id"] = event.get("id")

    log.info(f"  Event: {event.get('title')}")
    log.info(f"  Markets: {len(markets)}, Volume: ${total_volume:,.0f}")
    for t in tokens:
        log.info(f"    Bucket: {t['outcome']}, Yes Price: {t['price']}")

    return event

def get_market_outcomes(event):
    outcomes = []
    try:
        for token in event.get("tokens", []):
            outcomes.append({
                "bucket": str(token.get("outcome", "")),
                "price": float(token.get("price", 0)),
                "token_id": token.get("token_id"),
                "market_id": token.get("market_id"),
            })
    except Exception as e:
        log.warning(f"Outcomes parse failed: {e}")
    return outcomes

# ── Trade finders (enhanced with recency bias) ───────────────
# FUTURE FEATURE — hedge layer for high uncertainty days.
# When regime_uncertainty is high (precipitation disagreement > 20mm between ECMWF and GFS),
# consider buying tail bins at 2-3¢ as insurance.
# Do not implement yet — requires calibration data from 50+ resolved trades first.
def find_yes_trade(city, best_temp, outcomes, sigma, regime_mult, conf_mult,
                   balance, calibration, recency_adj=0):
    """Enhanced with recency bias adjustment."""
    target = round(best_temp + recency_adj) if best_temp is not None else None
    if target is None:
        return None

    best_trade, best_ev = None, MIN_EV

    log.info(f"YES search {city}: target={target}, sigma={sigma}, "
             f"regime={regime_mult}, conf={conf_mult}, recency_adj={recency_adj}")

    effective_temp = best_temp + recency_adj

    for o in outcomes:
        bucket = parse_bucket(o["bucket"])
        price = o["price"]

        if bucket is None:
            continue
        if price < MIN_PRICE or price > MAX_PRICE:
            continue
        if price < MIN_ENTRY_PRICE:
            continue

        bucket_c = _bucket_celsius(o["bucket"], bucket)  # °C for all comparisons
        if abs(bucket_c - effective_temp) > MAX_BUCKET_DISTANCE:
            continue

        p = bucket_probability(bucket_c, effective_temp, sigma)
        ev = calc_ev(p, price)

        if ev > best_ev:
            size = round(min(max(
                calc_kelly(p, price, balance) * regime_mult * conf_mult,
                MIN_BET), MAX_BET), 2)
            best_ev = ev
            best_trade = {
                "direction": "YES", "bucket": bucket,
                "bucket_label": _bucket_label(o["bucket"], bucket),
                "price": price,
                "p": round(p, 3), "ev": round(ev, 4), "size": size,
                "token_id": o.get("token_id"),
                "market_id": o.get("market_id"),
            }

    if best_trade:
        log.info(f"  SELECTED YES: bucket={best_trade['bucket']}, ev={best_trade['ev']:.4f}")
    else:
        log.info(f"  No YES trade (best_ev={best_ev:.4f})")

    return best_trade

def find_no_trade(city, best_temp, outcomes, sigma, regime_mult, conf_mult,
                  balance, calibration, recency_adj=0):
    """Enhanced with recency bias adjustment."""
    if not CFG.get("enable_no_trades", False):
        return None

    target = round(best_temp + recency_adj) if best_temp is not None else None
    if target is None:
        return None

    best_trade, best_ev = None, MIN_EV

    for o in outcomes:
        bucket = parse_bucket(o["bucket"])
        yes_price = o["price"]

        if bucket is None:
            continue

        bucket_c = _bucket_celsius(o["bucket"], bucket)  # °C for all comparisons
        if abs(bucket_c - target) <= 3:
            continue

        no_price = round(1.0 - yes_price, 3)
        if no_price < MIN_PRICE or no_price > MAX_PRICE:
            continue
        if no_price < MIN_ENTRY_PRICE:
            continue

        p_no = 1.0 - bucket_probability(bucket_c, best_temp + recency_adj, sigma)
        ev = calc_ev(p_no, no_price)

        if ev > best_ev and p_no > 0.80:
            size = round(min(max(
                calc_kelly(p_no, no_price, balance) * regime_mult * conf_mult,
                MIN_BET), MAX_BET), 2)
            best_ev = ev
            best_trade = {
                "direction": "NO", "bucket": bucket,
                "bucket_label": _bucket_label(o["bucket"], bucket),
                "yes_price": yes_price, "price": no_price,
                "p": round(p_no, 3), "ev": round(ev, 4), "size": size,
                "token_id": o.get("token_id"),
                "market_id": o.get("market_id"),
            }

    return best_trade


# ── Binary YES/NO market scanner ──────────────────────────────
_binary_cache: tuple = (0.0, [])
_BINARY_CACHE_TTL = 600  # 10 minutes — weather events open/close slowly
_city_binary_cache: dict = {}  # slug_prefix -> (timestamp, events)
_CITY_BINARY_CACHE_TTL = 300   # 5 minutes per city


def _fetch_weather_events_for_binary() -> list:
    global _binary_cache
    ts, events = _binary_cache
    if time.monotonic() - ts < _BINARY_CACHE_TTL:
        return events

    seen_ids: set = set()
    fresh: list = []

    def _merge(data):
        batch = data if isinstance(data, list) else data.get("data", [])
        added = 0
        for e in batch:
            eid = e.get("id")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                fresh.append(e)
                added += 1
        return added

    # Paginate through all active weather events using tag_id (only documented filter param)
    offset = 0
    limit = 500
    while True:
        try:
            r = requests.get(
                f"{POLY_API}/events",
                params={
                    "tag_id": WEATHER_TAG_ID,
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            n = _merge(data)
            log.info(f"[BINARY] Tag fetch offset={offset}: +{n} events ({len(fresh)} total)")
            # has_more flag for dict responses; treat full page as potentially more
            if isinstance(data, dict):
                if not data.get("has_more", False):
                    break
            elif n < limit:
                break
            offset += limit
        except Exception as e:
            log.warning(f"[BINARY] Tag fetch failed at offset={offset}: {e}")
            break

    # Pre-filter: keep only binary-shaped events to avoid scanning multi-bucket markets
    binary_only = [e for e in fresh if _is_binary_event(e)]
    log.info(f"[BINARY] Cache: {len(fresh)} fetched → {len(binary_only)} binary-shaped events")
    _binary_cache = (time.monotonic(), binary_only)
    return binary_only


def _fetch_city_binary_events(slug_prefix: str, month: int = 0, day: int = 0,
                               year: int = 0, forecast_temp: float = 0.0) -> list:
    """
    Slug-based lookup for single-degree binary markets for a specific city+date.
    Binary markets like 'Will the highest temperature in Seoul be 17°C on May 21?'
    have predictable slugs — we try forecast_temp ±3°C via /events?slug= (documented endpoint).
    """
    global _city_binary_cache
    cache_key = f"{slug_prefix}-{year}-{month}-{day}"
    ts, events = _city_binary_cache.get(cache_key, (0.0, []))
    if time.monotonic() - ts < _CITY_BINARY_CACHE_TTL:
        return events

    fresh: list = []
    seen_ids: set = set()
    month_name = MONTH_NAMES[month] if month else ""

    if not (slug_prefix and month_name and day and year and forecast_temp):
        _city_binary_cache[cache_key] = (time.monotonic(), fresh)
        return fresh

    base_temp = round(forecast_temp)
    for temp_offset in range(-3, 4):
        temp = base_temp + temp_offset
        # Try with and without year suffix — Polymarket uses both patterns
        slug_candidates = [
            f"will-the-highest-temperature-in-{slug_prefix}-be-{temp}c-on-{month_name}-{day}-{year}",
            f"will-the-highest-temperature-in-{slug_prefix}-be-{temp}c-on-{month_name}-{day}",
        ]
        for slug in slug_candidates:
            try:
                r = requests.get(
                    f"{POLY_API}/events",
                    params={"slug": slug, "active": "true", "closed": "false"},
                    timeout=10,
                )
                r.raise_for_status()
                data = r.json()
                batch = data if isinstance(data, list) else data.get("data", [])
                for e in batch:
                    eid = e.get("id")
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        fresh.append(e)
                        log.info(f"[BINARY-SLUG] Found: {e.get('title')!r}")
                if batch:
                    break  # found this temp, skip alt slug pattern
            except Exception as exc:
                log.debug(f"[BINARY-SLUG] {slug}: {exc}")

    log.info(f"[BINARY-SLUG] {slug_prefix} {month_name} {day}: {len(fresh)} binary events found")
    _city_binary_cache[cache_key] = (time.monotonic(), fresh)
    return fresh


def _is_binary_event(event: dict) -> bool:
    """True if the event is a single YES/NO market, not a multi-bucket."""
    markets = event.get("markets", [])
    if len(markets) == 1:
        outcomes = _parse_outcome_prices(markets[0].get("outcomes", "[]"))
        return len(outcomes) == 2
    return len(markets) == 2


# Detects: "or higher", "or above", "at least", "exceed", standalone "above"
_HIGHER_RE = re.compile(
    r'\bor\s+(?:higher|above|more)\b|\bexceed|\bat\s+least\b|\babove\b', re.IGNORECASE
)
# Detects: "or below", "or lower", "or less", "at most", standalone "below", "under"
_BELOW_RE = re.compile(
    r'\bor\s+(?:below|lower|less)\b|\bat\s+most\b|\bbelow\b|\bunder\b', re.IGNORECASE
)


def _parse_binary_condition(title: str) -> Optional[tuple]:
    """
    Parse (target_temp_celsius, condition) from a binary market title.
    condition: 'exact' | 'or_higher' | 'or_below'
    Returns None when no temperature can be extracted.
    Handles explicit °C / °F and bare numbers (>50 assumed °F for US markets).
    """
    condition = ("or_higher" if _HIGHER_RE.search(title)
                 else "or_below" if _BELOW_RE.search(title)
                 else "exact")

    # Explicit degree symbol with unit: 30°C / 30°F / 86 °F
    m = re.search(r'(\d+(?:\.\d+)?)\s*°\s*([cf])\b', title, re.IGNORECASE)
    if m:
        val, unit = float(m.group(1)), m.group(2).lower()
        temp_c = round((val - 32) * 5 / 9, 1) if unit == 'f' else val
        return (temp_c, condition)

    # Bare number preceded by "be" and followed by condition words or "on"
    m = re.search(
        r'\bbe\s+(\d+(?:\.\d+)?)\s*(?:degrees?\s*)?'
        r'(?:or\s+(?:higher|above|below|lower|more|less)|celsius|fahrenheit|on\b)',
        title, re.IGNORECASE
    )
    if m:
        val = float(m.group(1))
        temp_c = round((val - 32) * 5 / 9, 1) if val > 50 else val
        return (temp_c, condition)

    # Fallback: any number next to "degrees" or "°"
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:degrees?|°)', title, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        temp_c = round((val - 32) * 5 / 9, 1) if val > 50 else val
        return (temp_c, condition)

    return None


def _threshold_probability(threshold: float, best: float, sigma: float,
                            condition: str) -> float:
    """
    Probability for a threshold binary market.
    'or_higher' → P(temp ≥ threshold)
    'or_below'  → P(temp ≤ threshold)
    'exact'     → bucket_probability ±0.5°C
    """
    def phi(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    if condition == "or_higher":
        return max(0.01, min(0.99, 1.0 - phi((threshold - best) / sigma)))
    if condition == "or_below":
        return max(0.01, min(0.99, phi((threshold - best) / sigma)))
    # exact
    return bucket_probability(int(round(threshold)), best, sigma)


def find_binary_yesno_markets(city: str, month: int, day: int, year: int,
                               best_temp: float, sigma: float,
                               regime_mult: float, conf_mult: float,
                               balance: float, calibration: dict,
                               recency_adj: float = 0) -> List[dict]:
    """
    Search Polymarket for single YES/NO temperature markets matching city+date.
    Handles three market types:
      exact     — "Will temp be X°C on [date]?"
      or_higher — "Will temp be X°C or higher on [date]?"
      or_below  — "Will temp be X°C or below on [date]?"
    Returns conviction candidate dicts (mkt=None, market_type='binary').
    """
    if best_temp is None or conf_mult == 0.0:
        return []

    city_loc       = LOCATIONS[city]
    city_name      = city_loc["name"].lower()
    slug_prefix    = city_loc.get("slug_prefix", "")
    month_name     = MONTH_NAMES[month]
    date_str       = f"{year}-{month:02d}-{day:02d}"
    effective_temp = best_temp + recency_adj
    candidates: List[dict] = []

    # Merge shared tag-paginated cache with city-specific slug-based fetch
    shared_events = _fetch_weather_events_for_binary()
    city_events   = _fetch_city_binary_events(slug_prefix, month, day, year, effective_temp)
    seen_ids: set = set()
    all_events: list = []
    for _e in shared_events + city_events:
        _eid = _e.get("id")
        if _eid and _eid not in seen_ids:
            seen_ids.add(_eid)
            all_events.append(_e)

    city_binary_found = 0  # track city-matching binary markets for summary log

    # Debug: show what the cache actually contains
    log.info(f"[BINARY] Cache size: {len(shared_events)} shared + {len(city_events)} city-specific = {len(all_events)} total")
    for _i, _e in enumerate(all_events[:10]):
        _mkts = _e.get("markets", [])
        log.info(f"  [{_i}] title={_e.get('title')!r}  slug={_e.get('slug')!r}  markets={len(_mkts)}")

    for event in all_events:
        title_raw = event.get("title") or ""
        title     = title_raw.lower()
        slug      = (event.get("slug") or "").lower()

        if city_name not in title and slug_prefix not in slug:
            continue
        if month_name not in title or str(day) not in title:
            continue
        if "temperature" not in title and "temp" not in title:
            continue
        if not _is_binary_event(event):
            continue

        parsed = _parse_binary_condition(title)
        if parsed is None:
            log.info(f"[BINARY] No temp parsed: {title_raw}")
            continue
        target_temp, condition = parsed

        markets   = event.get("markets", [])
        yes_price = None
        token_id  = None
        market_id = None

        for m in markets:
            prices = _parse_outcome_prices(m.get("outcomePrices", "[]"))
            if prices:
                yes_price = float(prices[0])
                market_id = m.get("id")
                token_id  = (m.get("clobTokenIds", "").split(",")[0].strip('"[] ')
                             if m.get("clobTokenIds") else None)
                break
            if yes_price is None and m.get("bestAsk"):
                try:
                    yes_price = float(m["bestAsk"])
                    market_id = m.get("id")
                except (ValueError, TypeError):
                    pass

        if yes_price is None:
            continue

        no_price_pre = round(1.0 - yes_price, 4)
        vol = sum(float(m.get("volume", 0) or 0) for m in markets)

        # Always log every city-matching binary market found (before volume filter)
        city_binary_found += 1
        log.info(
            f"[BINARY-{city_name.upper()}] {title_raw} | "
            f"YES={yes_price:.3f} NO={no_price_pre:.3f} vol={vol:.0f}"
        )

        if vol < MIN_VOLUME:
            log.info(f"[BINARY] Low volume ({vol:.0f}): {title_raw}")
            continue

        no_price  = no_price_pre  # already computed above
        p_yes     = _threshold_probability(target_temp, effective_temp, sigma, condition)
        p_no      = 1.0 - p_yes
        cond_tag  = {"exact": "=", "or_higher": "≥", "or_below": "≤"}.get(condition, "?")

        log.info(f"[BINARY] {title_raw} | {cond_tag}{target_temp}°C "
                 f"best={effective_temp:.1f}°C | YES={yes_price:.3f} NO={no_price:.3f} "
                 f"p_yes={p_yes:.3f}")

        def _make_cand(direction, price, p,
                       _city=city, _date=date_str, _tid=token_id,
                       _mid=market_id, _title=title_raw,
                       _eid=event.get("id"), _bucket=int(round(target_temp)),
                       _cond=condition):
            size = round(min(max(
                calc_kelly(p, price, balance) * regime_mult * conf_mult,
                MIN_BET), MAX_BET), 2)
            return {
                "city":     _city,
                "date_str": _date,
                "mkt":      None,
                "trade": {
                    "direction":    direction,
                    "bucket":       _bucket,
                    "bucket_label": f"{_bucket}°C",
                    "price":        price,
                    "p":            round(p, 3),
                    "ev":           round(calc_ev(p, price), 4),
                    "size":         size,
                    "token_id":     _tid if direction == "YES" else None,
                    "market_id":    _mid,
                    "market_type":  "binary",
                    "binary_cond":  _cond,
                    "market_title": _title,
                    "event_id":     _eid,
                },
            }

        if MIN_PRICE <= yes_price <= MAX_PRICE:
            ev_yes = calc_ev(p_yes, yes_price)
            if ev_yes > MIN_EV:
                candidates.append(_make_cand("YES", yes_price, p_yes))
                log.info(f"  [BINARY YES] {cond_tag}{target_temp}°C EV={ev_yes:.4f} p={p_yes:.3f}")

        if MIN_PRICE <= no_price <= MAX_PRICE and p_no > 0.70:
            ev_no = calc_ev(p_no, no_price)
            if ev_no > MIN_EV:
                candidates.append(_make_cand("NO", no_price, p_no))
                log.info(f"  [BINARY NO] {cond_tag}{target_temp}°C EV={ev_no:.4f} p={p_no:.3f}")

    log.info(
        f"[BINARY-{city_name.upper()}] Scan complete — "
        f"{city_binary_found} binary markets found, {len(candidates)} candidates"
    )
    return candidates
