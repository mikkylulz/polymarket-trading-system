import json
from collections import defaultdict
from datetime import datetime, timezone

with open('data/btc_15m_trades.json') as f:
    all_trades = json.load(f)

settled = [t for t in all_trades if t.get('won') is not None]
overall_wr = sum(1 for t in settled if t['won']) / len(settled)
total_pnl  = sum(t.get('pnl',0) or 0 for t in settled)
print(f"Total settled: {len(settled)}  Wins: {sum(1 for t in settled if t['won'])}  "
      f"WR: {overall_wr*100:.1f}%  P&L: ${total_pnl:+.2f}")
print()

# ── Analysis 1: Module agreement ─────────────────────────────────────────────
def module_agreement(t):
    direction = t.get('direction', 'UP')
    mods = [t.get('p_trend', 0.5), t.get('p_level', 0.5),
            t.get('p_momentum', 0.5), t.get('p_reversal', 0.5)]
    if direction == 'UP':
        return sum(1 for m in mods if m > 0.5)
    else:
        return sum(1 for m in mods if m < 0.5)

print('='*65)
print('ANALYSIS 1  MODULE AGREEMENT vs WR')
print('='*65)
ag_groups = defaultdict(list)
for t in settled:
    ag_groups[module_agreement(t)].append(t)
print(f"  {'Modules':16}  {'Count':>6}  {'WR':>6}  {'Avg PnL':>9}  {'Total PnL':>10}")
print('  ' + '-'*55)
for ag in sorted(ag_groups.keys(), reverse=True):
    grp = ag_groups[ag]
    w   = sum(1 for t in grp if t['won'])
    pnl = sum(t.get('pnl',0) or 0 for t in grp)
    print(f"  {ag}/4 agree      {len(grp):>6}  {w/len(grp)*100:>5.1f}%  "
          f"${pnl/len(grp):>+8.2f}  ${pnl:>+9.2f}")

# ── Analysis 2: Direction WR ──────────────────────────────────────────────────
print()
print('='*65)
print('ANALYSIS 2  DIRECTION-SPECIFIC WR')
print('='*65)
for direction in ['UP', 'DOWN']:
    grp = [t for t in settled if t.get('direction') == direction]
    if not grp: continue
    w   = sum(1 for t in grp if t['won'])
    pnl = sum(t.get('pnl',0) or 0 for t in grp)
    avg_entry = sum(t.get('entry_price',0) for t in grp) / len(grp)
    print(f"  {direction:4}: {len(grp):>4} trades  WR={w/len(grp)*100:.1f}%  "
          f"avg_pnl=${pnl/len(grp):+.2f}  total=${pnl:+.2f}  avg_entry={avg_entry:.3f}")

# ── Analysis 3: Spread distribution ──────────────────────────────────────────
print()
print('='*65)
print('ANALYSIS 3  SPREAD DISTRIBUTION vs WR')
print('='*65)
spread_buckets = [(0.0,0.10,'0.00-0.10'), (0.10,0.20,'0.10-0.20'),
                  (0.20,0.30,'0.20-0.30'), (0.30,0.40,'0.30-0.40'), (0.40,1.0,'0.40+')]
print(f"  {'Spread':12}  {'Count':>6}  {'WR':>6}  {'Avg PnL':>9}  {'Total PnL':>10}")
print('  ' + '-'*55)
for (lo, hi, label) in spread_buckets:
    grp = [t for t in settled if lo <= (t.get('spread',0) or 0) < hi]
    if not grp: continue
    w   = sum(1 for t in grp if t['won'])
    pnl = sum(t.get('pnl',0) or 0 for t in grp)
    print(f"  {label:12}  {len(grp):>6}  {w/len(grp)*100:>5.1f}%  "
          f"${pnl/len(grp):>+8.2f}  ${pnl:>+9.2f}")

# ── Analysis 4: BTC price move magnitude ─────────────────────────────────────
print()
print('='*65)
print('ANALYSIS 4  BTC PRICE MOVE DURING WINDOW vs WR')
print('='*65)
def btc_move_pct(t):
    be = t.get('btc_entry', 0) or 0
    bo = t.get('btc_exit',  0) or be
    return abs(bo - be) / be * 100 if be else 0

dist_buckets = [(0,0.05,'<0.05%'), (0.05,0.15,'0.05-0.15%'),
                (0.15,0.30,'0.15-0.30%'), (0.30,0.50,'0.30-0.50%'), (0.50,99,'0.50%+')]
print(f"  {'BTC Move':12}  {'Count':>6}  {'WR':>6}  {'Avg PnL':>9}")
print('  ' + '-'*40)
for (lo, hi, label) in dist_buckets:
    grp = [t for t in settled if lo <= btc_move_pct(t) < hi]
    if not grp: continue
    w   = sum(1 for t in grp if t['won'])
    pnl = sum(t.get('pnl',0) or 0 for t in grp)
    print(f"  {label:12}  {len(grp):>6}  {w/len(grp)*100:>5.1f}%  ${pnl/len(grp):>+8.2f}")

# ── Analysis 5: Time of day ───────────────────────────────────────────────────
print()
print('='*65)
print('ANALYSIS 5  TIME OF DAY WR (UTC 4-hour windows)')
print('='*65)
windows = [(0,4,'00-04 UTC'), (4,8,'04-08 UTC'), (8,12,'08-12 UTC'),
           (12,16,'12-16 UTC'), (16,20,'16-20 UTC'), (20,24,'20-24 UTC')]
print(f"  {'Window':12}  {'Count':>6}  {'WR':>6}  {'Avg PnL':>9}  {'Total PnL':>10}")
print('  ' + '-'*60)
for (lo, hi, label) in windows:
    grp = [t for t in settled
           if lo <= datetime.fromisoformat(t['opened_at'].replace('Z','+00:00')).hour < hi]
    if not grp: continue
    w   = sum(1 for t in grp if t['won'])
    pnl = sum(t.get('pnl',0) or 0 for t in grp)
    bar = '#' * int(w/len(grp)*25)
    print(f"  {label:12}  {len(grp):>6}  {w/len(grp)*100:>5.1f}%  "
          f"${pnl/len(grp):>+8.2f}  ${pnl:>+9.2f}  {bar}")

# ── Analysis 6: Candidate filters ────────────────────────────────────────────
print()
print('='*65)
print('ANALYSIS 6  CANDIDATE FILTERS (baseline WR = ' + f'{overall_wr*100:.1f}%)')
print('='*65)

candidates = []

# A: module agreement >= threshold
for min_ag in [3, 4]:
    grp_in  = [t for t in settled if module_agreement(t) >= min_ag]
    grp_out = [t for t in settled if module_agreement(t) <  min_ag]
    if len(grp_in) < 30: continue
    wr_in = sum(1 for t in grp_in if t['won']) / len(grp_in)
    imp   = (wr_in - overall_wr) * 100
    red   = len(grp_out) / len(settled) * 100
    pnl   = sum(t.get('pnl',0) or 0 for t in grp_in)
    candidates.append((f'modules_agree >= {min_ag}', grp_in, wr_in, imp, red, pnl))

# B: spread thresholds
for max_sp in [0.25, 0.30, 0.35]:
    grp_in  = [t for t in settled if (t.get('spread',0) or 0) < max_sp]
    grp_out = [t for t in settled if (t.get('spread',0) or 0) >= max_sp]
    if len(grp_in) < 30: continue
    wr_in = sum(1 for t in grp_in if t['won']) / len(grp_in)
    imp   = (wr_in - overall_wr) * 100
    red   = len(grp_out) / len(settled) * 100
    pnl   = sum(t.get('pnl',0) or 0 for t in grp_in)
    candidates.append((f'spread < {max_sp}', grp_in, wr_in, imp, red, pnl))

# C: UP only when spread < 0.25 or 0.30, all DOWN unchanged
for max_sp_up in [0.25, 0.30]:
    up_in   = [t for t in settled if t.get('direction')=='UP' and (t.get('spread',0) or 0) < max_sp_up]
    dn_all  = [t for t in settled if t.get('direction')=='DOWN']
    combined = up_in + dn_all
    if len(combined) < 30: continue
    wr_c = sum(1 for t in combined if t['won']) / len(combined)
    imp  = (wr_c - overall_wr) * 100
    red  = (len(settled)-len(combined)) / len(settled) * 100
    pnl  = sum(t.get('pnl',0) or 0 for t in combined)
    candidates.append((f'UP: spread<{max_sp_up} only + all DOWN', combined, wr_c, imp, red, pnl))

# D: best 4h window only
for (lo, hi, label) in windows:
    grp = [t for t in settled
           if lo <= datetime.fromisoformat(t['opened_at'].replace('Z','+00:00')).hour < hi]
    if len(grp) < 30: continue
    wr  = sum(1 for t in grp if t['won']) / len(grp)
    imp = (wr - overall_wr) * 100
    red = (len(settled)-len(grp)) / len(settled) * 100
    pnl = sum(t.get('pnl',0) or 0 for t in grp)
    candidates.append((f'ONLY window {label}', grp, wr, imp, red, pnl))

# E: win_p > threshold
for min_wp in [0.55, 0.60, 0.65]:
    grp_in  = [t for t in settled if (t.get('p',0) or 0) >= min_wp]
    if len(grp_in) < 30: continue
    wr_in = sum(1 for t in grp_in if t['won']) / len(grp_in)
    imp   = (wr_in - overall_wr) * 100
    red   = (len(settled)-len(grp_in)) / len(settled) * 100
    pnl   = sum(t.get('pnl',0) or 0 for t in grp_in)
    candidates.append((f'win_p >= {min_wp}', grp_in, wr_in, imp, red, pnl))

print(f"  {'Filter':42}  {'N':>5}  {'WR':>6}  {'Impr':>6}  {'Reduc':>6}  {'PnL':>9}")
print('  ' + '-'*80)
for name, grp, wr, imp, red, pnl in sorted(candidates, key=lambda x: -x[3]):
    sig = '***' if len(grp) >= 60 else ('**' if len(grp) >= 30 else '*')
    print(f"  {name:42}  {len(grp):>5}  {wr*100:>5.1f}%  {imp:>+5.1f}%  {red:>5.1f}%  ${pnl:>+8.2f}  {sig}")

# ── Winning recommendation detail ────────────────────────────────────────────
print()
print('='*65)
print('RECOMMENDATION DETAIL')
print('='*65)
# Pick best candidate with imp >= 2%, red <= 30%, n >= 30
viable = [(name, grp, wr, imp, red, pnl) for name, grp, wr, imp, red, pnl in candidates
          if imp >= 2.0 and red <= 30.0 and len(grp) >= 30]
if viable:
    name, grp, wr, imp, red, pnl = sorted(viable, key=lambda x: -x[3])[0]
    all_passed_n   = len(grp)
    would_filter_n = len(settled) - len(grp)
    print(f"  Best filter  : {name}")
    print(f"  Subset trades: {all_passed_n}  WR={wr*100:.1f}%  "
          f"P&L=${pnl:+.2f}  avg=${pnl/all_passed_n:+.2f}")
    print(f"  WR improvement  : +{imp:.1f}% (baseline {overall_wr*100:.1f}% -> {wr*100:.1f}%)")
    print(f"  Trades filtered : {would_filter_n} ({red:.1f}% of history)")
    print()
    # Show a sample of what would have been filtered
    filtered = [t for t in settled if t not in grp]
    print(f"  Sample filtered trades (first 10):")
    print(f"  {'Opened':16}  {'Dir':4}  {'spread':7}  {'p':5}  {'mods':4}  {'W/L':4}  {'PnL':>7}")
    print('  ' + '-'*60)
    for t in filtered[:10]:
        res = 'WIN' if t['won'] else 'LOSS'
        pnl_t = t.get('pnl',0) or 0
        mods  = module_agreement(t)
        sp    = t.get('spread',0) or 0
        wp    = t.get('p', 0) or 0
        print(f"  {t['opened_at'][5:16]:16}  {t.get('direction','?'):4}  "
              f"{sp:.3f}    {wp:.3f}  {mods}/4  {res}  ${pnl_t:>+6.2f}")
else:
    print("  No filter met all three criteria (imp>=2%, red<=30%, n>=30)")
    print("  Best available:")
    best = sorted(candidates, key=lambda x: -x[3])[0]
    print(f"    {best[0]}  WR={best[2]*100:.1f}%  imp={best[3]:+.1f}%  red={best[4]:.1f}%  n={len(best[1])}")
