import json, itertools, numpy as np
from multiprocessing import Pool
import orb_lab as OL
GRID = [dict(n=n, entry=e, cutoff=c) for n in (5, 15, 30, 60) for e in ("touch", "close") for c in (60, 120, 240)]
def job(sym):
    out = []
    for p in GRID:
        res = OL.run(sym, **p)
        out.append(dict(sym=sym, **p, **{k: OL.stats(res, k) for k in OL.PER}))
    return out
if __name__ == "__main__":
    with Pool(8) as pool: R = [x for xs in pool.map(job, OL.MARKETS) for x in xs]
    json.dump(R, open("results/orb_stage1.json", "w"), default=float)
    for sym in OL.MARKETS:
        X = sorted([r for r in R if r["sym"] == sym], key=lambda r: -(r["SEL"]["mean"] if np.isfinite(r["SEL"]["mean"]) else -9))
        pos = np.mean([r["SEL"]["pf"] > 1 for r in X]) * 100
        print(f"\n{sym}: {pos:.0f}% of 24 base ORB configs profitable in SELECT (2016-23). Top 3:")
        for r in X[:3]:
            s, bk = r["SEL"], r["BACK"]
            print(f"   n={r['n']:2} {r['entry']:5} cutoff {r['cutoff']:3} | SEL {s['n']:4} tr ({s['tpy']:.0f}/yr) R {s['mean']:+.3f} t {s['t']:+.1f} PF {s['pf']:.2f} win {s['win']*100:.0f}% | BACK PF {bk['pf']:.2f} R {bk['mean']:+.3f}")
