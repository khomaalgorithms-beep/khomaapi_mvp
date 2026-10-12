"""Live cash: monthly $ for LIVE / ORB / BOTH at balance B and max-drawdown budget, sized so each setup's worst
historical drawdown fits the budget (min 1 MNQ micro). Full history and last 10 years."""
import numpy as np, orb_combo as OC, orb_lab as OL
OC.LV, OC.OR = OC.live_legs(), OC.orb_legs(); g = OL.G("NQ")
a = {d: v["net"] / v["rct"] for d, v in OC.LV.items()}; b = {d: v["net"] / v["rct"] for d, v in OC.OR.items()}
days = np.array(sorted(set(a) | set(b)))
def series(m): return np.array([{"LIVE": a.get(d, 0), "ORB": b.get(d, 0), "BOTH": a.get(d, 0) + b.get(d, 0)}[m] for d in days])
for lab, lo in (("FULL 2008-26", "2008-03-01"), ("LAST 10y", "2016-10-10")):
    k = g.days[days] >= np.datetime64(lo).astype(np.int64)
    print(f"\n=== {lab} ===")
    for m in ("LIVE", "ORB", "BOTH"):
        r = series(m)[k]; eq = np.cumsum(r); dd = -(eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min()
        months = len(set(str(g.dates[d])[:7] for d in days[k])); per_m = r.sum() / months
        mo = {}
        for d, v in zip(days[k], r): mm = str(g.dates[d])[:7]; mo[mm] = mo.get(mm, 0) + v
        lose = np.mean(np.array(list(mo.values())) < 0) * 100
        line = f"  {m:4} worstDD {dd:5.1f}R | {per_m:.2f}R/month | losing months {lose:.0f}% |"
        for bal in (25_000, 50_000, 100_000):
            risk = bal * 0.25 / dd; ok = risk * 1.5 >= 122 / (2 if m == 'BOTH' else 1) * (1 if m != 'BOTH' else 2)
            line += f" ${bal//1000}k@25%: {'$'+format(per_m*risk,',.0f')+'/mo' if risk >= 122/1.5 else 'too small'}"
        print(line)
