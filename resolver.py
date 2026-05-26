import json, logging, math, requests, time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from config import LOCATIONS, MARKETS_DIR
from state import update_historical, save_market, send_alert
from signals import parse_bucket, POLY_API
from learning import update_learning

log = logging.getLogger("weatherbet")

# ── Open-Meteo archive fetch ─────────────────────────────────
# Sentinel returned when every retry attempt times out.
# Distinguishes "transient network failure" from "data not available yet" (None).
_ARCHIVE_TIMEOUT = object()

_ARCHIVE_RETRY_DELAYS = (2, 4, 8)   # seconds before each retry; len+1 = total attempts


def fetch_actual_temp(city: str, date_str: str):
    """
    Fetch recorded daily max temp from Open-Meteo archive API.
    Archive data typically lags 2-5 days; returns None when not yet available.

    On Timeout retries with exponential backoff — 3 retries, delays 2s → 4s → 8s
    (4 total attempts).  Returns _ARCHIVE_TIMEOUT sentinel when all attempts
    time out so callers can distinguish network failures from missing data.
    Non-timeout errors (HTTP 4xx/5xx, JSON parse) return None immediately
    since retrying won't help.
    """
    loc = LOCATIONS[city]
    url = (f"https://archive-api.open-meteo.com/v1/archive"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&start_date={date_str}&end_date={date_str}"
           f"&daily=temperature_2m_max"
           f"&temperature_unit=celsius"
           f"&timezone={loc['timezone']}")

    max_attempts = len(_ARCHIVE_RETRY_DELAYS) + 1   # 4 total

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(url, timeout=12)
            r.raise_for_status()
            temps = r.json().get("daily", {}).get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                return round(float(temps[0]), 1)
            return None   # request succeeded but archive data not available yet
        except requests.exceptions.Timeout:
            if attempt < max_attempts:
                delay = _ARCHIVE_RETRY_DELAYS[attempt - 1]
                log.warning(f"Archive timeout {city} {date_str} "
                            f"(attempt {attempt}/{max_attempts}) — retrying in {delay}s")
                time.sleep(delay)
            else:
                log.warning(f"Archive timeout {city} {date_str} — "
                            f"all {max_attempts} attempts timed out, giving up")
                return _ARCHIVE_TIMEOUT
        except Exception as e:
            log.warning(f"Archive fetch failed {city} {date_str}: {e}")
            return None   # non-timeout errors don't benefit from retry

    return _ARCHIVE_TIMEOUT   # unreachable; satisfies linter


def _polymarket_actual_fallback(m: dict) -> Optional[float]:
    """
    When the Open-Meteo archive API is unavailable, infer the actual temperature
    from the Polymarket resolved market data.

    Strategy: fetch the event from Polymarket, find the market whose YES token
    price == 1.0 (the resolved winner), match it back to a bucket label stored
    in m["all_outcomes"] via market_id, then return parse_bucket(label).

    Suitable for calibration only — resolution is at bucket granularity (1°C),
    not sub-degree.  Returns None if the market is not yet resolved or no clear
    winner is found.
    """
    event_id     = m.get("event_id")
    all_outcomes = m.get("all_outcomes", [])
    if not event_id or not all_outcomes:
        return None

    # Build market_id → bucket label lookup from stored outcomes
    id_to_bucket = {str(o.get("market_id", "")): o.get("bucket", "")
                    for o in all_outcomes if o.get("market_id")}
    if not id_to_bucket:
        return None

    try:
        r = requests.get(f"{POLY_API}/events/{event_id}", timeout=10)
        r.raise_for_status()
        data = r.json()

        for mkt in data.get("markets", []):
            mkt_id = str(mkt.get("id", ""))
            if mkt_id not in id_to_bucket:
                continue

            # outcomePrices is [yes_price, no_price] as strings or floats
            raw = mkt.get("outcomePrices", "[]")
            try:
                prices = ([float(p) for p in json.loads(raw)]
                          if isinstance(raw, str) else [float(p) for p in raw])
            except Exception:
                continue

            if not prices or prices[0] < 0.99:
                continue   # not resolved YES, or not the winner

            bucket_label = id_to_bucket[mkt_id]
            temp_raw = parse_bucket(bucket_label)
            if temp_raw is not None:
                # Polymarket US-city markets use °F labels — convert to °C.
                if "°F" in bucket_label:
                    temp_c = round((temp_raw - 32) * 5 / 9, 1)
                else:
                    temp_c = float(temp_raw)
                log.info(
                    f"[POLY FALLBACK] {m.get('city')} {m.get('date')}: "
                    f"resolved bucket '{bucket_label}' → {temp_c}°C "
                    f"(used for calibration; archive unavailable)"
                )
                return temp_c

    except Exception as e:
        log.warning(f"Polymarket fallback failed {m.get('city')} {m.get('date')}: {e}")

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
    Called on every scan. Handles two cases:

    1. "open" markets past their date: fetch actual temp, compute PnL, mark resolved.
    2. "closed" markets (stop-loss / TP2 triggered): fetch actual temp if missing,
       then record into learning.json. Status stays "closed".

    Both paths call update_learning() once, guarded by learning_recorded flag.
    """
    today = datetime.now(timezone.utc).date()
    resolved_count   = 0
    archive_ok       = 0   # archive fetch succeeded
    archive_timeout  = 0   # all retries exhausted (network issue)
    archive_fallback = 0   # archive timed out but Polymarket resolved market used
    archive_miss     = 0   # fetch returned None (data not ready yet)

    for mp in MARKETS_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            city = m.get("city")
            if city not in LOCATIONS:
                continue

            mkt_date = datetime.strptime(m["date"], "%Y-%m-%d").date()
            if mkt_date > today:
                continue

            status = m.get("status")

            # ── "closed" markets (stop-loss / TP2) ───────────────────
            # Status stays "closed". We only need actual_temp for learning.
            if status == "closed":
                if m.get("learning_recorded"):
                    continue
                if m.get("actual_temp") is None:
                    actual = fetch_actual_temp(city, m["date"])
                    if actual is _ARCHIVE_TIMEOUT:
                        archive_timeout += 1
                        actual = _polymarket_actual_fallback(m)
                        if actual is None:
                            continue
                        archive_fallback += 1
                    elif actual is None:
                        archive_miss += 1
                        continue
                    else:
                        archive_ok += 1
                    m["actual_temp"] = actual
                update_learning(city, m)
                m["learning_recorded"] = True
                save_market(m)
                continue

            # ── already fully processed ───────────────────────────────
            if status == "resolved":
                continue

            # ── "open" markets past their date ────────────────────────
            if m.get("actual_temp") is None:
                actual = fetch_actual_temp(city, m["date"])
                if actual is _ARCHIVE_TIMEOUT:
                    archive_timeout += 1
                    actual = _polymarket_actual_fallback(m)
                    if actual is None:
                        continue
                    archive_fallback += 1
                elif actual is None:
                    log.info(f"Archive not yet available: {city} {m['date']}")
                    archive_miss += 1
                    continue
                else:
                    archive_ok += 1
                m["actual_temp"] = actual
            else:
                actual = m["actual_temp"]

            m["resolved_outcome"] = _match_bucket(actual, m.get("all_outcomes", []))
            m["status"] = "resolved"

            # Binary PnL — call update_learning BEFORE clearing position
            if m.get("position"):
                pos = m["position"]
                resolved_val = parse_bucket(m.get("resolved_outcome") or "")
                direction    = pos["direction"]
                pos_bucket   = pos["bucket"]

                if direction == "YES":
                    won = resolved_val is not None and pos_bucket == resolved_val
                else:
                    won = resolved_val is None or pos_bucket != resolved_val

                pnl = round(pos["size"] * (1.0 / pos["price"] - 1.0), 2) if won else -pos["size"]
                m["pnl"] = pnl

                result_msg = (f"RESOLUTION {'WIN' if won else 'LOSS'} | "
                              f"{m['city_name']} {m['date']} | PnL: ${pnl:+.2f}\n"
                              f"  Bet: {direction} {pos_bucket}C | "
                              f"Actual: {actual}C | Resolved: {m.get('resolved_outcome', '?')}")
                log.info(result_msg)
                send_alert(result_msg, priority="high" if pnl < 0 else "normal")

                # Record learning while position data is still intact
                if not m.get("learning_recorded"):
                    update_learning(city, m)
                    m["learning_recorded"] = True

                m["position"] = None

            # Feed historical data for sigma calibration
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

    total_attempts = archive_ok + archive_timeout + archive_miss
    if total_attempts > 0:
        log.info(
            f"[ARCHIVE SUMMARY] fetched={archive_ok} timed_out={archive_timeout} "
            f"poly_fallback={archive_fallback} not_ready={archive_miss} "
            f"(of {total_attempts} attempt(s) this scan)"
        )

    return resolved_count
