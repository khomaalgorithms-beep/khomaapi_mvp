"""Adaptive Spec A (expanding window, quarterly re-fit, best mean R) end-to-end:
which settings it picks over time, and LucidFlex presets on its real trade sequence (exact replay)."""
import numpy as np, json
from multiprocessing import Pool
import harness as Hn, stress as T, max_stress as MS
from lucid_sizes import SIZES, eval_run
import funded_hybrid as FH

M = np.load("results/grid_daily_R.npy"); g = Hn.load("NQ")


def picks(refit=63):
    out = np.full(g.D, -1); start = np.where(g.year >= 2012)[0][0]; i = start
    while i < g.D:
        k = int(np.nanargmax(np.nanmean(M[:, :i], 1))); j = min(g.D, i + refit); out[i:j] = k; i = j
    return out


def build():
    pk = picks()
    need = sorted(set(pk[pk >= 0].tolist()))
    legs_by = {k: T.legs(dict(chain=MS.chain_cfg(**MS.GRID[k]), comm=1.0, slip=1)) for k in need}
    lg = {}
    for d in range(g.D):
        k = pk[d]
        if k >= 0 and d in legs_by[k]:
            lg[d] = legs_by[k][d]
    return lg, pk


LG = None


def score(args):
    global LG
    if LG is None:
        LG, _ = build()
    size, frac, a, b = args
    A = SIZES[size]; gg = T.G
    st = np.where((gg.days >= np.datetime64(a).astype(np.int64)) & (gg.days <= np.datetime64(b).astype(np.int64)))[0]
    res = [eval_run([LG.get(x) for x in range(s, min(s + 180, gg.D))], A, frac * A["mll"], 0.48 * A["target"], 1) for s in st]
    p = np.array([r[0] for r in res]); dd = np.array([r[1] for r in res])
    return args, p.mean(), (np.median(dd[p]) if p.any() else np.nan), (np.percentile(dd[p], 75) if p.any() else np.nan)


if __name__ == "__main__":
    lg, pk = build()
    print("Settings chosen by the adaptive rule (start date -> setting):")
    prev = None
    for d in range(g.D):
        if pk[d] != prev and pk[d] >= 0:
            print(f"  {g.dates[d]}: {MS.GRID[pk[d]]}"); prev = pk[d]
    d = np.array(sorted(lg)); R = np.array([lg[x]["net"] / lg[x]["rct"] for x in d]); y = g.year[d]
    for nm, m in (("2012-26", y >= 2012), ("2020-26", y >= 2020), ("since Jul-25", g.days[d] >= np.datetime64("2025-07-01").astype(np.int64)), ("2026 YTD", y == 2026)):
        x = R[m]; print(f"  adaptive {nm:13}: n={len(x)} R={x.mean():+.3f} PF={x[x>0].sum()/-x[x<0].sum():.2f} t={x.mean()/x.std(ddof=1)*np.sqrt(len(x)):.2f}")
    per = (("2020-01-01", "2026-10-09"), ("2024-01-01", "2026-10-09"), ("2025-07-01", "2026-10-09"))
    jobs = [(s, f, a, b) for s in SIZES for f in (0.25, 0.4, 0.5) for a, b in per]
    with Pool(5) as pool:
        res = pool.map(score, jobs)
    print("\nLucidFlex eval with the ADAPTIVE rule (pass / median days): 2020-26 | 2024-26 | since Jul-2025")
    for s in SIZES:
        for f, nm in ((0.25, "Conservative"), (0.4, "Normal"), (0.5, "Aggressive")):
            cells = [r for r in res if r[0][0] == s and r[0][1] == f]
            cells.sort(key=lambda r: r[0][2])
            print(f"  {s:5} {nm:12} risk ${f*SIZES[s]['mll']:5.0f} | " + " | ".join(f"{r[1]*100:3.0f}% {r[2]:3.0f}d" for r in cells))
    json.dump(dict(picks=[MS.GRID[int(k)] if k >= 0 else None for k in pk[-1:]], res=[(list(r[0]), r[1], r[2], r[3]) for r in res]), open("results/adaptive_lucid.json", "w"), default=float)
