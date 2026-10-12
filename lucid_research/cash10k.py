"""$10,000 live cash account, live rule (10:30 with-trend, stop 0.2 ATR, MNQ, real costs). Risk per trade = f x current
equity (compounding) or fixed $; whole micros, skip if 1 micro exceeds 1.5x the risk budget. Find the largest risk
with max drawdown <= 25% over (a) 2008-26 and (b) the last 10 years. Report monthly $, CAGR, worst year, losing months."""
import numpy as np, dashboard_data as DDm
L, g = DDm.lg()
def run(f, lo, fixed=False, start=10_000.0):
    lo_i = np.searchsorted(g.days, np.datetime64(lo).astype(np.int64))
    eq = start; peak = start; mdd = 0.0; curve = []; months = {}
    for d in sorted(x for x in L if x >= lo_i):
        v = L[d]; risk = f if fixed else f * eq
        q = int(risk // v["rct"]); q = 1 if q == 0 and v["rct"] <= 1.5 * risk else q
        pnl = q * v["net"]; eq += pnl; peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
        m = str(g.dates[d])[:7]; months[m] = months.get(m, 0.0) + pnl; curve.append((g.year[d], eq))
        if eq <= 0: return dict(dead=True)
    yrs = (g.days[max(L)] - g.days[lo_i]) / 365.25
    yr = {}
    for y, e in curve: yr[y] = e
    ys = sorted(yr); prev = start; yret = {}
    for y in ys: yret[y] = yr[y] / prev - 1; prev = yr[y]
    mv = np.array(list(months.values()))
    return dict(dead=False, end=eq, mdd=mdd, cagr=(eq / start) ** (1 / yrs) - 1, yrs=yrs, avg_m=(eq - start) / (yrs * 12),
                lose_m=np.mean(mv < 0), worst_y=min(yret.values()), worst_y_n=min(yret, key=yret.get), yret=yret)
for lo, name in (("2008-03-01", "FULL 2008-26"), ("2016-10-10", "LAST 10y")):
    print(f"\n=== {name}: $10,000 start, compounding risk = % of equity ===")
    best = None
    for f in (0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02, 0.025, 0.03):
        r = run(f, lo)
        if r["dead"]: print(f"risk {f*100:.2f}%: account wiped"); continue
        ok = r["mdd"] <= 0.25; best = f if ok else best
        print(f"risk {f*100:5.2f}% | end ${r['end']:>10,.0f} | CAGR {r['cagr']*100:5.1f}% | avg/month ${r['avg_m']:6,.0f} | max DD {r['mdd']*100:4.1f}% {'OK' if ok else '  '} | losing months {r['lose_m']*100:.0f}% | worst year {r['worst_y']*100:+.0f}% ({r['worst_y_n']})")
    if best:
        r = run(best, lo); print(f"-> largest risk within 25% DD: {best*100:.2f}% of equity | yearly: " + " ".join(f"{y}:{v*100:+.0f}%" for y, v in r['yret'].items()))
# last 12 months at those sizes
for f in (0.005, 0.01, 0.02):
    r = run(f, "2025-10-10"); print(f"last 12 months at {f*100:.1f}%: ${r['end']-10000:+,.0f} ({(r['end']/10000-1)*100:+.1f}%), max DD {r['mdd']*100:.1f}%")
