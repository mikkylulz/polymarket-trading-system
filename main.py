import sys, os, time, logging, shutil
from datetime import datetime, timezone
from config import (ACTIVE_CITIES, SCAN_INTERVAL, PAPER_MODE,
                    MARKETS_DIR, STATE_FILE, HISTORICAL_DIR,
                    RAPID_SCAN_CITIES, RAPID_SCAN_INTERVAL,
                    RAPID_START_UTC, RAPID_END_UTC, BTC_LIVE_MODE)
from state import send_alert, load_calibration
from scanner import scan_and_update, clear_stale_positions, drain_priority_queue
from learning import print_learn_report
from crypto_scanner import run_crypto_scan, run_sol_scan, monitor_btc_open_trades

CRYPTO_SCAN_INTERVAL   = 120  # 2 minutes
PRIORITY_CHECK_INTERVAL = 30   # seconds between priority-queue polls during sleep

log = logging.getLogger("weatherbet")

# ── Reports ──────────────────────────────────────────────────
def print_status():
    import json
    from datetime import datetime, timezone, timedelta
    from config import PAPER_MODE, MARKETS_DIR

    # ── Conviction trades (market files) ─────────────────────
    open_pos, resolved = [], []
    for mp in MARKETS_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            if m.get("position"):
                open_pos.append(m)
            if m["status"] in ["resolved", "closed"] and m.get("pnl") is not None:
                resolved.append(m)
        except:
            continue

    conv_pnl  = sum(m["pnl"] for m in resolved)
    conv_wins = [m for m in resolved if m["pnl"] > 0]
    conv_wr   = len(conv_wins) / len(resolved) * 100 if resolved else 0

    now = datetime.now(timezone.utc)

    # ── Output ───────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  WEATHERBET ULTIMATE -- {'PAPER' if PAPER_MODE else 'LIVE'} MODE")
    print(f"{'='*60}")

    print(f"\n  CONVICTION MODE")
    print(f"  {'─'*56}")
    print(f"  Open          : {len(open_pos)}")
    print(f"  Resolved      : {len(resolved)}"
          + (f"  |  {len(conv_wins)} wins  |  Win rate: {conv_wr:.1f}%"
             if resolved else ""))
    print(f"  Conviction P&L: ${conv_pnl:+.2f}")
    if open_pos:
        print()
        for m in open_pos:
            pos  = m["position"]
            snap = m.get("forecast_snapshots", [{}])[-1]
            print(f"  {m['city_name']:12} {m['date']} | "
                  f"{pos['direction']} {pos['bucket']}C @ {pos['price']:.3f} | "
                  f"${pos['size']:.2f} | Forecast: {snap.get('best_temp', '?')}C")

    # ── BTC 5m trades (learning.json) ────────────────────────
    btc_pnl = btc_wr7 = btc_wr30 = btc_wr_all = None
    btc_settled = btc_wins = 0
    btc_kelly = 1.0
    try:
        from learning import load_learning
        btc_data    = load_learning().get("btc_5m", {})
        btc_all     = [t for t in btc_data.get("trades", []) if t.get("won") is not None]
        btc_settled = len(btc_all)
        btc_wins    = sum(1 for t in btc_all if t["won"])
        btc_pnl     = sum(t.get("pnl") or 0 for t in btc_all)
        btc_kelly   = btc_data.get("kelly_multiplier", 1.0)
        wrs         = btc_data.get("win_rates", {})
        btc_wr7     = wrs.get("7d")
        btc_wr30    = wrs.get("30d")
        btc_wr_all  = wrs.get("all")
    except Exception:
        pass

    btc_pnl = btc_pnl or 0.0
    print(f"\n  BTC 5-MINUTE")
    print(f"  {'─'*56}")
    if btc_settled:
        btc_losses = btc_settled - btc_wins
        all_str = f"{btc_wr_all*100:.1f}%" if btc_wr_all is not None else "n/a"
        wr7_str  = f"{btc_wr7*100:.1f}%"  if btc_wr7  is not None else "n/a"
        wr30_str = f"{btc_wr30*100:.1f}%" if btc_wr30 is not None else "n/a"
        print(f"  Settled       : {btc_settled}  |  {btc_wins}W {btc_losses}L  |  All WR: {all_str}")
        print(f"  Win rate      : 7d {wr7_str}  |  30d {wr30_str}  |  Kelly: {btc_kelly:.1f}x")
        print(f"  BTC P&L       : ${btc_pnl:+.2f}")
    else:
        print(f"  BTC P&L       : n/a  (no settled trades yet)")

    # ── SOL 5m trades (sol_5m_trades.json) ───────────────────
    sol_pnl = 0.0
    sol_settled = sol_wins = 0
    sol_wr7 = sol_wr30 = sol_wr_all = None
    sol_kelly = 1.0
    try:
        from crypto_scanner import SOL_TRADES_FILE
        if SOL_TRADES_FILE.exists():
            store   = json.loads(SOL_TRADES_FILE.read_text())
            sol_all = [t for t in store.get("trades", []) if t.get("won") is not None]
            sol_settled = len(sol_all)
            sol_wins    = sum(1 for t in sol_all if t["won"])
            sol_pnl     = sum(t.get("pnl") or 0 for t in sol_all)
            c7d  = (now - timedelta(days=7)).date().isoformat()
            c30d = (now - timedelta(days=30)).date().isoformat()
            t7d  = [t for t in sol_all if (t.get("opened_at") or "")[:10] >= c7d]
            t30d = [t for t in sol_all if (t.get("opened_at") or "")[:10] >= c30d]
            sol_wr7    = sum(1 for t in t7d  if t["won"]) / len(t7d)  if t7d  else None
            sol_wr30   = sum(1 for t in t30d if t["won"]) / len(t30d) if t30d else None
            sol_wr_all = sol_wins / sol_settled if sol_settled else None
            if sol_wr7 is not None and len(t7d) >= 5:
                sol_kelly = 0.5 if sol_wr7 < 0.30 else 1.0
    except Exception:
        pass

    print(f"\n  SOL 5-MINUTE")
    print(f"  {'─'*56}")
    if sol_settled:
        sol_losses = sol_settled - sol_wins
        all_str  = f"{sol_wr_all*100:.1f}%" if sol_wr_all is not None else "n/a"
        wr7_str  = f"{sol_wr7*100:.1f}%"    if sol_wr7   is not None else "n/a"
        wr30_str = f"{sol_wr30*100:.1f}%"   if sol_wr30  is not None else "n/a"
        print(f"  Settled       : {sol_settled}  |  {sol_wins}W {sol_losses}L  |  All WR: {all_str}")
        print(f"  Win rate      : 7d {wr7_str}  |  30d {wr30_str}  |  Kelly: {sol_kelly:.1f}x")
        print(f"  SOL P&L       : ${sol_pnl:+.2f}")
    else:
        print(f"  SOL P&L       : n/a  (no settled trades yet)")

    # ── Grand total ───────────────────────────────────────────
    grand_total = conv_pnl + btc_pnl + sol_pnl
    print(f"\n{'='*60}")
    print(f"  GRAND TOTAL   : ${grand_total:+.2f}"
          f"   (conv ${conv_pnl:+.2f}  btc ${btc_pnl:+.2f}  sol ${sol_pnl:+.2f})")
    print(f"{'='*60}")

def _crypto_edge_table(coin_label: str, trades: list, buckets: list):
    """Edge-by-price and timing stats for any crypto scanner's settled trades."""
    from datetime import datetime

    print(f"  {coin_label} EDGE BY ENTRY PRICE")
    print(f"  {'Bucket':10} {'Trades':>6}  {'Actual WR':>9}  {'Break-even':>10}  {'Edge':>8}")
    print(f"  {'-'*50}")
    for label, lo, hi, be in buckets:
        b = [t for t in trades if lo <= (t.get("entry_price") or 0) < hi]
        if not b:
            continue
        wr   = sum(1 for t in b if t["won"]) / len(b) * 100
        edge = wr - be
        sign = "+" if edge >= 0 else ""
        print(f"  {label:10} {len(b):>6}  {wr:>8.1f}%  {be:>9.1f}%  {sign}{edge:.1f}%")
    print()

    # ── Time-to-entry stats ───────────────────────────────────
    def _tte(t):
        try:
            o = datetime.fromisoformat(t["opened_at"])
            m = datetime.fromisoformat(t["market_open_at"])
            return max(0, (o - m).total_seconds())
        except Exception:
            return None

    timed = [(t, _tte(t)) for t in trades if _tte(t) is not None]
    if timed:
        avg_tte = sum(s for _, s in timed) / len(timed)
        print(f"  Avg time to entry : {avg_tte/60:.1f} min after market open  "
              f"({len(timed)}/{len(trades)} trades have timing data)")

        timing_buckets = [
            ("0–15 min",  0,    900),
            ("15–30 min", 900,  1800),
            ("30+ min",   1800, 9999),
        ]
        print(f"\n  WIN RATE BY ENTRY TIMING")
        print(f"  {'Window':12} {'Trades':>6}  {'Win Rate':>9}")
        print(f"  {'-'*32}")
        for label, lo, hi in timing_buckets:
            b = [t for t, s in timed if lo <= s < hi]
            if not b:
                continue
            wr = sum(1 for t in b if t["won"]) / len(b) * 100
            print(f"  {label:12} {len(b):>6}  {wr:>8.1f}%")
        print()


def print_report():
    import json
    from config import MARKETS_DIR
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

    # ── BTC 5m P&L (learning.json) ───────────────────────────
    btc_pnl = 0.0
    btc_settled = 0
    btc_all = []
    try:
        from learning import load_learning
        btc_all     = [t for t in load_learning().get("btc_5m", {}).get("trades", [])
                       if t.get("won") is not None]
        btc_settled = len(btc_all)
        btc_pnl     = sum(t.get("pnl") or 0 for t in btc_all)
    except Exception:
        pass

    # ── SOL 5m P&L (sol_5m_trades.json) ──────────────────────
    sol_pnl = 0.0
    sol_settled = 0
    sol_all = []
    try:
        from crypto_scanner import SOL_TRADES_FILE
        if SOL_TRADES_FILE.exists():
            store       = json.loads(SOL_TRADES_FILE.read_text())
            sol_all     = [t for t in store.get("trades", []) if t.get("won") is not None]
            sol_settled = len(sol_all)
            sol_pnl     = sum(t.get("pnl") or 0 for t in sol_all)
    except Exception:
        pass

    grand_total = total + btc_pnl + sol_pnl
    print(f"{'='*75}")
    print(f"  Conviction  : ${total:+.2f}  ({len(all_m)} trades)")
    if btc_settled:
        btc_wins = sum(1 for t in btc_all if t["won"])
        print(f"  BTC 5m      : ${btc_pnl:+.2f}  ({btc_settled} settled  {btc_wins}W {btc_settled-btc_wins}L)")
    if sol_settled:
        sol_wins = sum(1 for t in sol_all if t["won"])
        print(f"  SOL 5m      : ${sol_pnl:+.2f}  ({sol_settled} settled  {sol_wins}W {sol_settled-sol_wins}L)")
    print(f"  GRAND TOTAL : ${grand_total:+.2f}")
    print(f"{'='*75}\n")

    BTC_BUCKETS = [
        ("0-30¢",  0.00, 0.30, 15.0),
        ("30-50¢", 0.30, 0.50, 40.0),
        ("50-65¢", 0.50, 0.65, 57.5),
    ]
    SOL_BUCKETS = [
        ("0-30¢",  0.00, 0.30, 15.0),
        ("30-50¢", 0.30, 0.50, 40.0),
    ]
    if btc_settled:
        _crypto_edge_table("BTC", btc_all, BTC_BUCKETS)
    if sol_settled:
        _crypto_edge_table("SOL", sol_all, SOL_BUCKETS)

def _in_rapid_window():
    return RAPID_START_UTC <= datetime.now(timezone.utc).hour < RAPID_END_UTC

def _in_pause_window():
    return False  # pause disabled — re-enable when needed

# ── Entry point ──────────────────────────────────────────────
if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"

    if arg == "status":
        print_status()
    elif arg == "report":
        print_report()
    elif arg == "learn":
        print_learn_report()
    elif arg == "btc-clear-halt":
        from crypto_scanner import BTC_TRADES_FILE
        import json as _json
        if BTC_TRADES_FILE.exists():
            store = _json.loads(BTC_TRADES_FILE.read_text())
            store["halt_until"] = None
            BTC_TRADES_FILE.write_text(_json.dumps(store, indent=2))
            print("BTC loss-streak halt cleared.")
        else:
            print("No BTC trades file found.")
    elif arg == "sol-clear-halt":
        from crypto_scanner import SOL_TRADES_FILE
        import json as _json
        if SOL_TRADES_FILE.exists():
            store = _json.loads(SOL_TRADES_FILE.read_text())
            store["halt_until"] = None
            SOL_TRADES_FILE.write_text(_json.dumps(store, indent=2))
            print("SOL loss-streak halt cleared.")
        else:
            print("No SOL trades file found.")
    elif arg == "scan":
        scan_and_update()
    elif arg == "reset":
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

        btc_mode = "LIVE" if BTC_LIVE_MODE else "PAPER"
        log.info(f"WeatherBet Ultimate starting -- BTC-ONLY MODE (weather paused)")
        log.info(f"[BTC] Mode: {btc_mode}")
        send_alert(
            f"WeatherBot started — BTC-ONLY MODE\n"
            f"Weather scanner: PAUSED\n"
            f"BTC Scanner: {btc_mode}"
        )

        WEATHER_PAUSED = True   # weather scanner paused — BTC only mode

        stopped     = False
        last_crypto = time.monotonic()   # global BTC cadence — never reset by weather cycle
        while not stopped:
            if not WEATHER_PAUSED:
                rapid = _in_rapid_window()
                try:
                    if rapid:
                        scan_and_update(city_filter=RAPID_SCAN_CITIES)
                    else:
                        scan_and_update()
                except KeyboardInterrupt:
                    log.info("Stopped.")
                    stopped = True
                    break
                except Exception as e:
                    log.error(f"Scan error: {e}", exc_info=True)
                    send_alert(f"Scan error: {e}", priority="high")

            # Sleep loop: runs BTC scanner every 240s and drains priority queue.
            # When weather is paused the outer loop just re-enters the sleep immediately.
            sleep_interval = CRYPTO_SCAN_INTERVAL   # reuse the BTC cadence as the outer tick
            deadline       = time.monotonic() + sleep_interval

            while not stopped:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                chunk = min(PRIORITY_CHECK_INTERVAL, remaining)
                log.info(f"[BTC-ONLY] Next BTC scan in {remaining:.0f}s "
                         f"— sleeping {chunk:.0f}s")
                try:
                    time.sleep(chunk)
                except KeyboardInterrupt:
                    log.info("Stopped.")
                    stopped = True
                    break

                # Priority queue: fire any BIG MOVE re-scans that are ready
                try:
                    drain_priority_queue()
                except Exception as e:
                    log.error(f"Priority queue drain error: {e}", exc_info=True)

                # Mid-trade monitor: check open BTC positions every 30s
                try:
                    monitor_btc_open_trades()
                except Exception as e:
                    log.error(f"Mid-trade monitor error: {e}", exc_info=True)

                # BTC scanner: independent 240s cadence — skip during pause window
                if time.monotonic() - last_crypto >= CRYPTO_SCAN_INTERVAL:
                    if _in_pause_window():
                        log.info("[PAUSE] 00:00-05:00 UTC — BTC scan suppressed")
                    else:
                        try:
                            run_crypto_scan()
                        except KeyboardInterrupt:
                            log.info("Stopped.")
                            stopped = True
                        except Exception as e:
                            log.error(f"Crypto scan error: {e}", exc_info=True)
                        try:
                            run_sol_scan()
                        except KeyboardInterrupt:
                            log.info("Stopped.")
                            stopped = True
                        except Exception as e:
                            log.error(f"SOL scan error: {e}", exc_info=True)
                    last_crypto = time.monotonic()
