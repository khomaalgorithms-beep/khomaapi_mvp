import numpy as np, json, sys
sys.path.insert(0, "..")
import adaptive_lucid as AL, stress as T
from app.orb_validated import DATA
lg, _ = AL.build(); g = T.G
start = np.datetime64("2016-10-10").astype(np.int64)
d = [x for x in sorted(lg) if g.days[x] >= start]
R = np.array([lg[x]["net"] / lg[x]["rct"] for x in d])
yrs = g.year[d]
wins, losses = R[R > 0], R[R <= 0]
print(f"trades {len(R)} over 10y = {len(R)/10:.0f}/yr; trading days ~{len(R)/2510*100:.0f}% of sessions")
print(f"win {np.mean(R>0)*100:.0f}% | avg win {wins.mean():.2f}R | avg loss {losses.mean():.2f}R | PF {wins.sum()/-losses.sum():.2f}")
print(f"share of trades that are full stop-outs (<=-0.9R): {np.mean(R<=-0.9)*100:.0f}% | winners > +2R: {np.mean(R>2)*100:.0f}%")
# losing streaks
def streaks(x):
    out=[];c=0
    for v in x:
        if v<=0: c+=1
        else:
            if c: out.append(c)
            c=0
    if c: out.append(c)
    return np.array(out)
s = streaks(R)
print(f"longest losing streak 10y: {s.max()} | streaks >=6: {np.sum(s>=6)} ({np.sum(s>=6)/10:.1f}/yr) | >=8: {np.sum(s>=8)} | >=10: {np.sum(s>=10)}")
for y in range(2017, 2027):
    print(f"  {y}: longest losing streak {streaks(R[yrs==y]).max()}", end="")
print()
eq = np.cumsum(R); dd = eq - np.maximum.accumulate(np.r_[0, eq])[1:]
print(f"worst drawdown {dd.min():.1f}R")
# time under water
uw = 0; mx = 0
for v in dd:
    uw = uw + 1 if v < 0 else 0; mx = max(mx, uw)
print(f"longest stretch without a new equity high: {mx} trades (~{mx/123*12:.0f} months)")
# monthly
months = [str(g.dates[x])[:7] for x in d]
ms = {}
for m, r in zip(months, R): ms[m] = ms.get(m, 0) + r
mv = np.array(list(ms.values()))
print(f"months: {len(mv)}, losing months {np.mean(mv<0)*100:.0f}%, worst month {mv.min():.1f}R, best {mv.max():.1f}R")
# 2025-07 onward
rec = np.array([g.days[x] >= np.datetime64('2025-07-01').astype(np.int64) for x in d])
x = R[rec]; print(f"since Jul-2025: {len(x)} trades, win {np.mean(x>0)*100:.0f}%, PF {x[x>0].sum()/-x[x<0].sum():.2f}, net {x.sum():.1f}R")
print(json.dumps({s: {k: DATA['presets'][s][k] for k in ('eod',)} for s in DATA['presets']}, indent=0)[:3000])
print(json.dumps(DATA['funded'], indent=0)[:2500])
