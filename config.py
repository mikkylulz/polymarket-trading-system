import socket, sys, os, json, logging
from pathlib import Path
from dotenv import load_dotenv

# ── Windows Unicode fix ──────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

socket.setdefaulttimeout(30)

# ── Create data folders BEFORE logging ───────────────────────
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

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

# ── Credential validation ─────────────────────────────────────
_REQUIRED_ENV = [
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "VC_API_KEY",
]
_missing = [v for v in _REQUIRED_ENV if not os.getenv(v)]
if _missing:
    for _v in _missing:
        logging.getLogger("weatherbet").error(f"Missing required .env variable: {_v}")
    sys.exit(1)

CFG  = json.loads((BASE / "config.json").read_text())

BALANCE        = CFG["balance"]
MAX_BET        = CFG["max_bet"]
MIN_BET        = CFG["min_bet"]
MIN_EV         = CFG["min_ev"]
MIN_PRICE       = CFG["min_price"]
MIN_ENTRY_PRICE = CFG.get("min_entry_price", 0.15)
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
VC_KEY         = os.getenv("VC_API_KEY", "")
TG_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT        = os.getenv("TELEGRAM_CHAT_ID", "")
POLY_KEY        = os.getenv("POLYMARKET_API_KEY", "")
POLY_SECRET     = os.getenv("POLYMARKET_SECRET", "")
POLY_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")
PRIVATE_KEY          = os.getenv("PRIVATE_KEY", "")
POLYMARKET_PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", "")
BTC_LIVE_MODE   = os.getenv("BTC_LIVE_MODE", "false").lower() == "true"
ACTIVE_CITIES       = CFG["active_cities"]
LIVE_CITIES         = CFG.get("live_cities", CFG["active_cities"])
RAPID_SCAN_CITIES   = CFG.get("rapid_scan_cities", ["hong_kong", "singapore", "tokyo", "seoul", "chengdu"])
RAPID_SCAN_INTERVAL = CFG.get("rapid_scan_interval", 60)
RAPID_START_UTC     = CFG.get("rapid_start_utc", 1)
RAPID_END_UTC       = CFG.get("rapid_end_utc", 6)
MAX_BUCKET_DISTANCE = CFG.get("max_bucket_distance", 1.5)

# ── City definitions — RESOLUTION STATION COORDINATES ────────
# CRITICAL FIX: Use actual resolution station coordinates, not city centers
LOCATIONS = {
    # ── Asia Pacific ──
    "hong_kong": {
        "name": "Hong Kong",
        "station": "VHHH", "timezone": "Asia/Hong_Kong",
        "slug_prefix": "hong-kong", "series_slug": "hong-kong-daily-weather",
        "lat": 22.3080, "lon": 114.1747,
        "resolution_station": "HKO Observatory",
        "bias": -1.0, "sigma": 1.5,
        "agency": "HKO", "agency_url": "https://www.hko.gov.hk/textonly/v2/forecast/nday_v2.htm",
    },
    "singapore": {
        "name": "Singapore",
        "station": "WSSS", "timezone": "Asia/Singapore",
        "slug_prefix": "singapore", "series_slug": "singapore-daily-weather",
        # Changi Airport coordinates (actual resolution)
        "lat": 1.3502, "lon": 103.9940,
        "resolution_station": "WSSS Changi",
        "bias": -2.5, "sigma": 2.0,
        "agency": "NEA", "agency_url": "https://www.nea.gov.sg/api/weather/4day",
    },
    "tokyo": {
        "name": "Tokyo",
        "station": "RJTT", "timezone": "Asia/Tokyo",
        "slug_prefix": "tokyo", "series_slug": "tokyo-daily-weather",
        # Haneda Airport coordinates
        "lat": 35.5494, "lon": 139.7798,
        "resolution_station": "RJTT Haneda",
        "bias": -0.5, "sigma": 1.5,
        "agency": "JMA", "agency_url": "https://www.jma.go.jp/bosai/forecast/",
    },
    "seoul": {
        "name": "Seoul",
        "station": "RKSS", "timezone": "Asia/Seoul",
        "slug_prefix": "seoul", "series_slug": "seoul-daily-weather",
        # Gimpo Airport (RKSS) — official Polymarket resolution station
        "lat": 37.5583, "lon": 126.7906,
        "resolution_station": "RKSS Gimpo Airport",
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
    "new_york": {
        "name": "New York",
        "station": "KLGA", "timezone": "America/New_York",
        "slug_prefix": "new-york-city", "series_slug": "new-york-daily-weather",
        "slug_prefix_alts": ["new-york-city", "new-york", "nyc"],
        "title_search_terms": ["new york", "nyc"],
        # LaGuardia Airport (KLGA) — official Polymarket resolution station
        "lat": 40.7773, "lon": -73.8726,
        "resolution_station": "KLGA LaGuardia Airport",
        "bias": 0.0, "sigma": 2.0,
        "agency": "NWS", "agency_url": "https://forecast.weather.gov/MapClick.php?CityName=New+York&state=NY",
    },
    # ── Additional Asia Pacific ──
    "taipei": {
        "name": "Taipei",
        "station": "RCSS", "timezone": "Asia/Taipei",
        "slug_prefix": "taipei", "series_slug": "taipei-daily-weather",
        "lat": 25.0693, "lon": 121.5521,
        "resolution_station": "RCSS Songshan Airport",
        "bias": 0.0, "sigma": 2.0,
        "agency": "CWA", "agency_url": "https://www.cwa.gov.tw/V8/E/W/Town/Town.html?TID=6300300",
    },
    "shanghai": {
        "name": "Shanghai",
        "station": "ZSPD", "timezone": "Asia/Shanghai",
        "slug_prefix": "shanghai", "series_slug": "shanghai-daily-weather",
        # Pudong International Airport (ZSPD) — official Polymarket resolution station
        "lat": 31.1443, "lon": 121.8083,
        "resolution_station": "ZSPD Pudong Airport",
        "bias": +0.5, "sigma": 2.0,
        "agency": "CMA", "agency_url": "http://www.nmc.cn/publish/forecast/AHB/shanghai.html",
    },
    "shenzhen": {
        "name": "Shenzhen",
        "station": "ZGSZ", "timezone": "Asia/Shanghai",
        "slug_prefix": "shenzhen", "series_slug": "shenzhen-daily-weather",
        "lat": 22.6395, "lon": 113.8107,
        "resolution_station": "ZGSZ Shenzhen Airport",
        "bias": 0.0, "sigma": 2.0,
        "agency": "CMA", "agency_url": "http://www.nmc.cn/publish/forecast/AGD/shenzhen.html",
    },
    # ── US Cities ──
    "seattle": {
        "name": "Seattle",
        "station": "KSEA", "timezone": "America/Los_Angeles",
        "slug_prefix": "seattle", "series_slug": "seattle-daily-weather",
        "lat": 47.4502, "lon": -122.3088,
        "resolution_station": "KSEA Sea-Tac Airport",
        "bias": 0.0, "sigma": 2.0,
        "agency": "NWS", "agency_url": "https://forecast.weather.gov/MapClick.php?CityName=Seattle&state=WA",
    },
    "denver": {
        "name": "Denver",
        "station": "KBKF", "timezone": "America/Denver",
        "slug_prefix": "denver", "series_slug": "denver-daily-weather",
        # Buckley Space Force Base (KBKF) — official Polymarket resolution station
        "lat": 39.7005, "lon": -104.7519,
        "resolution_station": "KBKF Buckley SFB",
        "bias": 0.0, "sigma": 2.5,  # Chinook winds cause rapid swings
        "agency": "NWS", "agency_url": "https://forecast.weather.gov/MapClick.php?CityName=Denver&state=CO",
    },
    "dallas": {
        "name": "Dallas",
        "station": "KDAL", "timezone": "America/Chicago",
        "slug_prefix": "dallas", "series_slug": "dallas-daily-weather",
        # Love Field (KDAL) — official Polymarket resolution station
        "lat": 32.8471, "lon": -96.8517,
        "resolution_station": "KDAL Love Field",
        "bias": 0.0, "sigma": 2.5,  # Severe weather / cold front passages
        "agency": "NWS", "agency_url": "https://forecast.weather.gov/MapClick.php?CityName=Dallas&state=TX",
    },
    "atlanta": {
        "name": "Atlanta",
        "station": "KATL", "timezone": "America/New_York",
        "slug_prefix": "atlanta", "series_slug": "atlanta-daily-weather",
        "lat": 33.6407, "lon": -84.4277,
        "resolution_station": "KATL Hartsfield-Jackson Airport",
        "bias": 0.0, "sigma": 2.0,
        "agency": "NWS", "agency_url": "https://forecast.weather.gov/MapClick.php?CityName=Atlanta&state=GA",
    },
    "buenos_aires": {
        "name": "Buenos Aires",
        "station": "SAEZ", "timezone": "America/Argentina/Buenos_Aires",
        "slug_prefix": "buenos-aires", "series_slug": "buenos-aires-daily-weather",
        # Ezeiza International (SAEZ) — official Polymarket resolution station
        "lat": -34.8222, "lon": -58.5358,
        "resolution_station": "SAEZ Ezeiza International",
        "bias": 0.0, "sigma": 2.0,
        "agency": "SMN", "agency_url": "https://www.smn.gob.ar/pronostico",
        "southern_hemisphere": True,
    },
    # ── Additional US Cities ──
    "chicago": {
        "name": "Chicago",
        "station": "KORD", "timezone": "America/Chicago",
        "slug_prefix": "chicago", "series_slug": "chicago-daily-weather",
        "slug_prefix_alts": ["chicago"],
        "title_search_terms": ["chicago"],
        # O'Hare Airport — official resolution station
        "lat": 41.9796, "lon": -87.9046,
        "resolution_station": "KORD O'Hare Airport",
        "bias": 0.0, "sigma": 2.5,  # Lake-effect volatility
        "agency": "NWS", "agency_url": "https://forecast.weather.gov/MapClick.php?CityName=Chicago&state=IL",
    },
    "miami": {
        "name": "Miami",
        "station": "KMIA", "timezone": "America/New_York",
        "slug_prefix": "miami", "series_slug": "miami-daily-weather",
        "slug_prefix_alts": ["miami"],
        "title_search_terms": ["miami"],
        # Miami Intl Airport
        "lat": 25.7959, "lon": -80.2870,
        "resolution_station": "KMIA Miami Intl Airport",
        "bias": 0.0, "sigma": 1.5,  # Tropical = tight distribution
        "agency": "NWS", "agency_url": "https://forecast.weather.gov/MapClick.php?CityName=Miami&state=FL",
    },
    # ── Southern Hemisphere ──
    "wellington": {
        "name": "Wellington",
        "station": "NZWN", "timezone": "Pacific/Auckland",
        "slug_prefix": "wellington", "series_slug": "wellington-daily-weather",
        "slug_prefix_alts": ["wellington"],
        "title_search_terms": ["wellington"],
        # Wellington Airport
        "lat": -41.3272, "lon": 174.8052,
        "resolution_station": "NZWN Wellington Airport",
        "bias": 0.0, "sigma": 2.0,
        "southern_hemisphere": True,  # seasons reversed vs Northern Hemisphere
        "agency": "MetService", "agency_url": "https://www.metservice.com/towns-cities/locations/wellington",
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
    "new_york": {
        "summer":        {"months": [6,7,8],        "size": 1.0,  "note": "Hot/humid, more predictable"},
        "spring":        {"months": [3,4,5],        "size": 0.5,  "note": "Variable fronts"},
        "autumn":        {"months": [9,10,11],      "size": 0.75, "note": "Decent"},
        "winter":        {"months": [12,1,2],       "size": 0.25, "note": "Avoid - cold/snow events"},
    },
    "taipei": {
        "summer":        {"months": [5,6,7,8,9],   "size": 1.5,  "note": "Hot/humid, stable"},
        "typhoon":       {"months": [7,8,9],        "size": 0.5,  "note": "Override: typhoon risk"},
        "winter":        {"months": [12,1,2,3],    "size": 0.5,  "note": "NE monsoon, cooler"},
        "spring":        {"months": [4],            "size": 0.75, "note": "Warming transition"},
        "autumn":        {"months": [10,11],        "size": 0.75, "note": "Decent, post-typhoon"},
    },
    "shanghai": {
        "summer":        {"months": [6,7,8],        "size": 1.0,  "note": "Hot, some typhoon risk"},
        "spring":        {"months": [3,4,5],        "size": 0.75, "note": "Warming, fronts"},
        "autumn":        {"months": [9,10,11],      "size": 1.0,  "note": "Best — stable high pressure"},
        "winter":        {"months": [12,1,2],       "size": 0.5,  "note": "Cold fronts from north"},
    },
    "shenzhen": {
        "sw_monsoon":    {"months": [5,6,7,8,9],   "size": 1.5,  "note": "Best — like HK"},
        "transition":    {"months": [3,4,10,11],   "size": 0.5,  "note": "Frontal chaos"},
        "ne_monsoon":    {"months": [12,1,2],       "size": 0.75, "note": "Drier, more predictable"},
    },
    "seattle": {
        "summer":        {"months": [6,7,8,9],      "size": 1.0,  "note": "Dry/sunny, predictable"},
        "spring":        {"months": [3,4,5],        "size": 0.5,  "note": "Rainy, variable"},
        "autumn":        {"months": [10,11],        "size": 0.75, "note": "Decent before rain sets in"},
        "winter":        {"months": [12,1,2],       "size": 0.25, "note": "Avoid - persistent rain/fog"},
    },
    "denver": {
        "summer":        {"months": [6,7,8],        "size": 1.0,  "note": "Warm, afternoon storms"},
        "spring":        {"months": [3,4,5],        "size": 0.5,  "note": "Chinook wind events"},
        "autumn":        {"months": [9,10,11],      "size": 0.75, "note": "Decent, early snows"},
        "winter":        {"months": [12,1,2],       "size": 0.25, "note": "Avoid - blizzard risk"},
    },
    "dallas": {
        "summer":        {"months": [5,6,7,8,9],   "size": 1.0,  "note": "Hot and humid, predictable"},
        "spring":        {"months": [3,4],          "size": 0.5,  "note": "Severe weather season"},
        "autumn":        {"months": [10,11],        "size": 0.75, "note": "Decent"},
        "winter":        {"months": [12,1,2],       "size": 0.25, "note": "Avoid - ice event risk"},
    },
    "atlanta": {
        "summer":        {"months": [6,7,8,9],      "size": 1.0,  "note": "Hot/humid, stable"},
        "spring":        {"months": [3,4,5],        "size": 0.5,  "note": "Severe weather season"},
        "autumn":        {"months": [10,11],        "size": 0.75, "note": "Decent"},
        "winter":        {"months": [12,1,2],       "size": 0.5,  "note": "Mild but ice event risk"},
    },
    "buenos_aires": {
        # Southern Hemisphere — summer = Dec–Feb
        "summer":        {"months": [12,1,2],       "size": 1.0,  "note": "Hot/humid, predictable"},
        "autumn":        {"months": [3,4,5],        "size": 0.75, "note": "Cooling, some fronts"},
        "winter":        {"months": [6,7,8],        "size": 0.5,  "note": "Dry/mild, stable"},
        "spring":        {"months": [9,10,11],      "size": 0.75, "note": "Warming, variable"},
    },
    "chicago": {
        "summer":        {"months": [6,7,8],        "size": 1.0,  "note": "Warm/humid, predictable"},
        "spring":        {"months": [3,4,5],        "size": 0.5,  "note": "Severe weather season"},
        "autumn":        {"months": [9,10,11],      "size": 0.75, "note": "Decent before cold sets in"},
        "winter":        {"months": [12,1,2],       "size": 0.0,  "note": "SKIP — extreme cold/lake-effect"},
    },
    "miami": {
        "dry_season":    {"months": [11,12,1,2,3,4],"size": 1.5, "note": "Best — stable sunshine, tight range"},
        "wet_season":    {"months": [5,6,7,8,9,10], "size": 0.75,"note": "Afternoon storms cap temps"},
        "hurricane":     {"months": [8,9,10],       "size": 0.25, "note": "Override: hurricane risk"},
    },
    "wellington": {
        # Southern Hemisphere — seasons reversed
        "summer":        {"months": [12,1,2],       "size": 1.0,  "note": "Warmest, still windy"},
        "autumn":        {"months": [3,4,5],        "size": 0.75, "note": "Decent, cooling"},
        "winter":        {"months": [6,7,8],        "size": 0.25, "note": "Avoid — cold/stormy"},
        "spring":        {"months": [9,10,11],      "size": 0.75, "note": "Warming but notoriously windy"},
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
    "new_york":  {"london": 0.3, "atlanta": 0.3, "dallas": 0.2},
    "taipei":    {"hong_kong": 0.6, "shanghai": 0.5, "shenzhen": 0.5},
    "shanghai":  {"taipei": 0.5, "shenzhen": 0.4, "chengdu": 0.3},
    "shenzhen":  {"hong_kong": 0.7, "taipei": 0.5, "singapore": 0.3},
    "seattle":   {"denver": 0.3},
    "denver":    {"seattle": 0.3, "dallas": 0.3},
    "dallas":    {"atlanta": 0.4, "denver": 0.3, "new_york": 0.2},
    "atlanta":   {"dallas": 0.4, "new_york": 0.3},
}

# ── File paths ───────────────────────────────────────────────
STATE_FILE     = BASE / "data" / "state.json"
MARKETS_DIR    = BASE / "data" / "markets"
CALIB_FILE     = BASE / "data" / "calibration.json"
HISTORICAL_DIR = BASE / "data" / "historical"
CACHE_DIR      = BASE / "data" / "cache"

for _d in [MARKETS_DIR, HISTORICAL_DIR, CACHE_DIR]:
    _d.mkdir(parents=True, exist_ok=True)
