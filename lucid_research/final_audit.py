"""FINAL AUDIT: every client-facing statistic recomputed from the live rule (10:30 with-trend, stop 0.2 ATR, real costs)
over the FULL history 2008-03 .. 2026-10 and over the last 10 years, so the portal can quote the worst honest version."""
import numpy as np, harness as Hn, stress as T
NQ = Hn.load("NQ"); T.G = NQ; g = NQ; D = g.D
lg = T.legs(dict(chain=[("tod_mom", dict(T=60, k=0.0, s=0.2, ref="open", tf=20, trail=None), 1.0)], comm=1.0, slip=1))
def block(lo, hi, name):
    lo_i = np.searchsorted(g.days, np.datetime64(lo).astype(np.int64)); hi_i = np.searchsorted(g.days, np.datetime64(hi).astype(np.int64))
    ds = np.array(sorted(d for d in lg if lo_i <= d < hi_i)); r = np.array([lg[d]["net"] / lg[d]["rct"] for d in ds])
    sess = np.arange(lo_i, hi_i); valid = sess[~np.isnan(g.atr14[sess])] if hasattr(g, "atr14") else sess
    w = r > 0
    # streaks
    def longest(mask):
        best = cur = 0; runs = []
        for x in mask:
            cur = cur + 1 if x else 0; best = max(best, cur)
        return best
    def count_runs(mask, k):
        n = cur = 0
        for x in list(mask) + [False]:
            if x: cur += 1
            else:
                if cur >= k: n += 1
                cur = 0
        return n
    yrs = (hi_i - lo_i) / 252
    eq = np.cumsum(r); peak = np.maximum.accumulate(np.r_[0, eq])[1:]; dd = eq - peak
    # longest time without a new equity high, in calendar days
    hi_idx = 0; longest_uw = 0; uw_from = None; best = (0, None, None); runpk = -1e9
    for i, d in enumerate(ds):
        if eq[i] > runpk + 1e-9:
            gap = (g.dates[d] - g.dates[ds[hi_idx]]).astype(int)
            if gap > best[0]: best = (gap, str(g.dates[ds[hi_idx]]), str(g.dates[d]))
            runpk = eq[i]; hi_idx = i
    tail = (g.dates[ds[-1]] - g.dates[ds[hi_idx]]).astype(int)
    if tail > best[0]: best = (tail, str(g.dates[ds[hi_idx]]), "still below (" + str(g.dates[ds[-1]]) + ")")
    months = np.array([str(g.dates[d])[:7] for d in ds]); um = sorted(set(months))
    mr = np.array([r[months == m].sum() for m in um])
    traded = set(ds.tolist()); nt = [d for d in sess if d not in traded]
    # longest no-trade stretch (sessions)
    lnt = cur = 0
    for d in sess:
        cur = 0 if d in traded else cur + 1; lnt = max(lnt, cur)
    yr = g.year[ds]; ys = {y: r[yr == y].sum() for y in sorted(set(yr))}
    print(f"\n=== {name} ({lo} .. {hi}) ===")
    print(f"trades {len(r)} ({len(r)/yrs:.0f}/yr) | win {w.mean()*100:.1f}% | avg win {r[w].mean():.2f}R avg loss {r[~w].mean():.2f}R ratio {r[w].mean()/-r[~w].mean():.2f} | PF {r[w].sum()/-r[~w].sum():.2f} | total {r.sum():.1f}R")
    print(f"breakeven win% {100/(1+r[w].mean()/-r[~w].mean()):.1f}% | largest loss {r.min():.2f}R largest win {r.max():.2f}R")
    print(f"longest losing streak {longest(~w)} | runs of 6+ losers {count_runs(~w,6)} ({count_runs(~w,6)/yrs:.1f}/yr) | 10+ {count_runs(~w,10)} | longest winning streak {longest(w)} | 3+ wins {count_runs(w,3)/yrs:.1f}/yr")
    print(f"losing months {np.mean(mr<0)*100:.0f}% | worst month {mr.min():.1f}R | max drawdown {dd.min():.1f}R | longest without new high {best[0]} days ({best[0]/365:.1f} yrs) from {best[1]} to {best[2]}")
    print(f"no-trade sessions {len(nt)/len(sess)*100:.0f}% | longest no-trade stretch {lnt} sessions | losing years {sum(1 for v in ys.values() if v<0)}/{len(ys)}")
    print("yearly R: " + " ".join(f"{y}:{v:+.0f}" for y, v in ys.items()))
    if "drop" in name or True:
        k = int(len(r) * 0.05); print(f"without best 5% of trades: total {np.sort(r)[:-k].sum():.1f}R")
block("2008-03-01", "2026-10-10", "FULL 18.6y")
block("2016-10-10", "2026-10-10", "LAST 10y")
