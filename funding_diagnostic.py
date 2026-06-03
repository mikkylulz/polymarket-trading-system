"""
funding_diagnostic.py
Polls Gate.io for BTC_USDT and SOL_USDT funding rates every 5 minutes for 2 hours.
Saves each reading to data/funding_diagnostic.json and prints a final analysis.

Run:  python funding_diagnostic.py
"""
import json, math, os, statistics, time
from datetime import datetime, timezone

import requests

OUT = os.path.join(os.path.dirname(__file__), "data", "funding_diagnostic.json")
GATE_URL = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate"
CONTRACTS = ["BTC_USDT", "SOL_USDT"]
INTERVAL  = 300   # 5 minutes
READINGS  = 24    # 2 hours


def fetch_rate(contract: str) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    try:
        r = requests.get(GATE_URL, params={"contract": contract, "limit": 1}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            return {"ts": ts, "contract": contract, "raw": None, "rate_pct": None, "error": "empty response"}
        item = data[0]
        rate_pct = round(float(item["r"]) * 100, 6)
        return {
            "ts":          ts,
            "contract":    contract,
            "raw":         item,
            "rate":        float(item["r"]),
            "rate_pct":    rate_pct,
            "next_funding_time": item.get("t"),
            "error":       None,
        }
    except Exception as e:
        return {"ts": ts, "contract": contract, "raw": None, "rate_pct": None, "error": str(e)}


def load_results() -> list:
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_results(results: list):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(results, f, indent=2)


def print_reading(n: int, total: int, readings: list):
    print(f"\n[{n:02d}/{total}] {readings[-1]['ts'][:19]}Z" if readings else "")
    for r in readings:
        if r["error"]:
            print(f"  {r['contract']:9s}  ERROR: {r['error']}")
        else:
            print(f"  {r['contract']:9s}  rate={r['rate_pct']:+.6f}%  next_funding={r.get('next_funding_time','?')}")


def final_analysis(results: list):
    print("\n" + "=" * 60)
    print("FUNDING RATE DIAGNOSTIC — FINAL ANALYSIS")
    print("=" * 60)
    print(f"Total readings collected: {len(results)}")
    print(f"Period: {results[0]['ts'][:19]} → {results[-1]['ts'][:19]}")

    for contract in CONTRACTS:
        rows = [r for r in results if r["contract"] == contract and r.get("rate_pct") is not None]
        if not rows:
            print(f"\n{contract}: NO DATA (all errors)")
            continue

        rates = [r["rate_pct"] for r in rows]
        unique = sorted(set(rates))
        errors = [r for r in results if r["contract"] == contract and r.get("error")]

        print(f"\n{contract} ({len(rows)} successful / {len(errors)} errors)")
        print(f"  Unique values : {len(unique)}")
        print(f"  Min           : {min(rates):+.6f}%")
        print(f"  Max           : {max(rates):+.6f}%")
        print(f"  Mean          : {sum(rates)/len(rates):+.6f}%")
        if len(rates) >= 2:
            print(f"  Std dev       : {statistics.stdev(rates):.6f}%")
        else:
            print(f"  Std dev       : N/A (insufficient data)")
        print(f"  All values    : {unique}")

        # Change-frequency analysis
        changes = sum(1 for i in range(1, len(rates)) if rates[i] != rates[i-1])
        print(f"  Rate changes  : {changes} out of {len(rates)-1} consecutive pairs")

        if len(unique) == 1:
            verdict = "STUCK — single value for entire 2-hour window. Feed is broken or genuinely flat."
        elif changes == 0:
            verdict = "STUCK — value never changed between consecutive readings."
        elif changes <= 2:
            verdict = "BARELY MOVING — updates on Gate.io funding schedule (every 4-8h), not every 5 min."
        else:
            verdict = "LIVE — value updating more frequently than the funding period."
        print(f"  Verdict       : {verdict}")

        # Show the full time series
        print(f"  Time series   :")
        for r in rows:
            print(f"    {r['ts'][11:19]}  {r['rate_pct']:+.6f}%")


def main():
    print(f"Funding rate diagnostic starting — {READINGS} readings × {INTERVAL}s = {READINGS*INTERVAL//60} minutes")
    print(f"Output file: {OUT}")
    print(f"Contracts: {CONTRACTS}")

    results = load_results()

    for i in range(1, READINGS + 1):
        batch = []
        for contract in CONTRACTS:
            reading = fetch_rate(contract)
            batch.append(reading)
            results.append(reading)

        save_results(results)
        print_reading(i, READINGS, batch)

        if i < READINGS:
            print(f"  (sleeping {INTERVAL}s ...)")
            time.sleep(INTERVAL)

    final_analysis(results)
    print("\nDiagnostic complete. Results saved to:", OUT)


if __name__ == "__main__":
    main()
