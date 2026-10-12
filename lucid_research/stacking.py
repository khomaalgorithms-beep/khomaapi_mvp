"""'Stacking' plan: always one 50K eval running; each pass becomes a funded account (Safe or Standard) that keeps
trading in parallel, and a new eval is bought immediately. Fails -> buy the next eval. Max 5 funded at once (Lucid
household cap). Horizon 105 trading days (~5 months). Client keeps 90% of payouts. Starts every 5th day."""
import numpy as np, sys
from multiprocessing import Pool
sys.path.insert(0, "..")
import dashboard_data as DDm
from client_economics import funded
from lucid_sizes import SIZES
from app.orb_validated import DATA

def sim(s, end, L, A, risk_e, risk_f, tpk, pol):
    t = s; fees = 0.0; paid = 0.0; funded_end = []; n_funded = 0; pay_days = []
    while t < end - 1:
        active = [e for e in funded_end if e > t]
        if len(active) >= 5:
            t = min(active); continue
        fees += A["price"]
        ok, n = DDm.eval_run([L.get(x) for x in range(t, end)], A, risk_e, "eod")
        t += n
        if ok and t < end - 1:
            n_funded += 1
            m, pays, blown = funded([L.get(x) for x in range(t, end)], A, risk_f, tpk, pol)
            paid += 0.9 * sum(pays); funded_end.append(t + m)
    return fees, paid, n_funded

def job(a):
    pr, fmode, y0, y1, H = a[:5]; size = a[5] if len(a) > 5 else "50K"
    L, g = DDm.lg(); A = SIZES[size]; risk_e = DATA["presets"][size]["eod"][pr]["risk"]; risk_f = 0.12 * A["mll"]
    tpk, pol = (2.0, "E") if fmode == "safe" else (None, "A")
    st = np.where((g.year >= y0) & (g.year <= y1))[0][::5]; st = st[st + H < g.D]
    o = np.array([sim(s, s + H, L, A, risk_e, risk_f, tpk, pol) for s in st])
    net = o[:, 1] - o[:, 0]
    return a, dict(fees=o[:, 0].mean(), paid=o[:, 1].mean(), net=net.mean(), med=np.median(net), pos=(net > 0).mean(),
                   p10=np.percentile(net, 10), p25=np.percentile(net, 25), p75=np.percentile(net, 75), p90=np.percentile(net, 90),
                   nf=o[:, 2].mean(), zero=(o[:, 2] == 0).mean(), evals=(o[:, 0] / A["price"]).mean())

if __name__ == "__main__":
    jobs = [(p, f, y0, y1, 105) for p in ("cons", "norm", "aggr") for f in ("safe", "std") for (y0, y1) in ((2008, 2025), (2020, 2025), (2025, 2025))]
    with Pool(7) as pool: R = pool.map(job, jobs)
    for (y0, y1) in ((2008, 2025), (2020, 2025), (2025, 2025)):
        print(f"\n=== 50K stacking, 5 months, starts {y0}-{y1} ===")
        print(f"{'preset':5} {'funded':5} | evals  fees   payouts | NET avg  NET med  per-month | ahead | worst10% p25   p75   best10% | funded accts (none)")
        for a, o in R:
            if (a[2], a[3]) != (y0, y1): continue
            print(f"{a[0]:5} {a[1]:5} | {o['evals']:4.1f} ${o['fees']:5.0f} ${o['paid']:6.0f} | ${o['net']:6.0f} ${o['med']:6.0f}  ${o['net']/5:5.0f}/mo | {o['pos']*100:4.0f}% | ${o['p10']:6.0f} ${o['p25']:5.0f} ${o['p75']:5.0f} ${o['p90']:6.0f} | {o['nf']:.1f} ({o['zero']*100:.0f}% none)")
