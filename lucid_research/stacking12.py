"""12-month version of stacking.py (50K, all presets, Safe/Standard, long-run / good markets)."""
from multiprocessing import Pool
import stacking as S
if __name__ == "__main__":
    jobs = [(p, f, y0, y1, 250) for p in ("cons", "norm", "aggr") for f in ("safe", "std") for (y0, y1) in ((2008, 2025), (2020, 2025))]
    with Pool(7) as pool: R = pool.map(S.job, jobs)
    for (y0, y1) in ((2008, 2025), (2020, 2025)):
        print(f"\n=== 50K stacking, 12 months, starts {y0}-{y1} ===")
        for a, o in R:
            if (a[2], a[3]) != (y0, y1): continue
            print(f"{a[0]:5} {a[1]:5} | evals {o['evals']:4.1f} fees ${o['fees']:5.0f} payouts ${o['paid']:6.0f} | NET avg ${o['net']:6.0f} med ${o['med']:6.0f} (${o['net']/12:4.0f}/mo) | ahead {o['pos']*100:3.0f}% | p10 ${o['p10']:6.0f} p25 ${o['p25']:5.0f} p75 ${o['p75']:5.0f} p90 ${o['p90']:6.0f} | funded {o['nf']:.1f} ({o['zero']*100:.0f}% none)")
