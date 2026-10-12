"""Try to break Spec A (NQ 10:30 momentum + 20d trend). Periods: regime 2017-2026, OOS+HO 2020-2026."""
import numpy as np, math, harness as Hn, strategies as S
g = Hn.load("NQ"); rng = np.random.default_rng(7)
def run(p=None, slip=1.0, side_override=None, **kw):
    p = dict(T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None) | (p or {})
    tr = S.tod_mom(g, **p)
    side = tr["side"] if side_override is None else side_override(tr)
    r = Hn.simulate(g, tr["d"], tr["e"], side, tr["px"], tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"], slip_mult=slip)
    return r, g.year[r["d"]]
def st(R):
    pf = R[R > 0].sum() / -R[R < 0].sum(); t = R.mean() / R.std(ddof=1) * np.sqrt(len(R))
    return f"n={len(R):4d} R={R.mean():+.3f} t={t:5.2f} PF={pf:.2f} win={np.mean(R>0):.2f}"
def per(r, y, a=2020, b=2026): m = (y >= a) & (y <= b); return r["R"][m]

base, y = run(); R = per(base, y)
print("BASE 2020-26      ", st(R)); print("BASE 2017-26      ", st(per(base, y, 2017)))
print("\n1) parameter perturbation (2020-26) — plateau test")
for lab, p in [("T=10:15", dict(T=45)), ("T=10:45", dict(T=75)), ("T=11:00", dict(T=90)), ("stop 0.2ATR", dict(s=0.2)),
               ("stop 0.4ATR", dict(s=0.4)), ("stop 0.6ATR", dict(s=0.6)), ("trend 10d", dict(tf=10)), ("trend 50d", dict(tf=50)),
               ("no trend filter", dict(tf=None)), ("min move .15ATR", dict(k=0.15)), ("ref=18:00 open", dict(ref="globex")),
               ("trail 2R/1R", dict(trail=(2.0, 1.0)))]:
    r, yy = run(p); print(f"   {lab:16}", st(per(r, yy)))
print("\n2) execution delay: enter 1, 5, 15 bars late (same signal)")
for lag in (1, 5, 15):
    tr = S.tod_mom(g, T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None)
    e = tr["e"] + lag
    r = Hn.simulate(g, tr["d"], e, tr["side"], g.O[tr["d"], e], tr["stop"], None, tr["xm"])
    print(f"   +{lag:2d} min", st(per(r, g.year[r["d"]])))
print("\n3) cost stress (2020-26): slippage ticks per side")
for sm in (1, 2, 3, 4, 6, 8):
    r, yy = run(slip=sm); print(f"   {sm} tick", st(per(r, yy)))
print("\n4) long vs short")
for sd in (1, -1):
    m = (base["side"] == sd) & (y >= 2020); print("   long " if sd > 0 else "   short", st(base["R"][m]))
print("\n5) placebo: same days/times/stops, RANDOM direction (1000 runs, 2020-26)")
tr = S.tod_mom(g, T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None)
long_r = Hn.simulate(g, tr["d"], tr["e"], np.ones(len(tr["d"])), tr["px"], tr["stop"], None, tr["xm"])["R"]
short_r = Hn.simulate(g, tr["d"], tr["e"], -np.ones(len(tr["d"])), tr["px"], tr["stop"], None, tr["xm"])["R"]
m = g.year[tr["d"]] >= 2020
sims = np.array([np.where(rng.random(m.sum()) < 0.5, long_r[m], short_r[m]).mean() for _ in range(1000)])
print(f"   placebo mean R {sims.mean():+.3f}, 95th pct {np.percentile(sims,95):+.3f}, 99th {np.percentile(sims,99):+.3f}; real {R.mean():+.3f}; p={np.mean(sims>=R.mean()):.3f}")
print("\n6) trade-order Monte Carlo (10,000 reshuffles of 2020-26 trades, in R)")
eq = np.array([np.cumsum(rng.permutation(R)) for _ in range(10000)])
dd = (np.maximum.accumulate(eq, 1) - eq).max(1)
ls = []
for _ in range(2000):
    x = rng.permutation(R) < 0; best = cur = 0
    for v in x:
        cur = cur + 1 if v else 0; best = max(best, cur)
    ls.append(best)
print(f"   max drawdown (R): median {np.median(dd):.1f}, 95th {np.percentile(dd,95):.1f}, 99th {np.percentile(dd,99):.1f}")
print(f"   longest losing streak: median {np.median(ls):.0f}, 95th {np.percentile(ls,95):.0f}")
bs = np.array([rng.choice(R, len(R)).mean() for _ in range(10000)])
pfs = []
for _ in range(3000):
    x = rng.choice(R, len(R)); pfs.append(x[x > 0].sum() / -x[x < 0].sum())
print(f"   bootstrap mean R 95% CI [{np.percentile(bs,2.5):+.3f}, {np.percentile(bs,97.5):+.3f}], P(mean<=0)={np.mean(bs<=0):.3f}; PF 90% CI [{np.percentile(pfs,5):.2f}, {np.percentile(pfs,95):.2f}]")
print("\n7) Deflated Sharpe (Bailey & Lopez de Prado), N=26,883 trials")
from statistics import NormalDist
N = 26883; sr = R.mean() / R.std(ddof=1); n = len(R)
sk = ((R - R.mean())**3).mean() / R.std()**3; ku = ((R - R.mean())**4).mean() / R.std()**4
emc = 0.5772156649; Z = NormalDist().inv_cdf
# trial Sharpe dispersion: use observed per-trade Sharpe of all valid trials in 2020-23
import json
srs = [r["OOS_mean"] / max(1e-9, abs(r["OOS_mean"]) / max(abs(r["OOS_t"]), 1e-9) * math.sqrt(max(r["OOS_n"], 1))) for r in map(json.loads, open("results/trials_run.jsonl")) if r.get("OOS_n", 0) > 30 and r.get("OOS_t")]
vs = np.var([s for s in srs if np.isfinite(s)])
sr0 = math.sqrt(vs) * ((1 - emc) * Z(1 - 1 / N) + emc * Z(1 - 1 / (N * math.e)))
dsr = NormalDist().cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(1 - sk * sr + (ku - 1) / 4 * sr * sr))
print(f"   per-trade SR {sr:.3f}, expected max SR of {N} noise trials {sr0:.3f}, DSR = {dsr:.3f} (2020-26 sample)")
hm = y >= 2024; Rh = base["R"][hm]; srh = Rh.mean() / Rh.std(ddof=1)
print(f"   holdout-only (selection-free) per-trade SR {srh:.3f}, P(SR>0) = {NormalDist().cdf(srh*math.sqrt(len(Rh)-1)):.3f}")
print("\n8) rolling 12-month mean R (last 8 quarters)")
d = g.dates[base["d"]]
for q in range(8, 0, -1):
    end = np.datetime64("2026-10-10") - np.timedelta64(91 * (q - 1), "D"); st_ = end - np.timedelta64(365, "D")
    w = (d >= st_) & (d < end); print(f"   12m to {end}: R={base['R'][w].mean():+.3f} (n={w.sum()})")
