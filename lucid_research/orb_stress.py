"""MAX stress battery for the NQ ORB + prior-day-break system (fixed parameters)."""
import numpy as np, harness as Hn
from multiprocessing import Pool
import orb_lab as OL
SYS = dict(n=5, entry="touch", cutoff=60, filt=("pdl",), stop="atr", stop_k=0.2, be=1.0)
ALL = ("2008-03-01", "2026-10-31")
def st(res, per=ALL): return OL.stats(res, per)
def line(nm, s): print(f"  {nm:46} PF {s['pf']:.2f} | R/trade {s['mean']:+.3f} | t {s['t']:+.1f} | total {s['tot']:+6.1f}R | maxDD {s['dd']:5.1f}R")
def run_delay(minutes):
    g = OL.G("NQ"); t = OL.trades(g, **{k: v for k, v in SYS.items() if k != "be"})
    e = t["e"] + minutes; px = g.O[t["d"], np.minimum(e, Hn.M - 1)]
    sd = np.where(t["side"] > 0, px - (t["px"] - t["stop"]), (t["px"] + t["stop"]) - px)     # same stop PRICE, later fill
    ok = sd > 0.5
    r = Hn.simulate(g, t["d"][ok], e[ok], t["side"][ok], px[ok], sd[ok], None, np.full(ok.sum(), g.b), None, SYS["be"])
    k = ~g.suspect[r["d"]]; return dict(day=g.days[r["d"]][k], R=r["R"][k])
def placebo_pdl(seed):
    g = OL.G("NQ"); rng = np.random.default_rng(seed)
    ph, pl = g.p_high.copy(), g.p_low.copy()
    sh = rng.integers(30, 250); g.p_high = np.roll(ph, sh); g.p_low = np.roll(pl, sh)   # yesterday's levels from a random other day
    try: return st(OL.run("NQ", **SYS))["tot"]
    finally: g.p_high, g.p_low = ph, pl
def miss(seed, frac):
    res = OL.run("NQ", **SYS); rng = np.random.default_rng(seed); k = rng.random(len(res["R"])) >= frac
    return st(dict(day=res["day"][k], R=res["R"][k]))["pf"]
if __name__ == "__main__":
    base = OL.run("NQ", **SYS); r = base["R"]; days = base["day"]
    print("1) COSTS & EXECUTION (2008-2026)"); line("baseline (1 tick slippage, Lucid commission)", st(base))
    for k in (2, 3, 4): line(f"slippage x{k}", st(OL.run("NQ", slip=k, **SYS)))
    g = OL.G("NQ"); c0 = g.spec["comm"]; g.spec = dict(g.spec, comm=c0 * 3); line("commission x3", st(OL.run("NQ", **SYS))); g.spec = dict(g.spec, comm=c0)
    for m in (1, 2, 5): line(f"entry {m} min late (same stop price)", st(run_delay(m)))
    with Pool(8) as pool:
        for fr in (0.2, 0.3):
            pf = pool.starmap(miss, [(s, fr) for s in range(200)]); print(f"  {int(fr*100)}% of trades randomly missed (200 runs): PF median {np.median(pf):.2f}, worst 5% {np.percentile(pf,5):.2f}")
    print("\n2) OUTLIER DEPENDENCE")
    for q in (0.01, 0.05, 0.10):
        k = r < np.quantile(r, 1 - q); s = st(dict(day=days[k], R=r[k])); line(f"remove best {int(q*100)}% of trades", s)
    print("\n3) PLACEBOS (the edge should disappear)")
    with Pool(8) as pool: P = pool.map(placebo_pdl, range(100))
    print(f"  prior-day levels taken from a RANDOM other day (100 runs): total R median {np.median(P):+.1f} vs real {st(base)['tot']:+.1f} (real beats {np.mean(np.array(P) < st(base)['tot'])*100:.0f}%)")
    g.a0 = g.a; g.a = Hn.t("11:00")
    try: line("same rule on an 11:00-11:05 range (not the open)", st(OL.run("NQ", **SYS)))
    finally: g.a = g.a0
    print("\n4) SPLITS")
    yrs = g.year[np.searchsorted(g.days, days)]
    side = OL.trades(g, **{k: v for k, v in SYS.items() if k != "be"})
    sd = dict(zip(g.days[side["d"]], side["side"])); s_arr = np.array([sd.get(d, 0) for d in days])
    line("longs only", st(dict(day=days[s_arr > 0], R=r[s_arr > 0]))); line("shorts only", st(dict(day=days[s_arr < 0], R=r[s_arr < 0])))
    vp = g.volpct[np.searchsorted(g.days, days)]
    line("high-volatility regime", st(dict(day=days[vp > 0.5], R=r[vp > 0.5]))); line("low-volatility regime", st(dict(day=days[vp <= 0.5], R=r[vp <= 0.5])))
    tr = g.trend20[np.searchsorted(g.days, days)]
    line("trades WITH the 20-day trend", st(dict(day=days[tr == s_arr], R=r[tr == s_arr]))); line("trades AGAINST the 20-day trend", st(dict(day=days[tr == -s_arr], R=r[tr == -s_arr])))
    line("first half 2008-2016", st(base, ("2008-03-01", "2016-12-31"))); line("second half 2017-2026", st(base, ("2017-01-01", "2026-10-31")))
    print("\n5) MONTE CARLO")
    rng = np.random.default_rng(1); dd, ls = [], []
    for _ in range(10000):
        x = rng.permutation(r); eq = np.cumsum(x); dd.append(-(eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min())
        b = c = 0
        for v in x[:600]:
            c = c + 1 if v < 0 else 0; b = max(b, c)
        ls.append(b)
    print(f"  trade-order shuffle (10,000): max drawdown median {np.median(dd):.1f}R, 95th pct {np.percentile(dd,95):.1f}R, 99th {np.percentile(dd,99):.1f}R | longest losing streak (10 yrs of trades) median {np.median(ls):.0f}, 95th pct {np.percentile(ls,95):.0f}")
    # block bootstrap of calendar months -> 1-year outcomes
    months = np.array([str(np.datetime64(int(d), 'D'))[:7] for d in days]); um = sorted(set(months)); mR = np.array([r[months == m].sum() for m in um])
    allm = len(um); Y = np.array([rng.choice(mR, 12).sum() for _ in range(20000)])
    print(f"  1-year outcomes (20,000 bootstrap years of real months): median {np.median(Y):+.1f}R | P(losing year) {np.mean(Y<0)*100:.0f}% | worst 5% {np.percentile(Y,5):+.1f}R | best 5% {np.percentile(Y,95):+.1f}R")
    real_streak = 0; c = 0
    for v in r: c = c + 1 if v < 0 else 0; real_streak = max(real_streak, c)
    print(f"  actual longest losing streak 2008-26: {real_streak} trades | win rate {np.mean(r>0)*100:.0f}% | breakeven exits {np.mean(np.abs(r)<0.15)*100:.0f}%")
