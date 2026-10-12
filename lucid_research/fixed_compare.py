"""After the look-ahead fix: compare 3 pre-declared versions on real NQ data (real costs).
Trade stats per period + LucidFlex 50K eval pass (rule exits) at several risk levels, rolling starts."""
import numpy as np, json
from multiprocessing import Pool
import stress as T
from lucid_sizes import SIZES, eval_run

VERSIONS = {
    "A: 10:30 with-trend only": T.BASE_CHAIN[:1],
    "B: 10:30 both ways (counter half)": [T.BASE_CHAIN[0], T.BASE_CHAIN[4]],
    "C: real-time full list": T.BASE_CHAIN,
}
PER = {"2008-19": (2008, 2019), "2020-23": (2020, 2023), "2024-26": (2024, 2026)}


def st(x):
    return f"n={len(x):4d} R={x.mean():+.3f} t={x.mean()/x.std(ddof=1)*np.sqrt(len(x)):5.2f} PF={x[x>0].sum()/-x[x<0].sum():.2f}"


def job(name):
    lg = T.legs(dict(chain=VERSIONS[name], comm=1.0, slip=1)); g = T.G
    out = [name]
    d = np.array(sorted(lg)); R = np.array([lg[x]["net"] / lg[x]["rct"] for x in d]); y = g.year[d]
    for p, (a, b) in PER.items():
        out.append(f"   {p}: {st(R[(y >= a) & (y <= b)])}")
    last = g.days[d] >= np.datetime64("2025-10-10").astype(np.int64)
    out.append(f"   last 12m: {st(R[last])}")
    A = SIZES["50K"]
    for risk in (500, 700, 1000):
        for lab, (a, b) in (("2020-26", (2020, 2026)), ("2024-26", (2024, 2026))):
            starts = np.where((g.year >= a) & (g.year <= b))[0]
            res = [eval_run([lg.get(x) for x in range(s, min(s + 180, g.D))], A, risk, 1440, 1) for s in starts]
            p = np.array([r[0] for r in res]); dd = np.array([r[1] for r in res])
            out.append(f"   50K eval risk ${risk} {lab}: pass {p.mean()*100:3.0f}%  median {np.median(dd[p]):3.0f}d  75th {np.percentile(dd[p],75):3.0f}d")
    return "\n".join(out)


if __name__ == "__main__":
    with Pool(3) as pool:
        for txt in pool.map(job, list(VERSIONS)):
            print(txt, flush=True)
