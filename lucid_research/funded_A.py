"""Funded on Spec A: risk x daily TP x payout amount (lock-then-skim, trigger = MLL + $300)."""
import json
from multiprocessing import Pool
import funded_hybrid as FH
from lucid_sizes import SIZES
if __name__ == "__main__":
    jobs = []
    for s in SIZES:
        m = SIZES[s]["mll"]
        for f in (0.12, 0.16, 0.2):
            for tpf in (None, 0.15, 0.25):
                for p in ("design", "check", "recent"):
                    jobs.append((s, round(f * m), None if tpf is None else round(tpf * m), 300, 1000, p))
    with Pool(6) as pool:
        rows = pool.map(FH.job, jobs)
    json.dump(rows, open("results/funded_A.json", "w"), default=float)
    for s in SIZES:
        print(s)
        keys = sorted({(r["risk"], r["tp"]) for r in rows if r["size"] == s}, key=lambda k: (k[0], k[1] or 0))
        for k in keys:
            r = {x["period"]: x for x in rows if x["size"] == s and (x["risk"], x["tp"]) == k}
            print(f"  risk ${k[0]:4d} TP {str(k[1]):>5} | " + " | ".join(f"{p}: 1st {r[p]['med']:4.0f}d >=1 {r[p]['p1']*100:3.0f}% bb {r[p]['bb']*100:3.0f}% ${r[p]['cash']:5.0f}" for p in ("design", "check", "recent")))
