"""'Coin-flip' fast-pass evaluation: LucidFlex 50K (target $3,000, MLL $2,000 EOD trailing, 50% consistency, 40 micros),
one 10:30 trade/day sized to risk R, take-profit k x R (1:1, 1.5:1, 2:1), real costs. Same settings with RANDOM
direction = pure-luck baseline. Theory: zero-edge 1:1 random walk to +3000 before -2000 (static floor) = 40%."""
import numpy as np, sys
from multiprocessing import Pool
import stress as T, dashboard_data as DDm
from lucid_sizes import SIZES
A = SIZES["50K"]
WIN = {"2008-26": ("2008-03-01", "2026-10-09"), "2020-26": ("2020-01-01", "2026-10-09"), "last 12m": ("2025-10-12", "2026-10-09")}
CH = [("tod_mom", dict(T=60, k=0.0, s=0.2, ref="open", tf=20, trail=None), 1.0)]

def job(a):
    risk, k, rand = a
    lg = T.legs(dict(chain=CH, comm=1.0, slip=1, **({"direction": "random", "seed": 11} if rand else {}))); g = T.G
    out = {}
    for w, (x, y) in WIN.items():
        st = np.where((g.days >= np.datetime64(x).astype(np.int64)) & (g.days <= np.datetime64(y).astype(np.int64)))[0]
        res = [DDm.eval_run([lg.get(i) for i in range(s, min(s + 180, g.D))], A, risk, "eod", cap_frac=k * risk / A["target"]) for s in st]
        p = np.array([r[0] for r in res]); d = np.array([r[1] for r in res])
        # trading days actually traded until pass (calendar sessions used)
        out[w] = (p.mean(), float(np.median(d[p])) if p.any() else None, float(np.median(d[~p])) if (~p).any() else None)
    return a, out

if __name__ == "__main__":
    jobs = [(r, k, rd) for r in (750, 1000, 1250, 1500, 1900) for k in (1.0, 1.5, 2.0) for rd in (False, True)]
    with Pool(7) as pool: R = dict(pool.map(job, jobs))
    print(f"{'risk':>6} {'TP':>5} | " + " | ".join(f"{w:^34}" for w in WIN))
    print(f"{'':12} | " + " | ".join(f"{'pass  luck  edge  pass-d fail-d':^34}" for w in WIN))
    for r in (750, 1000, 1250, 1500, 1900):
        for k in (1.0, 1.5, 2.0):
            a, b = R[(r, k, False)], R[(r, k, True)]
            print(f"${r:5} {k:4.1f}R | " + " | ".join(f"{a[w][0]*100:4.0f}% {b[w][0]*100:4.0f}% {(a[w][0]-b[w][0])*100:+4.0f}  {a[w][1] or 0:5.0f}d {a[w][2] or 0:5.0f}d " for w in WIN))
