"""Decision matrix: account size x phase x setup (LIVE 10:30 / ORB+PDL / BOTH) on LucidFlex (EOD), 2008-2026."""
import sys, json, numpy as np
sys.path.insert(0, "..")
from multiprocessing import Pool
import orb_combo as OC, orb_lab as OL, dashboard_data as DDm
from lucid_sizes import SIZES
WIN = {"long run": ("2008-03-01", "2026-10-09"), "2020-26": ("2020-01-01", "2026-10-09"), "last 12m": ("2025-10-12", "2026-10-09")}
def init():
    OC.LV, OC.OR = OC.live_legs(), OC.orb_legs()
def eval_job(a):
    size, mode, frac = a; A = SIZES[size]; g = OL.G("NQ")
    E = OC.build(mode, frac * A["mll"]); out = {}
    for w, (x, y) in WIN.items():
        st = np.where((g.days >= np.datetime64(x).astype(np.int64)) & (g.days <= np.datetime64(y).astype(np.int64)))[0]
        res = [DDm.eval_run([E.get(i) for i in range(s, min(s + 180, g.D))], A, 1.0, "eod") for s in st]
        p = np.array([r[0] for r in res]); d = np.array([r[1] for r in res])
        out[w] = (float(p.mean()), float(np.median(d[p])) if p.any() else 0.0)
    return a, out
def funded(seqF, seqH, pol, A, rhalf):
    a = DDm.DD(A, "eod"); qual = 0; cyc = 0.0; pays = []; first = None
    for n in range(len(seqF)):
        L = (seqH if (pol == "E" and pays) else seqF)[n]; dp = 0.0
        if L is not None:
            if len(np.where(a.bal + L["worst"] <= a.path_floor(L["best"]))[0]): return pays, True, first
            dp = L["net"]
        a.bal += dp; cyc += dp
        if dp >= A["qual"]: qual += 1
        a.after_day(None); prof = a.bal - A["start"]; small = pol == "E" and not pays
        trig = (prof >= 1000) if small else (prof >= A["mll"] + 300 if not pays else a.bal - a.floor >= 1300 + 4 * rhalf)
        if qual >= 5 and cyc > 0 and trig:
            amt = min(500 if small else 1000, 0.5 * max(prof, 0), A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.floor = A["start"] + 100; pays.append(amt); first = first or n + 1; qual = 0; cyc = 0.0
                if len(pays) >= 5: return pays, False, first
    return pays, False, first
def funded_job(a):
    size, mode, frac = a; A = SIZES[size]; g = OL.G("NQ"); r = frac * A["mll"]; out = {}
    st = np.where((g.year >= 2008) & (g.year <= 2025))[0][::2]
    for nm, tp, pol in (("Standard", None, "A"), ("Safe", 2.0, "E")):
        F, Fh = OC.build(mode, r, tp, True), OC.build(mode, r / 2, tp, True)
        R = [funded([F.get(i) for i in range(s, min(s + 250, g.D))], [Fh.get(i) for i in range(s, min(s + 250, g.D))], pol, A, r / 2) for s in st]
        fs = [x[2] for x in R if x[2]]
        out[nm] = dict(paid=float(np.mean([len(x[0]) > 0 for x in R])), blown=float(np.mean([x[1] and not x[0] for x in R])),
                       cash=float(np.mean([0.9 * sum(x[0]) for x in R])), first=float(np.median(fs)) if fs else 0.0)
    return a, out
if __name__ == "__main__":
    EJ = [(s, m, f) for s in SIZES for m, fs in (("LIVE", (0.2, 0.3, 0.4)), ("ORB", (0.2, 0.3, 0.4)), ("BOTH", (0.15, 0.2, 0.3))) for f in fs]
    FJ = [(s, m, f) for s in SIZES for m, f in (("LIVE", 0.12), ("ORB", 0.12), ("BOTH", 0.06), ("BOTH", 0.08))]
    with Pool(8, initializer=init) as pool:
        ER = dict(pool.map(eval_job, EJ)); FR = dict(pool.map(funded_job, FJ))
    json.dump(dict(eval={json.dumps(k): v for k, v in ER.items()}, funded={json.dumps(k): v for k, v in FR.items()}), open("results/orb_matrix.json", "w"))
    for s in SIZES:
        mll = SIZES[s]["mll"]
        print(f"\n================ {s} (max loss ${mll:,}) ================")
        print("EVALUATION  (risk per trade PER strategy) | pass long run / 2020-26 / last 12m | median days")
        for (sz, m, f), o in ER.items():
            if sz != s: continue
            print(f"  {m:4} ${f*mll:5.0f} | {o['long run'][0]*100:3.0f}% / {o['2020-26'][0]*100:3.0f}% / {o['last 12m'][0]*100:3.0f}% | {o['long run'][1]:3.0f}d")
        print("FUNDED (2008-25 starts, 1 year) | Standard: paid / blown-before-pay / yr-1 $ / 1st pay days || Safe: same")
        for (sz, m, f), o in FR.items():
            if sz != s: continue
            a, b = o["Standard"], o["Safe"]
            print(f"  {m:4} ${f*mll:5.0f} | {a['paid']*100:3.0f}% / {a['blown']*100:3.0f}% / ${a['cash']:5.0f} / {a['first']:3.0f}d || {b['paid']*100:3.0f}% / {b['blown']*100:3.0f}% / ${b['cash']:5.0f} / {b['first']:3.0f}d")
