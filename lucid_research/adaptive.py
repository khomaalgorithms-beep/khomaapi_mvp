"""Adaptive (self-re-tuning) versions of Spec A. Every scheme picks one of the 45 settings using ONLY past data,
then trades it until the next re-fit. Judged on ALL years 2012-26 and on the recent flat stretch (2025-07 .. 2026-10).
Acceptance (pre-declared): better than the fixed rule in the recent stretch AND not worse over 2012-26 AND
profitable in >= the same number of calendar years. Plus a diagnosis of the recent period."""
import numpy as np, json
import harness as Hn, stress as T, max_stress as MS

M = np.load("results/grid_daily_R.npy"); g = Hn.load("NQ")
dn = g.days; yr = g.year
base = MS.GRID.index(dict(s=0.3, T1=60, tf=20, counter=True))
recent = dn >= np.datetime64("2025-07-01").astype(np.int64)
last12 = dn >= np.datetime64("2025-10-10").astype(np.int64)


def st(x):
    x = x[~np.isnan(x)]
    return dict(n=len(x), R=x.mean(), t=x.mean() / x.std(ddof=1) * np.sqrt(len(x)), pf=x[x > 0].sum() / -x[x < 0].sum())


def scheme(look_days, refit, crit):
    out = np.full(g.D, np.nan)
    start = np.where(yr >= 2012)[0][0]
    i = start
    while i < g.D:
        lo = 0 if look_days is None else max(0, i - look_days)
        W = M[:, lo:i]
        mu = np.nanmean(W, 1); sd = np.nanstd(W, 1)
        score = mu if crit == "mean" else mu / sd
        k = int(np.nanargmax(score))
        j = min(g.D, i + refit)
        out[i:j] = M[k, i:j]
        i = j
    return out


rows = []
fixed = M[base]
fy = {y: np.nanmean(fixed[yr == y]) > 0 for y in range(2012, 2027)}
print(f"FIXED rule   | 2012-26 {st(fixed[yr>=2012])['pf']:.2f} | since Jul-25 PF {st(fixed[recent])['pf']:.2f} R {st(fixed[recent])['R']:+.3f} | last 12m PF {st(fixed[last12])['pf']:.2f} | green years {sum(fy.values())}/15")
for look in (252, 504, 756, None):
    for refit, rn in ((63, "quarterly"), (252, "yearly")):
        for crit in ("mean", "sharpe"):
            a = scheme(look, refit, crit)
            gy = sum(np.nanmean(a[yr == y]) > 0 for y in range(2012, 2027))
            s_all, s_rec, s_12 = st(a[yr >= 2012]), st(a[recent]), st(a[last12])
            ok = s_rec["R"] > st(fixed[recent])["R"] and s_all["R"] >= st(fixed[yr >= 2012])["R"] - 0.005 and gy >= sum(fy.values())
            rows.append((look, rn, crit, s_all, s_rec, s_12, gy, ok))
            print(f"look {str(look or 'all'):>4}d refit {rn:9} {crit:6} | 2012-26 PF {s_all['pf']:.2f} R {s_all['R']:+.3f} | since Jul-25 PF {s_rec['pf']:.2f} R {s_rec['R']:+.3f} | last 12m PF {s_12['pf']:.2f} | green yrs {gy}/15 {'ACCEPT' if ok else ''}")

# ---- diagnosis of the recent stretch (no fitting): longs vs shorts, by volatility, by month of move size
lg = T.legs(dict(comm=1.0, slip=1))
d = np.array(sorted(lg)); R = np.array([lg[x]["net"] / lg[x]["rct"] for x in d])
side = np.array([np.sign(g.trend20[x]) for x in d])
rec = g.days[d] >= np.datetime64("2025-07-01").astype(np.int64); hist = (g.year[d] >= 2020) & ~rec
print("\nDIAGNOSIS (Spec A trades)")
for nm, m in (("2020 - Jun 2025", hist), ("Jul 2025 - Oct 2026", rec)):
    for sd, lab in ((1, "longs"), (-1, "shorts")):
        x = R[m & (side == sd)]
        print(f"  {nm:20} {lab:6}: n={len(x):3d} R={x.mean():+.3f} PF={x[x>0].sum()/-x[x<0].sum():.2f} win={np.mean(x>0)*100:.0f}%")
    x = R[m]
    big = np.mean(x > 2); print(f"  {nm:20} share of trades > +2R (trend days): {big*100:.1f}%   stop-outs (<= -0.9R): {np.mean(x <= -0.9)*100:.0f}%")
mv = np.abs(g.s_close - g.s_open) / g.atr14
for nm, a, b in (("2020 - Jun 2025", np.datetime64("2020-01-01"), np.datetime64("2025-07-01")), ("Jul 2025 - Oct 2026", np.datetime64("2025-07-01"), np.datetime64("2026-10-10"))):
    m = (g.days >= a.astype(np.int64)) & (g.days < b.astype(np.int64)) & g.sess_ok
    print(f"  NQ market {nm:20}: avg |open->close| = {np.nanmean(mv[m]):.2f} x ATR ; trend days (>0.8 ATR) {np.nanmean(mv[m] > 0.8)*100:.0f}% of days")
