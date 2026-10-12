"""12-month stacking by account size (one household, max 5 funded), Aggressive/Standard and Normal/Safe."""
from multiprocessing import Pool
import stacking as S
if __name__ == "__main__":
    jobs = [(p, f, y0, y1, 250, sz) for sz in ("25K", "50K", "100K", "150K") for (p, f) in (("aggr", "std"), ("norm", "safe"), ("norm", "std")) for (y0, y1) in ((2008, 2025), (2020, 2025))]
    with Pool(7) as pool: R = pool.map(S.job, jobs)
    for a, o in R:
        print(f"{a[5]:5} {a[0]}/{a[1]:4} {a[2]}-{a[3]} | fees ${o['fees']:5.0f} payouts ${o['paid']:6.0f} | NET avg ${o['net']:6.0f} (${o['net']/12:5.0f}/mo) med ${o['med']:6.0f} | ahead {o['pos']*100:3.0f}% | p10 ${o['p10']:6.0f} p90 ${o['p90']:6.0f}")
