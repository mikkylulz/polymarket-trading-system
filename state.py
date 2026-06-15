import json, logging, requests
from datetime import datetime, timezone
from typing import Dict, List, Optional
from config import (CFG, TG_TOKEN, TG_CHAT, STATE_FILE, MARKETS_DIR, CALIB_FILE,
                    HISTORICAL_DIR, LOCATIONS, ACTIVE_CITIES)

log = logging.getLogger("weatherbet")

# ── State management ─────────────────────────────────────────
_DEFAULT_STATE = {
    "seoul_front_day": 0,
    "seoul_front_date": None,
    "last_precip": {},
    "market_exposures": {},
    "session_pnl": 0.0,
}

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            log.warning("state.json is empty or corrupt — resetting to defaults")
    return dict(_DEFAULT_STATE)

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))

# ── Historical data management ───────────────────────────────
def load_historical(city: str) -> List[Dict]:
    """Load historical resolutions for recency bias detection."""
    path = HISTORICAL_DIR / f"{city}_history.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError):
            log.warning(f"{city}_history.json is empty or corrupt — returning []")
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
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": f"{emoji} WEATHERBET\n{msg}"},
            timeout=8
        )
        if r.status_code != 200:
            # e.g. bad chat_id, bot blocked, message too long — was silent before
            log.warning(f"Telegram rejected alert (HTTP {r.status_code}): "
                        f"{r.text[:200]}")
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

# ── Market storage ───────────────────────────────────────────
def market_path(city, date_str):
    return MARKETS_DIR / f"{city}_{date_str}.json"

def load_market(city, date_str):
    p = market_path(city, date_str)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, ValueError):
        log.warning(f"{p.name} is empty or corrupt — treating as missing")
        return None

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
    if not CALIB_FILE.exists():
        return {}
    try:
        return json.loads(CALIB_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        log.warning("calibration.json is empty or corrupt — returning {}")
        return {}

def save_calibration(d):
    CALIB_FILE.write_text(json.dumps(d, indent=2))

# ── Learned sigma ────────────────────────────────────────────
def compute_learned_sigma(city: str) -> Optional[Dict]:
    """
    Compute per-city forecast sigma as the sample std dev of
    (best_temp - actual_temp), one data point per resolved market
    (last snapshot only — closest to resolution time).
    Returns None when fewer than calibration_min markets exist.
    """
    import math
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

            # Update historical database (once per market — guard prevents repeated writes)
            if not m.get("history_recorded"):
                for snap in m.get("forecast_snapshots", []):
                    forecast = snap.get("best_temp")
                    if forecast:
                        update_historical(city, m["date"], actual, forecast)
                m["history_recorded"] = True
                save_market(m)

            # Calculate errors (always — sigma recalibration needs fresh aggregation)
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
