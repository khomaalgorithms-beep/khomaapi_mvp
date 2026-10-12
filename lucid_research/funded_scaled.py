"""Funded lock-then-skim with risk scaled to each account's MLL (fixes the 1-2 MNQ cap for 100K/150K)."""
import json
from multiprocessing import Pool
import funded_hybrid as FH
from lucid_sizes import SIZES
FR = (0.08, 0.12, 0.16, 0.2)
if __name__ == "__main__":
    jobs = [(s, round(f * SIZES[s]["mll"]), None, 300, 1000, p) for s in SIZES for f in FR for p in ("design", "check", "recent")]
    with Pool(6) as pool:
        rows = pool.map(FH.job, jobs)
    json.dump(rows, open("results/funded_scaled.json", "w"), default=float)
    for s in SIZES:
        print(s, flush=True)
        for f in FR:
            r = {x["period"]: x for x in rows if x["size"] == s and x["risk"] == round(f * SIZES[s]["mll"])}
            print(f"  risk {f*100:.0f}% (${f*SIZES[s]['mll']:.0f}) | " + " | ".join(
                f"{p}: 1st {r[p]['med']:3.0f}d <=60d {r[p]['within60']*100:3.0f}% >=1 {r[p]['p1']*100:3.0f}% bb {r[p]['bb']*100:3.0f}% ${r[p]['cash']:5.0f} bl {r[p]['blow']*100:3.0f}%"
                for p in ("design", "check", "recent")), flush=True)
