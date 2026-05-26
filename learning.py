import json, logging, math
from datetime import datetime, timezone, timedelta
from config import BASE, MARKETS_DIR, STATE_FILE

log = logging.getLogger("weatherbet")

LEARNING_FILE = BASE / "data" / "learning.json"

PRICE_RANGES = [
    (0.05, 0.15, "0.05-0.15"),
    (0.15, 0.35, "0.15-0.35"),
    (0.35, 0.65, "0.35-0.65"),
    (0.65, 0.85, "0.65-0.85"),
]


def load_learning() -> dict:
    if LEARNING_FILE.exists():
        try:
            return json.loads(LEARNING_FILE.read_text())
        except Exception:
            pass
    return {
        "cities": {},
        "price_ranges": {r[2]: {"trades": 0, "wins": 0, "win_rate": 0.0} for r in PRICE_RANGES},
        "recency_bias": {"total_adjusted": 0, "adjustment_helped": 0, "help_rate": 0.0},
        "btc_5m": {
            "trades": [],
            "win_rates": {"7d": None, "30d": None, "all": None},
            "kelly_multiplier": 1.0,
        },
        "last_updated": None,
    }


def save_learning(data: dict):
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    LEARNING_FILE.write_text(json.dumps(data, indent=2))


def _get_price_range(price: float) -> str:
    for lo, hi, label in PRICE_RANGES:
        if lo <= price < hi:
            return label
    return "other"


def _entry_session(opened_at_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(opened_at_iso.replace("Z", "+00:00"))
        return "morning" if dt.hour < 12 else "evening"
    except Exception:
        return "unknown"


def _compute_win_rates(trades: list) -> dict:
    now = datetime.now(timezone.utc)
    cutoff_7d  = (now - timedelta(days=7)).date().isoformat()
    cutoff_30d = (now - timedelta(days=30)).date().isoformat()

    settled = [t for t in trades if t.get("won") is not None]
    t_7d    = [t for t in settled if t.get("date", "") >= cutoff_7d]
    t_30d   = [t for t in settled if t.get("date", "") >= cutoff_30d]

    def _wr(lst):
        return round(sum(1 for t in lst if t["won"]) / len(lst), 4) if lst else None

    return {
        "7d":  _wr(t_7d),   "count_7d":  len(t_7d),
        "30d": _wr(t_30d),  "count_30d": len(t_30d),
        "all": _wr(settled),"count_all": len(settled),
    }


def get_kelly_multiplier(city: str) -> float:
    """
    Returns 0.5 when 7-day win rate < 30% (minimum 3 trades required).
    Restores to 1.0 only after recovering above 40% (hysteresis band).
    Returns 1.0 when there is insufficient data to judge.
    """
    try:
        data = load_learning()
        cd = data.get("cities", {}).get(city, {})
        wr = _compute_win_rates(cd.get("trades", []))
        wr_7d, n_7d = wr.get("7d"), wr.get("count_7d", 0)

        if wr_7d is None or n_7d < 3:
            return 1.0
        if wr_7d < 0.30:
            return 0.5
        if wr_7d >= 0.40:
            return 1.0
        return cd.get("kelly_multiplier", 1.0)  # hold current between 30–40%
    except Exception:
        return 1.0


def update_learning(city: str, mkt: dict):
    """
    Record one resolved trade into learning.json.
    Uses position_entry_snapshot when position has already been cleared
    (stop-loss case). Safe to call multiple times — caller sets
    mkt['learning_recorded'] = True to prevent double-counting.
    """
    pos = mkt.get("position") or mkt.get("position_entry_snapshot")
    if not pos:
        return

    pnl = mkt.get("pnl")
    actual_temp = mkt.get("actual_temp")
    if pnl is None or actual_temp is None:
        return

    snaps = mkt.get("forecast_snapshots", [])
    if not snaps:
        return
    best_temp = snaps[0].get("best_temp")
    if best_temp is None:
        return

    won            = pnl > 0
    forecast_error = round(abs(best_temp - actual_temp), 2)
    entry_price    = pos["price"]
    price_range    = _get_price_range(entry_price)
    entry_session  = _entry_session(pos.get("opened_at", ""))

    entry_hour = None
    try:
        dt = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
        entry_hour = dt.hour
    except Exception:
        pass

    recency_adj      = mkt.get("last_recency_adj", 0) or 0
    recency_triggered = abs(recency_adj) > 0.01
    recency_helped    = None
    if recency_triggered:
        recency_helped = abs((best_temp + recency_adj) - actual_temp) < forecast_error

    trade_record = {
        "date":                 mkt["date"],
        "direction":            pos["direction"],
        "bucket":               pos["bucket"],
        "entry_price":          entry_price,
        "price_range":          price_range,
        "best_temp":            best_temp,
        "actual_temp":          actual_temp,
        "forecast_error":       forecast_error,
        "won":                  won,
        "pnl":                  round(pnl, 2),
        "entry_hour_utc":       entry_hour,
        "entry_session":        entry_session,
        "recency_adj":          recency_adj,
        "recency_triggered":    recency_triggered,
        "recency_helped":       recency_helped,
        "repricing_triggered":  bool(mkt.get("repricing_ever_triggered")),
        "stop_loss_triggered":  mkt.get("status") == "closed" and pnl < 0,
    }

    data = load_learning()

    # ── Per-city ─────────────────────────────────────────────
    if city not in data["cities"]:
        data["cities"][city] = {"trades": [], "kelly_multiplier": 1.0}
    cd = data["cities"][city]
    cd["trades"].append(trade_record)

    wr = _compute_win_rates(cd["trades"])
    cd["win_rates"] = wr

    errors = [t["forecast_error"] for t in cd["trades"] if t.get("forecast_error") is not None]
    cd["avg_forecast_error"] = round(sum(errors) / len(errors), 2) if errors else None

    # Update Kelly multiplier stored per city
    wr_7d, n_7d = wr.get("7d"), wr.get("count_7d", 0)
    if wr_7d is not None and n_7d >= 3:
        if wr_7d < 0.30:
            cd["kelly_multiplier"] = 0.5
        elif wr_7d >= 0.40:
            cd["kelly_multiplier"] = 1.0

    # ── Price ranges ─────────────────────────────────────────
    if price_range in data["price_ranges"]:
        pr = data["price_ranges"][price_range]
        pr["trades"] = pr.get("trades", 0) + 1
        pr["wins"]   = pr.get("wins", 0) + (1 if won else 0)
        pr["win_rate"] = round(pr["wins"] / pr["trades"], 4)

    # ── Recency bias global ───────────────────────────────────
    if recency_triggered and recency_helped is not None:
        rb = data["recency_bias"]
        rb["total_adjusted"]    = rb.get("total_adjusted", 0) + 1
        rb["adjustment_helped"] = rb.get("adjustment_helped", 0) + (1 if recency_helped else 0)
        total = rb["total_adjusted"]
        rb["help_rate"] = round(rb["adjustment_helped"] / total, 4) if total else 0.0

    save_learning(data)
    log.info(f"[LEARNING] {city} {mkt['date']} won={won} err={forecast_error}°C "
             f"7d_wr={wr.get('7d')} kelly={cd['kelly_multiplier']:.1f}x")


def _compute_daily_pnl() -> list:
    """Aggregate resolved trade P&L by calendar date from market files."""
    daily: dict = {}
    try:
        for mp in MARKETS_DIR.glob("*.json"):
            try:
                m = json.loads(mp.read_text())
                pnl  = m.get("pnl")
                date = m.get("date")
                if pnl is not None and date and m.get("status") in ("resolved", "closed"):
                    daily[date] = daily.get(date, 0.0) + pnl
            except Exception:
                continue
    except Exception:
        pass
    return sorted(daily.items())  # [(date_str, pnl), ...]


def _compute_risk_metrics(daily_series: list) -> dict:
    """
    Sharpe ratio, Sortino ratio, and max drawdown from a daily P&L series.
    Annualisation uses sqrt(252) — standard trading-day convention.
    Sortino downside deviation: RMS of negative daily P&Ls over all n days.
    Max drawdown: largest peak-to-trough drop in cumulative P&L.
    """
    n = len(daily_series)
    if n < 2:
        return {"sharpe": None, "sortino": None, "max_drawdown": None,
                "trading_days": n, "mean_daily_pnl": None, "total_pnl": None}

    pnls = [v for _, v in daily_series]
    mean = sum(pnls) / n
    variance = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    std  = math.sqrt(variance) if variance > 0 else 0.0
    ann  = math.sqrt(252)

    sharpe = round((mean / std) * ann, 3) if std > 0 else None

    # Sortino — penalises only negative days; denominator is full n (standard formula)
    neg_sq_sum = sum(x ** 2 for x in pnls if x < 0)
    down_std   = math.sqrt(neg_sq_sum / n) if neg_sq_sum > 0 else 0.0
    sortino    = round((mean / down_std) * ann, 3) if down_std > 0 else None

    # Max drawdown on cumulative P&L curve starting from $0
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    return {
        "sharpe":        sharpe,
        "sortino":       sortino,
        "max_drawdown":  round(max_dd, 2),
        "trading_days":  n,
        "mean_daily_pnl": round(mean, 2),
        "total_pnl":     round(sum(pnls), 2),
    }


def _load_strategy_stats() -> dict:
    """
    Aggregate conviction and threshold trade stats for strategy comparison.
    Conviction stats come from market files; threshold stats from state.json.
    """
    # Conviction: resolved/closed market files
    conv_trades = conv_wins = 0
    conv_pnl = 0.0
    try:
        for mp in MARKETS_DIR.glob("*.json"):
            try:
                m = json.loads(mp.read_text())
                if m.get("status") in ("resolved", "closed") and m.get("pnl") is not None:
                    conv_trades += 1
                    conv_pnl += m["pnl"]
                    if m["pnl"] > 0:
                        conv_wins += 1
            except Exception:
                continue
    except Exception:
        pass

    # Threshold: state.json threshold_trades list
    th_entered = th_settled = th_wins = 0
    th_pnl = 0.0
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            for t in state.get("threshold_trades", []):
                th_entered += 1
                pnl = t.get("pnl")
                if pnl is not None:
                    th_settled += 1
                    th_pnl += pnl
                    if pnl > 0:
                        th_wins += 1
    except Exception:
        pass

    return {
        "conviction": {
            "trades": conv_trades, "wins": conv_wins, "pnl": round(conv_pnl, 2),
        },
        "threshold": {
            "entered": th_entered, "settled": th_settled,
            "wins": th_wins, "pnl": round(th_pnl, 2),
        },
    }


def print_learn_report():
    data = load_learning()
    cities = data.get("cities", {})

    print(f"\n{'='*65}")
    print(f"  WEATHERBET LEARNING REPORT")
    print(f"{'='*65}")

    if not cities:
        print("  No resolved trades recorded yet.")
        print(f"{'='*65}\n")
        return

    stats = []
    for city, cd in cities.items():
        wr = cd.get("win_rates", {})
        stats.append({
            "city":      city,
            "win_7d":    wr.get("7d"),
            "win_30d":   wr.get("30d"),
            "win_all":   wr.get("all"),
            "count_7d":  wr.get("count_7d", 0),
            "count_all": wr.get("count_all", 0),
            "avg_error": cd.get("avg_forecast_error"),
            "kelly":     cd.get("kelly_multiplier", 1.0),
        })

    # ── Top 3 by win rate ────────────────────────────────────
    ranked = sorted(stats, key=lambda x: (x["win_7d"] or x["win_all"] or 0), reverse=True)
    print(f"\n  TOP 3 CITIES — WIN RATE")
    print(f"  {'City':12} {'7d WR':>7} {'30d WR':>7} {'All WR':>7} {'7d N':>5}  {'Kelly':>7}")
    print(f"  {'-'*54}")
    for cs in ranked[:3]:
        wr7  = f"{cs['win_7d']:.0%}"  if cs["win_7d"]  is not None else "  —"
        wr30 = f"{cs['win_30d']:.0%}" if cs["win_30d"] is not None else "  —"
        wra  = f"{cs['win_all']:.0%}" if cs["win_all"] is not None else "  —"
        kelly_tag = "0.5x ⚠" if cs["kelly"] < 1.0 else "1.0x"
        print(f"  {cs['city']:12} {wr7:>7} {wr30:>7} {wra:>7} {cs['count_7d']:>5}  {kelly_tag:>7}")

    # ── Worst 3 by forecast error ─────────────────────────────
    err_ranked = sorted([c for c in stats if c["avg_error"] is not None],
                        key=lambda x: x["avg_error"], reverse=True)
    print(f"\n  WORST 3 CITIES — FORECAST ERROR")
    print(f"  {'City':12} {'Avg Error':>10} {'Trades':>7}")
    print(f"  {'-'*33}")
    for cs in err_ranked[:3]:
        print(f"  {cs['city']:12} {cs['avg_error']:>8.1f}°C {cs['count_all']:>7}")

    # ── Price range performance ───────────────────────────────
    pr = data.get("price_ranges", {})
    active = {k: v for k, v in pr.items() if v.get("trades", 0) > 0}
    print(f"\n  PRICE RANGE PERFORMANCE")
    if active:
        best_label = max(active, key=lambda k: active[k].get("win_rate", 0))
        print(f"  {'Range':>12} {'Trades':>7} {'Wins':>5} {'Win Rate':>9}")
        print(f"  {'-'*38}")
        for label in ["0.05-0.15", "0.15-0.35", "0.35-0.65", "0.65-0.85"]:
            if label in active:
                s = active[label]
                marker = " ◀ best" if label == best_label else ""
                print(f"  {label:>12} {s['trades']:>7} {s.get('wins',0):>5}"
                      f" {s['win_rate']:>8.0%}{marker}")
    else:
        print("  No data yet.")

    # ── Recency bias ──────────────────────────────────────────
    rb = data.get("recency_bias", {})
    n_adj = rb.get("total_adjusted", 0)
    print(f"\n  RECENCY BIAS ADJUSTMENT")
    if n_adj > 0:
        helped  = rb.get("adjustment_helped", 0)
        rate    = rb.get("help_rate", 0)
        verdict = "HELPING" if rate >= 0.5 else "HURTING"
        print(f"  Applied {n_adj}× — improved forecast {helped}/{n_adj} ({rate:.0%}) — {verdict}")
    else:
        print("  No recency adjustments recorded yet.")

    # ── Kelly warnings ────────────────────────────────────────
    reduced = [c for c in stats if c["kelly"] < 1.0]
    if reduced:
        print(f"\n  KELLY REDUCED — 7d win rate < 30%")
        for c in reduced:
            wr7 = f"{c['win_7d']:.0%}" if c["win_7d"] is not None else "n/a"
            print(f"  {c['city']:12} 7d={wr7} → Kelly 0.5x (recovers at 40%)")

    # ── BTC 5m performance ────────────────────────────────────
    btc = data.get("btc_5m", {})
    btc_trades = [t for t in btc.get("trades", []) if t.get("won") is not None]
    print(f"\n  BTC 5-MINUTE TRADES")
    if btc_trades:
        bwr  = btc.get("win_rates", {})
        wr7  = f"{bwr['7d']:.0%}" if bwr.get("7d")  is not None else "  —"
        wr30 = f"{bwr['30d']:.0%}" if bwr.get("30d") is not None else "  —"
        wra  = f"{bwr['all']:.0%}" if bwr.get("all") is not None else "  —"
        km   = btc.get("kelly_multiplier", 1.0)
        km_s = "0.5x ⚠" if km < 1.0 else "1.0x"
        wins = sum(1 for t in btc_trades if t["won"])
        total_pnl = sum(t.get("pnl") or 0 for t in btc_trades)
        print(f"  Settled: {len(btc_trades)} | W/L: {wins}/{len(btc_trades)-wins}"
              f" | 7d:{wr7}  30d:{wr30}  All:{wra}  Kelly:{km_s}")
        print(f"  Total PnL: ${total_pnl:+.2f}")
    else:
        print("  No settled BTC 5m trades yet.")

    # ── Strategy comparison ───────────────────────────────────
    ss = _load_strategy_stats()
    cv = ss["conviction"]
    th = ss["threshold"]

    print(f"\n  STRATEGY COMPARISON")
    print(f"  {'─'*56}")

    cv_wr = f"{cv['wins']/cv['trades']:.0%}" if cv["trades"] else "  —"
    print(f"  CONVICTION MODE  : {cv['trades']:>3} trades  "
          f"{cv['wins']:>3} wins  {cv_wr:>5}  ${cv['pnl']:+.2f}")

    if th["settled"]:
        th_wr = f"{th['wins']/th['settled']:.0%}"
        th_pnl_s = f"${th['pnl']:+.2f}"
        settled_note = f"{th['settled']} settled"
    else:
        th_wr = "  —"
        th_pnl_s = "n/a"
        settled_note = "none settled"
    print(f"  THRESHOLD LADDER : {th['entered']:>3} entered  "
          f"{th['wins']:>3} wins  {th_wr:>5}  {th_pnl_s}"
          f"  ({settled_note})")

    # ── Risk metrics ──────────────────────────────────────────
    daily_series = _compute_daily_pnl()
    metrics      = _compute_risk_metrics(daily_series)
    n_days       = metrics["trading_days"]
    print(f"\n  RISK METRICS  ({n_days} trading day{'s' if n_days != 1 else ''})")
    if n_days < 2:
        print("  Insufficient data — need ≥ 2 resolved trading days.")
    else:
        sharpe_s  = f"{metrics['sharpe']:+.2f}"  if metrics["sharpe"]  is not None else "n/a"
        sortino_s = f"{metrics['sortino']:+.2f}" if metrics["sortino"] is not None else "n/a"
        print(f"  Sharpe  (ann., √252) : {sharpe_s}")
        print(f"  Sortino (ann., √252) : {sortino_s}")
        print(f"  Max Drawdown         : ${metrics['max_drawdown']:.2f}")
        print(f"  Mean Daily PnL       : ${metrics['mean_daily_pnl']:+.2f}"
              f"   Total: ${metrics['total_pnl']:+.2f}")
        if n_days < 30:
            print(f"  ⚠  Small sample ({n_days}d) — treat as indicative only")

    updated = data.get("last_updated") or "never"
    print(f"\n  Last updated : {updated}")
    print(f"{'='*65}\n")
