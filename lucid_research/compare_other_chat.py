"""Apples-to-apples vs the other chat's table: live rule (adaptive Spec A; stop 0.2 ATR since 2020), LucidFlex 50K EOD,
presets Cons $500 / Normal $600 / Aggr $800, rolling starts, 180-day window, several windows and cost levels.
Also: the fixed 0.3-ATR rule, and a random-direction luck baseline for the last 12 months."""
import sys; sys.path.insert(0, "..")
import numpy as np
from multiprocessing import Pool
import stress as T, dashboard_data as DD
from lucid_sizes import SIZES
A = SIZES["50K"]; RISK = {"Cons": 500, "Normal": 600, "Aggr": 800}
WIN = {"2008-26": ("2008-03-01", "2026-10-09"), "2020-26": ("2020-01-01", "2026-10-09"), "2024-26": ("2024-01-01", "2026-10-09"),
       "since Jul-25": ("2025-07-01", "2026-10-09"), "last 12m": ("2025-10-12", "2026-10-09")}
COST = {"Lucid+1t/side": dict(comm=1.0, slip=1), "~5t RT": dict(comm=1.0, slip=2), "~9t RT": dict(comm=1.0, slip=4)}

def job(a):
    stop, cname, rand = a
    cfg = dict(chain=[("tod_mom", dict(T=60, k=0.0, s=stop, ref="open", tf=20, trail=None), 1.0)], **COST[cname])
    if rand: cfg.update(direction="random", seed=7)
    lg = T.legs(cfg); g = T.G; out = {}
    for w, (a_, b_) in WIN.items():
        st = np.where((g.days >= np.datetime64(a_).astype(np.int64)) & (g.days <= np.datetime64(b_).astype(np.int64)))[0]
        for p, r in RISK.items():
            res = [DD.eval_run([lg.get(x) for x in range(s, min(s + 180, g.D))], A, r, "eod") for s in st]
            done = [x for s, x in zip(st, res) if x[0] or s + x[1] < g.D or True]
            pas = np.mean([x[0] for x in res])
            # unfinished = window ran past the end of data without pass/fail
            unf = np.mean([(not x[0]) and (s + 180 > g.D) and (x[1] >= g.D - s) for s, x in zip(st, res)])
            out[(w, p)] = (pas, unf)
    return stop, cname, rand, out

if __name__ == "__main__":
    jobs = [(0.2, c, False) for c in COST] + [(0.3, "~5t RT", False), (0.2, "Lucid+1t/side", True)]
    with Pool(5) as pool: R = pool.map(job, jobs)
    for stop, c, rand, out in R:
        print(f"\n== stop {stop} ATR | cost {c} | {'RANDOM DIRECTION (luck baseline)' if rand else 'real rule'} ==")
        print(f"{'window':14}" + "".join(f"{p:>16}" for p in RISK))
        for w in WIN:
            print(f"{w:14}" + "".join(f"{out[(w,p)][0]*100:7.0f}% (unf {out[(w,p)][1]*100:2.0f}%)" for p in RISK))
