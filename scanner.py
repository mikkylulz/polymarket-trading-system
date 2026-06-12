import json, logging, math, requests, time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from config import (LOCATIONS, ACTIVE_CITIES, SHADOW_CITIES, LIVE_CITIES,
                    MIN_HOURS, MAX_HOURS, MIN_VOLUME,
                    BALANCE, STOP_LOSS, TP1_PCT, TP1_SIZE, TP2_PCT, PAPER_MODE,
                    VC_KEY, MARKETS_DIR, RAPID_SCAN_CITIES, MAX_BUCKET_DISTANCE,
                    RAPID_END_UTC, MARKET_CORRELATIONS)
from state import (load_state, save_state, load_market, save_market, new_market,
                   load_calibration, run_calibration, send_alert, update_historical)
from forecasts import (get_ecmwf, get_gfs, get_hrrr, get_jma, get_metar,
                       get_pressure_trend, get_hourly_peak, get_nws_discussion,
                       ASIAN_JMA_CITIES)
from scrapers import parse_nea_forecast, parse_hko_forecast, check_imd_heatwave
from signals import (get_regime_multiplier, get_sigma, detect_recency_bias,
                     check_correlation_exposure, parse_bucket, find_yes_trade,
                     find_no_trade, find_polymarket_event, get_market_outcomes,
                     POLY_API, calc_ev)
                     # find_binary_yesno_markets — paused; revisit once market structure is understood
from resolver import resolve_past_markets
from learning import get_kelly_multiplier

log = logging.getLogger("weatherbet")

# ── Forecast cache (rapid scan mode) ─────────────────────────
# ECMWF/GFS update every 6 hours; NEA/HKO update 2x/day.
# During 122s rapid scans we cache these for 30 minutes and only
# fetch METAR + Polymarket prices on every tick.
_forecast_cache: dict = {}
_CACHE_TTL = 1800  # 30 minutes

def _cache_get(city, date_str, source):
    entry = _forecast_cache.get((city, date_str, source))
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None

def _cache_set(city, date_str, source, data):
    _forecast_cache[(city, date_str, source)] = (time.monotonic(), data)

# ── Priority re-scan queue (in-memory, not persisted) ─────────
# Populated by BIG MOVE detection; drained by drain_priority_queue()
# which is called from main.py every 60 seconds during the sleep period.
_priority_queue:      dict  = {}      # city -> date_str of the triggering move
_priority_scan_times: dict  = {}      # city -> monotonic time of last priority scan
_PRIORITY_RATE_LIMIT: float = 3600.0  # max one priority re-scan per city per hour

# ── Conviction mode constants (config-driven) ─────────────────
from config import CFG as _CFG
CONVICTION_MIN_EV    = 0.15
# YES (buying a bucket) went 1/23 in the first 36 resolved trades while NO
# (fading a bucket) went 8/13 — demand substantially more edge on the YES side.
CONVICTION_MIN_EV_YES = 0.25
CONVICTION_SIZES     = _CFG.get("conviction_sizes", [8, 4, 3])
MAX_DAILY_TRADES     = _CFG.get("max_conviction_trades", 3)

# ── Low-price conviction gate ─────────────────────────────────
# Buckets below LOW_PRICE_THRESHOLD (e.g. 10–30¢) carry extreme payouts but
# require very tight forecast conditions to justify entry.  The old approach
# blocked all sub-35¢ YES trades; this replaces it with a confidence-based
# gate that allows entry when all four conditions are met simultaneously.
LOW_PRICE_THRESHOLD = 0.35   # prices below this trigger the stricter gate
LOW_PRICE_MIN_CONF  = 1.5    # model spread ≤ 1°C  (get_model_confidence → 1.5)
LOW_PRICE_MAX_SIGMA = 1.5    # calibrated forecast sigma ≤ 1.5°C
LOW_PRICE_MIN_EV    = 0.50   # strong edge required at extreme odds

# ── Conviction entry windows (UTC) ───────────────────────────
# New entries only during the hours the relevant exchange session is liquid.
_ASIAN_ENTRY_CITIES = frozenset([
    "hong_kong", "singapore", "tokyo", "seoul", "chengdu",
    "taipei", "shanghai", "shenzhen", "lucknow",
])
_US_ENTRY_CITIES = frozenset([
    "new_york", "chicago", "denver", "dallas", "atlanta", "seattle", "miami",
])
# 21:00–06:00 UTC for Asia (wraps midnight); 11:00–21:00 UTC for US.
# Other cities (wellington, buenos_aires, london, madrid) have no restriction.


# ── Enhanced model confidence ────────────────────────────────
def get_model_confidence(ecmwf_temp, gfs_temp, ecmwf_precip=None, gfs_precip=None):
    """Enhanced confidence with precipitation agreement."""
    if ecmwf_temp is None or gfs_temp is None:
        return 0.5

    spread = abs(ecmwf_temp - gfs_temp)

    # Base confidence on temperature spread
    if spread <= 1.0:
        conf = 1.5
    elif spread <= 2.0:
        conf = 1.0
    elif spread <= 3.0:
        conf = 0.5
    else:
        conf = 0.0

    # Reduce confidence if precipitation disagrees significantly
    if ecmwf_precip is not None and gfs_precip is not None:
        precip_spread = abs(ecmwf_precip - gfs_precip)
        if precip_spread > 10:  # One says dry, one says wet
            conf *= 0.5
            log.info(f"Precipitation disagreement: ECMWF={ecmwf_precip}mm, GFS={gfs_precip}mm")

    return conf

# ── Station bias with heatwave boost ─────────────────────────
def apply_station_bias(city, temp, heatwave_boost=0):
    """Enhanced bias with optional heatwave correction."""
    if temp is None:
        return None
    base_bias = LOCATIONS[city].get("bias", 0)
    total_bias = base_bias + heatwave_boost
    return round(temp + total_bias, 1)

# ── Seoul post-frontal tracker (FIXED) ───────────────────────
def get_seoul_frontal_adjustment(state, precip_mm=None, wind_gust=None):
    """
    FIXED: Now uses actual precipitation and wind data.
    Post-frontal sequence from session history:
    Day 1: -2°C, cloudy
    Day 2: -2°C, recovering
    Day 3: +3°C SPIKE (THE BIG TRADE)
    """
    day = state.get("seoul_front_day", 0)

    # Auto-detect frontal passage from precip/wind
    if precip_mm is not None and precip_mm > 5:
        if wind_gust and wind_gust > 40:
            # Strong front detected
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if state.get("seoul_front_date") != today:
                state["seoul_front_date"] = today
                state["seoul_front_day"] = 1
                log.info(f"Seoul front detected: precip={precip_mm}mm, gust={wind_gust}km/h")
                return -2, 1.0

    # Existing sequence
    if day == 0:
        return 0, 1.0
    elif day <= 2:
        return -2, 1.0
    elif day == 3:
        return +3, 2.0  # THE BIG TRADE - double size
    elif day == 4:
        return +1, 1.5
    else:
        return 0, 1.0

def update_seoul_front(state, precip_mm=None, wind_gust=None):
    """FIXED: Now accepts actual weather data."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if precip_mm and precip_mm > 5:
        if state.get("seoul_front_date") != today:
            state["seoul_front_date"] = today
            state["seoul_front_day"] = 1
            log.info(f"Seoul front detected: precip={precip_mm}mm")
    elif state.get("seoul_front_day", 0) > 0:
        last = state.get("seoul_front_date", today)
        try:
            days_since = (datetime.strptime(today, "%Y-%m-%d") -
                          datetime.strptime(last, "%Y-%m-%d")).days
            state["seoul_front_day"] = min(days_since + 1, 5)
        except:
            pass

# ── Enhanced forecast snapshot ───────────────────────────────
def take_forecast_snapshot(city, date_str, state, rapid_mode=False):
    """Comprehensive snapshot with all data sources.

    rapid_mode=True (01:00–06:00 UTC, 122s scans):
      - METAR: always fresh — stations update every ~30 min
      - ECMWF, GFS, pressure, NEA, HKO: 30-minute cache
        (model data refreshes every 6 hours; scraper data 2x/day)
    rapid_mode=False (normal hourly scan): all sources fetched fresh.
    """
    is_us_city    = LOCATIONS[city].get("agency") == "NWS"
    is_asian_city = city in ASIAN_JMA_CITIES

    if rapid_mode:
        ecmwf = _cache_get(city, date_str, "ecmwf")
        if ecmwf is None:
            ecmwf = get_ecmwf(city, date_str)
            _cache_set(city, date_str, "ecmwf", ecmwf)
            log.info(f"[CACHE MISS] ecmwf {city} {date_str}")
        gfs = _cache_get(city, date_str, "gfs")
        if gfs is None:
            gfs = get_gfs(city, date_str)
            _cache_set(city, date_str, "gfs", gfs)
            log.info(f"[CACHE MISS] gfs {city} {date_str}")
        if is_us_city:
            hrrr = _cache_get(city, date_str, "hrrr")
            if hrrr is None:
                hrrr = get_hrrr(city, date_str)
                _cache_set(city, date_str, "hrrr", hrrr)
                log.info(f"[CACHE MISS] hrrr {city} {date_str}")
        else:
            hrrr = {"temp": None, "precip": None, "weather_code": None}
        if is_asian_city:
            jma = _cache_get(city, date_str, "jma")
            if jma is None:
                jma = get_jma(city, date_str)
                _cache_set(city, date_str, "jma", jma)
                log.info(f"[CACHE MISS] jma {city} {date_str}")
        else:
            jma = {"temp": None, "precip": None, "weather_code": None}
        pressure = _cache_get(city, date_str, "pressure")
        if pressure is None:
            pressure = get_pressure_trend(city)
            _cache_set(city, date_str, "pressure", pressure)
            log.info(f"[CACHE MISS] pressure {city} {date_str}")
    else:
        ecmwf = get_ecmwf(city, date_str)
        gfs   = get_gfs(city, date_str)
        hrrr  = get_hrrr(city, date_str) if is_us_city    else {"temp": None, "precip": None, "weather_code": None}
        jma   = get_jma(city, date_str)  if is_asian_city else {"temp": None, "precip": None, "weather_code": None}
        pressure = get_pressure_trend(city)

    if rapid_mode:
        # METAR only matters for Seoul frontal detection and London pressure checks.
        # London is excluded from rapid_scan_cities, and Seoul frontal state is already
        # tracked in state.json between scans, so skipping METAR here is safe.
        metar = {}
    else:
        metar = get_metar(city)

    ecmwf_temp = ecmwf.get("temp")
    gfs_temp   = gfs.get("temp")
    hrrr_temp  = hrrr.get("temp")
    jma_temp   = jma.get("temp")
    ecmwf_precip = ecmwf.get("precip")
    gfs_precip   = gfs.get("precip")

    if ecmwf_temp is None and gfs_temp is None and hrrr_temp is None and jma_temp is None:
        log.warning(f"All models returned None for {city} {date_str} — skipping city this scan")
        return None

    # Agency-specific adjustments
    heatwave_boost = 0
    nea_data = {}
    hko_data = {}

    if city == "singapore":
        if rapid_mode:
            cached = _cache_get(city, date_str, "nea")
            if cached is None:
                nea_data = parse_nea_forecast(city)
                _cache_set(city, date_str, "nea", nea_data)
                log.info(f"[CACHE MISS] nea {city}")
            else:
                nea_data = cached
        else:
            nea_data = parse_nea_forecast(city)
    elif city == "hong_kong":
        if rapid_mode:
            cached = _cache_get(city, date_str, "hko")
            if cached is None:
                hko_data = parse_hko_forecast(city)
                _cache_set(city, date_str, "hko", hko_data)
                log.info(f"[CACHE MISS] hko {city}")
            else:
                hko_data = cached
        else:
            hko_data = parse_hko_forecast(city)
    elif city == "lucknow":
        imd_data = check_imd_heatwave(city)
        heatwave_boost = imd_data.get("boost", 0)

    # NWS forecast discussion — US cities only (agency=NWS); has its own 1-hour internal cache
    nws_data = {}
    if LOCATIONS[city].get("agency") == "NWS":
        if rapid_mode:
            nws_data = _cache_get(city, date_str, "nws") or {}
            if not nws_data:
                nws_data = get_nws_discussion(city)
                if nws_data:
                    _cache_set(city, date_str, "nws", nws_data)
        else:
            nws_data = get_nws_discussion(city)

    # Apply biases
    ecmwf_c = apply_station_bias(city, ecmwf_temp, heatwave_boost)
    gfs_c   = apply_station_bias(city, gfs_temp,   heatwave_boost)
    hrrr_c  = apply_station_bias(city, hrrr_temp,  heatwave_boost)
    jma_c   = apply_station_bias(city, jma_temp,   heatwave_boost)

    # Model confidence (ECMWF vs GFS spread; unchanged for all cities)
    conf = get_model_confidence(ecmwf_temp, gfs_temp, ecmwf_precip, gfs_precip)

    # Model spread gate — override conf=0.0 when 3-model ensemble disagrees beyond threshold.
    # Asian cities get 3.0°C tolerance (JMA/ECMWF/GFS can legitimately diverge more on
    # convective days); US cities get 2.5°C (HRRR/ECMWF/GFS tight-resolution agreement
    # is expected within that range for 0-24hr forecasts).
    _model_vals = [v for v in [ecmwf_c, gfs_c, hrrr_c, jma_c] if v is not None]
    if len(_model_vals) >= 2:
        _spread = max(_model_vals) - min(_model_vals)
        _spread_limit = 2.5 if is_us_city else 3.0
        if _spread > _spread_limit:
            conf = 0.0
            log.info(f"[MODEL CONFLICT] {city}: spread={_spread:.1f}°C > {_spread_limit}°C "
                     f"(ECMWF={ecmwf_c} GFS={gfs_c} HRRR={hrrr_c} JMA={jma_c}) "
                     f"— conf=0.0, skipping entry")

    # Seoul frontal adjustment (FIXED: uses actual precip)
    frontal_adj, frontal_size = 0, 1.0
    if city == "seoul":
        metar_precip = metar.get("precip", 0) or 0
        metar_gust = metar.get("wind_gust", 0) or 0
        frontal_adj, frontal_size = get_seoul_frontal_adjustment(
            state, precip_mm=metar_precip, wind_gust=metar_gust
        )
        update_seoul_front(state, ecmwf_precip, metar_gust)

    # Calculate best_temp
    # US cities: HRRR 0.50 + ECMWF 0.35 + GFS 0.15 (HRRR is dominant for 0-24hr CONUS)
    # Non-US cities: simple ECMWF/GFS mean (unchanged)
    if is_us_city:
        available = [(hrrr_c, 0.50), (ecmwf_c, 0.35), (gfs_c, 0.15)]
        present   = [(v, w) for v, w in available if v is not None]
        if present:
            total_w = sum(w for _, w in present)
            best = round(sum(v * w for v, w in present) / total_w + frontal_adj, 1)
            blend_note = "+".join(
                f"{int(w/total_w*100)}%{'HRRR' if v is hrrr_c else 'ECMWF' if v is ecmwf_c else 'GFS'}"
                for v, w in present
            )
            log.info(f"[HRRR BLEND] {city}: {blend_note} → best={best}°C "
                     f"(HRRR={hrrr_c} ECMWF={ecmwf_c} GFS={gfs_c})")
        else:
            best = None
    elif is_asian_city:
        available = [(jma_c, 0.40), (ecmwf_c, 0.40), (gfs_c, 0.20)]
        present   = [(v, w) for v, w in available if v is not None]
        if present:
            total_w = sum(w for _, w in present)
            best = round(sum(v * w for v, w in present) / total_w + frontal_adj, 1)
            _names  = {id(jma_c): "JMA", id(ecmwf_c): "ECMWF", id(gfs_c): "GFS"}
            blend_note = "+".join(
                f"{int(w/total_w*100)}%{_names.get(id(v), '?')}"
                for v, w in present
            )
            log.info(f"[JMA BLEND] {city}: {blend_note} → best={best}°C "
                     f"(JMA={jma_c} ECMWF={ecmwf_c} GFS={gfs_c})")
        else:
            best = None
    else:
        if ecmwf_c is not None and gfs_c is not None:
            best = round((ecmwf_c + gfs_c) / 2 + frontal_adj, 1)
        elif ecmwf_c is not None:
            best = round(ecmwf_c + frontal_adj, 1)
        elif gfs_c is not None:
            best = round(gfs_c + frontal_adj, 1)
        else:
            best = None

    # Singapore shower adjustment
    sg_adj = 0
    if city == "singapore" and nea_data:
        sg_adj = nea_data.get("temp_adj") or 0
        if sg_adj != 0 and best is not None:
            best = round(best + sg_adj, 1)

    # Hourly trend divergence — cached in rapid mode (ECMWF hourly updates every 6h)
    if rapid_mode:
        hourly_data = _cache_get(city, date_str, "hourly_peak")
        if hourly_data is None:
            hourly_data = get_hourly_peak(city, date_str)
            _cache_set(city, date_str, "hourly_peak", hourly_data)
            log.info(f"[CACHE MISS] hourly_peak {city} {date_str}")
    else:
        hourly_data = get_hourly_peak(city, date_str)
    hourly_peak_raw = hourly_data.get("peak")
    hourly_adj     = 0.0
    if hourly_peak_raw is not None and ecmwf_c is not None:
        hourly_peak_c = apply_station_bias(city, hourly_peak_raw, heatwave_boost)
        if hourly_peak_c is not None:
            divergence = hourly_peak_c - ecmwf_c
            if abs(divergence) > 1.5:
                log.warning(f"TREND DIVERGENCE {city} {date_str}: "
                            f"hourly_peak={hourly_peak_c}°C vs ECMWF_daily={ecmwf_c}°C "
                            f"(diff={divergence:+.1f}°C)")
                hourly_adj = round(divergence * 0.5, 1)
                if best is not None:
                    best = round(best + hourly_adj, 1)
                    log.info(f"Hourly trend adj applied: {city} best → {best}°C "
                             f"(hourly_adj={hourly_adj:+.1f}°C)")

    # Pressure check - skip if front approaching
    pressure_skip = False
    if pressure.get("falling") and city in ["seoul", "tokyo", "london"]:
        log.info(f"PRESSURE ALERT: {city} pressure falling {pressure['trend']:.1f}hPa - front approaching")
        if city == "seoul" and state.get("seoul_front_day", 0) == 0:
            pressure_skip = True

    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": date_str,
        "ecmwf_raw": ecmwf_temp,
        "gfs_raw": gfs_temp,
        "hrrr_raw": hrrr_temp,
        "jma_raw": jma_temp,
        "ecmwf_corrected": ecmwf_c,
        "gfs_corrected": gfs_c,
        "hrrr_corrected": hrrr_c,
        "jma_corrected": jma_c,
        "ecmwf_precip": ecmwf_precip,
        "gfs_precip": gfs_precip,
        "metar": metar,
        "pressure": pressure,
        "model_confidence": conf,
        "frontal_adj": frontal_adj,
        "frontal_size_mult": frontal_size,
        "sg_shower_adj": sg_adj,
        "sg_shower_type": nea_data.get("shower_type", "none"),
        "heatwave_boost": heatwave_boost,
        "hourly_peak": hourly_peak_raw,
        "hourly_adj": hourly_adj,
        "best_temp": best,
        "pressure_skip": pressure_skip,
        "nea_temp": nea_data.get("nea_temp"),
        "hko_temp": hko_data.get("hko_mid"),
        "nws_discussion": nws_data.get("text", "")[:500] if nws_data else "",
    }

    nws_snippet = nws_data.get("text", "")[:120].replace("\n", " ").strip() if nws_data else ""
    hrrr_note   = f" HRRR={hrrr_c}" if is_us_city    else ""
    jma_note    = f" JMA={jma_c}"   if is_asian_city else ""
    log.info(f"Snapshot {city} {date_str}: ECMWF={ecmwf_c} GFS={gfs_c}{hrrr_note}{jma_note} "
             f"hourly_peak={hourly_peak_raw} best={best}C "
             f"conf={conf} precip={ecmwf_precip}/{gfs_precip} "
             f"sg_adj={sg_adj} hw_boost={heatwave_boost} hourly_adj={hourly_adj:+.1f}"
             + (f" nws={nws_snippet!r}" if nws_snippet else ""))

    return snap

# ── Singapore morning check (enhanced) ───────────────────────
def singapore_morning_check(snap):
    """Enhanced with NEA data validation."""
    metar = snap.get("metar", {}).get("temp")
    best = snap.get("best_temp")
    nea_temp = snap.get("nea_temp")

    if best is None:
        log.info("SG morning check: no forecast data")
        return None

    # Validate against NEA if available
    if nea_temp and abs(best - nea_temp) > 3:
        log.info(f"SG validation: forecast {best} vs NEA {nea_temp}, using NEA")
        best = nea_temp

    if metar is None:
        return best

    if metar < 27:
        adjusted = min(best, max(metar + 3, 26))
        log.info(f"SG suppress: METAR={metar} best={best} -> {adjusted}")
        return adjusted
    elif metar > 29:
        adjusted = max(best, metar - 2)
        log.info(f"SG lift: METAR={metar} best={best} -> {adjusted}")
        return adjusted

    return best

# ── Auto-resolve (enhanced) ──────────────────────────────────
def auto_resolve(city, date_str, mkt):
    event_id = mkt.get("event_id")
    if not event_id:
        return mkt

    try:
        r = requests.get(f"{POLY_API}/events/{event_id}", timeout=10)
        data = r.json()

        if data.get("closed"):
            for token in data.get("tokens", []):
                if float(token.get("price", 0)) >= 0.95:
                    mkt["resolved_outcome"] = token.get("outcome")
                    mkt["status"] = "resolved"

            # Get actual temperature
            if VC_KEY and "YOUR" not in VC_KEY:
                loc = LOCATIONS[city]
                vc_url = (f"https://weather.visualcrossing.com/VisualCrossing"
                         f"WebServices/rest/services/timeline/"
                         f"{loc['lat']},{loc['lon']}/{date_str}/{date_str}"
                         f"?unitGroup=metric&include=days&key={VC_KEY}&contentType=json")
                vc_r = requests.get(vc_url, timeout=10)
                actual = vc_r.json()["days"][0].get("tempmax")
                mkt["actual_temp"] = actual

                # Update historical database
                for snap in mkt.get("forecast_snapshots", []):
                    forecast = snap.get("best_temp")
                    if forecast:
                        update_historical(city, date_str, actual, forecast)

            log.info(f"Resolved {city} {date_str}: {mkt['resolved_outcome']}")

    except Exception as e:
        log.warning(f"Auto-resolve failed {city}: {e}")

    return mkt

# ── Stale position detector ──────────────────────────────────
def clear_stale_positions():
    now = datetime.now(timezone.utc)
    cleared = 0

    for mp in MARKETS_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            if not m.get("position"):
                continue

            mkt_date = datetime.strptime(m["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            pos_age_hours = None

            if m["position"].get("opened_at"):
                opened = datetime.fromisoformat(m["position"]["opened_at"].replace("Z", "+00:00"))
                pos_age_hours = (now - opened).total_seconds() / 3600

            if mkt_date.date() < now.date() or (pos_age_hours and pos_age_hours > 48) or m.get("status") == "closed":
                log.info(f"CLEARING stale position: {m['city_name']} {m['date']}")
                m["position"] = None
                if m.get("status") != "closed":
                    m["status"] = "open"
                save_market(m)
                cleared += 1

        except Exception as e:
            log.warning(f"Stale check error {mp}: {e}")

    if cleared:
        log.info(f"Cleared {cleared} stale positions")
    return cleared

# ── Position management (enhanced) ───────────────────────────
def check_stops(mkt, current_price):
    pos = mkt.get("position")
    if not pos:
        return None
    entry = pos["price"]
    tp1 = pos.get("tp1_hit", False)
    if current_price <= entry * (1 - STOP_LOSS):
        return "stop_loss"
    if not tp1 and current_price >= TP1_PCT:
        return "take_profit_1"
    if tp1 and current_price >= TP2_PCT:
        return "take_profit_2"
    return None

def execute_trade(mkt, trade, balance):
    mode = "PAPER" if PAPER_MODE else "LIVE"
    blabel = trade.get("bucket_label") or f"{trade['bucket']}°C"
    msg = (f"{mode} OPEN | {mkt['city_name']} {mkt['date']}\n"
           f"  {trade['direction']} {blabel}\n"
           f"  Price:{trade['price']:.3f} Size:${trade['size']:.2f}"
           f" EV:{trade['ev']:.4f} P:{trade['p']:.1%}")
    log.info(msg)
    send_alert(msg, priority="high")

    mkt["position"] = {
        "direction": trade["direction"],
        "bucket": trade["bucket"],
        "bucket_label": trade.get("bucket_label") or f"{trade['bucket']}°C",
        "price": trade["price"],
        "size": trade["size"],
        "ev": trade["ev"],
        "p": trade["p"],
        "token_id": trade.get("token_id"),
        "market_id": trade.get("market_id"),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "tp1_hit": False,
    }
    return mkt

def close_position(mkt, current_price, reason):
    pos = mkt["position"]
    entry = pos["price"]
    size = pos["size"]

    # P&L formula: size × fraction × (exit / entry − 1)
    # Correct for binary markets where you buy shares at entry price.
    # stop_loss is the exception — it simplifies to −size × STOP_LOSS algebraically.

    if reason == "stop_loss":
        pnl = -size * STOP_LOSS
        msg = (f"CLOSED STOP_LOSS | {mkt['city_name']} {mkt['date']}\n"
               f"  Entry:{entry:.3f} Exit:{current_price:.3f} PnL:${pnl:+.2f}")
        log.info(msg)
        send_alert(msg, priority="high")
        mkt["pnl"] = round(pnl, 2)
        mkt["status"] = "closed"
        mkt["position"] = None
        return mkt

    elif reason == "take_profit_1":
        partial = size * TP1_SIZE * (TP1_PCT / entry - 1)
        mkt["position"]["tp1_hit"] = True
        mkt["pnl"] = round((mkt.get("pnl") or 0) + partial, 2)
        msg = f"TP1 HIT | {mkt['city_name']} {mkt['date']} | Entry:{entry:.3f} Exit:{TP1_PCT:.3f} PnL:${partial:+.2f}"
        log.info(msg)
        send_alert(msg)
        return mkt

    elif reason == "take_profit_2":
        pnl = size * (1 - TP1_SIZE) * (TP2_PCT / entry - 1)

    else:
        pnl = size * (current_price / entry - 1)

    prior = (mkt.get("pnl") or 0)
    total = prior + pnl
    msg = (f"CLOSED {reason.upper()} | {mkt['city_name']} {mkt['date']}\n"
           f"  Entry:{entry:.3f} Exit:{current_price:.3f} Leg:${pnl:+.2f} Total:${total:+.2f}")
    log.info(msg)
    send_alert(msg, priority="high" if total < 0 else "normal")

    mkt["pnl"] = round(total, 2)
    mkt["status"] = "closed"
    mkt["position"] = None
    return mkt

# ── Conviction mode helpers ───────────────────────────────────
def _city_conviction_eligible(city: str) -> bool:
    """
    Return False if a city has 5+ resolved conviction trades with win rate < 40%.
    Cities with fewer than 5 resolved trades always return True (insufficient data).
    """
    resolved = wins = 0
    for mp in MARKETS_DIR.glob(f"{city}_*.json"):
        try:
            m = json.loads(mp.read_text())
            if m.get("city") != city:
                continue
            if m.get("status") in ("resolved", "closed") and m.get("pnl") is not None:
                resolved += 1
                if m["pnl"] > 0:
                    wins += 1
        except Exception:
            continue

    if resolved < 5:
        return True

    win_rate = wins / resolved * 100
    if win_rate < 40.0:
        log.info(f"[CONVICTION SKIP] {city} — win rate {win_rate:.1f}% below 40% threshold "
                 f"({resolved} trades)")
        return False
    return True


def _city_in_entry_window(city: str, now: datetime) -> bool:
    """
    Return False when a new conviction entry would be outside the city group's
    defined UTC entry window. Existing open positions are unaffected.

    Asian cities : 21:00–06:00 UTC (wraps midnight)
    US cities    : 11:00–21:00 UTC
    All others   : always allowed
    """
    hour = now.hour
    if city in _ASIAN_ENTRY_CITIES:
        in_window = hour >= 21 or hour < 6
        if not in_window:
            log.info(f"[TIME GATE] {city} — outside Asian entry window "
                     f"(21:00-06:00 UTC), skipping new entry")
        return in_window
    if city in _US_ENTRY_CITIES:
        in_window = 11 <= hour < 21
        if not in_window:
            log.info(f"[TIME GATE] {city} — outside US entry window "
                     f"(11:00-21:00 UTC), skipping new entry")
        return in_window
    return True


def _session_start_utc() -> datetime:
    """Return the UTC datetime when the current trading session began.

    The daily limit resets at RAPID_END_UTC (06:00 UTC) — when the overnight
    rapid window closes and the normal trading day begins. Trades placed during
    the rapid window (01:00–06:00 UTC) belong to the PREVIOUS session and must
    not count against the current session's quota.

    Example (RAPID_END_UTC=6):
      Bot runs at 07:15 UTC May 20 → session started 06:00 UTC May 20.
      Trades at 00:53 and 04:22 UTC May 20 (rapid window) → before cutoff
      → NOT counted → slots=2 → bot can trade.
    """
    now = datetime.now(timezone.utc)
    cutoff = now.replace(hour=RAPID_END_UTC, minute=0, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff


def _parse_opened_at(entry: dict) -> Optional[datetime]:
    """Parse opened_at ISO string to a UTC-aware datetime. Returns None on failure."""
    raw = entry.get("opened_at") or ""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _count_today_trades() -> int:
    """Count primary positions entered since the current session started.

    'Session' resets at RAPID_END_UTC (06:00 UTC) each day. Rapid-window trades
    (01:00–06:00 UTC) belong to the overnight session; daytime trades belong to
    the current session. Position2 (repricing/drift) is excluded.
    Uses pos if still set (open or TP1-partial), falls back to snapshot when
    pos is cleared on full close — prevents double-counting after TP1.
    """
    session_start = _session_start_utc()
    count = 0
    for mp in MARKETS_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            pos  = m.get("position")
            snap = m.get("position_entry_snapshot")
            entry = pos if pos is not None else snap
            if not entry:
                continue
            dt = _parse_opened_at(entry)
            if dt is None:
                continue
            if dt >= session_start:
                log.info(f"  [daily-count] COUNTING {mp.name}  opened_at={entry.get('opened_at')}")
                count += 1
            else:
                log.debug(f"  [daily-count] skipping {mp.name}  opened_at={entry.get('opened_at')}")
        except Exception:
            pass
    log.info(f"[daily-count] session_start={session_start.strftime('%Y-%m-%d %H:%M UTC')}  trades_counted={count}")
    return count


def _apply_conviction_trades(candidates: list, balance: float, exposures: dict):
    """
    Conviction mode: rank all scan candidates by EV, enter at most 2 per day.

    Rank  Label                   Size
      1   CONVICTION TRADE #1     $10
      2   CONVICTION TRADE #2     $5

    Candidates below CONVICTION_MIN_EV (0.15) are silently dropped.
    If nothing qualifies, logs the 'no signal' message and alerts once.
    """
    trades_today = _count_today_trades()
    slots        = MAX_DAILY_TRADES - trades_today

    if slots <= 0:
        log.info(f"CONVICTION: {MAX_DAILY_TRADES} trades already entered this session — skipping")
        return

    log.info(f"CONVICTION: evaluating {len(candidates)} candidate(s), slots={slots}")
    eligible = []
    for c in candidates:
        trade     = c["trade"]
        direction = trade["direction"]
        ev        = trade["ev"]
        blabel    = trade.get("bucket_label") or f"{trade['bucket']}°C"
        best_temp = c.get("best_temp")

        # EV gate — asymmetric: fading buckets (NO) is the demonstrated edge;
        # buying buckets (YES) must clear a higher bar.
        min_ev = CONVICTION_MIN_EV if direction == "NO" else CONVICTION_MIN_EV_YES
        if ev < min_ev:
            log.info(f"  SKIP {c['city']} {direction} {blabel}: EV={ev:.4f} < {min_ev} (threshold)")
            continue

        # Low-price conviction gate — sub-35¢ entries allowed only when the
        # forecast is extremely tight: models agree within 1°C (conf ≥ 1.5),
        # calibrated sigma < 1.5°C, no model conflict, AND EV > 0.50.
        # This replaces the old hard YES price floor with a confidence-based filter
        # so Tokyo-style payouts (10–30¢) are reachable on high-conviction signals.
        if trade["price"] < LOW_PRICE_THRESHOLD:
            c_conf  = c.get("conf",  0.0)
            c_sigma = c.get("sigma", 99.0)
            skip_reason = None
            if c_conf == 0.0:
                skip_reason = f"model conflict (conf=0.0)"
            elif c_conf < LOW_PRICE_MIN_CONF:
                skip_reason = f"conf={c_conf:.2f} < {LOW_PRICE_MIN_CONF} required"
            elif c_sigma >= LOW_PRICE_MAX_SIGMA:
                skip_reason = f"sigma={c_sigma:.2f} >= {LOW_PRICE_MAX_SIGMA} max"
            elif ev < LOW_PRICE_MIN_EV:
                skip_reason = f"EV={ev:.4f} < {LOW_PRICE_MIN_EV} required at low price"
            if skip_reason:
                log.info(
                    f"[LOWPRICE SKIP] {c['city']} {direction} {blabel} "
                    f"@ {trade['price']:.3f} — {skip_reason}"
                )
                continue
            log.info(
                f"[LOWPRICE PASS] {c['city']} {direction} {blabel} "
                f"@ {trade['price']:.3f} — HIGH CONVICTION "
                f"conf={c_conf:.2f} sigma={c_sigma:.2f} EV={ev:.4f}"
            )

        # Distance gate — only for YES trades; NO trades are already directionally far
        # bucket_label carries the original unit; convert °F→°C before comparing to best_temp
        if direction == "YES":
            lbl = trade.get("bucket_label", "")
            bucket_c = (trade["bucket"] - 32) * 5 / 9 if "°F" in lbl else float(trade["bucket"])
            dist = abs(bucket_c - best_temp) if best_temp is not None else 0.0
            if dist > MAX_BUCKET_DISTANCE:
                log.info(
                    f"  SKIP {c['city']} {direction} {blabel}: "
                    f"dist={dist:.2f}°C > {MAX_BUCKET_DISTANCE} (bucket_c={bucket_c:.1f} best={best_temp:.1f}°C)"
                )
                continue

        log.info(f"  PASS {c['city']} {direction} {blabel}: EV={ev:.4f}")
        eligible.append(c)

    eligible.sort(key=lambda c: c["trade"]["ev"], reverse=True)

    if not eligible:
        msg = "No conviction trade today — no signal strong enough"
        log.info(f"CONVICTION: {msg}")
        send_alert(msg)
        return

    sizes  = CONVICTION_SIZES
    labels = [f"CONVICTION TRADE #{i+1}" for i in range(len(sizes))]

    for rank, cand in enumerate(eligible[:min(len(sizes), slots)]):
        trade     = cand["trade"]
        mkt       = cand["mkt"]
        city      = cand["city"]
        date_str  = cand["date_str"]
        size      = sizes[rank]
        label     = labels[rank]
        is_binary = trade.get("market_type") == "binary"

        # ── Correlation exposure Kelly adjustment ──────────────────
        corr         = MARKET_CORRELATIONS.get(city, {})
        corr_exposure = sum(exposures.get(c, 0) * w for c, w in corr.items())
        if corr_exposure > 4.0:
            kelly_corr = 0.25
            log.info(f"[KELLY ADJUSTED] {city} — correlation_exposure={corr_exposure:.2f} → kelly=0.25x")
        elif corr_exposure > 2.0:
            kelly_corr = 0.5
            log.info(f"[KELLY ADJUSTED] {city} — correlation_exposure={corr_exposure:.2f} → kelly=0.5x")
        else:
            kelly_corr = 1.0
        size = round(size * kelly_corr, 2)
        city_name = (mkt["city_name"] if mkt is not None
                     else LOCATIONS.get(city, {}).get("name", city))

        trade["size"] = size

        mode_tag = "PAPER" if PAPER_MODE else "LIVE"

        blabel = trade.get("bucket_label") or f"{trade['bucket']}°C"
        if is_binary:
            cond      = trade.get("binary_cond", "exact")
            cond_sym  = {"exact": "=", "or_higher": "≥", "or_below": "≤"}.get(cond, "?")
            cond_word = {"exact": "exact", "or_higher": "or higher", "or_below": "or below"}.get(cond, "")
            log.info(
                f"CONVICTION: {label} [YES/NO {cond_word.upper()}] | {city_name} {date_str}\n"
                f"  {trade['direction']} {cond_sym}{blabel}"
                f" @ {trade['price']:.3f}"
                f" | EV:{trade['ev']:.4f} | Size:${size:.2f} | {mode_tag}\n"
                f"  Market: {trade.get('market_title', '')}"
            )
            send_alert(
                f"{label} [YES/NO {cond_word.upper()}]\n"
                f"{city_name} {date_str}\n"
                f"{trade['direction']} {cond_sym}{blabel}"
                f" @ {trade['price']:.3f}\n"
                f"EV:{trade['ev']:.4f} | ${size:.2f} | {mode_tag}\n"
                f"{trade.get('market_title', '')}",
                priority="high",
            )
            exposures[city] = exposures.get(city, 0) + size
        else:
            log.info(
                f"CONVICTION: {label} | {city_name} {date_str}\n"
                f"  {trade['direction']} {blabel}"
                f" @ {trade['price']:.3f}"
                f" | EV:{trade['ev']:.4f} | Size:${size:.2f} | {mode_tag}"
            )
            send_alert(
                f"{label}\n"
                f"{city_name} {date_str}\n"
                f"{trade['direction']} {blabel}"
                f" @ {trade['price']:.3f}\n"
                f"EV:{trade['ev']:.4f} | Size:${size:.2f} | {mode_tag}",
                priority="high",
            )
            execute_trade(mkt, trade, balance)
            exposures[city] = exposures.get(city, 0) + size
            save_market(mkt)





# ── Priority re-scan queue drainer ───────────────────────────
def drain_priority_queue():
    """
    Called from the main sleep loop every 60 seconds.
    For each city in _priority_queue that is not rate-limited, runs a targeted
    single-city/single-date fresh scan (rapid_mode=False so ECMWF/GFS are not
    served from the 30-minute cache), then removes the city from the queue.
    Max one priority scan per city per hour (_PRIORITY_RATE_LIMIT).
    """
    if not _priority_queue:
        return
    now_mono = time.monotonic()
    for city, date_str in list(_priority_queue.items()):
        last = _priority_scan_times.get(city, 0.0)
        wait = _PRIORITY_RATE_LIMIT - (now_mono - last)
        if wait > 0:
            log.info(f"[PRIORITY QUEUE] {city} rate-limited — "
                     f"{wait:.0f}s until next priority scan allowed")
            continue
        _priority_scan_times[city] = now_mono
        del _priority_queue[city]
        try:
            scan_and_update(city_filter=[city], date_filter=date_str, rapid_mode=False)
            log.info(f"[PRIORITY SCAN] {city} triggered by BIG MOVE — scanned immediately")
        except Exception as e:
            log.error(f"[PRIORITY SCAN] {city} error: {e}", exc_info=True)


# ── Shadow mode ───────────────────────────────────────────────
# Cities without verified station calibration are scanned but never traded.
# The best candidate per city per date is logged to data/shadow_trades.json
# and graded against the actual temperature once the archive updates, so a
# city is promoted to active_cities on evidence (~15 resolved shadow trades
# with positive simulated P&L), not hope.
SHADOW_TRADES_FILE = MARKETS_DIR.parent / "shadow_trades.json"


def _load_shadow_trades() -> list:
    if not SHADOW_TRADES_FILE.exists():
        return []
    try:
        data = json.loads(SHADOW_TRADES_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_shadow_trades(trades: list):
    SHADOW_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SHADOW_TRADES_FILE.write_text(json.dumps(trades, indent=2))


def _shadow_bucket_hit(label: str, bucket_val: float, actual_c: float) -> bool:
    """Did the bucket resolve YES given the actual max temp (°C)?
    Mirrors resolver._match_bucket: half-up rounding in the bucket's own unit,
    with 'or below' / 'or higher' edge buckets."""
    actual = actual_c * 9 / 5 + 32 if "°F" in str(label) else actual_c
    rounded = math.floor(actual + 0.5)
    low = str(label).lower()
    if "or below" in low:
        return rounded <= bucket_val
    if "or higher" in low:
        return rounded >= bucket_val
    return rounded == bucket_val


def _resolve_shadow_trades():
    """Grade past-date shadow entries against actual temps ($1 flat stake)."""
    from resolver import fetch_actual_temp
    trades  = _load_shadow_trades()
    today   = datetime.now(timezone.utc).date().isoformat()
    changed = False
    for t in trades:
        if t.get("status") != "open" or t.get("date", "") >= today:
            continue
        actual = fetch_actual_temp(t["city"], t["date"])
        if not isinstance(actual, (int, float)):
            continue   # archive lags 2-5 days (or timed out) — retry next scan
        hit = _shadow_bucket_hit(t.get("bucket_label", ""), t["bucket"], actual)
        won = hit if t["direction"] == "YES" else not hit
        t["actual_temp"]    = actual
        t["forecast_error"] = (round(abs(t["best_temp"] - actual), 2)
                               if t.get("best_temp") is not None else None)
        t["won"]    = won
        t["pnl"]    = round(1.0 / t["price"] - 1.0, 2) if won else -1.0
        t["status"] = "resolved"
        changed = True
        log.info(f"[SHADOW RESOLVED] {t['city']} {t['date']} {t['direction']} "
                 f"{t.get('bucket_label')} @ {t['price']:.3f} — actual {actual}°C "
                 f"→ {'WIN' if won else 'LOSS'} (err {t['forecast_error']}°C)")
    if changed:
        _save_shadow_trades(trades)


def _run_shadow_scan(state, calibration, now):
    """Log (never trade) the best candidate per shadow city per date."""
    if not SHADOW_CITIES:
        return
    trades = _load_shadow_trades()
    logged = {(t.get("city"), t.get("date")) for t in trades}
    added  = 0
    for city in SHADOW_CITIES:
        if city not in LOCATIONS:
            continue
        for day_offset in (0, 1):
            target   = now + timedelta(days=day_offset)
            date_str = target.strftime("%Y-%m-%d")
            if (city, date_str) in logged:
                continue
            end_of_day = datetime(target.year, target.month, target.day,
                                  23, 59, 59, tzinfo=timezone.utc)
            hours_left = (end_of_day - now).total_seconds() / 3600
            if hours_left < MIN_HOURS or hours_left > MAX_HOURS:
                continue
            try:
                event = find_polymarket_event(city, target.month, target.day, target.year)
                if not event:
                    continue
                snap = take_forecast_snapshot(city, date_str, state, rapid_mode=False)
                if snap is None or snap.get("pressure_skip"):
                    continue
                best_temp = snap.get("best_temp")
                if best_temp is None:
                    continue
                conf_mult = snap.get("model_confidence", 1.0)
                outcomes  = get_market_outcomes(event)
                sigma     = get_sigma(city, calibration)
                yes_t = find_yes_trade(city, best_temp, outcomes, sigma, 1.0,
                                       conf_mult, BALANCE, calibration, 0)
                no_t  = find_no_trade(city, best_temp, outcomes, sigma, 1.0,
                                      conf_mult, BALANCE, calibration, 0)
                trade = max((t for t in (yes_t, no_t) if t),
                            key=lambda t: t["ev"], default=None)
                if not trade:
                    continue
                trades.append({
                    "city":         city,
                    "date":         date_str,
                    "direction":    trade["direction"],
                    "bucket":       trade["bucket"],
                    "bucket_label": trade.get("bucket_label") or f"{trade['bucket']}°C",
                    "price":        trade["price"],
                    "ev":           round(trade["ev"], 4),
                    "p":            round(trade["p"], 4),
                    "best_temp":    best_temp,
                    "sigma":        sigma,
                    "conf":         conf_mult,
                    "logged_at":    datetime.now(timezone.utc).isoformat(),
                    "status":       "open",
                    "actual_temp":  None,
                    "won":          None,
                    "pnl":          None,
                })
                logged.add((city, date_str))
                added += 1
                log.info(f"[SHADOW] {city} {date_str}: {trade['direction']} "
                         f"{trade.get('bucket_label')} @ {trade['price']:.3f} "
                         f"EV={trade['ev']:.4f} — logged, NO trade placed")
            except Exception as e:
                log.warning(f"[SHADOW] {city} {date_str} failed: {e}")
    if added:
        _save_shadow_trades(trades)
        log.info(f"[SHADOW] {added} new shadow candidate(s) logged")


# ── Main scan (enhanced) ─────────────────────────────────────
def scan_and_update(city_filter=None, date_filter=None, rapid_mode=None):
    """
    city_filter: list of city keys to restrict scanning (rapid or priority mode).
    date_filter: YYYY-MM-DD string; when set only that date is scanned per city.
    rapid_mode:  None → derived from city_filter (True when filter is set);
                 pass False explicitly for priority re-scans so fresh (uncached)
                 forecasts are fetched even though city_filter is set.
    """
    if rapid_mode is None:
        rapid_mode = city_filter is not None
    state = load_state()
    calibration = load_calibration()
    calibration = run_calibration(calibration)
    now = datetime.now(timezone.utc)
    month = now.month
    balance = BALANCE

    # Track exposures for correlation check
    exposures  = {}
    candidates = []   # conviction mode: collect all candidates before executing

    base_cities = ACTIVE_CITIES if PAPER_MODE else LIVE_CITIES
    cities = [c for c in base_cities if c in city_filter] if city_filter else base_cities
    for city in cities:
        if city not in LOCATIONS:
            continue

        log.info(f"--- {LOCATIONS[city]['name']} ---")

        for day_offset in range(0, 3):
            target_date = now + timedelta(days=day_offset)
            date_str = target_date.strftime("%Y-%m-%d")

            # Priority re-scan: skip every date except the one that triggered the alert
            if date_filter and date_str != date_filter:
                continue

            tgt_month = target_date.month
            tgt_day = target_date.day
            tgt_year = target_date.year

            end_of_day = datetime(target_date.year, target_date.month,
                                  target_date.day, 23, 59, 59,
                                  tzinfo=timezone.utc)
            hours_left = (end_of_day - now).total_seconds() / 3600

            if hours_left < MIN_HOURS or hours_left > MAX_HOURS:
                log.info(f"Skip {date_str}: hours_left={hours_left:.1f}")
                continue

            mkt = load_market(city, date_str)

            if mkt and mkt["status"] in ["resolved", "closed"]:
                save_market(mkt)
                continue

            if mkt and mkt["status"] == "open":
                mkt = auto_resolve(city, date_str, mkt)
                if mkt["status"] in ["resolved", "closed"]:
                    save_market(mkt)
                    continue

            event = find_polymarket_event(city, tgt_month, tgt_day, tgt_year)
            if not event:
                log.info(f"No event: {city} {date_str}")
                continue

            vol = float(event.get("volume", 0))
            if vol < MIN_VOLUME:
                log.info(f"Low volume {city}: ${vol:.0f} (min ${MIN_VOLUME})")
                continue

            if not mkt:
                mkt = new_market(city, date_str, event.get("id"), hours_left)

            # Take comprehensive snapshot
            snap = take_forecast_snapshot(city, date_str, state, rapid_mode=rapid_mode)
            if snap is None:
                log.warning(f"No model data for {city} {date_str} — skipping this scan")
                save_market(mkt)
                continue

            # Pressure skip check
            if snap.get("pressure_skip"):
                log.info(f"SKIPPING {city} {date_str}: front approaching, pressure falling")
                save_market(mkt)
                continue

            # Singapore morning check
            if city == "singapore" and (now.hour >= 22 or now.hour <= 5):
                checked = singapore_morning_check(snap)
                if checked is not None:
                    snap["best_temp"] = checked

            mkt["forecast_snapshots"].append(snap)
            best_temp = snap.get("best_temp")

            outcomes = get_market_outcomes(event)
            mkt["all_outcomes"] = outcomes

            # Recency bias detection
            recency = detect_recency_bias(city, outcomes, best_temp)
            recency_adj = recency.get("adjustment", 0)

            if recency.get("bias_detected"):
                log.info(f"RECENCY BIAS: adjusting best_temp by {recency_adj:.1f}C")
            mkt["last_recency_adj"] = recency_adj  # persisted for learning tracker

            # Market snapshots
            if mkt.get("market_snapshots"):
                last = mkt["market_snapshots"][-1]
                for o in outcomes:
                    b = parse_bucket(o["bucket"])
                    prev = next((x["price"] for x in last
                                if parse_bucket(x.get("bucket", "")) == b), None)
                    if prev and abs(o["price"] - prev) >= 0.15:
                        rel_move = abs(o["price"] - prev) / max(prev, 0.01)

                        # Classify the move by time-to-close and outcome price
                        if hours_left <= 6 and (o["price"] > 0.85 or o["price"] < 0.15):
                            move_class = "RESOLUTION"
                        elif hours_left > 12 and rel_move > 0.40:
                            move_class = "INFORMATION"
                        else:
                            move_class = "STANDARD"

                        if move_class == "RESOLUTION":
                            log.info(
                                f"[BIG MOVE RESOLUTION] {LOCATIONS[city]['name']} "
                                f"{o['bucket']}: {prev:.2f}->{o['price']:.2f} "
                                f"({hours_left:.1f}h to close, price={o['price']:.2f}) "
                                f"— resolution move, no re-scan"
                            )
                        else:
                            send_alert(
                                f"BIG MOVE | {LOCATIONS[city]['name']}\n"
                                f"{o['bucket']}: {prev:.2f}->{o['price']:.2f}",
                                priority="high",
                            )
                            if rel_move > 0.40 and city not in _priority_queue:
                                _priority_queue[city] = date_str
                                log.info(
                                    f"[PRIORITY QUEUE] {city} queued — BIG MOVE [{move_class}] "
                                    f"{o['bucket']}: {prev:.3f}→{o['price']:.3f} "
                                    f"({rel_move:.0%} relative, {hours_left:.1f}h to close)"
                                )

            mkt["market_snapshots"].append(
                [{"bucket": o["bucket"], "price": o["price"]} for o in outcomes]
            )

            # Regime and confidence
            regime_mult, regime_note = get_regime_multiplier(city, tgt_month)
            conf_mult = snap.get("model_confidence", 1.0)

            if regime_mult == 0.0:
                log.info(f"Regime SKIP {city}: {regime_note}")
                save_market(mkt)
                continue

            # NWS discussion confidence adjustment (US cities only)
            nws_text = (snap.get("nws_discussion") or "").lower()[:500]
            if nws_text:
                _HIGH_SIGNALS = [
                    "high confidence", "well above normal", "well below normal",
                    "models in good agreement", "good agreement",
                ]
                _LOW_SIGNALS = [
                    "uncertainty", "models disagree", "timing uncertain",
                    "low confidence", "poor agreement", "spread",
                ]
                _nws_hit = next((s for s in _HIGH_SIGNALS if s in nws_text), None)
                if _nws_hit:
                    conf_mult = min(2.0, conf_mult + 0.2)
                    log.info(f"[NWS] {city}: HIGH CONFIDENCE signal detected "
                             f"({_nws_hit!r}) — boosting conf to {conf_mult:.2f}")
                else:
                    _nws_hit = next((s for s in _LOW_SIGNALS if s in nws_text), None)
                    if _nws_hit:
                        conf_mult = max(0.0, conf_mult - 0.2)
                        log.info(f"[NWS] {city}: UNCERTAINTY signal detected "
                                 f"({_nws_hit!r}) — reducing conf to {conf_mult:.2f}")

            sigma = get_sigma(city, calibration)

            # Correlation exposure check
            corr_mult = check_correlation_exposure(city, state)

            # Learning feedback: halve Kelly when 7d win rate < 30%
            learn_mult = get_kelly_multiplier(city)
            if learn_mult < 1.0:
                log.info(f"[LEARNING] {city} 7d win rate below 30% — Kelly halved to 0.5x")
            total_size_mult = regime_mult * conf_mult * corr_mult * learn_mult

            # Check stops on existing position(s) — max 2 per city per date
            if mkt.get("position"):
                pos = mkt["position"]
                bucket = pos["bucket"]
                direction = pos["direction"]
                cur_p = None

                for o in outcomes:
                    b = parse_bucket(o["bucket"])
                    if b == bucket:
                        cur_p = o["price"]
                        if direction == "NO":
                            cur_p = round(1.0 - cur_p, 3)
                        break

                if cur_p is not None:
                    action = check_stops(mkt, cur_p)
                    if action == "stop_loss" and best_temp is not None:
                        # Price stops convert intraday repricing noise into realized
                        # losses (12 of the first 36 trades died this way). Honor a
                        # price stop only when the forecast itself has moved against
                        # the position — otherwise hold to resolution.
                        _lbl = pos.get("bucket_label", "")
                        _bucket_c = ((pos["bucket"] - 32) * 5 / 9
                                     if "°F" in _lbl else float(pos["bucket"]))
                        _fc_dist = abs(_bucket_c - best_temp)
                        _fc_against = (_fc_dist > 1.0 if pos["direction"] == "YES"
                                       else _fc_dist <= 1.0)
                        if not _fc_against:
                            log.info(
                                f"[STOP HELD] {city} {pos['direction']} {_lbl}: price hit stop "
                                f"({cur_p:.3f} vs entry {pos['price']:.3f}) but forecast "
                                f"{best_temp:.1f}°C (dist {_fc_dist:.1f}°C) still supports the "
                                f"position — holding to resolution"
                            )
                            action = None
                    if action:
                        # Snapshot preserved so learning tracker can access entry
                        # price and direction after close_position clears position.
                        mkt["position_entry_snapshot"] = dict(pos)
                        mkt = close_position(mkt, cur_p, action)
                        save_market(mkt)
                        save_state(state)
                        continue

                # Stops on position2 — handled inline (close_position only touches position)
                if mkt.get("position2"):
                    pos2 = mkt["position2"]
                    cur_p2 = None
                    for o in outcomes:
                        b = parse_bucket(o["bucket"])
                        if b == pos2["bucket"]:
                            cur_p2 = o["price"]
                            if pos2["direction"] == "NO":
                                cur_p2 = round(1.0 - cur_p2, 3)
                            break
                    if cur_p2 is not None:
                        action2 = check_stops({"position": pos2}, cur_p2)
                        if action2 == "stop_loss":
                            pnl2 = -pos2["size"] * STOP_LOSS
                            log.info(f"POS2 STOP LOSS {mkt['city_name']}: pnl=${pnl2:+.2f}")
                            send_alert(f"POS2 CLOSED stop_loss | {mkt['city_name']} {mkt['date']}"
                                       f" | PnL:${pnl2:+.2f}")
                            mkt["position2"] = None
                        elif action2 == "take_profit_1":
                            mkt["position2"]["tp1_hit"] = True
                            partial = pos2["size"] * TP1_SIZE * (TP1_PCT - pos2["price"])
                            send_alert(f"POS2 TP1 HIT | {mkt['city_name']} | +${partial:.2f}")
                        elif action2 == "take_profit_2":
                            pnl2 = pos2["size"] * (1 - TP1_SIZE) * (TP2_PCT - pos2["price"])
                            log.info(f"POS2 TP2 {mkt['city_name']}: pnl=${pnl2:+.2f}")
                            send_alert(f"POS2 CLOSED TP2 | {mkt['city_name']} {mkt['date']}"
                                       f" | PnL:${pnl2:+.2f}")
                            mkt["position2"] = None

                # Repricing: enter second position if primary is down 50%+ and cap not reached
                if (cur_p is not None and
                        cur_p <= pos["price"] * 0.50 and
                        not mkt.get("position2") and
                        best_temp is not None):
                    held_bucket = pos["bucket"]
                    last_snap = (mkt["market_snapshots"][-2]
                                 if len(mkt.get("market_snapshots", [])) >= 2
                                 else [])
                    last_prices = {parse_bucket(o.get("bucket", "")): o["price"]
                                   for o in last_snap}
                    # Only buckets that are: not currently held, moved ≥15¢ since last snapshot
                    repricing_candidates = [
                        o for o in outcomes
                        if parse_bucket(o["bucket"]) not in (None, held_bucket)
                        and abs(o["price"] - last_prices.get(
                            parse_bucket(o["bucket"]), o["price"])) >= 0.15
                    ]
                    if repricing_candidates:
                        yes_t2 = find_yes_trade(city, best_temp, repricing_candidates, sigma,
                                                total_size_mult, conf_mult, balance,
                                                calibration, recency_adj)
                        no_t2 = find_no_trade(city, best_temp, repricing_candidates, sigma,
                                              total_size_mult, conf_mult, balance,
                                              calibration, recency_adj)
                        trade2 = None
                        if yes_t2 and no_t2:
                            trade2 = yes_t2 if yes_t2["ev"] > no_t2["ev"] else no_t2
                        elif yes_t2:
                            trade2 = yes_t2
                        elif no_t2:
                            trade2 = no_t2

                        if trade2:
                            mode_tag = "PAPER" if PAPER_MODE else "LIVE"
                            t2_lbl  = trade2.get("bucket_label") or f"{trade2['bucket']}°C"
                            pos_lbl = pos.get("bucket_label") or f"{pos['bucket']}°C"
                            msg = (f"REPRICING OPPORTUNITY detected — new bucket entered\n"
                                   f"{mode_tag} | {mkt['city_name']} {mkt['date']}\n"
                                   f"  {trade2['direction']} {t2_lbl}"
                                   f" @ {trade2['price']:.3f}"
                                   f" | EV:{trade2['ev']:.4f} Size:${trade2['size']:.2f}\n"
                                   f"  Primary {pos_lbl} now {cur_p:.3f}"
                                   f" (entry {pos['price']:.3f})")
                            log.info(msg)
                            send_alert(msg, priority="high")
                            mkt["position2"] = {
                                "direction": trade2["direction"],
                                "bucket": trade2["bucket"],
                                "bucket_label": trade2.get("bucket_label") or f"{trade2['bucket']}°C",
                                "price": trade2["price"],
                                "size": trade2["size"],
                                "ev": trade2["ev"],
                                "p": trade2["p"],
                                "token_id": trade2.get("token_id"),
                                "market_id": trade2.get("market_id"),
                                "opened_at": datetime.now(timezone.utc).isoformat(),
                                "tp1_hit": False,
                                "repricing": True,
                            }
                            mkt["repricing_ever_triggered"] = True  # persists for learning
                            exposures[city] = exposures.get(city, 0) + trade2["size"]

                # Forecast drift: entry bucket >2°C from current best_temp
                if (best_temp is not None and
                        not mkt.get("position2") and
                        abs(pos["bucket"] - best_temp) > 2.0):
                    pos_lbl = pos.get("bucket_label") or f"{pos['bucket']}°C"
                    log.warning(f"FORECAST DRIFT — {city}: entered {pos_lbl} "
                                f"but forecast now {best_temp:.1f}°C")
                    # Only enter if the correctly-forecast bucket hasn't been bid up already
                    drift_candidates = [
                        o for o in outcomes
                        if parse_bucket(o["bucket"]) is not None
                        and parse_bucket(o["bucket"]) != pos["bucket"]
                        and abs(parse_bucket(o["bucket"]) - best_temp) < 2.0
                    ]
                    if drift_candidates:
                        drift_t = find_yes_trade(city, best_temp, drift_candidates, sigma,
                                                 total_size_mult, conf_mult, balance,
                                                 calibration, recency_adj)
                        if drift_t:
                            mode_tag = "PAPER" if PAPER_MODE else "LIVE"
                            dt_lbl  = drift_t.get("bucket_label") or f"{drift_t['bucket']}°C"
                            msg = (f"FORECAST DRIFT detected — {mkt['city_name']}: "
                                   f"original bucket {pos_lbl} drifting, "
                                   f"entering {dt_lbl}"
                                   f" at {drift_t['price']:.3f}\n"
                                   f"{mode_tag} | EV:{drift_t['ev']:.4f}"
                                   f" Size:${drift_t['size']:.2f}"
                                   f" | Forecast now {best_temp:.1f}°C")
                            log.info(msg)
                            send_alert(msg, priority="high")
                            mkt["position2"] = {
                                "direction": drift_t["direction"],
                                "bucket": drift_t["bucket"],
                                "price": drift_t["price"],
                                "size": drift_t["size"],
                                "ev": drift_t["ev"],
                                "p": drift_t["p"],
                                "token_id": drift_t.get("token_id"),
                                "market_id": drift_t.get("market_id"),
                                "opened_at": datetime.now(timezone.utc).isoformat(),
                                "tp1_hit": False,
                                "drift": True,
                            }
                            exposures[city] = exposures.get(city, 0) + drift_t["size"]

                # Track exposure and skip normal new-trade logic
                exposures[city] = exposures.get(city, 0) + pos["size"]
                save_market(mkt)
                continue

            # City performance gate — skip new entries for demonstrably wrong models
            if not _city_conviction_eligible(city):
                save_market(mkt)
                continue

            # Time gate — only enter during each city group's liquid window
            if not _city_in_entry_window(city, now):
                save_market(mkt)
                continue

            # Find new trade
            if best_temp is not None:
                yes_t = find_yes_trade(city, best_temp, outcomes, sigma,
                                      total_size_mult, conf_mult, balance,
                                      calibration, recency_adj)
                no_t = find_no_trade(city, best_temp, outcomes, sigma,
                                    total_size_mult, conf_mult, balance,
                                    calibration, recency_adj)

                trade = None
                if yes_t and no_t:
                    trade = yes_t if yes_t["ev"] > no_t["ev"] else no_t
                elif yes_t:
                    trade = yes_t
                elif no_t:
                    trade = no_t

                if trade:
                    candidates.append({
                        "city":      city,
                        "date_str":  date_str,
                        "mkt":       mkt,
                        "trade":     trade,
                        "best_temp": best_temp,
                        "conf":      conf_mult,  # needed by low-price conviction gate
                        "sigma":     sigma,      # needed by low-price conviction gate
                    })
                    _clbl = trade.get("bucket_label") or f"{trade['bucket']}°C"
                    log.info(f"CONVICTION candidate: {city} {date_str} "
                             f"{trade['direction']} {_clbl} "
                             f"EV={trade['ev']:.4f}")
                else:
                    log.info(f"No +EV trade for {city} {date_str}")
            else:
                log.info(f"No forecast for {city} {date_str}")

            # Binary YES/NO markets — paused; finding 0 markets, market structure unclear
            # if best_temp is not None and conf_mult > 0.0:
            #     binary_cands = find_binary_yesno_markets(
            #         city, tgt_month, tgt_day, tgt_year,
            #         best_temp, sigma, total_size_mult, conf_mult,
            #         balance, calibration, recency_adj,
            #     )
            #     if binary_cands:
            #         log.info(f"[BINARY] {len(binary_cands)} candidate(s) for {city} {date_str}")
            #         candidates.extend(binary_cands)

            save_market(mkt)

    _apply_conviction_trades(candidates, balance, exposures)

    # Update state with exposures
    state["market_exposures"] = exposures
    save_state(state)
    resolve_past_markets()

    # Shadow pass — uncalibrated cities: log candidates + grade past ones.
    # Skipped during rapid cycles (122s) to keep them light.
    if not rapid_mode:
        try:
            _resolve_shadow_trades()
            _run_shadow_scan(state, calibration, now)
        except Exception as e:
            log.warning(f"[SHADOW] pass failed: {e}")

    log.info("Scan complete.")
