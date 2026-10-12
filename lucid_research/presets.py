"""Eval presets per LucidFlex size. Pre-declared selection rule (REAL costs, rolling starts 2020-26, 180-day window):
  AGGRESSIVE   = chain, risk 50% of MLL (current fast mode)
  NORMAL       = highest pass rate with median days-to-pass <= 15
  CONSERVATIVE = highest pass rate with median <= 30 and 75th percentile <= 50
Then each chosen preset is re-scored on 2024-26 starts and under BAD execution."""
import numpy as np, itertools, json
from multiprocessing import Pool
import stress as T
from lucid_sizes import SIZES, eval_run

SIGS = {"chain": T.BASE_CHAIN, "10:30 only": T.BASE_CHAIN[:1]}
FRACS = [0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5]
CACHE = {}


def legs(sig, bad=False):
    k = (sig, bad)
    if k not in CACHE:
        cfg = dict(chain=SIGS[sig], comm=1.0, slip=1)
        if bad:
            cfg.update(comm=2.0, slip=4, skip=0.3, delay=1, seed=1)
        CACHE[k] = T.legs(cfg)
    return CACHE[k]


def score(size, sig, frac, y0=2020, y1=2026, bad=False):
    lg = legs(sig, bad); g = T.G; A = SIZES[size]
    st = np.where((g.year >= y0) & (g.year <= y1))[0]
    res = [eval_run([lg.get(d) for d in range(s, min(s + 180, g.D))], A, frac * A["mll"], 0.48 * A["target"], 1) for s in st]
    p = np.array([r[0] for r in res]); d = np.array([r[1] for r in res])
    return dict(pass_=p.mean(), med=float(np.median(d[p])) if p.any() else np.nan, p75=float(np.percentile(d[p], 75)) if p.any() else np.nan)


def job(args):
    size, sig, frac = args
    return dict(size=size, sig=sig, frac=frac, **score(size, sig, frac))


if __name__ == "__main__":
    jobs = [(s, sg, f) for s in SIZES for sg in SIGS for f in FRACS]
    with Pool(5) as pool:
        rows = pool.map(job, jobs)
    json.dump(rows, open("results/presets_grid.json", "w"), default=float)
    chosen = {}
    for s in SIZES:
        R = [r for r in rows if r["size"] == s]
        agg = [r for r in R if r["sig"] == "chain" and r["frac"] == 0.5][0]
        nor = max([r for r in R if r["med"] <= 15], key=lambda r: r["pass_"])
        con = max([r for r in R if r["med"] <= 30 and r["p75"] <= 50], key=lambda r: r["pass_"])
        chosen[s] = {"CONSERVATIVE": con, "NORMAL": nor, "AGGRESSIVE": agg}
        print(f"\n{s} grid (pass% / median d / 75th d):")
        for sg in SIGS:
            print(f"  {sg:11} " + "  ".join(f"{r['frac']:.2f}:{r['pass_']*100:3.0f}%/{r['med']:3.0f}/{r['p75']:3.0f}" for r in R if r["sig"] == sg))
    print("\n=== CHOSEN PRESETS (REAL 2020-26 | 2024-26 | BAD execution 2020-26) ===")
    out = []
    def chk(a):
        s, sg, f = a
        return a, score(s, sg, f, 2024, 2026), score(s, sg, f, bad=True)
    with Pool(5) as pool:
        extra = {a: (c, b) for a, c, b in pool.map(chk, [(s, r["sig"], r["frac"]) for s in chosen for r in chosen[s].values()])}
    for s in SIZES:
        for nm, r in chosen[s].items():
            c, b = extra[(s, r["sig"], r["frac"])]
            A = SIZES[s]
            print(f"{s:5} {nm:12} {r['sig']:10} risk ${r['frac']*A['mll']:5.0f}/trade | {r['pass_']*100:3.0f}% {r['med']:3.0f}d (75%: {r['p75']:.0f}d) | "
                  f"{c['pass_']*100:3.0f}% {c['med']:3.0f}d | {b['pass_']*100:3.0f}% {b['med']:3.0f}d")
            out.append(dict(size=s, preset=nm, sig=r["sig"], risk=r["frac"] * A["mll"], real=r, recent=c, bad=b))
    json.dump(out, open("results/presets_chosen.json", "w"), default=float)
