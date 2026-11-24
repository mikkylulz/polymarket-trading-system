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

def find_fix_idx(trades):
    for i, t in enumerate(trades):
        if 'p_reversal' in t:
            return i
    return len(trades)

btc_fix = find_fix_idx(btc_all)
sol_fix = find_fix_idx(sol_all)
print(f"BTC reversal deployed at trade {btc_fix+1} of {len(btc_all)}  "
      f"({btc_all[btc_fix]['opened_at'][:16] if btc_fix < len(btc_all) else 'N/A'})")
print(f"SOL reversal deployed at trade {sol_fix+1} of {len(sol_all)}  "
      f"({sol_all[sol_fix]['opened_at'][:16] if sol_fix < len(sol_all) else 'N/A'})")
print()

btc_post_all = [t for t in btc_all[btc_fix:] if t.get('won') is not None]
sol_post_all = [t for t in sol_all[sol_fix:] if t.get('won') is not None]
btc50 = btc_post_all[-50:]
sol50 = sol_post_all[-50:]
print(f"BTC post-fix settled: {len(btc_post_all)}  (using last {len(btc50)})")
print(f"SOL post-fix settled: {len(sol_post_all)}  (using last {len(sol50)})")
print()

def wr(lst):
    return sum(1 for t in lst if t.get('won')) / len(lst) * 100 if lst else 0

def avgpnl(lst):
    return sum(t.get('pnl', 0) for t in lst) / len(lst) if lst else 0

def analyze(trades50):
    settled = [t for t in trades50 if t.get('won') is not None]
    if not settled:
        return {}
    up = [t for t in settled if t['direction'] == 'UP']
    dn = [t for t in settled if t['direction'] == 'DOWN']
    winners = [t for t in settled if t['won']]
    losers  = [t for t in settled if not t['won']]
    w_winp  = sum(t.get('p', 0) for t in winners) / len(winners) if winners else 0
    l_winp  = sum(t.get('p', 0) for t in losers)  / len(losers)  if losers  else 0

    rev_perf = {}
    for rv in [0.30, 0.50, 0.70]:
        grp = [t for t in settled if t.get('p_reversal') == rv]
        if grp:
            rev_perf[rv] = {'n': len(grp), 'wr': wr(grp), 'pnl': avgpnl(grp)}

    rev_acc = {}
    for rv in [0.30, 0.50, 0.70]:
        grp = [t for t in settled if t.get('p_reversal') == rv]
        actual_up = []
        for t in grp:
            ei = t.get('btc_entry') or t.get('sol_entry') or 0
            ex = t.get('btc_exit')  or t.get('sol_exit')  or 0
            if ex and ei:
                actual_up.append(ex > ei)
            else:
                if t['direction'] == 'UP':
                    actual_up.append(t['won'])
                else:
                    actual_up.append(not t['won'])
        if actual_up:
            rev_acc[rv] = {
                'n': len(actual_up),
                'pct_up': sum(actual_up) / len(actual_up) * 100
            }

    return {
        'n': len(settled), 'up': up, 'dn': dn,
        'settled': settled, 'winners': winners, 'losers': losers,
        'w_winp': w_winp, 'l_winp': l_winp,
        'rev_perf': rev_perf, 'rev_acc': rev_acc,
    }

btc_r = analyze(btc50)
sol_r = analyze(sol50)

btc_pre = [t for t in btc_all[:btc_fix] if t.get('won') is not None][-50:]
sol_pre = [t for t in sol_all[:sol_fix] if t.get('won') is not None][-50:]

# ── ANALYSIS 1 ────────────────────────────────────────────────
print('=' * 62)
print('ANALYSIS 1 — Reversal Signal Distribution')
print('=' * 62)
for label, r in [('BTC', btc_r), ('SOL', sol_r)]:
    print(f'\n{label} (n={r["n"]}):')
    print(f'  {"Signal":<24}  {"Count":>5}  {"WR":>6}  {"Avg P&L":>8}')
    print('  ' + '-' * 48)
    for rv, sig in [(0.30, 'DOWN coming'), (0.50, 'neutral'), (0.70, 'UP coming')]:
        p = r['rev_perf'].get(rv, {})
        cnt   = p.get('n', 0)
        wrate = f"{p['wr']:.1f}%" if p else '-'
        pnl   = f"${p['pnl']:+.2f}" if p else '-'
        print(f'  p_reversal={rv} ({sig:<11})  {cnt:>5}  {wrate:>6}  {pnl:>8}')
    no_rv = sum(1 for t in r['settled'] if t.get('p_reversal') is None)
    if no_rv:
        print(f'  No p_reversal (pre-deployment): {no_rv}')

# ── ANALYSIS 2 ────────────────────────────────────────────────
print()
print('=' * 62)
print('ANALYSIS 2 — Direction Distribution Pre vs Post Fix')
print('=' * 62)
for label, pre, post_r, post50 in [
    ('BTC', btc_pre, btc_r, btc50),
    ('SOL', sol_pre, sol_r, sol50),
]:
    up_pre  = [t for t in pre if t['direction'] == 'UP']
    dn_pre  = [t for t in pre if t['direction'] == 'DOWN']
    up_post = post_r['up']
    dn_post = post_r['dn']
    n_pre   = len(pre)
    n_post  = post_r['n']
    print(f'\n{label}:')
    print(f'  {"":26}  {"Pre-fix":>12}  {"Post-fix":>10}')
    print(f'  {"Total trades":26}  {n_pre:>12}  {n_post:>10}')
    print(f'  {"UP count / %":26}  {len(up_pre):3d} ({len(up_pre)/n_pre*100:.0f}%)      '
          f'  {len(up_post):3d} ({len(up_post)/n_post*100:.0f}%)')
    print(f'  {"DOWN count / %":26}  {len(dn_pre):3d} ({len(dn_pre)/n_pre*100:.0f}%)      '
          f'  {len(dn_post):3d} ({len(dn_post)/n_post*100:.0f}%)')
    print(f'  {"UP WR":26}  {wr(up_pre):>11.1f}%  {wr(up_post):>9.1f}%')
    print(f'  {"DOWN WR":26}  {wr(dn_pre):>11.1f}%  {wr(dn_post):>9.1f}%')
    print(f'  {"Per-trade avg P&L":26}  ${avgpnl(pre):>+10.2f}  ${avgpnl(post_r["settled"]):>+8.2f}')

# ── ANALYSIS 3 ────────────────────────────────────────────────
print()
print('=' * 62)
print('ANALYSIS 3 — win_p Calibration Post-Fix')
print('=' * 62)
prev_deltas = {'BTC': -0.0265, 'SOL': +0.0461}
for label, r in [('BTC', btc_r), ('SOL', sol_r)]:
    delta   = r['w_winp'] - r['l_winp']
    prev    = prev_deltas[label]
    verdict = 'SIGNAL' if delta > 0.02 else ('MARGINAL' if delta > 0 else 'INVERTED')
    trend   = 'IMPROVED' if delta > prev else ('SAME' if abs(delta-prev) < 0.005 else 'WORSE')
    print(f'\n{label} (n={r["n"]}):')
    print(f'  Winners ({len(r["winners"])}) avg win_p : {r["w_winp"]:.4f}')
    print(f'  Losers  ({len(r["losers"])}) avg win_p  : {r["l_winp"]:.4f}')
    print(f'  Delta (post-fix)        : {delta:+.4f}  ({verdict})')
    print(f'  Delta (pre-fix baseline): {prev:+.4f}')
    print(f'  Trend                   : {trend}')

# ── ANALYSIS 4 ────────────────────────────────────────────────
print()
print('=' * 62)
print('ANALYSIS 4 — Reversal Signal Accuracy')
print('=' * 62)
for label, r in [('BTC', btc_r), ('SOL', sol_r)]:
    print(f'\n{label}:')
    print(f'  {"Signal":<20}  {"N":>4}  {"Act UP%":>8}  {"Act DN%":>8}  {"Correct?"}')
    print('  ' + '-' * 56)
    for rv, pred in [(0.70, 'UP coming'), (0.30, 'DOWN coming'), (0.50, 'neutral')]:
        acc = r['rev_acc'].get(rv)
        if not acc:
            continue
        pct_up = acc['pct_up']
        pct_dn = 100 - pct_up
        if rv == 0.70:
            correct = 'YES' if pct_up > 55 else ('WEAK' if pct_up >= 45 else 'NO')
        elif rv == 0.30:
            correct = 'YES' if pct_dn > 55 else ('WEAK' if pct_dn >= 45 else 'NO')
        else:
            correct = f'UP={pct_up:.0f}%/DN={pct_dn:.0f}%'
        print(f'  p_rev={rv} ({pred:<11})  {acc["n"]:>4}  {pct_up:>7.1f}%  '
              f'{pct_dn:>7.1f}%  {correct}')

# ── ANALYSIS 5 ────────────────────────────────────────────────
print()
print('=' * 62)
print('ANALYSIS 5 — Market Regime During Post-Fix Period')
print('=' * 62)
for label, trades50, px_key in [('BTC', btc50, 'btc_entry'), ('SOL', sol50, 'sol_entry')]:
    prices = [t.get(px_key, 0) for t in trades50 if t.get(px_key)]
    if not prices:
        continue
    net_pct = (prices[-1] - prices[0]) / prices[0] * 100
    rng_pct = (max(prices) - min(prices)) / statistics.mean(prices) * 100
    regime  = ('TRENDING UP' if net_pct > 1
               else 'TRENDING DOWN' if net_pct < -1 else 'CHOPPY')
    settled = [t for t in trades50 if t.get('won') is not None]
    print(f'\n{label}:')
    print(f'  Period  : {trades50[0]["opened_at"][:16]} -> {trades50[-1]["opened_at"][:16]}')
    print(f'  Start   : ${prices[0]:,.2f}    End: ${prices[-1]:,.2f}')
    print(f'  High    : ${max(prices):,.2f}    Low: ${min(prices):,.2f}')
    print(f'  Net     : {net_pct:+.2f}%   Range: {rng_pct:.2f}%')
    print(f'  Regime  : {regime}')
    print(f'  WR      : {wr(settled):.1f}%   Per-trade P&L: ${avgpnl(settled):+.2f}')

# ── SUMMARY ───────────────────────────────────────────────────
print()
print('=' * 62)
print('SUMMARY VERDICT')
print('=' * 62)
for label, r in [('BTC', btc_r), ('SOL', sol_r)]:
    rv_vals     = [t.get('p_reversal') for t in r['settled'] if t.get('p_reversal') is not None]
    unique_rv   = len(set(rv_vals))
    non_neutral = sum(1 for v in rv_vals if v != 0.50)
    delta       = r['w_winp'] - r['l_winp']
    up_pct      = len(r['up']) / r['n'] * 100
    acc70       = r['rev_acc'].get(0.70, {}).get('pct_up', 0)
    print(f'\n{label}:')
    print(f'  1. Reversal varied?     {unique_rv} unique vals, '
          f'{non_neutral}/{len(rv_vals)} non-neutral ({non_neutral/len(rv_vals)*100:.0f}%)')
    print(f'  2. UP frequency         {up_pct:.0f}% (pre-fix baseline: ~13-32%)')
    print(f'  3. win_p delta          {delta:+.4f} (need >+0.02 for real signal)')
    print(f'  4. p_rev=0.70 -> UP%    {acc70:.1f}% (need >55% for edge)')
    print(f'  5. Per-trade P&L        ${avgpnl(r["settled"]):+.2f}')
