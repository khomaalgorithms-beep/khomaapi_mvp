"""Funded 'lock-then-skim': request a fixed small payout only once profit >= MLL + buffer (drawdown locked first)."""
import numpy as np, json
from multiprocessing import Pool
import funded_fast as F
from lucid_sizes import SIZES, Acct

def funded(seq, A, risk, day_tp, trig, amt_fixed):
    a = Acct(A); qual = 0; cyc = 0.0; pays = []; first = None
    for n, L in enumerate(seq, 1):
        dp = 0.0
        if L is not None:
            prof = a.bal - A["start"]
            cap = max(c for lvl, c in A["tiers"] if prof >= lvl) if prof >= 0 else A["tiers"][0][1]
            q = min(max(1, int(risk * L["mult"] // L["rct"])), cap)
            B = L["best"] * q; W = L["worst"] * q
            br = np.where(a.bal + W <= a.mll)[0]
            hit = np.where(B >= day_tp + 0.5 * q)[0] if day_tp else np.array([], int)
            ib = br[0] if len(br) else 10**9; ih = hit[0] if len(hit) else 10**9
            if ib <= ih and ib < 10**9: return pays, True, first
            dp = day_tp if ih < 10**9 else L["net"] * q
        a.bal += dp; cyc += dp
        if dp >= A["qual"]: qual += 1
        a.eod(); prof = a.bal - A["start"]
        if qual >= 5 and cyc > 0 and prof >= trig:
            amt = min(amt_fixed, 0.5 * prof, A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.mll = A["start"] + 100
                pays.append(amt); first = first or n; qual = 0; cyc = 0.0
                if len(pays) >= 5: return pays, False, first
    return pays, False, first

def job(args):
    size, risk, tp, buf, amt, period = args
    lg, g = F.lg_(); A = SIZES[size]; rng = np.random.default_rng(4)
    trig = A["mll"] + buf
    if period == "recent":
        idx = np.where(g.days >= np.datetime64("2025-10-10").astype(np.int64))[0]; paths = []
        for _ in range(400):
            p = []
            while len(p) < 250:
                s = rng.integers(0, len(idx) - 10); p += list(idx[s:s + 10])
            paths.append(p[:250])
    else:
        y0, y1 = {"design": (2020, 2023), "check": (2024, 2025)}[period]
        paths = [list(range(s, min(s + 250, g.D))) for s in np.where((g.year >= y0) & (g.year <= y1))[0][::3]]
    R = [funded([lg.get(d) for d in p], A, risk, tp, trig, amt) for p in paths]
    fs = np.array([r[2] if r[2] else np.nan for r in R])
    return dict(size=size, risk=risk, tp=tp, buf=buf, amt=amt, period=period, cash=float(np.mean([0.9*sum(r[0]) for r in R])),
                p1=float(np.mean([len(r[0]) > 0 for r in R])), within60=float(np.mean(fs <= 60)), med=float(np.nanmedian(fs)),
                bb=float(np.mean([r[1] and r[2] is None for r in R])), blow=float(np.mean([r[1] for r in R])))

if __name__ == "__main__":
    jobs = []
    for s, A in SIZES.items():
        for risk in (140, 280):
            for tp in (None, 1.5 * A["qual"] * 2):
                for buf in (300, 600):
                    for amt in (500, 1000):
                        for p in ("design", "check", "recent"):
                            jobs.append((s, risk, tp, buf, amt, p))
    with Pool(6) as pool: rows = pool.map(job, jobs)
    json.dump(rows, open("results/funded_hybrid.json", "w"), default=float)
    for s in SIZES:
        R = {(r["risk"], r["tp"], r["buf"], r["amt"]): {} for r in rows if r["size"] == s}
        for r in rows:
            if r["size"] == s: R[(r["risk"], r["tp"], r["buf"], r["amt"])][r["period"]] = r
        print(f"\n{s}: risk | TP | payout when profit>=MLL+buf | amount  ->  design / check / recent: 1st-pay median, within60, >=1 pay, blown-before-1st, yr1 cash, blown-in-yr")
        for k, v in sorted(R.items(), key=lambda kv: -kv[1]["design"]["cash"]):
            print(f"  {k[0]:3.0f} {str(k[1]):>5} +{k[2]:3d} ${k[3]:4d} | " + " | ".join(f"{v[p]['med']:3.0f}d {v[p]['within60']*100:3.0f}% {v[p]['p1']*100:3.0f}% bb{v[p]['bb']*100:3.0f}% ${v[p]['cash']:5.0f} bl{v[p]['blow']*100:3.0f}%" for p in ("design","check","recent")))
