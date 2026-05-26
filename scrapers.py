import logging, re, requests
from typing import Dict
from config import LOCATIONS, SG_SHOWER_RULES

log = logging.getLogger("weatherbet")

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
