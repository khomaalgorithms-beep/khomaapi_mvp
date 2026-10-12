"""Funded-stage playbook for LucidFlex 50K (same NQ chain signals, exact minute replay, real costs).
Start a funded account on every day of the period and run 250 trading days (~1 year) or to 5 payouts / breach.
Playbooks = sizing rule x daily take-profit x payout trigger. Objective: average trader cash in year 1,
with P(>=1 payout) and blow rate. Design 2020-23 starts, check 2024-25 starts, plus last-12-months bootstrap."""
import sys, itertools, numpy as np
from multiprocessing import Pool
import harness as Hn, stress as T
from lucid_sizes import SIZES, Acct

LG = None


def setup():
    global LG
    if LG is None:
        LG = T.legs(dict(comm=1.0, slip=1))
    return LG, T.G


def funded(seq, A, rule, day_tp, trig):
    a = Acct(A); qual = 0; cyc = 0.0; pays = []
    for L in seq:
        dp = 0.0
        if L is not None:
            prof = a.bal - A["start"]
            cap = max(c for lvl, c in A["tiers"] if prof >= lvl) if prof >= 0 else A["tiers"][0][1]
            cushion = a.bal - a.mll
            if rule[0] == "fixed":
                risk = rule[1]
            elif rule[0] == "twophase":                       # small until lock, then fraction of cushion
                risk = rule[1] if not a.locked else max(rule[1], rule[2] * cushion)
            else:                                             # always a fraction of distance to the floor
                risk = max(140, rule[1] * cushion)
            q = int(risk * L["mult"] // L["rct"]); q = max(q, 1); q = min(q, cap)
            B = L["best"] * q; W = L["worst"] * q
            br = np.where(a.bal + W <= a.mll)[0]
            hit = np.where(B >= day_tp + 0.5 * q)[0] if day_tp else np.array([], int)
            ib = br[0] if len(br) else 10**9; ih = hit[0] if len(hit) else 10**9
            if ib <= ih and ib < 10**9:
                return pays, True
            dp = day_tp if ih < 10**9 else L["net"] * q
        a.bal += dp; cyc += dp
        if dp >= A["qual"]:
            qual += 1
        a.eod()
        prof = a.bal - A["start"]
        if qual >= 5 and cyc > 0 and prof >= trig:
            amt = min(0.5 * prof, A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.mll = A["start"] + 100
                pays.append(amt); qual = 0; cyc = 0.0
                if len(pays) >= 5:
                    return pays, False
    return pays, False


RULES = {"fixed 1 MNQ": ("fixed", 140), "fixed ~2 MNQ": ("fixed", 300), "fixed ~3 MNQ": ("fixed", 450),
         "2-phase: 1 MNQ then 15% cushion": ("twophase", 140, 0.15), "2-phase: 1 MNQ then 25% cushion": ("twophase", 140, 0.25),
         "cushion 10%": ("cushion", 0.10), "cushion 20%": ("cushion", 0.20)}
TPS = [None, 300, 600]
TRIGS = [1_000, 2_000, 3_000, 4_000]


def job(args):
    rname, tp, trig, period = args
    lg, g = setup(); A = SIZES["50K"]
    rng = np.random.default_rng(9)
    if period == "recent":
        idx = np.where(g.days >= np.datetime64("2025-10-10").astype(np.int64))[0]
        paths = []
        for _ in range(800):
            p = []
            while len(p) < 250:
                s = rng.integers(0, len(idx) - 10); p += list(idx[s:s + 10])
            paths.append(p[:250])
    else:
        y0, y1 = {"design": (2020, 2023), "check": (2024, 2025)}[period]
        paths = [list(range(s, min(s + 250, g.D))) for s in np.where((g.year >= y0) & (g.year <= y1))[0]]
    res = [funded([lg.get(d) for d in p], A, RULES[rname], tp, trig) for p in paths]
    cash = np.array([0.9 * sum(p) for p, _ in res]); n1 = np.array([len(p) > 0 for p, _ in res]); bl = np.array([b for _, b in res])
    npay = np.array([len(p) for p, _ in res])
    return dict(rule=rname, tp=tp, trig=trig, period=period, cash=cash.mean(), p1=n1.mean(), blow=bl.mean(), npay=npay.mean())


if __name__ == "__main__":
    period = sys.argv[1]
    jobs = [(r, tp, tr, period) for r in RULES for tp in TPS for tr in TRIGS]
    with Pool(5) as pool:
        rows = pool.map(job, jobs)
    rows.sort(key=lambda r: -r["cash"])
    print(f"=== 50K funded, year-1, {period} ===  (sorted by avg trader cash)")
    print(f"{'sizing':34} {'day TP':>6} {'payout@':>7} | {'avg cash':>8} {'>=1 pay':>7} {'payouts':>7} {'blow':>5}")
    for r in rows[:25] + [x for x in rows if x['rule'] == 'fixed 1 MNQ' and x['tp'] is None and x['trig'] == 3000]:
        print(f"{r['rule']:34} {str(r['tp']):>6} {r['trig']:7d} | ${r['cash']:7.0f} {r['p1']*100:6.0f}% {r['npay']:7.2f} {r['blow']*100:4.0f}%")
    import json; json.dump(rows, open(f"results/funded_opt_{period}.json", "w"), default=float)
