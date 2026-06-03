import json, statistics
from collections import Counter
from pathlib import Path

def load(p):
    f = Path(p)
    if not f.exists(): return []
    d = json.loads(f.read_text())
    return d if isinstance(d, list) else d.get('trades', [])

btc_all = load('data/btc_5m_trades.json')
sol_all = load('data/sol_5m_trades.json')

# New thresholds deployed ~15:43 UTC June 2 (from verification log timestamp).
# Find first trade where p_reversal is non-0.50 (confirms new code is live)
# OR use timestamp cutoff: 2026-06-02T15:43
DEPLOY_TS = "2026-06-02T15:43"

def after_deploy(trades):
    return [t for t in trades if t.get('opened_at','') >= DEPLOY_TS
            and t.get('p_reversal') is not None]

btc_new = after_deploy(btc_all)
sol_new = after_deploy(sol_all)

# Use last 30 settled for each
btc_s = [t for t in btc_new if t.get('won') is not None]
sol_s = [t for t in sol_new if t.get('won') is not None]
btc30 = btc_s[-30:] if len(btc_s) >= 30 else btc_s
sol30 = sol_s[-30:] if len(sol_s) >= 30 else sol_s

print(f"Post-deploy settled  BTC: {len(btc_s)} total, using {len(btc30)}")
print(f"Post-deploy settled  SOL: {len(sol_s)} total, using {len(sol30)}")
print(f"BTC period: {btc30[0]['opened_at'][:16]} -> {btc30[-1]['opened_at'][:16]}" if btc30 else "BTC: no data")
print(f"SOL period: {sol30[0]['opened_at'][:16]} -> {sol30[-1]['opened_at'][:16]}" if sol30 else "SOL: no data")
print()

def wr(lst):   return sum(1 for t in lst if t.get('won'))/len(lst)*100 if lst else 0
def pnl(lst):  return sum(t.get('pnl',0) for t in lst)/len(lst) if lst else 0

# Previous diagnostic baselines
PREV = {
    'BTC': {'down_pct':90, 'up_pct':10, 'down_wr':66.7, 'up_wr':0.0,
            'per_trade':0.44, 'win_p_delta':0.0360},
    'SOL': {'down_pct':92, 'up_pct':8,  'down_wr':58.3, 'up_wr':0.0,
            'per_trade':0.83, 'win_p_delta':0.0317},
}

def run_all(trades, label):
    if not trades:
        print(f"\n{label}: no settled post-deploy trades yet\n")
        return

    up   = [t for t in trades if t['direction'] == 'UP']
    dn   = [t for t in trades if t['direction'] == 'DOWN']
    wins = [t for t in trades if t['won']]
    loss = [t for t in trades if not t['won']]

    w_winp = sum(t.get('p',0) for t in wins)/len(wins) if wins else 0
    l_winp = sum(t.get('p',0) for t in loss)/len(loss) if loss else 0
    delta  = w_winp - l_winp

    prev = PREV[label]

    # ── Analysis 1 ──────────────────────────────────────────────
    print('='*62)
    print(f'ANALYSIS 1 — Reversal Signal Distribution ({label}, n={len(trades)})')
    print('='*62)
    print(f'  {"Signal":<24}  {"Count":>5}  {"WR":>6}  {"Avg P&L":>8}  {"% of trades"}')
    print('  '+'-'*56)
    for rv, sig in [(0.30,'DOWN coming'),(0.50,'neutral'),(0.70,'UP coming')]:
        grp   = [t for t in trades if t.get('p_reversal') == rv]
        cnt   = len(grp)
        pct   = f'{cnt/len(trades)*100:.0f}%'
        wrate = f'{wr(grp):.1f}%' if grp else '-'
        avgp  = f'${pnl(grp):+.2f}' if grp else '-'
        fired = '  <<< FIRED' if rv != 0.50 and cnt > 0 else ''
        print(f'  p_rev={rv} ({sig:<11})  {cnt:>5}  {wrate:>6}  {avgp:>8}  {pct}{fired}')

    non_neutral = sum(1 for t in trades if t.get('p_reversal') != 0.50)
    print(f'\n  Non-neutral signals: {non_neutral}/{len(trades)} ({non_neutral/len(trades)*100:.0f}%)')
    print(f'  Previous diagnostic:  0/33 (0%) — all stuck at 0.50')

    # ── Analysis 2 ──────────────────────────────────────────────
    print()
    print('='*62)
    print(f'ANALYSIS 2 — Direction Distribution ({label})')
    print('='*62)
    n = len(trades)
    up_pct  = len(up)/n*100
    dn_pct  = len(dn)/n*100
    print(f'  {"Metric":<26}  {"Prev diag":>10}  {"Now":>10}  {"Change"}')
    print('  '+'-'*56)
    print(f'  {"UP count / %":<26}  {prev["up_pct"]:>9.0f}%  {up_pct:>9.0f}%'
          f'  {"UP" if up_pct > prev["up_pct"] else "DOWN/same"}')
    print(f'  {"DOWN count / %":<26}  {prev["down_pct"]:>9.0f}%  {dn_pct:>9.0f}%')
    print(f'  {"UP WR":<26}  {prev["up_wr"]:>9.1f}%  {wr(up):>9.1f}%')
    print(f'  {"DOWN WR":<26}  {prev["down_wr"]:>9.1f}%  {wr(dn):>9.1f}%')
    print(f'  {"Per-trade avg P&L":<26}  ${prev["per_trade"]:>+9.2f}  ${pnl(trades):>+9.2f}'
          f'  {"IMPROVED" if pnl(trades) > prev["per_trade"] else "LOWER"}')
    print(f'\n  UP trades: {len(up)} | DOWN trades: {len(dn)}')
    for t in up:
        print(f'    UP  {t["opened_at"][:16]}  entry={t["entry_price"]:.3f}'
              f'  p_rev={t.get("p_reversal","?")}  {"WIN" if t["won"] else "LOSS"}')

    # ── Analysis 3 ──────────────────────────────────────────────
    print()
    print('='*62)
    print(f'ANALYSIS 3 — win_p Calibration ({label})')
    print('='*62)
    verdict = 'SIGNAL' if delta > 0.02 else ('MARGINAL' if delta > 0 else 'INVERTED')
    trend   = ('IMPROVED' if delta > prev['win_p_delta']
               else 'SAME' if abs(delta-prev['win_p_delta']) < 0.005 else 'WEAKER')
    print(f'  Winners ({len(wins)}) avg win_p : {w_winp:.4f}')
    print(f'  Losers  ({len(loss)}) avg win_p  : {l_winp:.4f}')
    print(f'  Delta (now)             : {delta:+.4f}  ({verdict})')
    print(f'  Delta (prev diagnostic) : {prev["win_p_delta"]:+.4f}')
    print(f'  Trend                   : {trend}')

    # ── Analysis 4 ──────────────────────────────────────────────
    print()
    print('='*62)
    print(f'ANALYSIS 4 — Reversal Signal Accuracy ({label})')
    print('='*62)
    has_data = False
    for rv, pred_dir in [(0.70,'UP'),(0.30,'DOWN')]:
        grp = [t for t in trades if t.get('p_reversal') == rv]
        if not grp:
            print(f'  p_rev={rv} ({pred_dir} coming): 0 trades — no data')
            continue
        has_data = True
        actual_up = []
        for t in grp:
            ei = t.get('btc_entry') or t.get('sol_entry') or 0
            ex = t.get('btc_exit')  or t.get('sol_exit')  or 0
            if ex and ei:
                actual_up.append(ex > ei)
            else:
                actual_up.append(t['won'] if t['direction']=='UP' else not t['won'])
        pct_up = sum(actual_up)/len(actual_up)*100 if actual_up else 0
        pct_dn = 100 - pct_up
        if rv == 0.70:
            correct = 'YES' if pct_up > 55 else ('WEAK' if pct_up >= 45 else 'NO')
        else:
            correct = 'YES' if pct_dn > 55 else ('WEAK' if pct_dn >= 45 else 'NO')
        print(f'  p_rev={rv} ({pred_dir} coming, n={len(grp)}): '
              f'actual UP={pct_up:.1f}% DN={pct_dn:.1f}%  Correct={correct}')
        for t in grp:
            ei = t.get('btc_entry') or t.get('sol_entry') or 0
            ex = t.get('btc_exit')  or t.get('sol_exit')  or 0
            actual = ('UP' if ex > ei else 'DOWN') if ex else 'unknown'
            print(f'    {t["opened_at"][:16]}  dir={t["direction"]}  '
                  f'p_rev={rv}  actual_move={actual}  {"WIN" if t["won"] else "LOSS"}')
    neutral = [t for t in trades if t.get('p_reversal') == 0.50]
    if neutral:
        nu = []
        for t in neutral:
            ei = t.get('btc_entry') or t.get('sol_entry') or 0
            ex = t.get('btc_exit')  or t.get('sol_exit')  or 0
            if ex and ei: nu.append(ex > ei)
        pct_u = sum(nu)/len(nu)*100 if nu else 0
        print(f'  p_rev=0.50 (neutral, n={len(neutral)}): '
              f'actual UP={pct_u:.1f}% DN={100-pct_u:.1f}%')

    # ── Analysis 5 ──────────────────────────────────────────────
    print()
    print('='*62)
    print(f'ANALYSIS 5 — Market Regime ({label})')
    print('='*62)
    px_key  = 'btc_entry' if label == 'BTC' else 'sol_entry'
    prices  = [t.get(px_key,0) for t in trades if t.get(px_key)]
    if prices:
        net_pct = (prices[-1]-prices[0])/prices[0]*100
        rng_pct = (max(prices)-min(prices))/statistics.mean(prices)*100
        regime  = ('TRENDING UP' if net_pct>1 else
                   'TRENDING DOWN' if net_pct<-1 else 'CHOPPY')
        print(f'  Period  : {trades[0]["opened_at"][:16]} -> {trades[-1]["opened_at"][:16]}')
        print(f'  Start   : ${prices[0]:,.2f}    End: ${prices[-1]:,.2f}')
        print(f'  High    : ${max(prices):,.2f}    Low: ${min(prices):,.2f}')
        print(f'  Net     : {net_pct:+.2f}%   Range: {rng_pct:.2f}%')
        print(f'  Regime  : {regime}')

    print()
    print('='*62)
    print(f'SUMMARY VERDICT ({label})')
    print('='*62)
    rv_vals     = [t.get('p_reversal') for t in trades if t.get('p_reversal') is not None]
    non_n       = sum(1 for v in rv_vals if v != 0.50)
    acc70       = 0.0
    grp70 = [t for t in trades if t.get('p_reversal')==0.70]
    if grp70:
        aus = []
        for t in grp70:
            ei = t.get('btc_entry') or t.get('sol_entry') or 0
            ex = t.get('btc_exit')  or t.get('sol_exit')  or 0
            aus.append((ex > ei) if ex else (t['won'] if t['direction']=='UP' else not t['won']))
        acc70 = sum(aus)/len(aus)*100 if aus else 0

    print(f'  1. Reversal varied?     {"YES" if non_n>0 else "NO"} — '
          f'{non_n}/{len(rv_vals)} non-neutral ({non_n/len(rv_vals)*100:.0f}%)')
    print(f'  2. UP frequency?        {len(up)/n*100:.0f}% (prev: {prev["up_pct"]}%, target: 25-30%)')
    print(f'  3. win_p delta?         {delta:+.4f} (prev: {prev["win_p_delta"]:+.4f})')
    print(f'  4. p_rev=0.70 -> UP?    '
          f'{"n/a (no 0.70 trades)" if not grp70 else f"{acc70:.1f}% (need >55%)"}')
    print(f'  5. Per-trade P&L?       ${pnl(trades):+.2f} (prev: ${prev["per_trade"]:+.2f}, target: $0.80+)')
    print()

run_all(btc30, 'BTC')
run_all(sol30, 'SOL')
