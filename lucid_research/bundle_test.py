"""Apply the fast-pass logic (priority chain of momentum signals, one trade/day, pass take-profit +
$1,450 daily take-profit, $1,000 risk) to EVERY Lucid-tradable market in the FirstRate bundle.
Time anchors are relative to each product's own main-session open (e.g. CL 09:00, GC 08:20, FX 08:20).
For each market report: trade-level stats of the chain (2008-19, 2020-26, 2024-26), Lucid pass rate and
days (rolling starts 2020-26) and the same with the edge removed (zero-edge luck baseline)."""
import sys, numpy as np
from multiprocessing import Pool
import harness as Hn, strategies as S
from lucid_sim import Account, START, TARGET

MICRO = {"ES", "NQ", "RTY", "YM", "CL", "GC", "SI"}
CHAIN = [("tod_mom", dict(T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None), 1.0),
         ("tod_mom", dict(T=90, k=0.0, s=0.3, ref="open", tf=20, trail=None), 1.0),
         ("tod_mom", dict(T=150, k=0.0, s=0.3, ref="open", tf=20, trail=None), 1.0),
         ("noise_area", dict(step=30, mult=1.0, s=0.5, tf=20, trail=(1.0, 1.0)), 1.0),
         ("tod_mom", dict(T=60, k=0.0, s=0.3, ref="open", tf=None, trail=None), 0.5)]


def build(g):
    dpp = Hn.SPEC[g.sym]["dpp"]
    streams = []
    for fam, p, mult in CHAIN:
        tr = getattr(S, fam)(g, **p)
        if len(tr["d"]) == 0:
            streams.append({}); continue
        r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"])
        streams.append({int(d): (r["stop"][i] * dpp, r["net"][i] * dpp, min(r["mae"][i], 0) * dpp,
                                 max(r["mfe"][i], 0) * dpp, bool(r["mae_first"][i]), mult, r["R"][i])
                        for i, d in enumerate(r["d"])})
    sched = {}
    for d in range(g.D):
        for s in streams:
            if d in s:
                sched[d] = s[d]; break
    return sched


def run(sched, s0, D, risk=1000, day_cap=1450, cons=0.5, maxd=120, unit=0.1):
    a = Account(); dps = []
    for n in range(1, maxd + 1):
        d = s0 + n - 1
        if d >= D:
            break
        x = sched.get(d); dp = 0.0
        if x is not None:
            rct, net, mae, mfe, mf, mult, _ = x
            rk = risk * mult
            q = int(rk // rct); q = 1 if q == 0 and rct <= 1.5 * rk else q
            q = min(q, int(round(4 / unit)))           # Lucid eval cap: 4 minis / 40 micros
            if q > 0:
                pnl, mae, mfe = net * q, mae * q, mfe * q
                need = START + TARGET - a.balance
                best = max(dps) if dps else 0.0
                need_c = max(need, best / cons - (a.balance - START)) if best > 0 else need
                tp = min(need_c, day_cap)
                hit = tp > 0 and mfe >= tp + 0.01 * rct * q
                if a.balance + min(mae, pnl) <= a.mll and (not hit or mf):
                    return False, True, n
                if hit:
                    pnl = tp
                a.balance += pnl; dp = pnl
        dps.append(dp)
        if a.balance >= START + TARGET and max(dps) <= cons * (a.balance - START):
            return True, False, n
        a.eod_update()
    return False, False, maxd


def lucid(g, sched, y0=2020, y1=2026):
    unit = 0.1 if g.sym in MICRO else 1.0
    starts = np.where((g.year >= y0) & (g.year <= y1))[0]
    res = [run(sched, s, g.D, unit=unit) for s in starts]
    p = np.array([x[0] for x in res]); dd = np.array([x[2] for x in res])
    return p.mean(), (np.median(dd[p]) if p.any() else np.nan)


def one(sym):
    g = Hn.load(sym)
    sched = build(g)
    days = np.array(sorted(sched)); R = np.array([sched[d][6] for d in days]); y = g.year[days]
    usd = np.array([sched[d][1] / sched[d][0] for d in days])        # net per $ risked (1 contract)
    out = dict(sym=sym, cover=float(np.mean([d in sched for d in np.where(g.year >= 2020)[0]])))
    for lab, m in (("08_19", (y >= 2008) & (y <= 2019)), ("20_26", y >= 2020), ("24_26", y >= 2024), ("last12", g.days[days] >= np.datetime64("2025-10-10").astype(np.int64))):
        x = R[m]
        out[f"R_{lab}"] = float(x.mean()) if len(x) else np.nan
        out[f"t_{lab}"] = float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))) if len(x) > 2 else np.nan
        out[f"PF_{lab}"] = float(x[x > 0].sum() / -x[x < 0].sum()) if (x < 0).any() else np.nan
    # min tradable risk: median $ risk of ONE contract (can we even size it under $1,000?)
    out["ct_risk"] = float(np.median([sched[d][0] for d in days if g.year[d] >= 2020]))
    out["pass"], out["days"] = lucid(g, sched)
    out["pass24"], out["days24"] = lucid(g, sched, 2024, 2026)
    # zero-edge baseline: remove each trade's 2020-26 average edge (per $ risked)
    mu = usd[y >= 2020].mean()
    z = {d: (v[0], v[1] - mu * v[0], min(v[2], v[1] - mu * v[0]), max(v[3] - mu * v[0], 0), v[4], v[5], v[6]) for d, v in sched.items()}
    out["pass0"], out["days0"] = lucid(g, z)
    Hn._cache.clear()
    return out


if __name__ == "__main__":
    syms = sys.argv[1].split(",") if len(sys.argv) > 1 else Hn.TRADABLE
    with Pool(5) as pool:
        rows = pool.map(one, syms)
    rows.sort(key=lambda r: -(r["pass"] - r["pass0"]))
    print(f"{'mkt':4} {'cover':>5} {'1ct$':>6} | {'R 08-19':>8} {'R 20-26':>8} {'t':>5} {'PF':>5} | {'R 24-26':>8} {'PF':>5} | {'PF 12m':>6} || "
          f"{'PASS 20-26':>10} {'days':>4} | {'luck':>5} | {'edge+':>5} || {'PASS 24-26':>10} {'days':>4}")
    for r in rows:
        print(f"{r['sym']:4} {r['cover']*100:4.0f}% {r['ct_risk']:6.0f} | {r['R_08_19']:+8.3f} {r['R_20_26']:+8.3f} {r['t_20_26']:5.2f} {r['PF_20_26']:5.2f} | "
              f"{r['R_24_26']:+8.3f} {r['PF_24_26']:5.2f} | {r['PF_last12']:6.2f} || {r['pass']*100:9.0f}% {r['days']:4.0f} | {r['pass0']*100:4.0f}% | {(r['pass']-r['pass0'])*100:+4.0f} || {r['pass24']*100:9.0f}% {r['days24']:4.0f}")
    import json
    json.dump(rows, open("results/bundle_test.json", "w"), default=float)
