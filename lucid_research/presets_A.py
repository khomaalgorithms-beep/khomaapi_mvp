"""Presets on the FINAL system (Spec A only), same pre-declared rule as presets.py:
  CONSERVATIVE = max pass with median <= 30 & 75th <= 50 | NORMAL = max pass with median <= 15
  AGGRESSIVE   = max pass with median <= 10 (fastest sensible)
Real costs, rolling starts 2020-26, 180-day window; re-scored on 2024-26 and BAD execution."""
import json, numpy as np
from multiprocessing import Pool
import presets as P
from lucid_sizes import SIZES

FR = [0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.65, 0.8, 1.0]


def job(a):
    s, f = a
    return dict(size=s, frac=f, **P.score(s, "10:30 only", f))


def chk(a):
    s, f = a
    return a, P.score(s, "10:30 only", f, 2024, 2026), P.score(s, "10:30 only", f, bad=True)


if __name__ == "__main__":
    with Pool(6) as pool:
        rows = pool.map(job, [(s, f) for s in SIZES for f in FR])
    chosen = {}
    for s in SIZES:
        R = [r for r in rows if r["size"] == s]
        print(f"{s}: " + "  ".join(f"{r['frac']*100:.0f}%:{r['pass_']*100:.0f}%/{r['med']:.0f}d/{r['p75']:.0f}" for r in R))
        pick = lambda cond: max([r for r in R if cond(r)], key=lambda r: r["pass_"])
        chosen[s] = {"CONSERVATIVE": pick(lambda r: r["med"] <= 30 and r["p75"] <= 50),
                     "NORMAL": pick(lambda r: r["med"] <= 15), "AGGRESSIVE": pick(lambda r: r["med"] <= 10)}
    with Pool(6) as pool:
        ex = {tuple(a): (c, b) for a, c, b in pool.map(chk, [(s, r["frac"]) for s in chosen for r in chosen[s].values()])}
    out = []
    for s in SIZES:
        for nm, r in chosen[s].items():
            c, b = ex[(s, r["frac"])]
            risk = r["frac"] * SIZES[s]["mll"]
            out.append(dict(size=s, preset=nm, risk=risk, frac=r["frac"], real=r, recent=c, bad=b))
            print(f"{s:5} {nm:12} risk ${risk:5.0f} | 2020-26 {r['pass_']*100:3.0f}% {r['med']:3.0f}d (75% {r['p75']:.0f}) | 2024-26 {c['pass_']*100:3.0f}% {c['med']:3.0f}d | BAD {b['pass_']*100:3.0f}% {b['med']:3.0f}d")
    json.dump(dict(grid=rows, chosen=out), open("results/presets_A.json", "w"), default=float)
