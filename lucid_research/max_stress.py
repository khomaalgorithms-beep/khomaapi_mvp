"""MAX-level robustness battery for the final NQ logic (one trade/day priority chain, real costs).
Usage: python3 max_stress.py walkforward|pbo|shuffle|noise|mccost
All results -> results/max_<test>.json and printed."""
import sys, json, itertools, math, numpy as np
from multiprocessing import Pool
import harness as Hn, stress as T

YEARS = list(range(2008, 2027))


def chain_cfg(s=0.3, T1=60, tf=20, counter=True):
    ch = []
    for f, p, m in T.BASE_CHAIN:
        p = dict(p)
        if f == "tod_mom":
            p["s"] = s
            if p["T"] == 60:
                p["T"] = T1
            if p.get("tf"):
                p["tf"] = tf
        else:
            p["tf"] = tf
        ch.append((f, p, m))
    return ch[:1]          # FINAL system = Spec A only (s, T1, tf vary)


GRID = [dict(s=s, T1=t1, tf=tf, counter=True) for s in (0.2, 0.25, 0.3, 0.35, 0.4) for t1 in (45, 60, 75) for tf in (10, 20, 50)]


def daily_R(params):
    lg = T.legs(dict(chain=chain_cfg(**params), comm=1.0, slip=1))
    g = T.G
    R = np.full(g.D, np.nan)
    for d, v in lg.items():
        R[d] = v["net"] / v["rct"]
    return R


def stats(x):
    x = x[~np.isnan(x)]
    if len(x) < 3:
        return dict(n=len(x), mean=np.nan, t=np.nan, pf=np.nan)
    return dict(n=int(len(x)), mean=float(x.mean()), t=float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))),
                pf=float(x[x > 0].sum() / -x[x < 0].sum()))


# ------------------------------------------------------------------ 1. walk-forward re-selection
def walkforward():
    with Pool(5) as pool:
        Rs = pool.map(daily_R, GRID)
    M = np.vstack(Rs); g = Hn.load("NQ"); yr = g.year
    base = GRID.index(dict(s=0.3, T1=60, tf=20, counter=True))
    out = {"years": {}}
    oos = np.full(g.D, np.nan); dflt = M[base].copy()
    for Y in range(2012, 2027):
        past = yr < Y
        score = [np.nanmean(M[i][past]) / np.nanstd(M[i][past]) for i in range(len(GRID))]
        pick = int(np.argmax(score))
        m = yr == Y
        oos[m] = M[pick][m]
        out["years"][Y] = dict(pick=GRID[pick], oos=stats(M[pick][m]), default=stats(M[base][m]))
        print(f"{Y}: picked {GRID[pick]} -> OOS {stats(M[pick][m])}  | fixed default {stats(M[base][m])}", flush=True)
    out["oos_all"] = stats(oos[yr >= 2012]); out["oos_2020"] = stats(oos[yr >= 2020])
    out["default_all"] = stats(dflt[yr >= 2012]); out["default_2020"] = stats(dflt[yr >= 2020])
    # plateau: how many of the 36 settings are profitable 2020-26 / 2008-19
    out["share_pos_2020"] = float(np.mean([np.nanmean(M[i][yr >= 2020]) > 0 for i in range(len(GRID))]))
    out["share_pos_0819"] = float(np.mean([np.nanmean(M[i][yr < 2020]) > 0 for i in range(len(GRID))]))
    print("WALK-FORWARD OOS 2012-26:", out["oos_all"], "| 2020-26:", out["oos_2020"])
    print("fixed default 2012-26:", out["default_all"], "| 2020-26:", out["default_2020"])
    print(f"settings profitable 2020-26: {out['share_pos_2020']*100:.0f}%, 2008-19: {out['share_pos_0819']*100:.0f}%")
    np.save("results/grid_daily_R.npy", M)
    json.dump(out, open("results/max_walkforward.json", "w"), default=float)


# ------------------------------------------------------------------ 2. probability of backtest overfitting (CSCV)
def pbo():
    M = np.load("results/grid_daily_R.npy"); g = Hn.load("NQ")
    days = np.where(~np.all(np.isnan(M), axis=0))[0]
    X = np.nan_to_num(M[:, days])
    S = 16; blocks = np.array_split(np.arange(X.shape[1]), S)
    logits = []; deg = []
    for comb in itertools.combinations(range(S), S // 2):
        IS = np.concatenate([blocks[i] for i in comb]); OS = np.concatenate([blocks[i] for i in range(S) if i not in comb])
        sr_is = X[:, IS].mean(1) / (X[:, IS].std(1) + 1e-12); sr_os = X[:, OS].mean(1) / (X[:, OS].std(1) + 1e-12)
        best = int(np.argmax(sr_is))
        rank = (sr_os < sr_os[best]).mean()            # relative OOS rank of the IS winner
        w = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(math.log(w / (1 - w))); deg.append(sr_os[best])
    logits = np.array(logits)
    res = dict(PBO=float(np.mean(logits <= 0)), combos=len(logits), winner_oos_sr_mean=float(np.mean(deg)),
               winner_oos_positive=float(np.mean(np.array(deg) > 0)))
    print("PBO (CSCV, 16 blocks, 12,870 splits):", res)
    json.dump(res, open("results/max_pbo.json", "w"))


# ------------------------------------------------------------------ 3. shuffled-intraday market (simulator-bias check)
def shuffled_R(seed):
    rng = np.random.default_rng(seed)
    g = Hn.load("NQ")
    O, H, L, C = g.O.astype(np.float64), g.H.astype(np.float64), g.L.astype(np.float64), g.C.astype(np.float64)
    for d in range(g.D):
        r = np.diff(C[d], prepend=O[d, 0])                      # minute close-to-close moves
        wick_up = H[d] - np.maximum(O[d], C[d]); wick_dn = np.minimum(O[d], C[d]) - L[d]
        p = rng.permutation(len(r))
        c = O[d, 0] + np.cumsum(r[p]); o = np.r_[O[d, 0], c[:-1]]
        g.O[d] = o; g.C[d] = c
        g.H[d] = np.maximum(o, c) + wick_up[p]; g.L[d] = np.minimum(o, c) - wick_dn[p]
    T.G = g
    lg = T.legs(dict(comm=1.0, slip=1))
    R = np.array([v["net"] / v["rct"] for d, v in lg.items() if g.year[d] >= 2016])
    Hn._cache.clear(); T.G = None
    return stats(R)


def shuffle():
    with Pool(5) as pool:
        res = pool.map(shuffled_R, range(1, 6))
    for i, r in enumerate(res, 1):
        print(f"SHUFFLED intraday (seed {i}): {r}")
    json.dump(res, open("results/max_shuffle.json", "w"))


# ------------------------------------------------------------------ 4. price-noise injection
def noisy_R(args):
    seed, ticks = args
    rng = np.random.default_rng(seed)
    g = Hn.load("NQ"); tk = 0.25
    for A in (g.O, g.H, g.L, g.C):
        A += (rng.integers(-ticks, ticks + 1, A.shape) * tk).astype(np.float32)
    hi = np.maximum.reduce([g.O, g.H, g.C]); lo = np.minimum.reduce([g.O, g.L, g.C]); g.H[:] = hi; g.L[:] = lo
    T.G = g
    lg = T.legs(dict(comm=1.0, slip=1))
    R = np.array([v["net"] / v["rct"] for d, v in lg.items() if g.year[d] >= 2016])
    Hn._cache.clear(); T.G = None
    return dict(seed=seed, ticks=ticks, **stats(R))


def noise():
    with Pool(5) as pool:
        res = pool.map(noisy_R, [(s, t) for t in (1, 2, 4) for s in (1, 2)])
    for r in res:
        print(f"NOISE ±{r['ticks']} ticks per price (seed {r['seed']}): n={r['n']} meanR={r['mean']:+.3f} t={r['t']:.2f} PF={r['pf']:.2f}")
    json.dump(res, open("results/max_noise.json", "w"))


# ------------------------------------------------------------------ 5. Monte Carlo of random execution
def mc_one(seed):
    rng = np.random.default_rng(seed)
    comm = rng.uniform(1.0, 3.0); miss = rng.uniform(0.0, 0.3); lat = int(rng.integers(0, 4))
    base = T.legs(dict(comm=comm, slip=0, delay=lat, skip=miss, seed=seed))
    g = T.G
    out = {}
    for d, v in base.items():
        sl = min(rng.lognormal(np.log(1.5), 0.7), 20) * 0.25 * 2.0 * 2      # entry + exit slip, $ per MNQ, fat-tailed
        out[d] = dict(v, net=v["net"] - sl, best=v["best"] - sl, worst=v["worst"] - sl)
    R = np.array([v["net"] / v["rct"] for d, v in out.items() if g.year[d] >= 2020])
    days = np.arange(g.D); st = np.where(g.year >= 2020)[0][::2]
    res = [T.run_eval(out, days, s) for s in st]
    p = np.array([r[0] for r in res]); dd = np.array([r[2] for r in res])
    return dict(seed=seed, comm=comm, miss=miss, lat=lat, **stats(R), pass_=float(p.mean()), med=float(np.median(dd[p])) if p.any() else np.nan)


def mccost():
    with Pool(5) as pool:
        res = pool.map(mc_one, range(1, 201))
    pf = np.array([r["pf"] for r in res]); ps = np.array([r["pass_"] for r in res]); m = np.array([r["mean"] for r in res])
    summ = dict(n=len(res), pf_p5=float(np.percentile(pf, 5)), pf_med=float(np.median(pf)), pf_p95=float(np.percentile(pf, 95)),
                p_pf_below_1=float(np.mean(pf < 1)), meanR_p5=float(np.percentile(m, 5)), pass_p5=float(np.percentile(ps, 5)),
                pass_med=float(np.median(ps)), pass_p95=float(np.percentile(ps, 95)), med_days=float(np.nanmedian([r["med"] for r in res])))
    print("MC random execution (200 worlds, 2020-26):", summ)
    json.dump(dict(summary=summ, runs=res), open("results/max_mccost.json", "w"), default=float)




# ------------------------------------------------------------------ 3b. pure random walk (clean simulator-bias null)
def rw_R(seed):
    """Each day: iid Gaussian minute moves with that day's REAL minute volatility; no momentum, no daily drift,
    no trend. Wicks drawn from the same volatility. Expected result: lose ~costs."""
    rng = np.random.default_rng(seed)
    g = Hn.load("NQ")
    prev_close = float(g.O[0, 0])
    for d in range(g.D):
        realC = g.C[d].astype(np.float64)
        sig = np.std(np.diff(realC)[g.real[d, 1:]]) if g.real[d].sum() > 10 else 1.0
        r = rng.normal(0, max(sig, 0.25), Hn.M)
        c = prev_close + np.cumsum(r); o = np.r_[prev_close, c[:-1]]
        g.O[d] = o; g.C[d] = c
        g.H[d] = np.maximum(o, c) + np.abs(rng.normal(0, sig / 2, Hn.M)); g.L[d] = np.minimum(o, c) - np.abs(rng.normal(0, sig / 2, Hn.M))
        prev_close = c[-1]
    g.real[:] = True
    Hn._features(g)
    T.G = g
    lg = T.legs(dict(comm=1.0, slip=1))
    R = np.array([v["net"] / v["rct"] for d, v in lg.items() if g.year[d] >= 2016])
    Rg = np.array([(v["net"] + 1.0 + 2 * 0.25 * 2) / v["rct"] for d, v in lg.items() if g.year[d] >= 2016])   # gross (costs added back)
    Hn._cache.clear(); T.G = None
    return dict(net=stats(R), gross=stats(Rg))


def randomwalk():
    with Pool(5) as pool:
        res = pool.map(rw_R, range(1, 6))
    for i, r in enumerate(res, 1):
        print(f"RANDOM WALK (seed {i}): net {r['net']} | gross (before costs) meanR {r['gross']['mean']:+.3f} t={r['gross']['t']:.2f}")
    json.dump(res, open("results/max_randomwalk.json", "w"))


if __name__ == "__main__":
    dict(walkforward=walkforward, pbo=pbo, shuffle=shuffle, noise=noise, mccost=mccost, randomwalk=randomwalk)[sys.argv[1]]()
