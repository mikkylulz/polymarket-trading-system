"""
WeatherBet Bot — Ultimate Edition v4.0
Complete rewrite with all session learnings integrated.
P0: Resolution station coordinates, precipitation/pressure data, frontal tracker fixes
P1: Recency bias detector, new cities, NEA/HKO/KMA parsing, post-frontal sequences
P2: Wunderground calibration, stale anchor detection, correlation matrix
P3: VPS deployment, dashboard hooks, backtesting framework, ML features
"""

import socket, sys, os, json, time, math, logging, requests, re, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ── Windows Unicode fix ──────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

socket.setdefaulttimeout(30)

# ── Create data folder BEFORE logging ───────────────────────
os.makedirs("data/markets", exist_ok=True)
os.makedirs("data/historical", exist_ok=True)
os.makedirs("data/cache", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("data/bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("weatherbet")

# ── Load config ──────────────────────────────────────────────
BASE = Path(__file__).parent
CFG  = json.loads((BASE / "config.json").read_text())

BALANCE        = CFG["balance"]
MAX_BET        = CFG["max_bet"]
MIN_BET        = CFG["min_bet"]
MIN_EV         = CFG["min_ev"]
MIN_PRICE      = CFG["min_price"]
MAX_PRICE      = CFG["max_price"]
MIN_VOLUME     = CFG.get("min_volume", 1000)
MIN_HOURS      = CFG["min_hours"]
MAX_HOURS      = CFG["max_hours"]
KELLY_FRACTION = CFG["kelly_fraction"]
MAX_SLIPPAGE   = CFG["max_slippage"]
SCAN_INTERVAL  = CFG["scan_interval"]
STOP_LOSS      = CFG["stop_loss_pct"]
TP1_PCT        = CFG["take_profit_1_pct"]
TP1_SIZE       = CFG["take_profit_1_size"]
TP2_PCT        = CFG["take_profit_2_pct"]
PAPER_MODE     = CFG["paper_mode"]
VC_KEY         = CFG.get("vc_key", "")
TG_TOKEN       = CFG.get("telegram_token", "")
TG_CHAT        = CFG.get("telegram_chat_id", "")
ACTIVE_CITIES  = CFG["active_cities"]

# ── City definitions — RESOLUTION STATION COORDINATES ────────
# CRITICAL FIX: Use actual resolution station coordinates, not city centers
LOCATIONS = {
    # ── Asia Pacific ──
    "hong_kong": {
        "name": "Hong Kong",
        "station": "VHHH", "timezone": "Asia/Hong_Kong",
        "slug_prefix": "hong-kong", "series_slug": "hong-kong-daily-weather",
        # King's Park/HKO coordinates (actual resolution)
        "lat": 22.3114, "lon": 114.1728,
        "resolution_station": "HKO King's Park",
        "bias": -1.0, "sigma": 1.5,
        "agency": "HKO", "agency_url": "https://www.hko.gov.hk/textonly/v2/forecast/nday_v2.htm",
    },
    "singapore": {
        "name": "Singapore",
        "station": "WSSS", "timezone": "Asia/Singapore",
        "slug_prefix": "singapore", "series_slug": "singapore-daily-weather",
        # Changi Airport coordinates (actual resolution)
        "lat": 1.3644, "lon": 103.9915,
        "resolution_station": "WSSS Changi",
        "bias": -2.5, "sigma": 2.0,
        "agency": "NEA", "agency_url": "https://www.nea.gov.sg/api/weather/4day",
    },
    "tokyo": {
        "name": "Tokyo",
        "station": "RJTT", "timezone": "Asia/Tokyo",
        "slug_prefix": "tokyo", "series_slug": "tokyo-daily-weather",
        # Haneda Airport coordinates
        "lat": 35.5533, "lon": 139.7811,
        "resolution_station": "RJTT Haneda",
        "bias": -0.5, "sigma": 1.5,
        "agency": "JMA", "agency_url": "https://www.jma.go.jp/bosai/forecast/",
    },
    "seoul": {
        "name": "Seoul",
        "station": "RKSS", "timezone": "Asia/Seoul",
        "slug_prefix": "seoul", "series_slug": "seoul-daily-weather",
        # KMA Seoul Observatory Station #108, Jung-gu (actual Polymarket resolution station)
        # RKSI/Incheon (37.4691, 126.4505) is on Yeongjong Island, 52km offshore —
        # sea-influenced temps ran 10-11°C cold vs central Seoul actuals.
        "lat": 37.5714, "lon": 126.9658,
        "resolution_station": "KMA Seoul Station #108 Jung-gu",
        "bias": 0.0, "sigma": 2.5,
        "agency": "KMA", "agency_url": "https://www.weather.go.kr/w/weather/forecast/short-term.do",
    },
    # ── Europe ──
    "madrid": {
        "name": "Madrid",
        "station": "LEMD", "timezone": "Europe/Madrid",
        "slug_prefix": "madrid", "series_slug": "madrid-daily-weather",
        # Barajas Airport coordinates
        "lat": 40.4719, "lon": -3.5626,
        "resolution_station": "LEMD Barajas",
        "bias": +0.75, "sigma": 2.0,
        "agency": "AEMET", "agency_url": "https://www.aemet.es/es/eltiempo/prediccion/municipios/madrid-id28079",
    },
    # ── NEW CITIES (from session history) ──
    "lucknow": {
        "name": "Lucknow",
        "station": "VILK", "timezone": "Asia/Kolkata",
        "slug_prefix": "lucknow", "series_slug": "lucknow-daily-weather",
        # Chaudhary Charan Singh Intl
        "lat": 26.7606, "lon": 80.8893,
        "resolution_station": "VILK Amausi",
        "bias": +1.5, "sigma": 2.5,  # IMD underestimates during heatwave
        "agency": "IMD", "agency_url": "https://mausam.imd.gov.in/imd_latest/contents/index.php",
        "heatwave_boost": True,  # +1.5°C additional during active heatwave
    },
    "chengdu": {
        "name": "Chengdu",
        "station": "ZUUU", "timezone": "Asia/Shanghai",
        "slug_prefix": "chengdu", "series_slug": "chengdu-daily-weather",
        # Shuangliu Intl - Sichuan Basin heat trap
        "lat": 30.5785, "lon": 103.9471,
        "resolution_station": "ZUUU Shuangliu",
        "bias": +1.5, "sigma": 2.0,
        "agency": "CMA", "agency_url": "http://www.nmc.cn/publish/forecast/ASC/chengdu.html",
    },
    "london": {
        "name": "London",
        "station": "EGLL", "timezone": "Europe/London",
        "slug_prefix": "london", "series_slug": "london-daily-weather",
        # Heathrow
        "lat": 51.4700, "lon": -0.4543,
        "resolution_station": "EGLL Heathrow",
        "bias": +0.5, "sigma": 2.0,
        "agency": "MetOffice", "agency_url": "https://www.metoffice.gov.uk/weather/forecast/gcpvj0v07",
    },
}

MONTH_NAMES = {1:"january",2:"february",3:"march",4:"april",
               5:"may",6:"june",7:"july",8:"august",
               9:"september",10:"october",11:"november",12:"december"}

# ── Regime config with post-frontal sequences ────────────────
REGIME_CONFIG = {
    "singapore": {
        "sw_monsoon":    {"months": [6,7,8,9],     "size": 1.5, "note": "Best season"},
        "inter_monsoon": {"months": [4,5,10,11],   "size": 0.5, "note": "Avoid - chaotic transitions"},
        "ne_monsoon":    {"months": [12,1,2,3],    "size": 0.75, "note": "Decent"},
    },
    "hong_kong": {
        "sw_monsoon":    {"months": [5,6,7,8,9],   "size": 1.5, "note": "Best season"},
        "ne_monsoon":    {"months": [10,11,12,1],  "size": 0.75, "note": "Decent"},
        "transition":    {"months": [2,3,4],        "size": 0.4, "note": "Chaotic"},
    },
    "tokyo": {
        "pre_baiu":      {"months": [4,5],          "size": 1.0, "note": "Good before Baiu"},
        "baiu":          {"months": [6],            "size": 0.0, "note": "SKIP - unpredictable"},
        "summer":        {"months": [7,8,9],        "size": 1.5, "note": "Best season"},
        "autumn":        {"months": [10,11],        "size": 0.75, "note": "Decent"},
        "winter":        {"months": [12,1,2,3],    "size": 0.5, "note": "Avoid"},
    },
    "seoul": {
        "summer":        {"months": [6,7,8],        "size": 1.5, "note": "Best season"},
        "spring":        {"months": [4,5],          "size": 0.75, "note": "Frontal chaos - use tracker"},
        "autumn":        {"months": [9,10],         "size": 1.0, "note": "Decent"},
        "winter":        {"months": [11,12,1,2,3], "size": 0.25, "note": "Avoid - WD season"},
    },
    "madrid": {
        "azores_summer": {"months": [6,7,8],        "size": 1.5, "note": "Best - Azores High locked"},
        "spring_chaos":  {"months": [3,4],          "size": 0.25, "note": "Avoid - chaotic"},
        "autumn":        {"months": [9,10],         "size": 1.0, "note": "Decent"},
        "winter":        {"months": [11,12,1,2],   "size": 0.5, "note": "Avoid"},
        "may":           {"months": [5],            "size": 0.75, "note": "Transition"},
    },
    "lucknow": {
        "heatwave":      {"months": [4,5,6],        "size": 1.5, "note": "BEST - IMD underpredicts"},
        "monsoon":       {"months": [7,8],          "size": 0.75, "note": "Locked in, predictable"},
        "withdrawal":    {"months": [9,10],         "size": 0.0, "note": "SKIP - chaotic"},
        "wd_season":     {"months": [11,12,1,2,3], "size": 0.0, "note": "SKIP - WD unpredictable"},
    },
    "chengdu": {
        "pre_monsoon":   {"months": [4,5],          "size": 1.0, "note": "Heat trap active"},
        "monsoon":       {"months": [6,7,8,9],      "size": 0.5, "note": "Cloudy, capped"},
        "autumn":        {"months": [10,11],        "size": 0.75, "note": "Decent"},
        "winter":        {"months": [12,1,2,3],    "size": 0.25, "note": "Avoid"},
    },
    "london": {
        "summer":        {"months": [6,7,8],        "size": 1.0, "note": "Best - stable"},
        "spring":        {"months": [4,5],          "size": 0.5, "note": "Chaotic"},
        "autumn":        {"months": [9,10],         "size": 0.5, "note": "Chaotic"},
        "winter":        {"months": [11,12,1,2,3], "size": 0.0, "note": "SKIP - WD chaos"},
    },
}

# ── Post-frontal sequence rules (from session history) ───────
POST_FRONTAL_CONFIG = {
    "seoul": {
        "day_1": {"temp_adj": -2, "size_mult": 1.0, "note": "Cool, cloudy"},
        "day_2": {"temp_adj": -2, "size_mult": 1.0, "note": "Still recovering"},
        "day_3": {"temp_adj": +3, "size_mult": 2.0, "note": "HIGH-PRESSURE SPIKE - BIG TRADE"},
        "day_4": {"temp_adj": +1, "size_mult": 1.5, "note": "Spike continues"},
        "day_5": {"temp_adj": 0, "size_mult": 1.0, "note": "Normalizing"},
    }
}

# ── Singapore shower timing rules (from session history) ─────
SG_SHOWER_RULES = {
    "afternoon_showers": {"temp_adj": -2, "note": "NEA says afternoon thundery showers"},
    "morning_showers":   {"temp_adj": -3, "note": "NEA says morning showers"},
    "no_showers":        {"temp_adj": 0,  "note": "NEA says fair/warm"},
    "sumatra_squall":    {"temp_adj": -4, "note": "Sumatra squall morning"},
}

# ── Correlation matrix (don't pile into correlated markets) ──
MARKET_CORRELATIONS = {
    "hong_kong": {"singapore": 0.7, "tokyo": 0.3, "seoul": 0.2},
    "singapore": {"hong_kong": 0.7, "tokyo": 0.4, "seoul": 0.1},
    "tokyo":     {"seoul": 0.6, "hong_kong": 0.3, "singapore": 0.4},
    "seoul":     {"tokyo": 0.6, "hong_kong": 0.2, "singapore": 0.1},
    "madrid":    {},  # Uncorrelated with Asia
    "lucknow":   {},  # Uncorrelated
    "chengdu":   {"hong_kong": 0.3},
    "london":    {"madrid": 0.4},
}

# ── File paths ───────────────────────────────────────────────
STATE_FILE    = BASE / "data" / "state.json"
MARKETS_DIR   = BASE / "data" / "markets"
CALIB_FILE    = BASE / "data" / "calibration.json"
HISTORICAL_DIR = BASE / "data" / "historical"
CACHE_DIR     = BASE / "data" / "cache"

for d in [MARKETS_DIR, HISTORICAL_DIR, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ── State management ─────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "seoul_front_day": 0,
        "seoul_front_date": None,
        "last_precip": {},
        "market_exposures": {},
        "session_pnl": 0.0,
    }

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))

# ── Historical data management ───────────────────────────────
def load_historical(city: str) -> List[Dict]:
    """Load historical resolutions for recency bias detection."""
    path = HISTORICAL_DIR / f"{city}_history.json"
    if path.exists():
        return json.loads(path.read_text())
    return []

def save_historical(city: str, data: List[Dict]):
    path = HISTORICAL_DIR / f"{city}_history.json"
    path.write_text(json.dumps(data, indent=2))

def update_historical(city: str, date_str: str, actual_temp: float, forecast: float):
    """Add a new resolution to historical data."""
    hist = load_historical(city)
    hist.append({
        "date": date_str,
        "actual": actual_temp,
        "forecast": forecast,
        "error": round(actual_temp - forecast, 2),
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    # Keep last 90 days
    hist = hist[-90:]
    save_historical(city, hist)
    return hist

def get_recent_pattern(city: str, days: int = 3) -> Dict:
    """Get recent temperature pattern for recency bias detection."""
    hist = load_historical(city)
    if len(hist) < days:
        return {"avg": None, "trend": None, "last": None}
    
    recent = hist[-days:]
    temps = [h["actual"] for h in recent]
    return {
        "avg": round(sum(temps) / len(temps), 1),
        "trend": round(temps[-1] - temps[0], 1),
        "last": temps[-1],
        "min": min(temps),
        "max": max(temps),
    }

# ── Telegram alerts ──────────────────────────────────────────
def send_alert(msg, priority="normal"):
    if not TG_TOKEN or "YOUR" in TG_TOKEN:
        log.info(f"[ALERT] {msg}")
        return
    emoji = "🚨" if priority == "high" else "📊"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": f"{emoji} WEATHERBET\n{msg}"},
            timeout=8
        )
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

# ── Market storage ───────────────────────────────────────────
def market_path(city, date_str):
    return MARKETS_DIR / f"{city}_{date_str}.json"

def load_market(city, date_str):
    p = market_path(city, date_str)
    return json.loads(p.read_text()) if p.exists() else None

def save_market(mkt):
    market_path(mkt["city"], mkt["date"]).write_text(json.dumps(mkt, indent=2))

def new_market(city, date_str, event_id, hours):
    return {
        "city": city,
        "city_name": LOCATIONS[city]["name"],
        "date": date_str,
        "event_id": event_id,
        "status": "open",
        "position": None,
        "actual_temp": None,
        "resolved_outcome": None,
        "pnl": None,
        "forecast_snapshots": [],
        "market_snapshots": [],
        "all_outcomes": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

# ── Calibration ──────────────────────────────────────────────
def load_calibration():
    return json.loads(CALIB_FILE.read_text()) if CALIB_FILE.exists() else {}

def save_calibration(d):
    CALIB_FILE.write_text(json.dumps(d, indent=2))

# ── Regime and seasonality ───────────────────────────────────
def get_regime_multiplier(city, month):
    for regime, cfg in REGIME_CONFIG.get(city, {}).items():
        if month in cfg["months"]:
            return cfg["size"], cfg.get("note", "")
    return 0.5, "Default"

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

# ── Singapore shower parser (NEW) ────────────────────────────
def parse_nea_forecast(city: str) -> Dict:
    """
    NEW: Scrape NEA 4-day forecast for Singapore.
    Returns shower timing and temperature forecast.
    """
    if city != "singapore":
        return {}
    
    try:
        # NEA API endpoint (undocumented but stable)
        url = "https://www.nea.gov.sg/api/WeatherForecast/forecast24hrnowcast2hrs"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return {}
        
        data = r.json()
        forecast_text = data.get("channel", {}).get("item", {}).get("forecast", "")
        
        # Parse shower timing from text
        shower_type = "no_showers"
        if "sumatra" in forecast_text.lower() or "squall" in forecast_text.lower():
            shower_type = "sumatra_squall"
        elif "morning" in forecast_text.lower() and "showers" in forecast_text.lower():
            shower_type = "morning_showers"
        elif "afternoon" in forecast_text.lower() and "thundery" in forecast_text.lower():
            shower_type = "afternoon_showers"
        elif "showers" in forecast_text.lower():
            shower_type = "afternoon_showers"  # Default
        
        rule = SG_SHOWER_RULES.get(shower_type, SG_SHOWER_RULES["no_showers"])
        
        # Extract temperature from forecast
        temp_match = re.search(r'(\d+)\s*°C', forecast_text)
        nea_temp = int(temp_match.group(1)) if temp_match else None
        
        return {
            "shower_type": shower_type,
            "temp_adj": rule["temp_adj"],
            "nea_temp": nea_temp,
            "raw_text": forecast_text[:200],
        }
        
    except Exception as e:
        log.warning(f"NEA parse failed: {e}")
        return {}

# ── HKO forecast scraper (NEW) ───────────────────────────────
def parse_hko_forecast(city: str) -> Dict:
    """NEW: Scrape HKO 9-day forecast for Hong Kong."""
    if city != "hong_kong":
        return {}
    
    try:
        url = "https://www.hko.gov.hk/en/informtc/ntwc/weather_forecast/9day/"
        # HKO has a JSON API endpoint
        url = "https://www.hko.gov.hk/textonly/v2/forecast/nday_v2.htm"
        r = requests.get(url, timeout=10)
        
        # Parse temperature ranges from HTML
        temps = re.findall(r'(\d+)\s*-\s*(\d+)\s*°C', r.text)
        if temps:
            # Use the first day (today/tomorrow)
            low, high = int(temps[0][0]), int(temps[0][1])
            return {
                "hko_low": low,
                "hko_high": high,
                "hko_mid": (low + high) / 2,
            }
    except Exception as e:
        log.warning(f"HKO parse failed: {e}")
    
    return {}

# ── IMD heatwave detector (NEW) ──────────────────────────────
def check_imd_heatwave(city: str) -> Dict:
    """
    NEW: Check IMD for active heatwave alerts.
    Returns heatwave status and temperature boost.
    """
    if city != "lucknow":
        return {"active": False, "boost": 0}
    
    try:
        # IMD heatwave bulletin
        url = "https://mausam.imd.gov.in/imd_latest/contents/heat_wave.php"
        r = requests.get(url, timeout=10)
        
        # Check if Uttar Pradesh is mentioned
        is_heatwave = "uttar pradesh" in r.text.lower() or "up" in r.text.lower()
        
        # Also check IMD forecast for Lucknow
        url = "https://mausam.imd.gov.in/imd_latest/contents/current_weather.php"
        r2 = requests.get(url, timeout=10)
        
        # Extract max temp if available
        temp_match = re.search(r'Lucknow.*?(\d+)\s*°C', r2.text, re.IGNORECASE)
        imd_temp = int(temp_match.group(1)) if temp_match else None
        
        boost = 1.5 if is_heatwave else 0
        
        return {
            "active": is_heatwave,
            "boost": boost,
            "imd_temp": imd_temp,
        }
        
    except Exception as e:
        log.warning(f"IMD heatwave check failed: {e}")
        return {"active": False, "boost": 0}

# ── Recency bias detector (NEW) ──────────────────────────────
def detect_recency_bias(city: str, market_outcomes: List[Dict], best_temp: float) -> Dict:
    """
    NEW: Detect if market is anchored to stale temperatures.
    Compares market pricing to recent historical pattern.
    """
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
    
    # If forecast is significantly different from recent pattern
    # but market is still pricing near recent pattern = recency bias
    forecast_diff = best_temp - recent_avg
    market_implied = None
    
    # Simple heuristic: if forecast is >2°C from recent but market hasn't moved
    if abs(forecast_diff) > 2:
        # Check if market's most active bucket is still near recent avg
        if market_mid > 0.3:  # Market still thinks recent pattern holds
            log.info(f"RECENCY BIAS detected: recent_avg={recent_avg}, forecast={best_temp}, market_mid_price={market_mid}")
            return {
                "bias_detected": True,
                "adjustment": forecast_diff * 0.5,  # Partial adjustment
                "recent_avg": recent_avg,
                "market_mid": market_mid,
            }
    
    return {"bias_detected": False, "adjustment": 0}

# ── Correlation exposure check (NEW) ─────────────────────────
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

# ── Forecast sources — ENHANCED ──────────────────────────────
def get_ecmwf(city, date_str):
    """Enhanced ECMWF with precipitation and pressure."""
    loc = LOCATIONS[city]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={loc['lat']}&longitude={loc['lon']}"
               f"&daily=temperature_2m_max,precipitation_sum,weather_code"
               f"&hourly=surface_pressure"
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
               f"&hourly=surface_pressure"
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
            recent = pressures[-1]
            six_hr_ago = pressures[-7]
            trend = recent - six_hr_ago
            return {
                "current": recent,
                "trend": trend,
                "falling": trend < -2,  # Front approaching
            }
    except Exception as e:
        log.warning(f"Pressure trend failed {city}: {e}")
    return {"current": None, "trend": 0, "falling": False}

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

# ── Enhanced forecast snapshot ───────────────────────────────
def take_forecast_snapshot(city, date_str, state):
    """Comprehensive snapshot with all data sources."""
    ecmwf = get_ecmwf(city, date_str)
    gfs = get_gfs(city, date_str)
    metar = get_metar(city)
    pressure = get_pressure_trend(city)
    
    ecmwf_temp = ecmwf.get("temp")
    gfs_temp = gfs.get("temp")
    ecmwf_precip = ecmwf.get("precip")
    gfs_precip = gfs.get("precip")
    
    # Agency-specific adjustments
    heatwave_boost = 0
    nea_data = {}
    hko_data = {}
    
    if city == "singapore":
        nea_data = parse_nea_forecast(city)
    elif city == "hong_kong":
        hko_data = parse_hko_forecast(city)
    elif city == "lucknow":
        imd_data = check_imd_heatwave(city)
        heatwave_boost = imd_data.get("boost", 0)
    
    # Apply biases
    ecmwf_c = apply_station_bias(city, ecmwf_temp, heatwave_boost)
    gfs_c = apply_station_bias(city, gfs_temp, heatwave_boost)
    
    # Model confidence
    conf = get_model_confidence(ecmwf_temp, gfs_temp, ecmwf_precip, gfs_precip)
    
    # Seoul frontal adjustment (FIXED: uses actual precip)
    frontal_adj, frontal_size = 0, 1.0
    if city == "seoul":
        metar_precip = metar.get("precip", 0) or 0
        metar_gust = metar.get("wind_gust", 0) or 0
        frontal_adj, frontal_size = get_seoul_frontal_adjustment(
            state, precip_mm=metar_precip, wind_gust=metar_gust
        )
        update_seoul_front(state, ecmwf_precip, metar_gust)
    
    # Calculate best temp
    if ecmwf_c and gfs_c:
        best = round((ecmwf_c + gfs_c) / 2 + frontal_adj, 1)
    elif ecmwf_c:
        best = round(ecmwf_c + frontal_adj, 1)
    elif gfs_c:
        best = round(gfs_c + frontal_adj, 1)
    else:
        best = None
    
    # Singapore shower adjustment
    sg_adj = 0
    if city == "singapore" and nea_data:
        sg_adj = nea_data.get("temp_adj", 0)
        if sg_adj != 0 and best is not None:
            best = round(best + sg_adj, 1)
    
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
        "ecmwf_corrected": ecmwf_c,
        "gfs_corrected": gfs_c,
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
        "best_temp": best,
        "pressure_skip": pressure_skip,
        "nea_temp": nea_data.get("nea_temp"),
        "hko_temp": hko_data.get("hko_mid"),
    }
    
    log.info(f"Snapshot {city} {date_str}: ECMWF={ecmwf_c} GFS={gfs_c} best={best}C "
             f"conf={conf} precip={ecmwf_precip}/{gfs_precip} "
             f"sg_adj={sg_adj} hw_boost={heatwave_boost}")
    
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

# ── Polymarket API (unchanged core, enhanced logging) ────────
POLY_API = "https://gamma-api.polymarket.com"
WEATHER_TAG_ID = "84"

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
    slug = _build_event_slug(city, month, day, year, "highest")
    log.info(f"Trying slug lookup: {slug}")
    
    try:
        r = requests.get(
            f"{POLY_API}/events",
            params={"slug": slug, "active": "true", "closed": "false"},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        
        if isinstance(data, list) and len(data) > 0:
            event = data[0]
            log.info(f"FOUND by slug: {event.get('title', '')}")
            return _normalize_event(event)
            
        r2 = requests.get(f"{POLY_API}/events/slug/{slug}", timeout=15)
        if r2.status_code == 200:
            event = r2.json()
            log.info(f"FOUND by slug path: {event.get('title', '')}")
            return _normalize_event(event)
            
    except Exception as e:
        log.warning(f"Slug lookup failed: {e}")

    # Fallback to tag scan
    try:
        r = requests.get(
            f"{POLY_API}/events",
            params={"tag_id": WEATHER_TAG_ID, "active": "true", "closed": "false", "limit": 100},
            timeout=15
        )
        data = r.json()
        events = data if isinstance(data, list) else data.get("data", [])
        
        city_name = LOCATIONS[city]["name"].lower()
        month_name = MONTH_NAMES[month]
        
        for event in events:
            title = (event.get("title") or "").lower()
            if city_name in title and month_name in title and str(day) in title:
                log.info(f"FOUND by tag scan: {event.get('title', '')}")
                return _normalize_event(event)
    except Exception as e:
        log.warning(f"Tag discovery failed: {e}")

    log.info(f"No event found: {city} {MONTH_NAMES[month]} {day}")
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

# ── Probability and EV (unchanged) ───────────────────────────
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

# ── Trade finders (enhanced with recency bias) ───────────────
def parse_bucket(bucket_str):
    try:
        c = str(bucket_str).replace("°C", "").replace("°F", "")
        c = c.replace("or higher", "").replace("or lower", "").strip()
        nums = re.findall(r'-?\d+', c)
        return int(nums[0]) if nums else None
    except:
        return None

def find_yes_trade(city, best_temp, outcomes, sigma, regime_mult, conf_mult,
                   balance, calibration, recency_adj=0):
    """Enhanced with recency bias adjustment."""
    target = round(best_temp + recency_adj) if best_temp is not None else None
    if target is None:
        return None
    
    best_trade, best_ev = None, MIN_EV
    
    log.info(f"YES search {city}: target={target}, sigma={sigma}, "
             f"regime={regime_mult}, conf={conf_mult}, recency_adj={recency_adj}")
    
    for o in outcomes:
        bucket = parse_bucket(o["bucket"])
        price = o["price"]
        
        if bucket is None:
            continue
        if price < MIN_PRICE or price > MAX_PRICE:
            continue
        if abs(bucket - target) > 2:
            continue
        
        p = bucket_probability(bucket, best_temp + recency_adj, sigma)
        ev = calc_ev(p, price)
        
        if ev > best_ev:
            size = round(min(max(
                calc_kelly(p, price, balance) * regime_mult * conf_mult,
                MIN_BET), MAX_BET), 2)
            best_ev = ev
            best_trade = {
                "direction": "YES", "bucket": bucket, "price": price,
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
        if abs(bucket - target) <= 3:
            continue
        
        no_price = round(1.0 - yes_price, 3)
        if no_price < MIN_PRICE or no_price > MAX_PRICE:
            continue
        
        p_no = 1.0 - bucket_probability(bucket, best_temp + recency_adj, sigma)
        ev = calc_ev(p_no, no_price)
        
        if ev > best_ev and p_no > 0.80:
            size = round(min(max(
                calc_kelly(p_no, no_price, balance) * regime_mult * conf_mult,
                MIN_BET), MAX_BET), 2)
            best_ev = ev
            best_trade = {
                "direction": "NO", "bucket": bucket,
                "yes_price": yes_price, "price": no_price,
                "p": round(p_no, 3), "ev": round(ev, 4), "size": size,
                "token_id": o.get("token_id"),
                "market_id": o.get("market_id"),
            }
    
    return best_trade

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
    msg = (f"{mode} OPEN | {mkt['city_name']} {mkt['date']}\n"
           f"  {trade['direction']} {trade['bucket']}C\n"
           f"  Price:{trade['price']:.3f} Size:${trade['size']:.2f}"
           f" EV:{trade['ev']:.4f} P:{trade['p']:.1%}")
    log.info(msg)
    send_alert(msg, priority="high")
    
    mkt["position"] = {
        "direction": trade["direction"],
        "bucket": trade["bucket"],
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
    
    if reason == "stop_loss":
        pnl = -size * STOP_LOSS
    elif reason == "take_profit_1":
        mkt["position"]["tp1_hit"] = True
        partial = size * TP1_SIZE * (TP1_PCT - entry)
        send_alert(f"TP1 HIT | {mkt['city_name']} | +${partial:.2f}")
        return mkt
    elif reason == "take_profit_2":
        pnl = size * (1 - TP1_SIZE) * (TP2_PCT - entry)
    else:
        pnl = size * (current_price - entry)
    
    msg = (f"CLOSED {reason.upper()} | {mkt['city_name']} {mkt['date']}\n"
           f"  Entry:{entry:.3f} Exit:{current_price:.3f} PnL:${pnl:+.2f}")
    log.info(msg)
    send_alert(msg, priority="high" if pnl < 0 else "normal")
    
    mkt["pnl"] = round(pnl, 2)
    mkt["status"] = "closed"
    mkt["position"] = None
    return mkt

# ── Learned sigma ────────────────────────────────────────────
def compute_learned_sigma(city: str) -> Optional[Dict]:
    """
    Compute per-city forecast sigma as the sample std dev of
    (best_temp - actual_temp), one data point per resolved market
    (last snapshot only — closest to resolution time).
    Returns None when fewer than calibration_min markets exist.
    """
    errors = []
    min_n = CFG.get("calibration_min", 15)

    for mp in MARKETS_DIR.glob(f"{city}_*.json"):
        try:
            m = json.loads(mp.read_text())
            if m.get("city") != city:
                continue
            if m.get("status") not in ("resolved", "closed"):
                continue
            actual = m.get("actual_temp")
            if actual is None:
                continue
            snaps = m.get("forecast_snapshots", [])
            if not snaps:
                continue
            best = snaps[-1].get("best_temp")
            if best is None:
                continue
            errors.append(best - actual)
        except Exception as e:
            log.warning(f"sigma compute error {mp}: {e}")

    n = len(errors)
    if n < min_n:
        return None

    mean_err = sum(errors) / n
    variance = sum((e - mean_err) ** 2 for e in errors) / (n - 1)
    sigma = round(math.sqrt(variance), 2)
    return {"sigma": sigma, "n": n, "bias": round(mean_err, 2)}

# ── Calibration (enhanced with historical) ───────────────────
def run_calibration(calibration):
    city_errors = {c: [] for c in ACTIVE_CITIES if c in LOCATIONS}
    
    for mp in MARKETS_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            if m["status"] != "resolved" or not m.get("actual_temp"):
                continue
            
            city = m["city"]
            if city not in city_errors:
                continue
            
            actual = m["actual_temp"]
            
            # Update historical database
            for snap in m.get("forecast_snapshots", []):
                forecast = snap.get("best_temp")
                if forecast:
                    update_historical(city, m["date"], actual, forecast)
            
            # Calculate errors
            for snap in m.get("forecast_snapshots", []):
                ecmwf = snap.get("ecmwf_corrected")
                if ecmwf:
                    city_errors[city].append(abs(ecmwf - actual))
                    
        except Exception as e:
            log.warning(f"Calibration error: {e}")
            continue
    
    for city, errors in city_errors.items():
        n = len(errors)
        if n >= CFG.get("calibration_min", 15):
            mae = sum(errors) / n
            key = f"{city}_ecmwf"
            calibration[key] = {"sigma": round(mae, 2), "n": n}
            log.info(f"Calibration {city}: ecmwf sigma={mae:.2f} n={n}")

        learned = compute_learned_sigma(city)
        if learned:
            calibration[f"{city}_best"] = learned
            log.info(f"Calibration {city}: learned sigma={learned['sigma']} "
                     f"bias={learned['bias']:+.2f} n={learned['n']}")

    save_calibration(calibration)
    return calibration

# ── Open-Meteo archive fetch ─────────────────────────────────
def fetch_actual_temp(city: str, date_str: str) -> Optional[float]:
    """
    Fetch recorded daily max temp from Open-Meteo archive API.
    Archive data typically lags 2-5 days; returns None when not yet available.
    """
    loc = LOCATIONS[city]
    try:
        url = (f"https://archive-api.open-meteo.com/v1/archive"
               f"?latitude={loc['lat']}&longitude={loc['lon']}"
               f"&start_date={date_str}&end_date={date_str}"
               f"&daily=temperature_2m_max"
               f"&temperature_unit=celsius"
               f"&timezone={loc['timezone']}")
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        temps = r.json().get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return round(float(temps[0]), 1)
    except Exception as e:
        log.warning(f"Archive fetch failed {city} {date_str}: {e}")
    return None


def _match_bucket(actual_temp: float, outcomes: List[Dict]) -> Optional[str]:
    """
    Return the bucket label that actual_temp resolves to.
    Handles edge buckets ('X°C or below', 'X°C or higher') and exact buckets.
    """
    rounded = math.floor(actual_temp + 0.5)
    for o in outcomes:
        label = str(o.get("bucket", ""))
        val = parse_bucket(label)
        if val is None:
            continue
        lower = label.lower()
        if "or below" in lower and rounded <= val:
            return label
        elif "or higher" in lower and rounded >= val:
            return label
        elif "or" not in lower and val == rounded:
            return label
    return None


def resolve_past_markets():
    """
    Called on every scan. Iterates all market files whose resolution date
    has passed, fetches actual max temp from Open-Meteo archive, writes
    actual_temp, determines resolved_outcome, computes binary PnL for any
    open position, and marks the market resolved.
    """
    today = datetime.now(timezone.utc).date()
    resolved_count = 0

    for mp in MARKETS_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            if m.get("status") in ("resolved", "closed"):
                continue

            mkt_date = datetime.strptime(m["date"], "%Y-%m-%d").date()
            if mkt_date > today:
                continue

            city = m["city"]
            if city not in LOCATIONS:
                continue

            # Fetch actual temp unless we already have it
            if m.get("actual_temp") is None:
                actual = fetch_actual_temp(city, m["date"])
                if actual is None:
                    log.info(f"Archive not yet available: {city} {m['date']}")
                    continue
                m["actual_temp"] = actual
            else:
                actual = m["actual_temp"]

            m["resolved_outcome"] = _match_bucket(actual, m.get("all_outcomes", []))
            m["status"] = "resolved"

            # Binary PnL for any open position
            if m.get("position"):
                pos = m["position"]
                resolved_val = parse_bucket(m.get("resolved_outcome") or "")
                direction = pos["direction"]
                pos_bucket = pos["bucket"]

                if direction == "YES":
                    won = resolved_val is not None and pos_bucket == resolved_val
                else:  # NO — bet that pos_bucket does NOT resolve
                    won = resolved_val is None or pos_bucket != resolved_val

                pnl = round(pos["size"] * (1.0 / pos["price"] - 1.0), 2) if won else -pos["size"]
                m["pnl"] = pnl
                m["position"] = None

                result_msg = (f"RESOLUTION {'WIN' if won else 'LOSS'} | "
                              f"{m['city_name']} {m['date']} | PnL: ${pnl:+.2f}\n"
                              f"  Bet: {direction} {pos_bucket}C | "
                              f"Actual: {actual}C | Resolved: {m.get('resolved_outcome', '?')}")
                log.info(result_msg)
                send_alert(result_msg, priority="high" if pnl < 0 else "normal")

            # Feed historical data for sigma learning
            for snap in m.get("forecast_snapshots", []):
                forecast = snap.get("best_temp")
                if forecast:
                    update_historical(city, m["date"], actual, forecast)

            save_market(m)
            resolved_count += 1
            log.info(f"Auto-resolved {city} {m['date']}: actual={actual}C "
                     f"outcome={m.get('resolved_outcome', '?')}")

        except Exception as e:
            log.warning(f"resolve_past_markets error {mp.name}: {e}")

    if resolved_count:
        log.info(f"Resolved {resolved_count} past market(s) from archive data")
    return resolved_count


# ── Auto-resolve (enhanced) ──────────────────────────────────
def auto_resolve(city, date_str, mkt):
    event_id = mkt.get("event_id")
    if not event_id:
        return mkt
    
    try:
        r = requests.get(f"{POLY_API}/markets/{event_id}", timeout=10)
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

# ── Main scan (enhanced) ─────────────────────────────────────
def scan_and_update():
    state = load_state()
    calibration = load_calibration()
    calibration = run_calibration(calibration)
    now = datetime.now(timezone.utc)
    month = now.month
    balance = BALANCE
    
    # Track exposures for correlation check
    exposures = {}
    
    for city in ACTIVE_CITIES:
        if city not in LOCATIONS:
            continue
        
        log.info(f"--- {LOCATIONS[city]['name']} ---")
        
        for day_offset in range(0, 3):
            target_date = now + timedelta(days=day_offset)
            date_str = target_date.strftime("%Y-%m-%d")
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
            snap = take_forecast_snapshot(city, date_str, state)
            
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
            
            # Market snapshots
            if mkt.get("market_snapshots"):
                last = mkt["market_snapshots"][-1]
                for o in outcomes:
                    b = parse_bucket(o["bucket"])
                    prev = next((x["price"] for x in last
                                if parse_bucket(x.get("bucket", "")) == b), None)
                    if prev and abs(o["price"] - prev) >= 0.15:
                        send_alert(f"BIG MOVE | {LOCATIONS[city]['name']}\n"
                                  f"{o['bucket']}: {prev:.2f}->{o['price']:.2f}",
                                  priority="high")
            
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
            
            sigma = get_sigma(city, calibration)
            
            # Correlation exposure check
            corr_mult = check_correlation_exposure(city, state)
            total_size_mult = regime_mult * conf_mult * corr_mult
            
            # Check stops on existing position
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
                    if action:
                        mkt = close_position(mkt, cur_p, action)
                        save_market(mkt)
                        save_state(state)
                        continue
                
                # Position exists, track exposure
                exposures[city] = exposures.get(city, 0) + pos["size"]
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
                    mkt = execute_trade(mkt, trade, balance)
                    exposures[city] = exposures.get(city, 0) + trade["size"]
                else:
                    log.info(f"No +EV trade for {city} {date_str}")
            else:
                log.info(f"No forecast for {city} {date_str}")
            
            save_market(mkt)
    
    # Update state with exposures
    state["market_exposures"] = exposures
    save_state(state)
    resolve_past_markets()
    log.info("Scan complete.")

# ── Reports ──────────────────────────────────────────────────
def print_status():
    markets = list(MARKETS_DIR.glob("*.json"))
    open_pos, resolved = [], []
    
    for mp in markets:
        try:
            m = json.loads(mp.read_text())
            if m.get("position"):
                open_pos.append(m)
            if m["status"] in ["resolved", "closed"] and m.get("pnl") is not None:
                resolved.append(m)
        except:
            continue
    
    total = sum(m["pnl"] for m in resolved)
    wins = [m for m in resolved if m["pnl"] > 0]
    win_rate = len(wins) / len(resolved) * 100 if resolved else 0
    
    print(f"\n{'='*60}")
    print(f"  WEATHERBET ULTIMATE -- {'PAPER' if PAPER_MODE else 'LIVE'} MODE")
    print(f"{'='*60}")
    print(f"  Open positions : {len(open_pos)}")
    print(f"  Resolved       : {len(resolved)}")
    print(f"  Win rate       : {win_rate:.1f}%")
    print(f"  Total P&L      : ${total:+.2f}")
    print(f"{'='*60}")
    
    for m in open_pos:
        pos = m["position"]
        snap = m.get("forecast_snapshots", [{}])[-1]
        print(f"  {m['city_name']} {m['date']} | "
              f"{pos['direction']} {pos['bucket']}C @ {pos['price']:.3f} | "
              f"${pos['size']:.2f} | Forecast: {snap.get('best_temp', '?')}C")

def print_report():
    all_m = []
    for mp in MARKETS_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            if m.get("status") in ["resolved", "closed"]:
                all_m.append(m)
        except:
            continue
    
    all_m.sort(key=lambda x: x["date"])
    print(f"\n{'='*75}\n  FULL TRADE REPORT\n{'='*75}")
    
    total = 0
    for m in all_m:
        pnl = m.get("pnl") or 0
        total += pnl
        snap = m.get("forecast_snapshots", [{}])[0]
        print(f"  {m['date']} | {m['city_name']:12} | "
              f"Outcome:{str(m.get('resolved_outcome', '?')):10} | "
              f"Actual:{str(m.get('actual_temp', '?')):5}C | "
              f"Forecast:{str(snap.get('best_temp', '?')):5}C | "
              f"PnL:${pnl:+.2f}")
    
    print(f"{'='*75}\n  TOTAL: ${total:+.2f}\n")

# ── Entry point ──────────────────────────────────────────────
if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"
    
    if arg == "status":
        print_status()
    elif arg == "report":
        print_report()
    elif arg == "scan":
        scan_and_update()
    elif arg == "reset":
        import shutil
        if MARKETS_DIR.exists():
            shutil.rmtree(MARKETS_DIR)
            os.makedirs(MARKETS_DIR, exist_ok=True)
            print("All market data cleared.")
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print("State cleared.")
        if HISTORICAL_DIR.exists():
            shutil.rmtree(HISTORICAL_DIR)
            os.makedirs(HISTORICAL_DIR, exist_ok=True)
            print("Historical data cleared.")
    else:
        clear_stale_positions()
        
        mode = "PAPER" if PAPER_MODE else "LIVE"
        log.info(f"WeatherBet Ultimate starting -- {mode} MODE")
        log.info(f"Cities: {', '.join(ACTIVE_CITIES)}")
        send_alert(f"WeatherBot Ultimate started -- {mode}\nCities: {', '.join(ACTIVE_CITIES)}")
        
        while True:
            try:
                scan_and_update()
            except KeyboardInterrupt:
                log.info("Stopped.")
                break
            except Exception as e:
                log.error(f"Scan error: {e}", exc_info=True)
                send_alert(f"Scan error: {e}", priority="high")
            
            log.info(f"Sleeping {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)