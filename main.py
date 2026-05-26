import sys, os, time, logging, shutil
from datetime import datetime, timezone
from config import (ACTIVE_CITIES, SCAN_INTERVAL, PAPER_MODE,
                    MARKETS_DIR, STATE_FILE, HISTORICAL_DIR,
                    RAPID_SCAN_CITIES, RAPID_SCAN_INTERVAL,
                    RAPID_START_UTC, RAPID_END_UTC, BTC_LIVE_MODE)
from state import send_alert, load_calibration
from scanner import scan_and_update, clear_stale_positions, drain_priority_queue
from learning import print_learn_report
from crypto_scanner import run_crypto_scan

CRYPTO_SCAN_INTERVAL   = 240  # 4 minutes
PRIORITY_CHECK_INTERVAL = 60   # seconds between priority-queue polls during sleep

log = logging.getLogger("weatherbet")

# ── Reports ──────────────────────────────────────────────────
def print_status():
    import json
    from datetime import datetime, timezone
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
    btc_pnl = 0.0
    btc_settled = btc_wins = 0
    try:
        from learning import load_learning
        btc_data = load_learning().get("btc_5m", {})
        btc_all  = [t for t in btc_data.get("trades", []) if t.get("won") is not None]
        btc_settled = len(btc_all)
        btc_wins    = sum(1 for t in btc_all if t["won"])
        btc_pnl     = sum(t.get("pnl") or 0 for t in btc_all)
    except Exception:
        pass

    print(f"\n  BTC 5-MINUTE")
    print(f"  {'─'*56}")
    if btc_settled:
        btc_wr = btc_wins / btc_settled * 100
        print(f"  Settled       : {btc_settled}  |  {btc_wins} wins  |  Win rate: {btc_wr:.1f}%")
        print(f"  BTC P&L       : ${btc_pnl:+.2f}")
    else:
        print(f"  BTC P&L       : n/a  (no settled trades yet)")

    # ── Grand total ───────────────────────────────────────────
    grand_total = conv_pnl + btc_pnl
    print(f"\n{'='*60}")
    print(f"  GRAND TOTAL   : ${grand_total:+.2f}"
          f"   (conv ${conv_pnl:+.2f}  btc ${btc_pnl:+.2f})")
    print(f"{'='*60}")

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
    try:
        from learning import load_learning
        btc_all     = [t for t in load_learning().get("btc_5m", {}).get("trades", [])
                       if t.get("won") is not None]
        btc_settled = len(btc_all)
        btc_pnl     = sum(t.get("pnl") or 0 for t in btc_all)
    except Exception:
        pass

    grand_total = total + btc_pnl
    print(f"{'='*75}")
    print(f"  Conviction  : ${total:+.2f}  ({len(all_m)} trades)")
    if btc_settled:
        print(f"  BTC 5m      : ${btc_pnl:+.2f}  ({btc_settled} settled)")
    print(f"  GRAND TOTAL : ${grand_total:+.2f}")
    print(f"{'='*75}\n")

def _in_rapid_window():
    return RAPID_START_UTC <= datetime.now(timezone.utc).hour < RAPID_END_UTC

# ── Entry point ──────────────────────────────────────────────
if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"

    if arg == "status":
        print_status()
    elif arg == "report":
        print_report()
    elif arg == "learn":
        print_learn_report()
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

        mode     = "PAPER" if PAPER_MODE else "LIVE"
        btc_mode = "LIVE"  if BTC_LIVE_MODE else "PAPER"
        log.info(f"WeatherBet Ultimate starting -- {mode} MODE")
        log.info(f"[BTC] Mode: {btc_mode}")
        log.info(f"Cities: {', '.join(ACTIVE_CITIES)}")
        log.info(f"Rapid window: {RAPID_START_UTC:02d}:00-{RAPID_END_UTC:02d}:00 UTC "
                 f"({RAPID_SCAN_INTERVAL}s) → {', '.join(RAPID_SCAN_CITIES)}")
        send_alert(
            f"WeatherBot Ultimate started -- {mode}\n"
            f"BTC Scanner: {btc_mode}\n"
            f"Cities: {', '.join(ACTIVE_CITIES)}"
        )

        stopped     = False
        last_crypto = time.monotonic()   # global BTC cadence — never reset by weather cycle
        while not stopped:
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

            # Sleep until next weather scan.
            # Wake every PRIORITY_CHECK_INTERVAL (60s) to drain the BIG MOVE
            # priority queue and run the BTC scanner on its own 240s cadence.
            weather_sleep = RAPID_SCAN_INTERVAL if rapid else SCAN_INTERVAL
            tag           = "[RAPID]" if rapid else "[NORMAL]"
            deadline      = time.monotonic() + weather_sleep

            while not stopped:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                chunk = min(PRIORITY_CHECK_INTERVAL, remaining)
                log.info(f"{tag} Next weather scan in {remaining:.0f}s "
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

                # BTC scanner: independent 240s cadence
                if time.monotonic() - last_crypto >= CRYPTO_SCAN_INTERVAL:
                    try:
                        run_crypto_scan()
                    except KeyboardInterrupt:
                        log.info("Stopped.")
                        stopped = True
                    except Exception as e:
                        log.error(f"Crypto scan error: {e}", exc_info=True)
                    last_crypto = time.monotonic()
