"""Funded 50K (EOD) with vs without the vol filter: first payout %, blown-before-payout %, days, year-1 cash."""
import numpy as np, harness as Hn, stress as T, dashboard_data as DDm
from lucid_sizes import SIZES
NQ = Hn.load("NQ"); T.G = NQ; g = NQ; D = g.D
lg = T.legs(dict(chain=[("tod_mom", dict(T=60, k=0.0, s=0.2, ref="open", tf=20, trail=None), 1.0)], comm=1.0, slip=1))
v = g.atr14 / g.px
V = set()
for d in lg:
    if d >= 250:
        w = v[d-250:d]; w = w[np.isfinite(w)]
        if len(w) and np.isfinite(v[d]) and v[d] >= np.median(w): V.add(d)
A = SIZES["50K"]
for name, L in (("base", lg), ("vol filter", {d: lg[d] for d in V})):
    for risk in (240, 360):
        for lab, y0, y1 in (("2008-25", 2008, 2025), ("2020-25", 2020, 2025)):
            starts = np.where((g.year >= y0) & (g.year <= y1))[0][::2]
            R = [DDm.funded_run([L.get(x) for x in range(s, min(s + 250, D))], A, risk, "eod") for s in starts]
            paid = np.mean([len(r[0]) > 0 for r in R]); bb = np.mean([r[1] and r[2] is None for r in R])
            fs = [r[2] for r in R if r[2]]; cash = np.mean([0.9 * sum(r[0]) for r in R])
            print(f"{name:11} risk ${risk} {lab}: payout {paid*100:3.0f}% | blown before payout {bb*100:3.0f}% | 1st payout {np.median(fs):4.0f}d | yr-1 cash ${cash:,.0f}")
