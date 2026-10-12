"""Stage 4 hard tests for the chosen NQ system (pre-registered criteria in ORB_PREREGISTRATION.md)."""
import json, numpy as np
from multiprocessing import Pool
import orb_lab as OL
SYS = dict(n=5, entry="touch", cutoff=60, filt=("pdl",), stop="atr", stop_k=0.2, tgt_R=None, be=1.0)
def st(cfg, per="SEL", **o): return OL.stats(OL.run("NQ", **{**cfg, **o}), per)
def rnd(seed): return st(SYS, rand=seed)["tot"]
NEIGH = [dict(n=3), dict(n=10), dict(n=15), dict(entry="close"), dict(cutoff=30), dict(cutoff=120),
         dict(filt=("pdl", "onl")), dict(filt=("pdl", "trend20")), dict(filt=("pdl", "gap")), dict(filt=()),
         dict(stop_k=0.15), dict(stop_k=0.25), dict(stop_k=0.3), dict(stop="opp", stop_k=None),
         dict(be=None), dict(be=0.75), dict(be=1.5), dict(tgt_R=3.0), dict(tgt_R=5.0)]
if __name__ == "__main__":
    full = OL.run("NQ", **SYS)
    for per in ("SEL", "BACK", "HO"):
        s = OL.stats(full, per)
        print(f"{per:5} n {s['n']:4} ({s['tpy']:.0f}/yr) | R {s['mean']:+.3f} t {s['t']:+.1f} | PF {s['pf']:.2f} | win {s['win']*100:.0f}% | total {s['tot']:+.1f}R maxDD {s['dd']:.1f}R")
    s = OL.stats(full, ("2008-03-01", "2026-10-31")); print(f"ALL   n {s['n']} PF {s['pf']:.2f} t {s['t']:+.1f} total {s['tot']:+.1f}R maxDD {s['dd']:.1f}R ret/DD {s['rd']:.2f}")
    yrs = {}
    for d, r in zip(full["day"], full["R"]): y = str(np.datetime64(int(d), "D"))[:4]; yrs[y] = yrs.get(y, 0) + r
    print("yearly R: " + " ".join(f"{y}:{v:+.0f}" for y, v in yrs.items()), "| losing years", sum(v < 0 for v in yrs.values()), "/", len(yrs))
    print("\nNeighbourhood (one step away; SELECT PF / BACK PF / HO PF):")
    out = []
    for o in NEIGH:
        cfg = {**SYS, **o}; res = OL.run("NQ", **cfg)
        a, b, c = OL.stats(res, "SEL"), OL.stats(res, "BACK"), OL.stats(res, "HO")
        out.append(a["pf"]); print(f"   {json.dumps(o):38} SEL PF {a['pf']:.2f} t {a['t']:+.1f} ({a['tpy']:.0f}/yr) | BACK {b['pf']:.2f} | HO {c['pf']:.2f}")
    print(f"   -> {np.mean(np.array(out) > 1)*100:.0f}% of neighbours profitable in SELECT")
    with Pool(8) as pool: R = pool.map(rnd, range(200))
    real = OL.stats(full, "SEL")["tot"]; z = (real - np.mean(R)) / np.std(R)
    print(f"\nRandom-direction baseline (200 runs, same days/stops/exits): mean {np.mean(R):+.1f}R sd {np.std(R):.1f} | real {real:+.1f}R | z = {z:+.1f} | beaten by {np.mean(np.array(R) >= real)*100:.1f}% of random runs")
    for k in (2.0, 3.0):
        s = OL.stats(OL.run("NQ", slip=k, **SYS), ("2016-01-01", "2026-10-31"))
        print(f"Slippage x{k:.0f} (2016-26): PF {s['pf']:.2f} R {s['mean']:+.3f} t {s['t']:+.1f}")
    N = 192 + 8 * 3 * 13 + 8 * 90
    print(f"\nConfigurations tried in this study: ~{N}. Expected best |t| of {N} pure-noise tests ~ {np.sqrt(2*np.log(N)):.2f} (if independent; they are highly correlated, so the true bar is lower).")
