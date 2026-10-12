"""FUNDED SAFE mode search (pre-registered, LucidFlex 50K funded, EOD trailing, real costs).
Grid (fixed before running): risk 4/6/8/10/12% of MLL x high-vol filter off/on x take-profit none/1R/1.5R/2R
x payout policy A ($1,000 at profit >= MLL+$300) / B ($500 every time, as soon as allowed) / D (first $500 asap, then A)
/ E (first $500 asap, then A with risk halved). Full year simulated (up to 5 payouts).
SELECTION (in-sample = funded accounts started 2008-2019 only): maximise P(first payout within 120 trading days)
SUBJECT TO in-sample P(account alive after 1 year or 5 payouts) >= the current funded mode's; tie-break year-1 cash. VALIDATION (never seen): starts 2020-2025, and starts 2025 (current)."""
import numpy as np, itertools, json, sys
from multiprocessing import Pool
import harness as Hn, stress as T, dashboard_data as DDm
from lucid_sizes import SIZES

_S = {}
def setup():
    if not _S:
        L, g = DDm.lg(); v = g.atr14 / g.px; V = set()
        for d in L:
            if d >= 250:
                w = v[d-250:d]; w = w[np.isfinite(w)]
                if len(w) and np.isfinite(v[d]) and v[d] >= np.median(w): V.add(d)
        _S.update(L=L, g=g, V=V)
    return _S

def run(seq, A, risk, tpk, pol, horizon=250):
    a = DDm.DD(A, "eod"); qual = 0; cyc = 0.0; first = None; pays = []
    for n, L in enumerate(seq, 1):
        dp = 0.0
        r = risk * (0.5 if (pol == "E" and pays) else 1.0)
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
                return "blown", first, pays
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
                pays.append(amt); first = first or n; qual = 0; cyc = 0.0
                if len(pays) >= 5:
                    return "done5", first, pays
    return "alive", first, pays

def run_kind(seq, A, risk, tpk, pol, kind="eod"):
    a = DDm.DD(A, kind); qual = 0; cyc = 0.0; first = None; pays = []
    for n, L in enumerate(seq, 1):
        dp = 0.0
        r = risk * (0.5 if (pol == "E" and pays) else 1.0)
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
                return "blown", first, pays
            dp = tp if ih < 10**9 else L["net"] * q
        a.bal += dp; cyc += dp
        if dp >= A["qual"]: qual += 1
        a.bal_start = a.bal - dp; a.after_day(None if kind != "intraday" or L is None else B[:(ih + 1 if ih < 10**9 else len(B))])
        prof = a.bal - A["start"]
        small = pol == "B" or (pol in ("D", "E") and not pays)
        trig = (prof >= 1000) if small else (prof >= A["mll"] + 300 if not pays else a.bal - a.floor >= 1000 + 300 + 4 * r)
        if qual >= 5 and cyc > 0 and trig:
            amt = min(500 if small else 1000, 0.5 * max(prof, 0), A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.floor = A["start"] + 100
                pays.append(amt); first = first or n; qual = 0; cyc = 0.0
                if len(pays) >= 5:
                    return "done5", first, pays
    return "alive", first, pays

def job(cfg):
    S = setup(); L, g, V = S["L"], S["g"], S["V"]
    size, frac, vol, tpk, pol = cfg; A = SIZES[size]; risk = frac * A["mll"]
    LL = {d: L[d] for d in V} if vol else L
    out = {}
    for lab, a, b in (("ins", "2008-03-01", "2019-12-31"), ("oos", "2020-01-01", "2025-10-09"), ("now", "2025-01-01", "2025-10-09")):
        st = np.where((g.days >= np.datetime64(a).astype(np.int64)) & (g.days <= np.datetime64(b).astype(np.int64)))[0][::2]
        R = [run([LL.get(x) for x in range(s, min(s + 250, g.D))], A, risk, tpk, pol) for s in st]
        k = np.array([r[0] for r in R]); f = np.array([r[1] or 10**9 for r in R])
        out[lab] = dict(p120=float(np.mean(f <= 120)), paid=float(np.mean(f < 10**9)), blown=float(np.mean(k == "blown")),
                        bb=float(np.mean((k == "blown") & (f == 10**9))), alive=float(np.mean(k != "blown")),
                        cash=float(np.mean([0.9 * sum(r[2]) for r in R])),
                        med=float(np.median(f[f < 10**9])) if (f < 10**9).any() else None)
    return cfg, out

if __name__ == "__main__":
    size = sys.argv[1] if len(sys.argv) > 1 else "50K"
    grid = list(itertools.product([size], [0.04, 0.06, 0.08, 0.10, 0.12], [False, True], [None, 1.0, 1.5, 2.0], ["A", "B", "D", "E"]))
    with Pool(7) as pool: R = pool.map(job, grid)
    base = [x for x in R if x[0][1:] == (0.12, False, None, "A")][0]
    ok = lambda x: x[1]["ins"]["alive"] >= base[1]["ins"]["alive"]
    R.sort(key=lambda x: (not ok(x), -x[1]["ins"]["p120"], -x[1]["ins"]["cash"]))
    f = lambda o: f"pay<=120d {o['p120']*100:3.0f}% | paid {o['paid']*100:3.0f}% | blown-before-pay {o['bb']*100:3.0f}% | alive@1y {o['alive']*100:3.0f}% | med {o['med'] or 0:3.0f}d | cash ${o['cash']:,.0f}"
    print("rank by IN-SAMPLE (2008-19) P(paid within 120d):")
    for cfg, o in R[:10]:
        print(f"{'OK ' if ok((cfg,o)) else 'XX '}risk {cfg[1]:.2f} vol {str(cfg[2]):5} tp {str(cfg[3]):4} pol {cfg[4]} \n   INS 08-19 {f(o['ins'])}\n   OOS 20-25 {f(o['oos'])}\n   2025      {f(o['now'])}")
    print("\nCURRENT funded mode (12%, no filter, hold, pol A):")
    print(f"   INS 08-19 {f(base[1]['ins'])}\n   OOS 20-25 {f(base[1]['oos'])}\n   2025      {f(base[1]['now'])}")
    oos_rank = sorted(R, key=lambda x: -x[1]["oos"]["p120"])
    pos = [i for i, x in enumerate(oos_rank) if x[0] == R[0][0]][0]
    print(f"\nselected config's OOS rank: {pos+1} of {len(R)}")
    json.dump([[list(c), o] for c, o in R], open(f"funded_safe_{size}.json", "w"))
