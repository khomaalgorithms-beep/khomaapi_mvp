"""LucidFlex 50K numbers for the ORB system vs the live 10:30 rule (same simulator as the product)."""
import sys, numpy as np
sys.path.insert(0, "..")
from multiprocessing import Pool
import orb_lab as OL, harness as Hn, dashboard_data as DDm, funded_safe as FS, client_economics as CE
from lucid_sizes import SIZES
SYS = dict(n=5, entry="touch", cutoff=60, filt=("pdl",), stop="atr", stop_k=0.2, be=1.0)
def orb_legs():
    g = OL.G("NQ"); t = OL.trades(g, **{k: v for k, v in SYS.items() if k != "be"})
    r = Hn.simulate(g, t["d"], t["e"], t["side"], t["px"], t["stop"], None, np.full(len(t["d"]), g.b), None, SYS["be"])
    dpp = 2.0; slip = g.spec["tick"] * g.spec["slip"]; cm = g.spec["comm"]; L = {}
    for i in range(len(r["d"])):
        d = int(r["d"][i])
        if g.suspect[d]: continue
        ei = int(r["e"][i]); x = ei + int(r["hold"][i]); sd = float(r["side"][i]); fill = float(t["px"][i]) + sd * slip
        H = g.H[d, ei:x + 1].astype(float); Lo = g.L[d, ei:x + 1].astype(float); C = g.C[d, ei:x + 1].astype(float)
        best = (np.where(sd > 0, H, -Lo) - sd * fill) * dpp; worst = (np.where(sd > 0, Lo, -H) - sd * fill) * dpp
        best[0] = worst[0] = (C[0] - fill) * sd * dpp
        L[d] = dict(best=best - cm, worst=worst - cm, net=float(r["net"][i]) * dpp, rct=float(r["stop"][i]) * dpp)
    return L, g
WIN = {"2008-26": ("2008-03-01", "2026-10-09"), "2020-26": ("2020-01-01", "2026-10-09"), "last 12m": ("2025-10-12", "2026-10-09")}
def job(a):
    which, risk = a
    L, g = orb_legs() if which == "ORB" else DDm.lg()
    A = SIZES["50K"]; out = {}
    for w, (x, y) in WIN.items():
        st = np.where((g.days >= np.datetime64(x).astype(np.int64)) & (g.days <= np.datetime64(y).astype(np.int64)))[0]
        res = [DDm.eval_run([L.get(i) for i in range(s, min(s + 180, g.D))], A, risk, "eod") for s in st]
        p = np.array([r[0] for r in res]); d = np.array([r[1] for r in res])
        out[w] = (p.mean(), float(np.median(d[p])) if p.any() else 0)
    # funded (2008-25 starts, every 2nd day): Standard (12% MLL hold) and Safe (TP 2R, $500 first, half risk after)
    st = np.where((g.year >= 2008) & (g.year <= 2025))[0][::2]
    for nm, tpk, pol in (("std", None, "A"), ("safe", 2.0, "E")):
        R = [CE.funded([L.get(i) for i in range(s, min(s + 250, g.D))], A, 0.12 * A["mll"], tpk, pol) for s in st]
        out["f_" + nm] = (np.mean([len(r[1]) > 0 for r in R]), np.mean([r[2] and not r[1] for r in R]), np.mean([0.9 * sum(r[1]) for r in R]))
    return a, out
if __name__ == "__main__":
    jobs = [(w, r) for w in ("ORB", "LIVE") for r in (400, 600, 800)]
    with Pool(6) as pool: R = dict(pool.map(job, jobs))
    for w in ("ORB", "LIVE"):
        print(f"\n=== {'NEW: NQ 5-min ORB + prior-day break' if w=='ORB' else 'CURRENT: live 10:30 momentum rule'} | LucidFlex 50K, EOD ===")
        for r in (400, 600, 800):
            o = R[(w, r)]
            print(f"  eval risk ${r}: " + " | ".join(f"{k} {o[k][0]*100:3.0f}% ({o[k][1]:.0f}d)" for k in WIN) +
                  f" || funded Std: paid {o['f_std'][0]*100:.0f}% blown-unpaid {o['f_std'][1]*100:.0f}% yr1 ${o['f_std'][2]:.0f} | Safe: paid {o['f_safe'][0]*100:.0f}% blown-unpaid {o['f_safe'][1]*100:.0f}% yr1 ${o['f_safe'][2]:.0f}")
    # correlation of daily R with the live rule
    Lo, g = orb_legs(); Ll, _ = DDm.lg()
    days = sorted(set(Lo) | set(Ll)); a = np.array([Lo[d]["net"] / Lo[d]["rct"] if d in Lo else 0 for d in days]); b = np.array([Ll[d]["net"] / Ll[d]["rct"] if d in Ll else 0 for d in days])
    both = sum(1 for d in days if d in Lo and d in Ll)
    print(f"\nDaily correlation with the live rule: {np.corrcoef(a, b)[0,1]:+.2f} | days both trade: {both} of {len(days)}")
