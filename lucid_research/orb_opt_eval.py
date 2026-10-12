"""Plateau scoring, walk-forward and PBO on the 1,800-config ORB grid (see ORB_PREREGISTRATION.md addendum)."""
import itertools, json, numpy as np
import orb_lab as OL
from orb_opt_grid import GRID, NS, ENT, FIL, STK, BE, TG
M = np.load("results/orb_opt_M.npy").astype(np.float64); g = OL.G("NQ"); yr = g.year
start = np.searchsorted(g.days, np.datetime64("2008-03-01").astype(np.int64)); M = M[start:]; yr = yr[start:]; days = g.days[start:]
C = len(GRID); pos = {c: i for i, c in enumerate(GRID)}
dims = [NS, ENT, FIL, STK, BE, TG]; ordered = [True, False, False, True, True, True]
nbrs = []
for c in GRID:
    nb = [pos[c]]
    for k, vals in enumerate(dims):
        i = vals.index(c[k])
        cand = [i - 1, i + 1] if ordered[k] else [j for j in range(len(vals)) if j != i]
        for j in cand:
            if 0 <= j < len(vals): nb.append(pos[c[:k] + (vals[j],) + c[k + 1:]])
    nbrs.append(nb)
def sharpe(X): s = X.std(0); return np.where(s > 0, X.mean(0) / np.where(s > 0, s, 1) * np.sqrt(252), 0)
def plateau(sh): return np.array([sh[nb].mean() for nb in nbrs])
FIX = pos[(5, "touch", ("pdl",), 0.20, 1.0, None)]
def desc(i): n, e, f, sk, be, tg = GRID[i]; return f"n{n} {e} {'+'.join(f)} stop{sk} BE{be} tgt{tg or 'close'}"
def perf(r):
    eq = np.cumsum(r); dd = -(eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min(); t = r[r != 0]
    return dict(sh=r.mean() / r.std() * np.sqrt(252), tot=r.sum(), dd=dd, pf=t[t > 0].sum() / -t[t < 0].sum(), n=len(t))
sel = (yr >= 2016) & (yr <= 2023)
shS = sharpe(M[sel]); plS = plateau(shS)
print(f"Fixed system: {desc(FIX)} | SELECT Sharpe {shS[FIX]:.2f}, plateau {plS[FIX]:.2f} (rank {int((plS > plS[FIX]).sum())+1}/{C})")
print(f"Peak (single best) in SELECT: {desc(int(shS.argmax()))} Sharpe {shS.max():.2f}")
best_pl = int(plS.argmax()); print(f"Plateau best in SELECT: {desc(best_pl)} plateau {plS[best_pl]:.2f} own {shS[best_pl]:.2f}")
print(f"Share of all 1,800 configs with SELECT Sharpe > 0: {np.mean(shS > 0)*100:.0f}%  (>0.5: {np.mean(shS > 0.5)*100:.0f}%)")
# ---- walk-forward ----
wf_pl, wf_pk, picks = [], [], []
for Y in range(2012, 2027):
    past = yr < Y; cur = yr == Y
    sh = sharpe(M[past]); pl = plateau(sh)
    i_pl, i_pk = int(pl.argmax()), int(sh.argmax())
    wf_pl.append(M[cur, i_pl]); wf_pk.append(M[cur, i_pk]); picks.append((Y, desc(i_pl), M[cur, i_pl].sum(), M[cur, FIX].sum()))
oos = yr >= 2012
a, b, c = perf(np.concatenate(wf_pl)), perf(np.concatenate(wf_pk)), perf(M[oos, FIX])
print("\nWALK-FORWARD 2012-2026 (choose on all prior years, trade next year blind):")
for nm, p in (("plateau-optimized", a), ("peak-optimized (overfit-prone)", b), ("fixed system", c)):
    print(f"   {nm:31} Sharpe {p['sh']:.2f} | total {p['tot']:+6.1f}R | maxDD {p['dd']:5.1f}R | PF {p['pf']:.2f} | trades {p['n']}")
print("   year: plateau pick -> its R vs fixed R")
for Y, d, r1, r0 in picks: print(f"   {Y}: {d:42} {r1:+6.1f}R vs {r0:+6.1f}R")
# ---- PBO (CSCV) ----
B = 16; edges = np.linspace(0, len(M), B + 1).astype(int)
S1 = np.array([M[edges[k]:edges[k + 1]].sum(0) for k in range(B)]); S2 = np.array([(M[edges[k]:edges[k + 1]] ** 2).sum(0) for k in range(B)])
NN = np.array([edges[k + 1] - edges[k] for k in range(B)])
logits = []
for comb in itertools.combinations(range(B), B // 2):
    i = np.zeros(B, bool); i[list(comb)] = True
    def sh(mask):
        n = NN[mask].sum(); s1 = S1[mask].sum(0); s2 = S2[mask].sum(0); m = s1 / n; v = s2 / n - m * m
        return m / np.sqrt(np.maximum(v, 1e-12))
    shi, sho = sh(i), sh(~i)
    best = int(shi.argmax()); w = (sho < sho[best]).mean() + 0.5 * (sho == sho[best]).mean()
    w = min(max(w, 1e-4), 1 - 1e-4); logits.append(np.log(w / (1 - w)))
logits = np.array(logits)
print(f"\nPBO (probability the in-sample best is below median out-of-sample), 12,870 splits: {np.mean(logits < 0)*100:.1f}%")
print(f"Median out-of-sample percentile of the in-sample best: {np.median(1/(1+np.exp(-logits)))*100:.0f}%")
json.dump(dict(best_plateau=desc(best_pl), fixed=desc(FIX)), open("results/orb_opt_summary.json", "w"))
