"""Per account x preset dollar stats + streak / no-trade-day stats for the live rule (adaptive Spec A), Oct 2016-Oct 2026."""
import sys; sys.path.insert(0, "..")
import numpy as np
import adaptive_lucid as AL, stress as T, harness as Hn
from app.orb_validated import DATA
lg, _ = AL.build(); g = T.G
start = np.datetime64("2016-10-10").astype(np.int64)
alld = np.array([d for d in range(g.D) if g.days[d] >= start and g.sess_ok[d]])
tr = [d for d in alld if d in lg]
R = np.array([lg[d]["net"] / lg[d]["rct"] for d in tr])
def runs(mask):
    out = []; c = 0
    for v in mask:
        if v: c += 1
        else:
            if c: out.append(c)
            c = 0
    if c: out.append(c)
    return np.array(out) if out else np.array([0])
ws = runs(R > 0); ls = runs(R <= 0)
print(f"longest WIN streak {ws.max()} | 3+ wins in a row: {np.sum(ws>=3)/10:.1f}/yr | 4+: {np.sum(ws>=4)} in 10y")
print(f"longest LOSS streak {ls.max()} | avg loss streak {ls.mean():.1f}")
print(f"largest single win {R.max():.1f}R | largest single loss {R.min():.2f}R | losses worse than -1.1R: {np.mean(R<-1.1)*100:.1f}%")
traded = np.isin(alld, tr)
print(f"no-trade days {np.mean(~traded)*100:.0f}% | longest run of trading days without a trade {runs(~traded).max()} | runs of 5+ no-trade days per year {np.sum(runs(~traded)>=5)/10:.1f}")
# weeks with zero trades
wk = {}
for d, t in zip(alld, traded):
    k = np.datetime64(int(g.days[d]), "D").astype("datetime64[W]")
    wk[k] = wk.get(k, 0) + t
v = np.array(list(wk.values()))
print(f"weeks: {len(v)} | weeks with 0 trades {np.mean(v==0)*100:.0f}% | avg trades/week {v.mean():.1f} | max {v.max()}")
# $ per account x preset (EOD presets) with eval cap applied on MFE; funded no cap
def money(risk, cap, maxq):
    out = []
    for d in tr:
        L = lg[d]; q = int(risk // L["rct"]); q = 1 if q == 0 and L["rct"] <= 1.5 * risk else q; q = min(q, maxq)
        if q <= 0: continue
        if cap is not None and L["best"].max() * q >= cap: out.append(cap)
        else: out.append(L["net"] * q)
    x = np.array(out); return x
MAXE = {"25K": 20, "50K": 40, "100K": 60, "150K": 100}; MAXF = {"25K": 10, "50K": 20, "100K": 30, "150K": 40}; TGT = {"25K": 1250, "50K": 3000, "100K": 6000, "150K": 9000}
print("\nsize preset risk | avg loser | avg winner | biggest winner | win% (with eval cap) | 4 losses in a row")
rows = []
for s in ("25K", "50K", "100K", "150K"):
    for pr in ("cons", "norm", "aggr"):
        rk = DATA["presets"][s]["eod"][pr]["risk"]; x = money(rk, round(0.48 * TGT[s]), MAXE[s])
        w, l = x[x > 0], x[x <= 0]
        rows.append((s, pr, rk, l.mean(), w.mean(), w.max(), (x > 0).mean()))
        print(f"{s:5} {pr:5} ${rk:5} | ${l.mean():7.0f} | ${w.mean():7.0f} | ${w.max():7.0f} | {np.mean(x>0)*100:4.0f}% | ${4*l.mean():7.0f}")
    rk = DATA["funded"][s]["eod"]["risk"]; x = money(rk, None, MAXF[s]); w, l = x[x > 0], x[x <= 0]
    print(f"{s:5} FUND  ${rk:5} | ${l.mean():7.0f} | ${w.mean():7.0f} | ${w.max():7.0f} | {np.mean(x>0)*100:4.0f}% | ${4*l.mean():7.0f}")
