"""Do other markets add to NQ in ONE Lucid account? Same-day trades are combined conservatively
(sized from start-of-day, worst excursions assumed simultaneous; pass take-profit/day cap applied to the
combined day using the sum of MFEs only if every leg reached its share — approximated by applying the
cap to the summed P&L when summed MFE >= cap and no leg's MAE would breach first)."""
import numpy as np, harness as Hn, bundle_test as B
from lucid_sim import Account, START, TARGET
S = {}
for s in ("NQ", "ES", "GC", "SI", "PL"):
    g = Hn.load(s); sch = B.build(g)
    S[s] = {int(g.days[d]): v for d, v in sch.items()}; Hn._cache.clear()
allday = sorted(set().union(*[set(v) for v in S.values()]))
yr = np.array([int(str(np.datetime64(d, "D"))[:4]) for d in allday])
nq = np.array([[S[k][d][6] if d in S[k] else np.nan for d in allday] for k in S])
m = yr >= 2020
print("daily R correlation with NQ (2020+):", {k: round(float(np.corrcoef(np.nan_to_num(nq[0][m]), np.nan_to_num(nq[i][m]))[0, 1]), 2) for i, k in enumerate(S)})
UNIT = {"NQ": .1, "ES": .1, "GC": .1, "SI": .1, "PL": 1}
def run(s0, risks, cap=1450, maxd=120):
    a = Account(); dps = []
    for n in range(1, maxd + 1):
        i = s0 + n - 1
        if i >= len(allday): break
        d = allday[i]; pnl = mae = mfe = 0.0; mf = False; used = 0.0
        for k, rk in risks.items():
            x = S[k].get(d)
            if not x: continue
            rct, net, ma, mx, f, mult, _ = x; r_ = rk * mult
            q = int(r_ // rct); q = 1 if q == 0 and rct <= 1.5 * r_ else q
            q = min(q, int((4 - used) / UNIT[k]))
            if q <= 0: continue
            used += q * UNIT[k]; pnl += net * q; mae += ma * q; mfe += mx * q; mf = mf or f
        dp = 0.0
        if pnl or mae:
            need = START + TARGET - a.balance; best = max(dps) if dps else 0
            tp = min(max(need, best / .5 - (a.balance - START)) if best > 0 else need, cap)
            hit = mfe >= tp + 1
            if a.balance + min(mae, pnl) <= a.mll and (not hit or mf): return False, True, n
            dp = tp if hit else pnl; a.balance += dp
        dps.append(dp)
        if a.balance >= START + TARGET and max(dps) <= .5 * (a.balance - START): return True, False, n
        a.eod_update()
    return False, False, maxd
def ev(risks, y0=2020, y1=2026):
    st = np.where((yr >= y0) & (yr <= y1))[0]; res = [run(s, risks) for s in st]
    p = np.array([r[0] for r in res]); dd = np.array([r[2] for r in res])
    return p.mean(), np.median(dd[p])
for lab, rk in [("NQ $1000 alone", {"NQ": 1000}), ("NQ 700 + ES 300", {"NQ": 700, "ES": 300}),
                ("NQ 700 + GC 300", {"NQ": 700, "GC": 300}), ("NQ 700 + SI 300", {"NQ": 700, "SI": 300}),
                ("NQ 600 + GC 200 + SI 200", {"NQ": 600, "GC": 200, "SI": 200}),
                ("NQ 800 + GC 400", {"NQ": 800, "GC": 400}), ("NQ 500 GC 250 SI 250 PL 250", {"NQ": 500, "GC": 250, "SI": 250, "PL": 250})]:
    p, d = ev(rk); p2, d2 = ev(rk, 2024, 2026)
    print(f"{lab:30} 2020-26: pass {p*100:3.0f}% {d:3.0f}d | 2024-26: pass {p2*100:3.0f}% {d2:3.0f}d")
