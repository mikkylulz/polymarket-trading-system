"""
elon_tweet_bot.py — Polymarket Elon Musk tweet-count market bot.

Strategy
--------
Tracks Elon's current tweet pace via XTracker, projects the final weekly
count, and buys YES positions on the bucket(s) closest to that projection
while prices are low (0.01 – 0.21).  Exits on 3×/5× profit, pace drift,
or time-based cleanup in the final 6 hours.

All positions share the same Polymarket wallet as the crypto scanners.

Run via:  python main.py elon
Data:     data/elon_trades.json
"""

import json, logging, math, os, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List

import requests

from config import BASE
from state  import send_alert
from xtracker_client import (
    _fetch_elon_active_trackings,
    _fetch_elon_tweet_count,
    _calculate_pace_metrics,
)

log = logging.getLogger("weatherbet")

# ══════════════════════════════════════════════════════════════════════════════
# Configuration (Phase 8)
# ══════════════════════════════════════════════════════════════════════════════

ELON_LIVE_MODE            = False   # PAPER MODE — never enable until 2 weeks validated
ELON_PAUSED               = os.getenv("ELON_PAUSED", "false").lower() == "true"
ELON_MAX_POSITIONS        = 5
ELON_MAX_POSITION_SIZE    = 2.00
ELON_TOTAL_EXPOSURE       = 10.00

ELON_PRICE_MIN            = 0.005
ELON_PRICE_MAX_PRIMARY    = 0.10
ELON_PRICE_MAX_EXTENDED   = 0.21
ELON_MIN_SCORE_ENTRY      = 60

ELON_PROFIT_TAKE_MULT     = 3.0
ELON_FULL_EXIT_MULT       = 5.0
ELON_STOP_LOSS_PCT        = 0.60
ELON_PACE_DRIFT_THRESHOLD = 40       # tweets — exit if bucket drifts this far from projection

ELON_SCAN_INTERVAL        = 90       # seconds between scans
ELON_FINAL_HOUR_CUTOFF    = 6        # close non-center positions in last N hours

ELON_DATA_FILE            = BASE / "data" / "elon_trades.json"
ELON_BACKUP_FILE          = BASE / "data" / "elon_trades_backup.json"

POLY_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

_PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")
_PROXY_WALLET    = os.getenv("POLYMARKET_PROXY_WALLET", "")
_POLY_KEY        = os.getenv("POLYMARKET_API_KEY", "")
_POLY_SECRET     = os.getenv("POLYMARKET_SECRET", "")
_POLY_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")


# ══════════════════════════════════════════════════════════════════════════════
# Persistence
# ══════════════════════════════════════════════════════════════════════════════

def _load_trades() -> list:
    try:
        if ELON_DATA_FILE.exists():
            data = json.loads(ELON_DATA_FILE.read_text())
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save_trades(trades: list):
    ELON_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    ELON_DATA_FILE.write_text(json.dumps(trades, indent=2, default=str))
    # rolling backup
    ELON_BACKUP_FILE.write_text(json.dumps(trades, indent=2, default=str))


def _open_positions() -> List[dict]:
    return [t for t in _load_trades() if t.get("status") == "open"]


def _total_exposure() -> float:
    return sum(t.get("size", 0) for t in _open_positions())


# ══════════════════════════════════════════════════════════════════════════════
# Market discovery (Phase 2)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_bucket(title: str) -> dict:
    """Parse bucket title string into numeric bounds.

    '<20'  → low=0,   high=20,  center=10
    '500+' → low=500, high=999, center=750
    'X-Y'  → low=X,   high=Y,   center=(X+Y)/2
    """
    t = title.strip()
    if t.startswith("<"):
        high = float(t[1:])
        return {"name": t, "low": 0.0, "high": high, "center": high / 2}
    if t.endswith("+"):
        low = float(t[:-1])
        return {"name": t, "low": low, "high": 999.0, "center": low + 100.0}
    parts = t.split("-")
    if len(parts) == 2:
        try:
            low, high = float(parts[0]), float(parts[1])
            return {"name": t, "low": low, "high": high, "center": (low + high) / 2}
        except ValueError:
            pass
    return {"name": t, "low": 0.0, "high": 0.0, "center": 0.0}


_MONTH_NUM = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

def _title_duration_days(title: str) -> Optional[float]:
    """Parse title like 'Elon Musk # tweets June 5 - June 12, 2026?'
    to get the tracking window duration in days."""
    import re
    months = "|".join(_MONTH_NUM.keys())
    pat = rf'({months})\s+(\d{{1,2}}).*?({months})\s+(\d{{1,2}})'
    m = re.search(pat, title.lower())
    if not m:
        return None
    try:
        now = datetime.now(timezone.utc)
        y   = now.year
        s   = datetime(y, _MONTH_NUM[m.group(1)], int(m.group(2)), tzinfo=timezone.utc)
        e   = datetime(y, _MONTH_NUM[m.group(3)], int(m.group(4)), tzinfo=timezone.utc)
        if e < s:   # crosses year boundary
            e = e.replace(year=y + 1)
        return (e - s).days
    except Exception:
        return None


def _get_market_buckets(event: dict) -> List[dict]:
    """Extract all buckets from a Polymarket event dict."""
    buckets = []
    for m in event.get("markets", []):
        title = m.get("groupItemTitle", "")
        if not title:
            continue
        bucket = _parse_bucket(title)

        raw_prices = m.get("outcomePrices", "[]")
        try:
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            yes_p  = float(prices[0]) if prices else 0.0
            no_p   = float(prices[1]) if len(prices) > 1 else round(1.0 - yes_p, 4)
        except Exception:
            yes_p, no_p = 0.0, 1.0

        raw_ids      = (m.get("clobTokenIds") or "").split(",")
        yes_token_id = raw_ids[0].strip('"[] ') if raw_ids else ""
        no_token_id  = raw_ids[1].strip('"[] ') if len(raw_ids) > 1 else ""

        bucket.update({
            "market_id":    m.get("id", ""),
            "event_id":     event.get("id", ""),
            "event_title":  event.get("title", ""),
            "yes_price":    yes_p,
            "no_price":     no_p,
            "yes_token_id": yes_token_id,
            "no_token_id":  no_token_id,
            "volume":       float(m.get("volume") or 0),
        })
        buckets.append(bucket)

    buckets.sort(key=lambda b: b["low"])
    log.info(
        f"[ELON BUCKETS] {event.get('title','?')!r} has {len(buckets)} buckets, "
        f"prices {min(b['yes_price'] for b in buckets):.4f}–"
        f"{max(b['yes_price'] for b in buckets):.4f}"
    )
    return buckets


def _find_elon_tweet_markets() -> List[dict]:
    """Query Gamma API for active Elon tweet-count markets (7-day only).

    Uses the 'tweets-markets' tag slug which is specific to these markets.
    Filters to 7-day windows by parsing the title date range.
    """
    seen_ids: set      = set()
    elon_events: list  = []

    # Primary: tweets-markets tag (most targeted)
    for params in [
        {"active": "true", "closed": "false", "limit": 50, "tag_slug": "tweets-markets"},
        {"active": "true", "closed": "false", "limit": 50, "tag_slug": "pop-culture"},
    ]:
        try:
            r = requests.get(f"{POLY_API}/events", params=params, timeout=12)
            r.raise_for_status()
            raw = r.json()
            events = raw if isinstance(raw, list) else raw.get("data", [])
            for ev in events:
                slug  = (ev.get("slug")  or "").lower()
                title = (ev.get("title") or "").lower()
                if "elon" not in slug and "elon" not in title:
                    continue
                if "tweet" not in slug and "tweet" not in title:
                    continue
                eid = ev.get("id")
                if eid not in seen_ids:
                    seen_ids.add(eid)
                    elon_events.append(ev)
        except Exception as e:
            log.warning(f"[ELON MARKETS] fetch failed ({params.get('tag_slug')}): {e}")

    # Filter to 7-day windows only by parsing title
    weekly_markets = []
    for ev in elon_events:
        # Ensure we have the full markets list
        if not ev.get("markets"):
            try:
                r2 = requests.get(f"{POLY_API}/events/{ev['id']}", timeout=10)
                if r2.status_code == 200:
                    ev = r2.json()
            except Exception:
                pass

        dur = _title_duration_days(ev.get("title", ""))
        if dur is not None and 5 <= dur <= 9:
            ev["_duration_days"] = dur
            weekly_markets.append(ev)
        else:
            # Skip 2-day / daily markets (dur < 5)
            log.debug(f"[ELON MARKETS] skip {ev.get('title')!r} dur={dur}")

    log.info(f"[ELON MARKETS] found {len(weekly_markets)} active 7-day markets")
    return weekly_markets


# ══════════════════════════════════════════════════════════════════════════════
# Bucket scoring (Phase 3)
# ══════════════════════════════════════════════════════════════════════════════

def _score_bucket(bucket: dict, pace_projection: float,
                  velocity: str, current_pace: float) -> int:
    """Return 0-100 score for this bucket as an entry candidate."""
    center   = bucket["center"]
    yes_price = bucket["yes_price"]
    distance = abs(center - pace_projection)

    # Zone score: max 100 at center, 0 beyond 40 tweets away
    if distance < 40:
        zone_score = max(0, 100 - int(distance * 2.5))
    else:
        zone_score = 0

    # Price attractiveness
    if ELON_PRICE_MIN <= yes_price <= ELON_PRICE_MAX_PRIMARY:
        price_score = 100
    elif ELON_PRICE_MAX_PRIMARY < yes_price <= ELON_PRICE_MAX_EXTENDED:
        price_score = 50
    else:
        price_score = 0

    # Velocity bonus
    velocity_bonus = 0
    if velocity == "accelerating" and center > current_pace:
        velocity_bonus = 20
    elif velocity == "decelerating" and center < current_pace:
        velocity_bonus = 20

    score = int(zone_score * 0.5 + price_score * 0.4 + velocity_bonus)
    return min(100, max(0, score))


# ══════════════════════════════════════════════════════════════════════════════
# Live order (shared CLOB client)
# ══════════════════════════════════════════════════════════════════════════════

def _get_elon_clob_client():
    """Reuse the same lazy CLOB client initializer from crypto_scanner."""
    try:
        from crypto_scanner import _get_clob_client
        return _get_clob_client()
    except Exception as e:
        log.error(f"[ELON LIVE] CLOB client init failed: {e}")
        return None


def _elon_live_order(token_id: str, price: float, size: float) -> str:
    """Place a FOK limit buy order for an Elon market bucket.
    Returns order_id on success, empty string on failure.
    Only called when ELON_LIVE_MODE=True.
    """
    if not _PRIVATE_KEY:
        log.warning("[ELON LIVE] PRIVATE_KEY not set — cannot place order")
        return ""
    client = _get_elon_clob_client()
    if client is None:
        return ""
    try:
        from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
        price_r   = round(min(price + 0.03, 0.97), 4)
        usdc_size = round(size, 2)
        args      = MarketOrderArgsV2(token_id=token_id, amount=usdc_size)
        resp      = client.create_and_post_order(args, OrderType.FOK)
        oid       = (resp or {}).get("orderID") or (resp or {}).get("id") or ""
        if oid:
            log.info(f"[ELON LIVE] Order placed: {oid}")
        return oid or ""
    except Exception as e:
        log.error(f"[ELON LIVE] Order placement failed: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# Entry logic (Phase 5)
# ══════════════════════════════════════════════════════════════════════════════

def _enter_position(bucket: dict, score: int, pace: dict, tracking: dict):
    """Record (and optionally place) a new position."""
    yes_price = bucket["yes_price"]
    open_pos  = _open_positions()

    if len(open_pos) >= ELON_MAX_POSITIONS:
        log.info("[ELON ENTRY] at max positions — skip")
        return
    if _total_exposure() >= ELON_TOTAL_EXPOSURE:
        log.info("[ELON ENTRY] exposure cap reached — skip")
        return

    already_open = {t["token_id"] for t in open_pos}
    if bucket["yes_token_id"] in already_open:
        log.info(f"[ELON ENTRY] already holding {bucket['name']} — skip")
        return

    size = ELON_MAX_POSITION_SIZE if score > 80 else 1.00
    size = min(size, ELON_TOTAL_EXPOSURE - _total_exposure())
    if size < 0.50:
        log.info("[ELON ENTRY] not enough exposure room — skip")
        return

    mode_tag      = "ELON LIVE" if ELON_LIVE_MODE else "ELON PAPER"
    poly_order_id = ""
    if ELON_LIVE_MODE:
        poly_order_id = _elon_live_order(bucket["yes_token_id"], yes_price, size)
        if not poly_order_id:
            log.warning("[ELON LIVE] Order not confirmed — not recording")
            return

    shares = round(size / yes_price, 4) if yes_price > 0 else 0

    trade = {
        "id":               f"elon_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "opened_at":        datetime.now(timezone.utc).isoformat(),
        "market_title":     bucket["event_title"],
        "event_id":         bucket["event_id"],
        "market_id":        bucket["market_id"],
        "bucket_name":      bucket["name"],
        "token_id":         bucket["yes_token_id"],
        "entry_price":      yes_price,
        "size":             size,
        "shares":           shares,
        "pace_projection":  pace["tweets_per_hour_recent"] * (
            pace.get("hours_remaining", 0) + pace.get("hours_elapsed", 0)
        ) if "hours_remaining" in pace else None,
        "velocity":         pace.get("velocity", "stable"),
        "score_at_entry":   score,
        "tracking_id":      tracking.get("id", ""),
        "poly_order_id":    poly_order_id,
        "status":           "open",
        "profit_take_done": False,
        "won":              None,
        "pnl":              None,
        "exit_price":       None,
        "closed_at":        None,
        "exit_reason":      None,
    }

    trades = _load_trades()
    trades.append(trade)
    _save_trades(trades)

    projection = pace.get("projected_final", "?")
    msg = (
        f"[{mode_tag}] OPEN | {bucket['name']} @ ${yes_price:.4f}  size=${size:.2f}\n"
        f"  Market: {bucket['event_title']}\n"
        f"  Pace projection: {projection:.0f} tweets  distance: "
        f"{abs(bucket['center'] - float(projection)):.0f}  velocity: {pace.get('velocity')}\n"
        f"  Score: {score}  Shares: {shares:.2f}  Break-even WR: {yes_price*100:.1f}%"
    )
    log.info(msg)
    send_alert(msg, priority="normal")


# ══════════════════════════════════════════════════════════════════════════════
# Exit logic (Phase 6)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_current_price(token_id: str) -> Optional[float]:
    """Get current YES price for a token from Polymarket CLOB."""
    try:
        r = requests.get(f"{CLOB_API}/price",
                         params={"token_id": token_id, "side": "BUY"},
                         timeout=8)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception:
        pass
    # Fallback: use Gamma API last price from market
    return None


def _close_position(trade: dict, exit_price: float, reason: str):
    """Mark trade closed and compute P&L."""
    shares = trade.get("shares", 0)
    size   = trade.get("size", 0)

    # Gross payout = shares × $1 (each YES share pays $1 if wins)
    # For paper: P&L = (exit_price / entry_price - 1) × size
    pnl = round((exit_price / trade["entry_price"] - 1.0) * size, 2) if trade["entry_price"] > 0 else 0

    trades = _load_trades()
    for t in trades:
        if t.get("id") == trade["id"]:
            t.update({
                "status":      "closed",
                "exit_price":  exit_price,
                "closed_at":   datetime.now(timezone.utc).isoformat(),
                "pnl":         pnl,
                "exit_reason": reason,
            })
            break
    _save_trades(trades)

    mult = round(exit_price / trade["entry_price"], 2) if trade["entry_price"] > 0 else 0
    mode_tag = "ELON LIVE" if ELON_LIVE_MODE else "ELON PAPER"
    msg = (
        f"[{mode_tag}] EXIT | {trade['bucket_name']} "
        f"entry=${trade['entry_price']:.4f} exit=${exit_price:.4f} "
        f"mult={mult:.1f}x P&L=${pnl:+.2f} reason={reason}"
    )
    log.info(msg)
    send_alert(msg, priority="normal" if pnl >= 0 else "high")


def _check_exits(pace_projection: float):
    """Check all open positions for exit conditions."""
    open_pos = _open_positions()
    if not open_pos:
        return

    now_utc = datetime.now(timezone.utc)

    for t in open_pos:
        token_id   = t.get("token_id", "")
        entry      = t.get("entry_price", 0)
        bucket_ctr = None

        # Re-derive bucket center from name
        bucket_name = t.get("bucket_name", "")
        if bucket_name:
            b = _parse_bucket(bucket_name)
            bucket_ctr = b["center"]

        current_p = _fetch_current_price(token_id)
        if current_p is None:
            continue

        # ── 1. Full exit mult ─────────────────────────────────────────
        if current_p >= entry * ELON_FULL_EXIT_MULT:
            _close_position(t, current_p, f"{ELON_FULL_EXIT_MULT}x full exit")
            continue

        # ── 2. Partial profit take (tracked via flag in record) ───────
        if current_p >= entry * ELON_PROFIT_TAKE_MULT and not t.get("profit_take_done"):
            trades = _load_trades()
            for tr in trades:
                if tr.get("id") == t["id"]:
                    tr["profit_take_done"] = True
                    break
            _save_trades(trades)
            log.info(
                f"[ELON PROFIT TAKE] {bucket_name} @ ${current_p:.4f}  "
                f"{ELON_PROFIT_TAKE_MULT}x hit — flagged (paper: no partial sell)"
            )

        # ── 3. Stop loss ──────────────────────────────────────────────
        if current_p <= entry * (1.0 - ELON_STOP_LOSS_PCT):
            _close_position(t, current_p, "stop loss 60%")
            continue

        # ── 4. Pace drift exit ────────────────────────────────────────
        if (bucket_ctr is not None
                and abs(bucket_ctr - pace_projection) > ELON_PACE_DRIFT_THRESHOLD
                and current_p >= entry * 2.0):
            _close_position(t, current_p, f"pace drift >{ELON_PACE_DRIFT_THRESHOLD} tweets (up >{2.0}x)")
            continue

        # ── 5. Time-based final-hour cleanup ─────────────────────────
        try:
            close_ts_str = t.get("opened_at", "")
            tracking_end = None
            # try to infer from market title
            active = _fetch_elon_active_trackings()
            for tr in active:
                if tr.get("id") == t.get("tracking_id"):
                    tracking_end = datetime.fromisoformat(
                        tr["endDate"].replace("Z", "+00:00"))
                    break
            if tracking_end:
                hours_left = (tracking_end - now_utc).total_seconds() / 3600
                if hours_left <= ELON_FINAL_HOUR_CUTOFF:
                    if bucket_ctr is not None and abs(bucket_ctr - pace_projection) > 20:
                        _close_position(t, current_p,
                                        f"final {ELON_FINAL_HOUR_CUTOFF}h cleanup — off-pace")
                        continue
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Rotation (Phase 7)
# ══════════════════════════════════════════════════════════════════════════════

def _maybe_rotate(candidate_bucket: dict, candidate_score: int,
                  pace_projection: float, velocity: str, current_pace: float):
    """Sell weakest position if a much better opportunity exists."""
    open_pos = _open_positions()
    if len(open_pos) < ELON_MAX_POSITIONS:
        return   # have room — no rotation needed

    # Score each open position with current market data
    scored = []
    for t in open_pos:
        b = _parse_bucket(t.get("bucket_name", ""))
        cp = _fetch_current_price(t.get("token_id", "")) or t["entry_price"]
        b["yes_price"] = cp
        s = _score_bucket(b, pace_projection, velocity, current_pace)
        scored.append((s, t))

    if not scored:
        return

    worst_score, worst_trade = min(scored, key=lambda x: x[0])

    if candidate_score > worst_score + 20:
        cp = _fetch_current_price(worst_trade.get("token_id", ""))
        if cp:
            log.info(
                f"[ELON ROTATE] selling weakest={worst_trade.get('bucket_name')} "
                f"(score {worst_score}), buying new={candidate_bucket['name']} (score {candidate_score})"
            )
            _close_position(worst_trade, cp, f"rotation to {candidate_bucket['name']}")


# ══════════════════════════════════════════════════════════════════════════════
# Main scan loop
# ══════════════════════════════════════════════════════════════════════════════

def _run_scan():
    """Single scan cycle: fetch data → score buckets → enter/exit."""
    log.info("--- ELON TWEET SCAN ---")

    # ── 1. Get active trackings and pace metrics ──────────────────
    trackings = _fetch_elon_active_trackings()
    if not trackings:
        log.info("[ELON SCAN] no active trackings found")
        return

    # Focus on the tracking that has a market link (the primary weekly market)
    primary = next(
        (t for t in trackings if t.get("marketLink") and
         t.get("endDate") and
         (datetime.fromisoformat(t["endDate"].replace("Z", "+00:00"))
          - datetime.fromisoformat(t["startDate"].replace("Z", "+00:00"))
         ).days >= 5),
        trackings[0],
    )

    count_data = _fetch_elon_tweet_count(primary)
    if count_data is None:
        log.warning("[ELON SCAN] could not get tweet count — skip")
        return

    pace = _calculate_pace_metrics(count_data)
    pace.update({
        "hours_elapsed":   count_data["hours_elapsed"],
        "hours_remaining": count_data["hours_remaining"],
    })

    projection    = pace["projected_final"]
    velocity      = pace["velocity"]
    current_rate  = pace["tweets_per_hour_recent"]
    current_count = count_data["current_count"]

    log.info(
        f"[ELON SCAN] count={current_count}  elapsed={count_data['hours_elapsed']:.1f}h  "
        f"rate={current_rate:.2f}/h  projected={projection:.0f}  velocity={velocity}"
    )

    # ── 2. Check exits on current positions ──────────────────────
    _check_exits(projection)

    # ── 3. Find markets and score buckets ────────────────────────
    markets = _find_elon_tweet_markets()
    if not markets:
        log.info("[ELON SCAN] no qualifying 7-day markets found")
        return

    open_token_ids = {t["token_id"] for t in _open_positions()}
    best_score     = 0
    best_bucket    = None
    best_market    = None

    for market in markets:
        buckets = _get_market_buckets(market)
        for bucket in buckets:
            yes_p = bucket["yes_price"]

            if yes_p < ELON_PRICE_MIN or yes_p > ELON_PRICE_MAX_EXTENDED:
                continue
            if bucket["yes_token_id"] in open_token_ids:
                continue

            score = _score_bucket(bucket, projection, velocity, current_rate * count_data["hours_elapsed"])
            if score > best_score:
                best_score  = score
                best_bucket = bucket
                best_market = market

    if best_bucket is None:
        log.info("[ELON SCAN] no bucket passed scoring threshold this cycle")
        return

    log.info(
        f"[ELON SCAN] best bucket: {best_bucket['name']}  "
        f"yes_price={best_bucket['yes_price']:.4f}  score={best_score}  "
        f"center={best_bucket['center']:.0f}  projection={projection:.0f}  "
        f"distance={abs(best_bucket['center']-projection):.0f}"
    )

    if best_score < ELON_MIN_SCORE_ENTRY:
        log.info(f"[ELON SCAN] best score {best_score} < {ELON_MIN_SCORE_ENTRY} minimum — no entry")
        return

    # ── 4+5. Entry blocked when paused ───────────────────────────
    if ELON_PAUSED:
        log.info("[ELON BOT PAUSED] no new entries — existing positions still tracked")
        return

    if len(_open_positions()) >= ELON_MAX_POSITIONS:
        _maybe_rotate(best_bucket, best_score, projection, velocity,
                      current_rate * count_data["hours_elapsed"])

    _enter_position(best_bucket, best_score, pace, primary)


# ══════════════════════════════════════════════════════════════════════════════
# Status report
# ══════════════════════════════════════════════════════════════════════════════

def print_elon_status():
    """Print human-readable status for main.py status command."""
    trades  = _load_trades()
    settled = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    open_t  = [t for t in trades if t.get("status") == "open"]
    wins    = [t for t in settled if (t.get("pnl") or 0) > 0]
    total_pnl = sum(t.get("pnl", 0) or 0 for t in settled)
    wr        = len(wins) / len(settled) * 100 if settled else 0
    mode_tag  = "LIVE" if ELON_LIVE_MODE else "PAPER"

    print(f"\n  ELON TWEET MARKETS ({mode_tag})")
    print(f"  {'─'*56}")
    print(f"  Total trades  : {len(trades)}  (settled={len(settled)}  open={len(open_t)})")
    if settled:
        print(f"  Win / Loss    : {len(wins)}/{len(settled)}  WR={wr:.1f}%")
        print(f"  P&L           : ${total_pnl:+.2f}")

    if open_t:
        print()
        print("  OPEN POSITIONS")
        print(f"  {'Opened':16}  {'Bucket':10}  {'Entry':6}  {'Score':5}  {'P&L est':8}")
        print("  " + "-"*52)
        for t in open_t:
            cp   = _fetch_current_price(t.get("token_id", ""))
            est  = round((cp / t["entry_price"] - 1) * t["size"], 2) if cp and t["entry_price"] else 0
            print(f"  {str(t['opened_at'])[:16].replace('T',' '):16}  "
                  f"{t.get('bucket_name','?'):10}  {t['entry_price']:.4f}  "
                  f"{t.get('score_at_entry',0):>5}  ${est:>+7.2f}")

    return total_pnl


# ══════════════════════════════════════════════════════════════════════════════
# Run loop (entry point for main.py)
# ══════════════════════════════════════════════════════════════════════════════

def run():
    """Blocking run loop. Called by main.py via `python main.py elon`."""
    mode = "LIVE" if ELON_LIVE_MODE else "PAPER"
    log.info(f"[ELON BOT] Starting — mode={mode}  scan_interval={ELON_SCAN_INTERVAL}s")
    log.info(f"[ELON BOT] Max positions={ELON_MAX_POSITIONS}  "
             f"Exposure cap=${ELON_TOTAL_EXPOSURE:.2f}  Size=${ELON_MAX_POSITION_SIZE:.2f}")

    send_alert(
        f"Elon Tweet Bot started — {mode} mode\n"
        f"Max positions: {ELON_MAX_POSITIONS}  Exposure: ${ELON_TOTAL_EXPOSURE:.0f}",
        priority="normal",
    )

    import time as _time
    try:
        while True:
            try:
                _run_scan()
            except Exception as e:
                log.error(f"[ELON BOT] scan error: {e}", exc_info=True)
            _time.sleep(ELON_SCAN_INTERVAL)
    except KeyboardInterrupt:
        log.info("[ELON BOT] Stopped by user (Ctrl+C)")
        print_elon_status()
