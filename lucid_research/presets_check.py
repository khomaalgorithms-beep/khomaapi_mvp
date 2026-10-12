"""Select presets from results/presets_grid.json (rule in presets.py) and re-score on 2024-26 and BAD execution."""
import json, numpy as np
from multiprocessing import Pool
import presets as P
from lucid_sizes import SIZES

def chk(a):
    s, sg, f = a
    return a, P.score(s, sg, f, 2024, 2026), P.score(s, sg, f, bad=True)

if __name__ == "__main__":
    rows = json.load(open("results/presets_grid.json"))
    chosen = {}
    for s in SIZES:
        R = [r for r in rows if r["size"] == s]
        print(f"{s} grid (risk as % of MLL: pass / median d / 75th d)")
        for sg in P.SIGS:
            print(f"  {sg:11} " + "  ".join(f"{r['frac']*100:.0f}%:{r['pass_']*100:3.0f}%/{r['med']:3.0f}/{r['p75']:3.0f}" for r in R if r["sig"] == sg))
        chosen[s] = {"CONSERVATIVE": max([r for r in R if r["med"] <= 30 and r["p75"] <= 50], key=lambda r: r["pass_"]),
                     "NORMAL": max([r for r in R if r["med"] <= 15], key=lambda r: r["pass_"]),
                     "AGGRESSIVE": [r for r in R if r["sig"] == "chain" and r["frac"] == 0.5][0]}
    with Pool(5) as pool:
        ex = {tuple(a): (c, b) for a, c, b in pool.map(chk, [(s, r["sig"], r["frac"]) for s in chosen for r in chosen[s].values()])}
    out = []
    for s in SIZES:
        for nm, r in chosen[s].items():
            c, b = ex[(s, r["sig"], r["frac"])]
            out.append(dict(size=s, preset=nm, sig=r["sig"], risk=r["frac"] * SIZES[s]["mll"], real=r, recent=c, bad=b))
            print(f"{s:5} {nm:12} {r['sig']:10} risk ${r['frac']*SIZES[s]['mll']:5.0f} | REAL {r['pass_']*100:3.0f}% {r['med']:3.0f}d (75% {r['p75']:.0f}d) | 2024-26 {c['pass_']*100:3.0f}% {c['med']:3.0f}d | BAD {b['pass_']*100:3.0f}% {b['med']:3.0f}d", flush=True)
    json.dump(out, open("results/presets_chosen.json", "w"), default=float)
