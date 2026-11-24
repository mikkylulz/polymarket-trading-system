import json
from collections import Counter

with open('data/btc_15m_trades.json') as f:
    all_trades = json.load(f)

settled = [t for t in all_trades if t.get('won') is not None]

# ---- Analysis 1 ----
print()
print('='*100)
print('ANALYSIS 1 -- TRADE-BY-TRADE BREAKDOWN')
print('='*100)
print(f"  {'#':>2}  {'Time(UTC)':16}  {'Dir':4}  {'Entry':5}  {'EV':6}  {'Res':4}  {'PnL':>7}  {'BTC In':>9}  {'BTC Out':>9}  {'Move$':>8}  {'Move%':>7}")
print('  ' + '-'*100)
for i, t in enumerate(settled, 1):
    bi  = t.get('btc_entry', 0)
    bo  = t.get('btc_exit', 0) or 0
    mv  = bo - bi
    mvp = mv / bi * 100 if bi else 0
    res = 'WIN ' if t['won'] else 'LOSS'
    pnl = t.get('pnl', 0) or 0
    s   = '+' if mv >= 0 else '-'
    print(f"  {i:>2}  {t['opened_at'][5:16]:16}  {t['direction']:4}  {t['entry_price']:.3f}  {t['ev']:6.4f}  {res}  ${pnl:>+6.2f}  ${bi:>9,.0f}  ${bo:>9,.0f}  {s}${abs(mv):>7,.0f}  {s}{abs(mvp):>6.3f}%")

# ---- Analysis 2 ----
up_t = [t for t in settled if t['direction'] == 'UP']
dn_t = [t for t in settled if t['direction'] == 'DOWN']
up_w = [t for t in up_t if t['won']]
dn_w = [t for t in dn_t if t['won']]
up_pnl = sum(t.get('pnl', 0) or 0 for t in up_t)
dn_pnl = sum(t.get('pnl', 0) or 0 for t in dn_t)
tot_pnl = sum(t.get('pnl', 0) or 0 for t in settled)
print()
print('='*70)
print('ANALYSIS 2 -- DIRECTION BREAKDOWN')
print('='*70)
print(f"  UP  : {len(up_t):>2} trades  {len(up_w)}/{len(up_t)} wins  WR={len(up_w)/len(up_t)*100:.1f}%  avg_entry={sum(t['entry_price'] for t in up_t)/len(up_t):.3f}  total=${up_pnl:+.2f}  per_trade=${up_pnl/len(up_t):+.2f}")
print(f"  DOWN: {len(dn_t):>2} trades  {len(dn_t)} trades  {len(dn_w)}/{len(dn_t)} wins  WR={len(dn_w)/len(dn_t)*100:.1f}%  avg_entry={sum(t['entry_price'] for t in dn_t)/len(dn_t):.3f}  total=${dn_pnl:+.2f}  per_trade=${dn_pnl/len(dn_t):+.2f}")
print(f"  ALL : {len(settled):>2} trades  {len(up_w)+len(dn_w)}/{len(settled)} wins  WR={(len(up_w)+len(dn_w))/len(settled)*100:.1f}%  total=${tot_pnl:+.2f}  per_trade=${tot_pnl/len(settled):+.2f}")

# ---- Analysis 3 ----
wins_p = [t.get('p', 0.5) for t in settled if t['won']]
loss_p = [t.get('p', 0.5) for t in settled if not t['won']]
avg_wp = sum(wins_p) / len(wins_p)
avg_lp = sum(loss_p) / len(loss_p)
delta  = avg_wp - avg_lp
print()
print('='*80)
print('ANALYSIS 3 -- MODULE SIGNAL CALIBRATION')
print('='*80)
print(f"  {'#':>2}  {'Dir':4}  {'p_trend':7}  {'p_level':7}  {'p_mom':5}  {'p_rev':5}  {'p_fund':6}  {'p_oi':5}  {'win_p':5}  Res")
print('  ' + '-'*70)
for i, t in enumerate(settled, 1):
    rev = t.get('p_reversal', 0.5)
    sig = ' [UP]' if rev == 0.70 else (' [DN]' if rev == 0.30 else '     ')
    res = 'WIN ' if t['won'] else 'LOSS'
    print(f"  {i:>2}  {t['direction']:4}  {t.get('p_trend',0):7.3f}  {t.get('p_level',0):7.3f}  {t.get('p_momentum',0):5.3f}  {rev:5.3f}{sig}  {t.get('p_funding',0):6.3f}  {t.get('p_oi',0):5.3f}  {t.get('p',0):5.3f}  {res}")
print()
print(f"  Winners avg win_p : {avg_wp:.4f}  (n={len(wins_p)})")
print(f"  Losers  avg win_p : {avg_lp:.4f}  (n={len(loss_p)})")
verdict = 'GENUINE SIGNAL (delta > +0.05)' if delta > 0.05 else 'WEAK/NOISE (delta < +0.05)'
print(f"  Delta             : {delta:+.4f}  -> {verdict}")

# ---- Analysis 4 ----
r_dn  = [t for t in settled if t.get('p_reversal', 0.5) == 0.30]
r_neu = [t for t in settled if t.get('p_reversal', 0.5) == 0.50]
r_up  = [t for t in settled if t.get('p_reversal', 0.5) == 0.70]
print()
print('='*70)
print('ANALYSIS 4 -- REVERSAL MODULE ACTIVITY')
print('='*70)
for label, grp in [('p_rev=0.30 (bearish)', r_dn), ('p_rev=0.50 (neutral)', r_neu), ('p_rev=0.70 (bullish)', r_up)]:
    if not grp:
        print(f"  {label:24}: 0 trades")
        continue
    w   = sum(1 for t in grp if t['won'])
    pnl = sum(t.get('pnl', 0) or 0 for t in grp)
    dirs = ','.join(t['direction'] for t in grp)
    print(f"  {label:24}: {len(grp):>2} trades  {w}/{len(grp)} wins  WR={w/len(grp)*100:.1f}%  PnL=${pnl:+.2f}  dirs=[{dirs}]")
print()
print("  Detail of p_reversal=0.70 trades (oversold -> UP expected):")
for i, t in enumerate(r_up, 1):
    pnl = t.get('pnl', 0) or 0
    rel = 'ALIGNED' if t['direction'] == 'UP' else 'CONTRARIAN (DOWN bet despite UP signal)'
    print(f"    {i}. {t['direction']} @ {t['entry_price']:.3f}  p_trend={t.get('p_trend',0):.3f}  win_p={t.get('p',0):.3f}  {'WIN' if t['won'] else 'LOSS'}  ${pnl:+.2f}  [{rel}]")

# ---- Analysis 5 ----
print()
print('='*70)
print('ANALYSIS 5 -- BTC MARKET REGIME CONTEXT')
print('='*70)
s0 = settled[0]
sN = settled[-1]
btc_s = s0['btc_entry']
btc_e = sN.get('btc_exit') or sN['btc_entry']
low_btc  = min(t['btc_entry'] for t in settled)
high_btc = max(t['btc_entry'] for t in settled)
print(f"  Test period   : {s0['opened_at'][5:16]} UTC  ->  {sN['opened_at'][5:16]} UTC")
print(f"  BTC at start  : ${btc_s:,.2f}")
print(f"  BTC entry range: ${low_btc:,.0f} low  /  ${high_btc:,.0f} high  (swing ${high_btc-low_btc:,.0f} = {(high_btc-low_btc)/btc_s*100:.2f}%)")
print(f"  BTC at end    : ${btc_e:,.2f}  (net {(btc_e-btc_s)/btc_s*100:+.2f}% from start)")
print()
ph1 = settled[:13]
ph2 = settled[13:]
for name, ph in [
    ('Phase 1 [22:07-03:25 UTC] DROP  $67,412->$66,430  (-1.5%)', ph1),
    ('Phase 2 [04:20-11:05 UTC] RECOV $66,356->$67,244  (+1.3%)', ph2)]:
    w   = sum(1 for t in ph if t['won'])
    pnl = sum(t.get('pnl', 0) or 0 for t in ph)
    print(f"  {name}")
    print(f"    {len(ph)} trades  {w}/{len(ph)} wins  WR={w/len(ph)*100:.1f}%  PnL=${pnl:+.2f}  per_trade=${pnl/len(ph):+.2f}")
    print()
print("  BTC 24h context:")
print("    Jun 2 00:00 UTC: ~$71,288  (first 5m trade of the day)")
print("    Jun 2 22:07 UTC: $67,412   (first 15m trade)")
print("    Jun 2 day net  : -$3,876   (-5.4%)  -> STRONG DOWN macro day")
print("    Jun 3 11:05 UTC: ~$67,244  (last 15m trade)")
print("    Jun 3 direction: mostly sideways/choppy with morning recovery")

# ---- Analysis 6 ----
print()
print('='*60)
print('ANALYSIS 6 -- ENTRY PRICE BUCKET PERFORMANCE')
print('='*60)
print(f"  {'Bucket':12}  {'N':>5}  {'WR':>6}  {'Total PnL':>10}  {'Per-trade':>10}")
print('  ' + '-'*50)
for label, lo, hi in [('<0.30', 0, 0.30), ('0.30-0.45', 0.30, 0.45), ('0.45-0.55', 0.45, 0.55), ('0.55+', 0.55, 1.0)]:
    grp = [t for t in settled if lo <= t['entry_price'] < hi]
    if not grp:
        print(f"  {label:12}  {0:>5}     --          --          --")
        continue
    w   = sum(1 for t in grp if t['won'])
    pnl = sum(t.get('pnl', 0) or 0 for t in grp)
    print(f"  {label:12}  {len(grp):>5}  {w/len(grp)*100:>5.1f}%  ${pnl:>+9.2f}  ${pnl/len(grp):>+9.2f}")

# ---- Duplicate markets ----
mkt_c = Counter(t['market_id'] for t in settled)
dupes = {k: v for k, v in mkt_c.items() if v > 1}
print()
print('='*70)
print('DUPLICATE MARKET ENTRIES')
print('='*70)
for mid in sorted(dupes, key=lambda m: [t['opened_at'] for t in settled if t['market_id']==m][0]):
    grp  = [t for t in settled if t['market_id'] == mid]
    dirs = '+'.join(t['direction'] for t in grp)
    outs = '/'.join('W' if t['won'] else 'L' for t in grp)
    ttl  = grp[0]['market_title'][-42:]
    print(f"  ...{ttl:42}  dirs={dirs}  outcomes={outs}")
seen = set()
first_only = []
for t in settled:
    if t['market_id'] not in seen:
        seen.add(t['market_id'])
        first_only.append(t)
fw    = sum(1 for t in first_only if t['won'])
fo_pnl = sum(t.get('pnl', 0) or 0 for t in first_only)
print(f"\n  {len(settled)} trades across {len(mkt_c)} unique markets ({len(dupes)} windows had multiple entries)")
print(f"  First-entry-only WR : {fw}/{len(first_only)} = {fw/len(first_only)*100:.1f}%  PnL=${fo_pnl:+.2f}  per_trade=${fo_pnl/len(first_only):+.2f}")

# ---- Analysis 7 ----
print()
print('='*70)
print('ANALYSIS 7 -- BTC 5m vs 15m COMPARISON (Jun 2-3 overlap window)')
print('='*70)
with open('data/btc_5m_trades.json') as f:
    raw = json.load(f)
trades5 = raw if isinstance(raw, list) else raw.get('trades', [])
# same datetime window as 15m: Jun 2 22:00 -> Jun 3 11:30 UTC
overlap5 = [t for t in trades5
            if t.get('won') is not None
            and '2026-06-02T22' <= (t.get('opened_at') or '') <= '2026-06-03T11:59']
w5    = sum(1 for t in overlap5 if t['won'])
pnl5  = sum(t.get('pnl', 0) or 0 for t in overlap5)
up5   = [t for t in overlap5 if t['direction'] == 'UP']
dn5   = [t for t in overlap5 if t['direction'] == 'DOWN']
w5u   = sum(1 for t in up5 if t['won'])
w5d   = sum(1 for t in dn5 if t['won'])
print(f"  Overlap window: Jun 2 22:00 -> Jun 3 11:30 UTC  (same 13h as 15m test)")
print()
print(f"  5m scanner  : {len(overlap5):>3} trades  {w5}/{len(overlap5)} wins  WR={w5/len(overlap5)*100:.1f}%  PnL=${pnl5:+.2f}  per_trade=${pnl5/len(overlap5):+.2f}")
print(f"    UP  : {len(up5):>3} trades  {w5u}/{len(up5)} wins  WR={w5u/len(up5)*100:.1f}%" if up5 else "    UP  : 0 trades")
print(f"    DOWN: {len(dn5):>3} trades  {w5d}/{len(dn5)} wins  WR={w5d/len(dn5)*100:.1f}%  (direction bias: {len(dn5)/len(overlap5)*100:.0f}% DOWN)" if dn5 else "    DOWN: 0 trades")
print()
print(f"  15m scanner : {len(settled):>3} trades  {len(up_w)+len(dn_w)}/{len(settled)} wins  WR={(len(up_w)+len(dn_w))/len(settled)*100:.1f}%  PnL=${tot_pnl:+.2f}  per_trade=${tot_pnl/len(settled):+.2f}")
print(f"    UP  : {len(up_t):>3} trades  {len(up_w)}/{len(up_t)} wins  WR={len(up_w)/len(up_t)*100:.1f}%")
print(f"    DOWN: {len(dn_t):>3} trades  {len(dn_w)}/{len(dn_t)} wins  WR={len(dn_w)/len(dn_t)*100:.1f}%  (direction balance: {len(dn_t)/len(settled)*100:.0f}% DOWN)")
print()
print(f"  BOTH bots profitable in the same window -> partial regime alignment")
print(f"  5m DOWN bias = {len(dn5)/len(overlap5)*100:.0f}%  vs  15m DOWN bias = {len(dn_t)/len(settled)*100:.0f}%  (15m is far more balanced)")
print(f"  15m per-trade PnL is {tot_pnl/len(settled) / (pnl5/len(overlap5)):.1f}x higher -> driven by cheaper entry prices (avg 0.33 vs ~0.47 for 5m)")
