import logging, requests, time
from datetime import datetime, timezone
from typing import Dict, Optional
from config import LOCATIONS

log = logging.getLogger("weatherbet")

def get_ecmwf(city, date_str):
    """Enhanced ECMWF with precipitation and pressure."""
    loc = LOCATIONS[city]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={loc['lat']}&longitude={loc['lon']}"
               f"&daily=temperature_2m_max,precipitation_sum,weather_code"
               f"&temperature_unit=celsius"
               f"&forecast_days=7&timezone={loc['timezone']}"
               f"&models=ecmwf_ifs025")
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        precip = daily.get("precipitation_sum", [])
        weather = daily.get("weather_code", [])

        if date_str in dates:
            idx = dates.index(date_str)
            return {
                "temp": round(temps[idx], 1) if temps else None,
                "precip": round(precip[idx], 1) if precip else None,
                "weather_code": weather[idx] if weather else None,
            }
    except Exception as e:
        log.warning(f"ECMWF failed {city}: {e}")
    return {"temp": None, "precip": None, "weather_code": None}

def get_gfs(city, date_str):
    """Enhanced GFS with precipitation and pressure."""
    loc = LOCATIONS[city]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={loc['lat']}&longitude={loc['lon']}"
               f"&daily=temperature_2m_max,precipitation_sum,weather_code"
               f"&temperature_unit=celsius"
               f"&forecast_days=7&timezone={loc['timezone']}"
               f"&models=gfs_seamless")
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        precip = daily.get("precipitation_sum", [])
        weather = daily.get("weather_code", [])

        if date_str in dates:
            idx = dates.index(date_str)
            return {
                "temp": round(temps[idx], 1) if temps else None,
                "precip": round(precip[idx], 1) if precip else None,
                "weather_code": weather[idx] if weather else None,
            }
    except Exception as e:
        log.warning(f"GFS failed {city}: {e}")
    return {"temp": None, "precip": None, "weather_code": None}

ASIAN_JMA_CITIES = frozenset([
    "tokyo", "seoul", "hong_kong", "singapore",
    "taipei", "shanghai", "shenzhen", "chengdu",
])


def get_jma(city, date_str):
    """JMA-GSM forecast — East Asian cities only.
    JMA Global Spectral Model (~20km, updates 4x/day) is more accurate than
    ECMWF/GFS for Japan/Korea/SE Asia regional patterns. Supports up to 10 days
    (non-null); we request 7 to match ECMWF/GFS range.
    """
    if city not in ASIAN_JMA_CITIES:
        return {"temp": None, "precip": None, "weather_code": None}
    loc = LOCATIONS[city]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={loc['lat']}&longitude={loc['lon']}"
               f"&daily=temperature_2m_max,precipitation_sum,weather_code"
               f"&temperature_unit=celsius"
               f"&forecast_days=7&timezone={loc['timezone']}"
               f"&models=jma_gsm")
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        daily  = data.get("daily", {})
        dates  = daily.get("time", [])
        temps  = daily.get("temperature_2m_max", [])
        precip = daily.get("precipitation_sum", [])
        weather = daily.get("weather_code", [])

        if date_str in dates:
            idx = dates.index(date_str)
            return {
                "temp":         round(temps[idx], 1) if temps  else None,
                "precip":       round(precip[idx], 1) if precip else None,
                "weather_code": weather[idx] if weather else None,
            }
    except Exception as e:
        log.warning(f"JMA failed {city}: {e}")
    return {"temp": None, "precip": None, "weather_code": None}


def get_hrrr(city, date_str):
    """HRRR forecast — US/NWS cities only.
    HRRR (~3km resolution, hourly updates) is significantly more accurate than
    ECMWF/GFS for 0–24hr US forecasts. Only covers CONUS; returns None for others.
    forecast_days capped at 3 (HRRR is a short-range model).
    """
    if LOCATIONS[city].get("agency") != "NWS":
        return {"temp": None, "precip": None, "weather_code": None}
    loc = LOCATIONS[city]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={loc['lat']}&longitude={loc['lon']}"
               f"&daily=temperature_2m_max,precipitation_sum,weather_code"
               f"&temperature_unit=celsius"
               f"&forecast_days=2&timezone={loc['timezone']}"
               f"&models=gfs_hrrr")
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        precip = daily.get("precipitation_sum", [])
        weather = daily.get("weather_code", [])

        if date_str in dates:
            idx = dates.index(date_str)
            return {
                "temp": round(temps[idx], 1) if temps else None,
                "precip": round(precip[idx], 1) if precip else None,
                "weather_code": weather[idx] if weather else None,
            }
    except Exception as e:
        log.warning(f"HRRR failed {city}: {e}")
    return {"temp": None, "precip": None, "weather_code": None}


def get_metar(city):
    """Enhanced METAR with wind gust data."""
    station = LOCATIONS[city]["station"]
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        if data:
            obs = data[0]
            return {
                "temp": obs.get("temp"),
                "wind_gust": obs.get("windGust"),
                "precip": obs.get("precip"),  # May not exist
                "pressure": obs.get("altim"),
            }
    except Exception as e:
        log.warning(f"METAR failed {city}: {e}")
    return {}

def get_hourly_peak(city: str, date_str: str) -> dict:
    """
    Fetch hourly temperature_2m from ECMWF and return the peak value
    for date_str (local timezone). Used to detect intraday divergence
    from the daily temperature_2m_max. Fetched uncached every scan
    because hourly data updates more frequently than 6-hourly model runs.
    """
    loc = LOCATIONS[city]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={loc['lat']}&longitude={loc['lon']}"
               f"&hourly=temperature_2m"
               f"&temperature_unit=celsius"
               f"&forecast_days=3&timezone={loc['timezone']}"
               f"&models=ecmwf_ifs025")
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        hourly = r.json().get("hourly", {})
        times  = hourly.get("time", [])
        temps  = hourly.get("temperature_2m", [])
        day_temps = [t for t, ts in zip(temps, times)
                     if ts.startswith(date_str) and t is not None]
        if day_temps:
            return {"peak": round(max(day_temps), 1), "readings": len(day_temps)}
    except Exception as e:
        log.warning(f"Hourly peak failed {city}: {e}")
    return {"peak": None, "readings": 0}

_nws_cache: dict = {}
_NWS_CACHE_TTL = 3600  # NWS discussions update ~3x/day; 1-hour cache is safe

def get_nws_discussion(city: str) -> dict:
    """
    Fetch NWS Area Forecast Discussion (AFD) for a US city.

    NWS removed the forecastDiscussion field from /points. New 3-step flow:
      1. GET /points/{lat},{lon}  → properties.cwa  (office code, e.g. "OKX")
      2. GET /products/types/AFD/locations/{cwa}  → latest AFD product id
      3. GET /products/{productId}  → productText

    Returns {"text": str, "url": str} or {} on failure.
    """
    ts, cached = _nws_cache.get(city, (0.0, {}))
    if time.monotonic() - ts < _NWS_CACHE_TTL:
        return cached

    loc = LOCATIONS[city]
    lat, lon = loc.get("lat"), loc.get("lon")
    if lat is None or lon is None:
        return {}

    headers = {"User-Agent": "weatherbet-bot/1.0 (forecast research)", "Accept": "application/geo+json"}
    try:
        # Step 1: resolve CWA office code
        r = requests.get(
            f"https://api.weather.gov/points/{lat},{lon}",
            headers=headers, timeout=10,
        )
        r.raise_for_status()
        props = r.json().get("properties", {})
        cwa = props.get("cwa")
        if not cwa:
            log.warning(f"NWS /points no cwa for {city} — keys returned: {list(props.keys())}")
            _nws_cache[city] = (time.monotonic(), {})
            return {}

        # Step 2: get latest AFD product id for this office
        r2 = requests.get(
            f"https://api.weather.gov/products/types/AFD/locations/{cwa}",
            headers=headers, timeout=10,
        )
        r2.raise_for_status()
        graph = r2.json().get("@graph", [])
        if not graph:
            log.warning(f"NWS AFD product list empty for {city} ({cwa})")
            _nws_cache[city] = (time.monotonic(), {})
            return {}
        product_id = graph[0].get("id")
        if not product_id:
            log.warning(f"NWS AFD graph[0] has no id for {city} ({cwa})")
            _nws_cache[city] = (time.monotonic(), {})
            return {}

        # Step 3: fetch full discussion text
        product_url = f"https://api.weather.gov/products/{product_id}"
        r3 = requests.get(product_url, headers=headers, timeout=10)
        r3.raise_for_status()
        text = r3.json().get("productText", "")
        result = {"text": text, "url": product_url}
        _nws_cache[city] = (time.monotonic(), result)
        log.debug(f"NWS discussion fetched: {city} cwa={cwa} ({len(text)} chars)")
        return result

    except Exception as e:
        log.warning(f"NWS discussion failed {city}: {e}")
        _nws_cache[city] = (time.monotonic(), {})
        return {}


def get_pressure_trend(city):
    """NEW: Get 6-hour pressure trend for frontal detection."""
    loc = LOCATIONS[city]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={loc['lat']}&longitude={loc['lon']}"
               f"&hourly=surface_pressure"
               f"&forecast_days=1&timezone={loc['timezone']}")
        r = requests.get(url, timeout=10)
        data = r.json()
        pressures = data.get("hourly", {}).get("surface_pressure", [])
        if len(pressures) >= 7:
            now_hour   = datetime.now(timezone.utc).hour
            recent     = pressures[now_hour]
            six_hr_ago = pressures[max(0, now_hour - 6)]
            trend = recent - six_hr_ago
            return {
                "current": recent,
                "trend": trend,
                "falling": trend < -2,  # Front approaching
            }
    except Exception as e:
        log.warning(f"Pressure trend failed {city}: {e}")
    return {"current": None, "trend": 0, "falling": False}
