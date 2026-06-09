"""
xtracker_client.py — XTracker API client for Elon tweet count data.

Provides:
  _fetch_elon_tweet_count()      → current count + recent posts in active period
  _fetch_elon_active_trackings() → active tracking windows from XTracker
  _calculate_pace_metrics()      → rate, projection, velocity for a tracking window
"""

import logging, time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

log = logging.getLogger("weatherbet")

XTRACKER_BASE     = "https://xtracker.polymarket.com/api"
XTRACKER_HANDLE   = "elonmusk"
XTRACKER_PLATFORM = "X"

_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ── Caches ────────────────────────────────────────────────────────────────────
_posts_cache:         Optional[list]  = None
_posts_cache_mono:    float           = 0.0
_trackings_cache:     Optional[list]  = None
_trackings_cache_mono: float          = 0.0
CACHE_TTL = 60.0   # seconds


# ══════════════════════════════════════════════════════════════════════════════
# Core data fetchers
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_all_posts() -> Optional[list]:
    """Fetch all Elon posts from XTracker (60s cache). Returns list of dicts."""
    global _posts_cache, _posts_cache_mono
    now = time.monotonic()
    if now - _posts_cache_mono < CACHE_TTL and _posts_cache is not None:
        return _posts_cache
    try:
        r = requests.get(
            f"{XTRACKER_BASE}/users/{XTRACKER_HANDLE}/posts",
            params={"platform": XTRACKER_PLATFORM, "limit": 500},
            headers=_HEADERS,
            timeout=12,
        )
        r.raise_for_status()
        posts = r.json().get("data", [])
        _posts_cache      = posts
        _posts_cache_mono = now
        return posts
    except Exception as e:
        log.warning(f"[ELON XTRACKER] posts fetch failed: {e}")
        return _posts_cache   # return stale on error


def _fetch_elon_active_trackings() -> list:
    """Return list of active XTracker tracking windows (60s cache)."""
    global _trackings_cache, _trackings_cache_mono
    now = time.monotonic()
    if now - _trackings_cache_mono < CACHE_TTL and _trackings_cache is not None:
        return _trackings_cache
    try:
        r = requests.get(
            f"{XTRACKER_BASE}/users/{XTRACKER_HANDLE}/trackings",
            params={"platform": XTRACKER_PLATFORM, "activeOnly": "true"},
            headers=_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        raw_trackings = r.json().get("data", [])

        # Fix #1: exclude windows that haven't started yet
        now_utc = datetime.now(timezone.utc)
        started = []
        future  = []
        for t in raw_trackings:
            try:
                start = _parse_dt(t["startDate"])
                if start <= now_utc:
                    started.append(t)
                else:
                    future.append(t)
            except Exception:
                started.append(t)   # keep if unparseable

        if future:
            log.info(f"[ELON FILTER] Excluded {len(future)} future tracking(s) (start > now): "
                     f"{[t.get('title','?')[:30] for t in future]}")
        log.info(f"[ELON FILTER] Active trackings: {len(started)}")

        _trackings_cache      = started
        _trackings_cache_mono = now
        return started
    except Exception as e:
        log.warning(f"[ELON XTRACKER] trackings fetch failed: {e}")
        return _trackings_cache or []


# ══════════════════════════════════════════════════════════════════════════════
# Count extraction
# ══════════════════════════════════════════════════════════════════════════════

def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _fetch_elon_tweet_count(tracking: dict) -> Optional[dict]:
    """
    Return tweet count and recent-post list for a specific tracking window.

    Result:
      {
        'current_count':    int,   # tweets since period start
        'recent_6h_count':  int,   # tweets in last 6 hours
        'recent_1h_count':  int,   # tweets in last 1 hour
        'period_start':     datetime,
        'period_end':       datetime,
        'hours_elapsed':    float,
        'hours_remaining':  float,
      }
    Returns None if posts unavailable.
    """
    posts = _fetch_all_posts()
    if posts is None:
        return None

    try:
        period_start = _parse_dt(tracking["startDate"])
        period_end   = _parse_dt(tracking["endDate"])
    except (KeyError, ValueError) as e:
        log.warning(f"[ELON XTRACKER] bad tracking dates: {e}")
        return None

    now_utc         = datetime.now(timezone.utc)
    hours_elapsed   = max((now_utc - period_start).total_seconds() / 3600, 0.01)
    hours_remaining = max((period_end   - now_utc).total_seconds() / 3600, 0.0)
    cutoff_6h       = now_utc - timedelta(hours=6)
    cutoff_1h       = now_utc - timedelta(hours=1)

    current_count = recent_6h = recent_1h = 0
    for p in posts:
        try:
            ts = _parse_dt(p["createdAt"])
        except Exception:
            continue
        if ts >= period_start:
            current_count += 1
            if ts >= cutoff_6h:
                recent_6h += 1
            if ts >= cutoff_1h:
                recent_1h += 1

    return {
        "current_count":   current_count,
        "recent_6h_count": recent_6h,
        "recent_1h_count": recent_1h,
        "period_start":    period_start,
        "period_end":      period_end,
        "hours_elapsed":   hours_elapsed,
        "hours_remaining": hours_remaining,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Pace and projection
# ══════════════════════════════════════════════════════════════════════════════

def _calculate_pace_metrics(count_data: dict) -> dict:
    """
    Derive pace, projection, and velocity direction from count data.

    Returns:
      {
        'tweets_per_hour_overall': float,
        'tweets_per_hour_recent':  float,   # last 6h
        'projected_final':         float,
        'velocity':                str,     # 'accelerating'|'decelerating'|'stable'
        'confidence':              str,     # 'high'|'medium'|'low'
      }
    """
    current    = count_data["current_count"]
    elapsed    = count_data["hours_elapsed"]
    remaining  = count_data["hours_remaining"]
    recent_6h  = count_data["recent_6h_count"]
    window_6h  = min(elapsed, 6.0)

    rate_overall = current / elapsed if elapsed > 0 else 0.0
    rate_recent  = recent_6h / window_6h if window_6h > 0 else rate_overall

    projected = current + rate_recent * remaining

    # Velocity: compare recent rate vs overall rate
    if elapsed < 6.0 or rate_overall < 0.1:
        velocity   = "stable"
        confidence = "low"      # window too young for reliable velocity
    else:
        ratio = rate_recent / rate_overall if rate_overall > 0 else 1.0
        if ratio > 1.20:
            velocity = "accelerating"
        elif ratio < 0.80:
            velocity = "decelerating"
        else:
            velocity = "stable"
        confidence = "high" if elapsed >= 24 else "medium"

    log.info(
        f"[ELON XTRACKER] tweets={current}  elapsed={elapsed:.1f}h  "
        f"rate_overall={rate_overall:.2f}/h  rate_recent={rate_recent:.2f}/h  "
        f"projected={projected:.0f}  velocity={velocity}"
    )

    return {
        "tweets_per_hour_overall": round(rate_overall, 3),
        "tweets_per_hour_recent":  round(rate_recent,  3),
        "projected_final":         round(projected, 1),
        "velocity":                velocity,
        "confidence":              confidence,
    }
