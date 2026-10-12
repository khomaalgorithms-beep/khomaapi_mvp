"""Does a client's prop-firm spend get covered by payouts?  Full lifecycle, one account at a time, for 12 months:
buy eval (LucidFlex price w/ coupon, activation free) -> eval runs (real live rule, preset risk, consistency+TP cap)
-> fail: buy next eval | pass: funded (Safe or Standard) -> payouts (client keeps 90%) -> blow: buy next eval.
Started every 5th trading day 2008-2025 (and 2020-25 for 'good markets'). Reports fees, payouts, net, P(net>0),
P(net > $3,500 product price)."""
import numpy as np, sys
from multiprocessing import Pool
sys.path.insert(0, "..")
import dashboard_data as DDm, funded_safe as FS
from lucid_sizes import SIZES
from app.orb_validated import DATA
H = 250

def funded(seq, A, risk, tpk, pol):
    """funded_safe.run_kind logic, returning (days used, payouts gross, blown)."""
    a = DDm.DD(A, "eod"); qual = 0; cyc = 0.0; pays = []
    for n, L in enumerate(seq, 1):
        dp = 0.0; r = risk * (0.5 if (pol == "E" and pays) else 1.0)
        if L is not None:
            prof = a.bal - A["start"]
            cap = max(c for lvl, c in A["tiers"] if prof >= lvl) if prof >= 0 else A["tiers"][0][1]
            q = min(max(1, int(r // L["rct"])), cap)
            B = L["best"] * q; W = L["worst"] * q; fl = a.path_floor(B)
            br = np.where(a.bal + W <= fl)[0]; ib = br[0] if len(br) else 10**9
            ih = 10**9; tp = 0.0
            if tpk:
                tp = tpk * L["rct"] * q; hit = np.where(B >= tp + 0.5 * q)[0]; ih = hit[0] if len(hit) else 10**9
            if ib <= ih and ib < 10**9:
                return n, pays, True
            dp = tp if ih < 10**9 else L["net"] * q
        a.bal += dp; cyc += dp
        if dp >= A["qual"]: qual += 1
        a.after_day(None)
        prof = a.bal - A["start"]
        small = pol == "B" or (pol in ("D", "E") and not pays)
        trig = (prof >= 1000) if small else (prof >= A["mll"] + 300 if not pays else a.bal - a.floor >= 1000 + 300 + 4 * r)
        if qual >= 5 and cyc > 0 and trig:
            amt = min(500 if small else 1000, 0.5 * max(prof, 0), A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.floor = A["start"] + 100
                pays.append(amt); qual = 0; cyc = 0.0
                if len(pays) >= 5:
                    return n, pays, False
    return len(seq), pays, False

def job(a):
    size, pr, fmode, y0, y1 = a
    L, g = DDm.lg(); A = SIZES[size]; risk_e = DATA["presets"][size]["eod"][pr]["risk"]; risk_f = 0.12 * A["mll"]
    tpk, pol = (2.0, "E") if fmode == "safe" else (None, "A")
    starts = np.where((g.year >= y0) & (g.year <= y1))[0][::5]; starts = starts[starts + H < g.D]
    out = []
    for s in starts:
        t = s; end = s + H; fees = 0.0; paid = 0.0; evals = 0; first = None
        while t < end - 1:
            fees += A["price"]; evals += 1
            ok, n = DDm.eval_run([L.get(x) for x in range(t, end)], A, risk_e, "eod")
            t += n
            if not ok or t >= end - 1:
                continue
            m, pays, blown = funded([L.get(x) for x in range(t, end)], A, risk_f, tpk, pol)
            if pays and first is None: first = t - s
            paid += 0.9 * sum(pays); t += m
            if not blown and t < end - 1:     # 5 payouts reached -> account goes live; client restarts the cycle
                pass
        out.append((fees, paid, paid - fees, evals, first))
    o = np.array([[x[0], x[1], x[2], x[3]] for x in out]); f = [x[4] for x in out if x[4] is not None]
    return a, dict(fees=o[:, 0].mean(), paid=o[:, 1].mean(), net_mean=o[:, 2].mean(), net_med=float(np.median(o[:, 2])),
                   p_pos=float((o[:, 2] > 0).mean()), p_3500=float((o[:, 2] > 3500).mean()), p_any=float((o[:, 1] > 0).mean()),
                   evals=o[:, 3].mean(), worst=float(np.percentile(o[:, 2], 10)), best=float(np.percentile(o[:, 2], 90)),
                   first_med=float(np.median(f)) if f else None)

if __name__ == "__main__":
    jobs = [(s, p, f, y0, y1) for s in SIZES for p in ("cons", "norm", "aggr") for f in ("safe", "std") for (y0, y1) in ((2008, 2025), (2020, 2025))]
    with Pool(7) as pool: R = pool.map(job, jobs)
    for (y0, y1) in ((2008, 2025), (2020, 2025)):
        print(f"\n===== first 12 months, years started {y0}-{y1} (client keeps 90%; fees = LucidFlex evals w/ coupon) =====")
        print(f"{'size':5} {'preset':5} {'funded':5} | {'evals':>5} {'fees':>7} {'payouts':>8} {'NET mean':>9} {'NET med':>8} | {'any pay':>7} {'net>0':>6} {'net>$3.5k':>9} | {'bad 10%':>8} {'good 10%':>9} | 1st pay d")
        for a, o in R:
            if (a[3], a[4]) != (y0, y1): continue
            print(f"{a[0]:5} {a[1]:5} {a[2]:5} | {o['evals']:5.1f} ${o['fees']:6.0f} ${o['paid']:7.0f} ${o['net_mean']:8.0f} ${o['net_med']:7.0f} | {o['p_any']*100:6.0f}% {o['p_pos']*100:5.0f}% {o['p_3500']*100:8.0f}% | ${o['worst']:7.0f} ${o['best']:8.0f} | {o['first_med'] or 0:.0f}")
