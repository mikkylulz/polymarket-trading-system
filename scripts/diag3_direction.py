import json
from collections import Counter

btc = json.load(open('data/btc_5m_trades.json'))['trades']
settled = [t for t in btc if t.get('won') is not None]
last200 = settled[-200:]

print(f'Analysing {len(last200)} settled BTC trades')
print(f'Period: {last200[0]["opened_at"][:16]} -> {last200[-1]["opened_at"][:16]}')
print()

def naive_ev(price):
    return 0.5 / price - 1 if price > 0 else 0

rows = []
for t in last200:
    d  = t['direction']
    ep = t['entry_price']
    no_price  = ep          if d == 'DOWN' else round(1 - ep, 4)
    yes_price = round(1-ep, 4) if d == 'DOWN' else ep

    ev_yes = naive_ev(yes_price)
    ev_no  = naive_ev(no_price)
    naive_best       = 'DOWN' if ev_no > ev_yes else 'UP'
    cheaper_side     = 'DOWN' if no_price < yes_price else 'UP'
    bot_follows_naive = (d == naive_best)
    bot_is_contrarian = (d == cheaper_side)

    rows.append({**t,
        'yes_price': yes_price, 'no_price': no_price,
        'ev_yes': round(ev_yes,4), 'ev_no': round(ev_no,4),
        'naive_best': naive_best, 'cheaper_side': cheaper_side,
        'bot_follows_naive': bot_follows_naive,
        'bot_is_contrarian': bot_is_contrarian,
    })

up_rows   = [r for r in rows if r['direction'] == 'UP']
down_rows = [r for r in rows if r['direction'] == 'DOWN']
n = len(rows)

print('=== DIRECTION BREAKDOWN ===')
print(f'UP:   {len(up_rows):3d} ({len(up_rows)/n*100:.1f}%)')
print(f'DOWN: {len(down_rows):3d} ({len(down_rows)/n*100:.1f}%)')
print()

# Naive EV alignment
follows = sum(1 for r in rows if r['bot_follows_naive'])
print('=== NAIVE EV ALIGNMENT ===')
print(f'Bot direction = naive best EV side: {follows}/{n} ({follows/n*100:.1f}%)')
print(f'  DOWN trades matching naive: {sum(1 for r in down_rows if r["bot_follows_naive"])}/{len(down_rows)}')
print(f'  UP   trades matching naive: {sum(1 for r in up_rows   if r["bot_follows_naive"])}/{len(up_rows)}')
print()

# Contrarian check
contrarian = sum(1 for r in rows if r['bot_is_contrarian'])
print('=== CONTRARIAN CHECK ===')
print(f'Bot = cheaper Polymarket side: {contrarian}/{n} ({contrarian/n*100:.1f}%)')
avg_yes_dn = sum(r['yes_price'] for r in down_rows)/len(down_rows)
avg_no_dn  = sum(r['no_price']  for r in down_rows)/len(down_rows)
avg_yes_up = sum(r['yes_price'] for r in up_rows)/len(up_rows)
avg_no_up  = sum(r['no_price']  for r in up_rows)/len(up_rows)
print(f'  DOWN trades: avg NO={avg_no_dn:.4f}  avg YES={avg_yes_dn:.4f}')
print(f'  UP   trades: avg YES={avg_yes_up:.4f}  avg NO={avg_no_up:.4f}')
print()

# Market context for DOWN trades
down_mkt_up = sum(1 for r in down_rows if r['yes_price'] > 0.50)
down_mkt_eq = sum(1 for r in down_rows if r['yes_price'] == 0.50)
down_mkt_dn = sum(1 for r in down_rows if r['yes_price'] < 0.50)
print(f'=== MARKET CONTEXT — DOWN TRADES ({len(down_rows)}) ===')
print(f'  YES>0.50 (mkt favours UP,   bot is CONTRARIAN): {down_mkt_up:3d} ({down_mkt_up/len(down_rows)*100:.1f}%)')
print(f'  YES=0.50 (neutral):                              {down_mkt_eq:3d} ({down_mkt_eq/len(down_rows)*100:.1f}%)')
print(f'  YES<0.50 (mkt favours DOWN, bot FOLLOWS mkt):   {down_mkt_dn:3d} ({down_mkt_dn/len(down_rows)*100:.1f}%)')
print()

up_mkt_dn = sum(1 for r in up_rows if r['no_price'] > 0.50)
up_mkt_up = sum(1 for r in up_rows if r['no_price'] < 0.50)
print(f'=== MARKET CONTEXT — UP TRADES ({len(up_rows)}) ===')
print(f'  NO>0.50 (mkt favours DOWN, bot is CONTRARIAN): {up_mkt_dn:3d} ({up_mkt_dn/len(up_rows)*100:.1f}%)')
print(f'  NO<0.50 (mkt favours UP,   bot FOLLOWS mkt):   {up_mkt_up:3d} ({up_mkt_up/len(up_rows)*100:.1f}%)')
print()

# Pearson correlations
def pearson(x, y):
    m = len(x)
    mx, my = sum(x)/m, sum(y)/m
    cov = sum((x[i]-mx)*(y[i]-my) for i in range(m))/m
    sdx = (sum((v-mx)**2 for v in x)/m)**0.5
    sdy = (sum((v-my)**2 for v in y)/m)**0.5
    return cov/(sdx*sdy) if sdx*sdy else 0

dir_bin    = [1 if r['direction']=='DOWN' else 0 for r in rows]
no_prices  = [r['no_price']  for r in rows]
yes_prices = [r['yes_price'] for r in rows]
confs      = [r.get('confidence', 0.5) for r in rows]

r_no  = pearson(no_prices,  dir_bin)
r_yes = pearson(yes_prices, dir_bin)
r_cf  = pearson(confs,      dir_bin)

print('=== CORRELATION: POLYMARKET PRICE vs DIRECTION CHOSEN ===')
print(f'r(NO price  -> DOWN chosen): {r_no:+.4f}')
print(f'r(YES price -> DOWN chosen): {r_yes:+.4f}')
print(f'  If |r_no| > 0.5: Polymarket price drives direction selection')
print(f'  If |r_no| < 0.1: modules drive direction, not market price')
print()
print(f'r(confidence -> DOWN chosen): {r_cf:+.4f}')
print(f'  (confidence = prob going UP; strong negative = modules drive DOWN)')
print()

# Entry price distributions
print('=== ENTRY PRICE DISTRIBUTION ===')
down_ent = Counter(round(r['entry_price'],3) for r in down_rows)
up_ent   = Counter(round(r['entry_price'],3) for r in up_rows)
print('DOWN (NO price) top entries:')
for p, c in sorted(down_ent.items(), key=lambda x: -x[1])[:8]:
    print(f'  NO={p:.3f}  YES~{1-p:.3f}  n={c:3d}  {"#"*min(c,30)}')
print()
print('UP (YES price) top entries:')
for p, c in sorted(up_ent.items(), key=lambda x: -x[1])[:8]:
    print(f'  YES={p:.3f}  NO~{1-p:.3f}  n={c:3d}  {"#"*min(c,30)}')
print()

# Module alignment
print('=== MODULE ALIGNMENT WITH DIRECTION ===')
down_mod_dn = sum(1 for r in down_rows if r.get('confidence', 0.5) < 0.50)
down_mod_up = sum(1 for r in down_rows if r.get('confidence', 0.5) >= 0.50)
up_mod_up   = sum(1 for r in up_rows   if r.get('confidence', 0.5) >= 0.50)
up_mod_dn   = sum(1 for r in up_rows   if r.get('confidence', 0.5) < 0.50)
print(f'DOWN trades: modules lean DOWN (conf<0.50):  {down_mod_dn}/{len(down_rows)} ({down_mod_dn/len(down_rows)*100:.1f}%)')
print(f'DOWN trades: modules lean UP   (conf>=0.50): {down_mod_up}/{len(down_rows)} ({down_mod_up/len(down_rows)*100:.1f}%)')
print(f'UP   trades: modules lean UP   (conf>=0.50): {up_mod_up}/{len(up_rows)} ({up_mod_up/len(up_rows)*100:.1f}%)')
print(f'UP   trades: modules lean DOWN (conf<0.50):  {up_mod_dn}/{len(up_rows)} ({up_mod_dn/len(up_rows)*100:.1f}%)')
print()

print('=== AVERAGE MODULE VALUES BY DIRECTION ===')
for direction, grp in [('DOWN', down_rows), ('UP', up_rows)]:
    print(f'{direction} ({len(grp)} trades):')
    for mod in ['p_trend', 'p_level', 'p_momentum', 'p_funding', 'p_oi', 'confidence', 'spread']:
        vals = [r.get(mod, 0) for r in grp]
        avg  = sum(vals)/len(vals)
        print(f'  {mod:12s}: {avg:.4f}')
    print()

# NO price bucket → direction
print('=== NO PRICE AT SCAN -> DIRECTION TAKEN ===')
buckets = [(0.0,0.30,'0-30c'),(0.30,0.40,'30-40c'),
           (0.40,0.45,'40-45c'),(0.45,0.495,'45-49c'),
           (0.495,0.500,'~49.5c'),(0.500,1.0,'>50c')]
for lo, hi, label in buckets:
    inb = [r for r in rows if lo <= r['no_price'] < hi]
    if not inb: continue
    nd = sum(1 for r in inb if r['direction']=='DOWN')
    nu = sum(1 for r in inb if r['direction']=='UP')
    wr_all = sum(1 for r in inb if r['won'])/len(inb)*100
    print(f'  NO={label:8s}: n={len(inb):3d}  DOWN={nd:3d}({nd/len(inb)*100:.0f}%)  UP={nu:3d}({nu/len(inb)*100:.0f}%)  WR={wr_all:.1f}%')

print()
# Final verdict
print('=== VERDICT ===')
pct_contrarian = contrarian/n*100
pct_follows_naive = follows/n*100
r_no_abs = abs(r_no)
print(f'Bot picks cheaper Polymarket side: {pct_contrarian:.1f}% of trades')
print(f'Bot matches naive-EV best side:    {pct_follows_naive:.1f}% of trades')
print(f'NO price->direction correlation:   r={r_no:+.4f}')
if r_no_abs > 0.5:
    print('VERDICT: Polymarket price is the PRIMARY driver of direction.')
    print('The bot is a mechanical contrarian — buying whatever is cheaper.')
elif r_no_abs > 0.2:
    print('VERDICT: Polymarket price has MODERATE influence on direction.')
    print('Both market price and modules contribute.')
else:
    print('VERDICT: Polymarket price has WEAK influence on direction.')
    print('Module signals (momentum) are the primary direction driver.')
