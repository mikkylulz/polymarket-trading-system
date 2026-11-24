import json, math
from collections import defaultdict
from datetime import datetime, timezone

with open('data/sol_5m_trades.json') as f:
    raw = json.load(f)
trades = raw if isinstance(raw, list) else raw.get('trades', [])
settled = [t for t in trades if t.get('won') is not None]

total_wins = sum(1 for t in settled if t['won'])
overall_wr = total_wins / len(settled)
total_pnl  = sum(t.get('pnl', 0) or 0 for t in settled)

print(f"Total settled: {len(settled)}  Wins: {total_wins}  "
      f"WR: {overall_wr*100:.1f}%  P&L: ${total_pnl:+.2f}")
print()

# Wilson score 95% CI for a proportion
def wilson_ci(wins, n):
    if n == 0: return (0, 0)
    z   = 1.96
    p   = wins / n
    mid = (p + z*z/(2*n)) / (1 + z*z/n)
    hw  = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
    return (max(0, mid-hw)*100, min(1, mid+hw)*100)

def stats(grp, label, indent='  '):
    if not grp:
        print(f"{indent}{label:35}  n=  0  (insufficient data)")
        return
    n   = len(grp)
    w   = sum(1 for t in grp if t['won'])
    wr  = w / n * 100
    pnl = sum(t.get('pnl',0) or 0 for t in grp)
    lo, hi = wilson_ci(w, n)
    sig = '***' if n >= 60 else ('**' if n >= 30 else '*  <- CAUTION n<30')
    print(f"{indent}{label:35}  n={n:>4}  WR={wr:>5.1f}%  CI=[{lo:>4.1f}%-{hi:>4.1f}%]  "
          f"avg=${pnl/n:>+6.2f}  {sig}")

# ── Module agreement ──────────────────────────────────────────────────────────
def mod_agree(t):
    d = t.get('direction', 'UP')
    # p_reversal absent in early trades — treat None as neutral 0.5
    mods = [
        t.get('p_trend',     0.5) or 0.5,
        t.get('p_level',     0.5) or 0.5,
        t.get('p_momentum',  0.5) or 0.5,
        t.get('p_reversal',  0.5) or 0.5,
    ]
    if d == 'UP':
        return sum(1 for m in mods if m > 0.5)
    else:
        return sum(1 for m in mods if m < 0.5)

print('='*80)
print('ANALYSIS 1  MODULE AGREEMENT vs WR  (4 directional modules)')
print('='*80)
ag_groups = defaultdict(list)
for t in settled:
    ag_groups[mod_agree(t)].append(t)

# Also note how many trades had p_reversal = None (pre-reversal module)
no_reversal = sum(1 for t in settled if t.get('p_reversal') is None)
print(f"  Note: {no_reversal} trades have p_reversal=None (pre-reversal module, treated as neutral)")
print()
for ag in sorted(ag_groups.keys(), reverse=True):
    stats(ag_groups[ag], f"{ag}/4 modules agree", indent='  ')

# ── Direction WR ──────────────────────────────────────────────────────────────
print()
print('='*80)
print('ANALYSIS 2  DIRECTION-SPECIFIC WR')
print('='*80)
for d in ['UP', 'DOWN']:
    grp = [t for t in settled if t.get('direction') == d]
    if not grp: continue
    avg_entry = sum(t.get('entry_price',0) for t in grp) / len(grp)
    stats(grp, f"{d} trades  (avg_entry={avg_entry:.3f})", indent='  ')

# ── Spread distribution ───────────────────────────────────────────────────────
print()
print('='*80)
print('ANALYSIS 3  SPREAD DISTRIBUTION vs WR')
print('='*80)
spread_buckets = [(0.0,0.10,'0.00-0.10'), (0.10,0.20,'0.10-0.20'),
                  (0.20,0.30,'0.20-0.30'), (0.30,0.40,'0.30-0.40'),
                  (0.40,1.0, '0.40+    ')]
for (lo, hi, label) in spread_buckets:
    grp = [t for t in settled if lo <= (t.get('spread',0) or 0) < hi]
    stats(grp, f"spread {label}", indent='  ')

# ── SOL price move magnitude ──────────────────────────────────────────────────
print()
print('='*80)
print('ANALYSIS 4  SOL PRICE MOVE DURING WINDOW vs WR')
print('='*80)
def sol_move(t):
    se = t.get('sol_entry', 0) or 0
    sx = t.get('sol_exit',  0) or se
    return abs(sx - se) / se * 100 if se else 0

dist_buckets = [(0,0.05,'<0.05%   '), (0.05,0.15,'0.05-0.15%'),
                (0.15,0.30,'0.15-0.30%'), (0.30,0.50,'0.30-0.50%'),
                (0.50,99, '0.50%+   ')]
for (lo, hi, label) in dist_buckets:
    grp = [t for t in settled if lo <= sol_move(t) < hi]
    stats(grp, f"SOL move {label}", indent='  ')

# ── Time of day ───────────────────────────────────────────────────────────────
print()
print('='*80)
print('ANALYSIS 5  TIME OF DAY WR (UTC 4-hour windows)')
print('='*80)
windows = [(0,4,'00-04 UTC'), (4,8,'04-08 UTC'), (8,12,'08-12 UTC'),
           (12,16,'12-16 UTC'), (16,20,'16-20 UTC'), (20,24,'20-24 UTC')]

window_results = []
for (lo, hi, label) in windows:
    grp = [t for t in settled
           if lo <= datetime.fromisoformat(
               t['opened_at'].replace('Z','+00:00')).hour < hi]
    if not grp:
        continue
    w   = sum(1 for t in grp if t['won'])
    wr  = w / len(grp)
    pnl = sum(t.get('pnl',0) or 0 for t in grp)
    lo_ci, hi_ci = wilson_ci(w, len(grp))
    bar  = '#' * int(wr*25)
    sig  = '***' if len(grp) >= 60 else ('**' if len(grp) >= 30 else '*')
    print(f"  {label:12}  n={len(grp):>4}  WR={wr*100:>5.1f}%  "
          f"CI=[{lo_ci:>4.1f}%-{hi_ci:>4.1f}%]  avg=${pnl/len(grp):>+6.2f}  {bar}")
    window_results.append((label, grp, wr, len(grp)))

# Compare to BTC 15m findings
print()
print('  BTC 15m comparison:')
print('  12-16 UTC was the weak window for BTC 15m (38.7% WR)')
sol_weak = [(l,g,wr,n) for l,g,wr,n in window_results if '12-16' in l]
sol_best = max(window_results, key=lambda x: x[2])
sol_worst = min(window_results, key=lambda x: x[2])
if sol_weak:
    l, g, wr, n = sol_weak[0]
    lo_ci, hi_ci = wilson_ci(sum(1 for t in g if t['won']), n)
    print(f"  SOL 12-16 UTC: n={n}  WR={wr*100:.1f}%  CI=[{lo_ci:.1f}%-{hi_ci:.1f}%]")
print(f"  SOL worst window: {sol_worst[0]}  WR={sol_worst[2]*100:.1f}%")
print(f"  SOL best window : {sol_best[0]}  WR={sol_best[2]*100:.1f}%")

# ── Candidate filters ─────────────────────────────────────────────────────────
print()
print('='*80)
print('ANALYSIS 6  CANDIDATE FILTERS')
print(f'  Baseline WR: {overall_wr*100:.1f}%  Target: improve by >=2%, eliminate <=25% trades, n>=40')
print('='*80)

candidates = []

# Module agreement
for min_ag in [3, 4]:
    grp_in  = [t for t in settled if mod_agree(t) >= min_ag]
    grp_out = [t for t in settled if mod_agree(t) <  min_ag]
    if len(grp_in) < 40: continue
    w_in  = sum(1 for t in grp_in if t['won'])
    wr_in = w_in / len(grp_in)
    imp   = (wr_in - overall_wr) * 100
    red   = len(grp_out) / len(settled) * 100
    pnl   = sum(t.get('pnl',0) or 0 for t in grp_in)
    lo_ci, hi_ci = wilson_ci(w_in, len(grp_in))
    candidates.append((f'modules_agree >= {min_ag}', grp_in, wr_in, imp, red, pnl, lo_ci, hi_ci))

# Spread thresholds
for max_sp in [0.25, 0.30, 0.35]:
    grp_in  = [t for t in settled if (t.get('spread',0) or 0) < max_sp]
    grp_out = [t for t in settled if (t.get('spread',0) or 0) >= max_sp]
    if len(grp_in) < 40: continue
    w_in  = sum(1 for t in grp_in if t['won'])
    wr_in = w_in / len(grp_in)
    imp   = (wr_in - overall_wr) * 100
    red   = len(grp_out) / len(settled) * 100
    pnl   = sum(t.get('pnl',0) or 0 for t in grp_in)
    lo_ci, hi_ci = wilson_ci(w_in, len(grp_in))
    candidates.append((f'spread < {max_sp}', grp_in, wr_in, imp, red, pnl, lo_ci, hi_ci))

# win_p thresholds
for min_wp in [0.55, 0.58, 0.60, 0.62]:
    grp_in  = [t for t in settled if (t.get('p',0) or 0) >= min_wp]
    if len(grp_in) < 40: continue
    w_in  = sum(1 for t in grp_in if t['won'])
    wr_in = w_in / len(grp_in)
    imp   = (wr_in - overall_wr) * 100
    red   = (len(settled)-len(grp_in)) / len(settled) * 100
    pnl   = sum(t.get('pnl',0) or 0 for t in grp_in)
    lo_ci, hi_ci = wilson_ci(w_in, len(grp_in))
    candidates.append((f'win_p >= {min_wp}', grp_in, wr_in, imp, red, pnl, lo_ci, hi_ci))

# Time windows — block worst, keep best
for (lo, hi, label) in windows:
    # Block this window
    grp_in  = [t for t in settled
               if not (lo <= datetime.fromisoformat(
                   t['opened_at'].replace('Z','+00:00')).hour < hi)]
    grp_out = [t for t in settled
               if lo <= datetime.fromisoformat(
                   t['opened_at'].replace('Z','+00:00')).hour < hi]
    if len(grp_in) < 40: continue
    w_in  = sum(1 for t in grp_in if t['won'])
    wr_in = w_in / len(grp_in)
    imp   = (wr_in - overall_wr) * 100
    red   = len(grp_out) / len(settled) * 100
    pnl   = sum(t.get('pnl',0) or 0 for t in grp_in)
    lo_ci, hi_ci = wilson_ci(w_in, len(grp_in))
    candidates.append((f'block {label}', grp_in, wr_in, imp, red, pnl, lo_ci, hi_ci))

# Direction: DOWN only
grp_dn = [t for t in settled if t.get('direction') == 'DOWN']
if len(grp_dn) >= 40:
    w_dn  = sum(1 for t in grp_dn if t['won'])
    wr_dn = w_dn / len(grp_dn)
    imp   = (wr_dn - overall_wr) * 100
    red   = (len(settled)-len(grp_dn)) / len(settled) * 100
    pnl   = sum(t.get('pnl',0) or 0 for t in grp_dn)
    lo_ci, hi_ci = wilson_ci(w_dn, len(grp_dn))
    candidates.append(('DOWN trades only', grp_dn, wr_dn, imp, red, pnl, lo_ci, hi_ci))

# Sort by improvement
print(f"  {'Filter':35}  {'N':>5}  {'WR':>6}  {'Impr':>6}  {'Elim':>6}  {'CI (95%)':>14}  {'Meets criteria'}")
print('  ' + '-'*100)
for name, grp, wr, imp, red, pnl, lo_ci, hi_ci in sorted(candidates, key=lambda x: -x[3]):
    meets = (imp >= 2.0 and red <= 25.0 and len(grp) >= 40)
    tag   = '<-- VIABLE' if meets else ''
    print(f"  {name:35}  {len(grp):>5}  {wr*100:>5.1f}%  {imp:>+5.1f}%  {red:>5.1f}%  "
          f"[{lo_ci:>4.1f}%-{hi_ci:>4.1f}%]  {tag}")

# Detailed verdict
print()
print('='*80)
print('VERDICT')
print('='*80)
viable = [(n,g,wr,imp,red,pnl,lo,hi) for n,g,wr,imp,red,pnl,lo,hi in candidates
          if imp >= 2.0 and red <= 25.0 and len(g) >= 40]

if viable:
    best = sorted(viable, key=lambda x: -x[3])[0]
    name, grp, wr, imp, red, pnl, lo_ci, hi_ci = best
    would_filter = len(settled) - len(grp)
    print(f"  FILTER FOUND: {name}")
    print(f"  Subset    : {len(grp)} trades  WR={wr*100:.1f}%  CI=[{lo_ci:.1f}%-{hi_ci:.1f}%]")
    print(f"  Improvement  : +{imp:.1f}% (baseline {overall_wr*100:.1f}% -> {wr*100:.1f}%)")
    print(f"  Trades eliminated: {would_filter} ({red:.1f}%)")
    print(f"  P&L (subset) : ${pnl:+.2f}  avg=${pnl/len(grp):+.2f}")
    print()
    filtered_out = [t for t in settled if t not in grp]
    print(f"  Sample of filtered trades (first 12):")
    print(f"  {'Opened':16}  {'Dir':4}  {'sp':5}  {'win_p':5}  {'mods':4}  {'W/L':4}  {'PnL':>7}")
    print('  ' + '-'*60)
    for t in filtered_out[:12]:
        res = 'WIN ' if t['won'] else 'LOSS'
        pnl_t = t.get('pnl',0) or 0
        sp    = t.get('spread',0) or 0
        wp    = t.get('p',0) or 0
        mg    = mod_agree(t)
        print(f"  {t['opened_at'][5:16]:16}  {t.get('direction','?'):4}  "
              f"{sp:.3f}  {wp:.3f}  {mg}/4  {res}  ${pnl_t:>+6.2f}")
else:
    print("  NO single filter met all three criteria (imp>=2%, elim<=25%, n>=40).")
    print()
    print("  Best individual candidates (by improvement):")
    for name, grp, wr, imp, red, pnl, lo_ci, hi_ci in sorted(candidates, key=lambda x: -x[3])[:5]:
        fails = []
        if imp < 2.0:  fails.append(f"imp={imp:+.1f}% (need >=2%)")
        if red > 25.0: fails.append(f"elim={red:.0f}% (need <=25%)")
        if len(grp)<40: fails.append(f"n={len(grp)} (need >=40)")
        print(f"    {name:35}  WR={wr*100:.1f}%  imp={imp:+.1f}%  elim={red:.0f}%  FAILS: {', '.join(fails)}")
    print()
    print("  RECOMMENDATION: Do not add a filter to SOL 5m. No evidence-backed filter")
    print("  meets the stated criteria. Adding one would be data mining.")
