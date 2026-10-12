"""LucidFlex 25K / 50K / 100K / 150K: eval pass + funded payouts for the NQ fast-pass logic, exact replay.
Rules (Lucid help center + lucidtrading.com, read 2026-10-11):
  target / MLL (EOD trailing, locks at start+$100 once EOD balance > start+MLL+$100) / max eval size / price w coupon
  funded scaling tiers, payout: 5 days >= min daily profit, request min $500, max min(50% profit, cap), 90/10.
Scenarios: RAW (no costs), REAL (Lucid $1.00 RT/MNQ + 1 tick), BAD ($2 + 4 ticks + 30% missed + 1 min late),
WORST ($3 + 8 ticks + TP needs 4 ticks), RECENT (block bootstrap of the last 12 months, real costs)."""
import sys, numpy as np
from multiprocessing import Pool
import harness as Hn, stress as T

SIZES = {
    "25K":  dict(start=25_000, target=1_250, mll=1_000, eval_micros=20, price=50.30, reset=50,
                 tiers=[(0, 10), (1_000, 20)], qual=100, pay_max=1_000),
    "50K":  dict(start=50_000, target=3_000, mll=2_000, eval_micros=40, price=90.20, reset=90,
                 tiers=[(0, 20), (1_000, 30), (2_000, 40)], qual=150, pay_max=2_000),
    "100K": dict(start=100_000, target=6_000, mll=3_000, eval_micros=60, price=170.60, reset=170,
                 tiers=[(0, 30), (1_000, 40), (2_000, 50), (3_000, 60)], qual=200, pay_max=2_500),
    "150K": dict(start=150_000, target=9_000, mll=4_500, eval_micros=100, price=250.40, reset=250,
                 tiers=[(0, 40), (1_000, 50), (2_000, 60), (3_000, 80), (4_500, 100)], qual=250, pay_max=3_000),
}
SCEN = {
    "RAW (no costs)":            dict(comm=0.0, slip=0),
    "REAL (Lucid comm + 1 tick)": dict(comm=1.0, slip=1),
    "BAD (+4 ticks, 30% missed, 1m late)": dict(comm=2.0, slip=4, skip=0.3, delay=1, seed=1),
    "WORST ($3 + 8 ticks + TP 4 ticks)": dict(comm=3.0, slip=8, through=4),
    "RECENT 12 months (real costs)": dict(comm=1.0, slip=1, recent=True),
}


class Acct:
    def __init__(s, A):
        s.A = A; s.bal = A["start"]; s.peak = s.bal; s.mll = s.bal - A["mll"]; s.locked = False
    def eod(s):
        s.peak = max(s.peak, s.bal)
        if s.locked or s.peak > s.A["start"] + s.A["mll"] + 100:
            s.locked = True; s.mll = s.A["start"] + 100
        else:
            s.mll = s.peak - s.A["mll"]


def size_q(L, risk, cap, min1=False):
    q = int(risk * L["mult"] // L["rct"]); q = 1 if q == 0 and (min1 or L["rct"] <= 1.5 * risk * L["mult"]) else q
    return min(q, cap)


def eval_run(seq, A, risk, cap_day, through):
    a = Acct(A); dps = []
    for n, L in enumerate(seq, 1):
        dp = 0.0
        if L is not None:
            q = size_q(L, risk, A["eval_micros"])
            if q > 0:
                B = L["best"] * q; W = L["worst"] * q
                need = A["start"] + A["target"] - a.bal; best = max(dps) if dps else 0.0
                tp = min(max(need, best / .5 - (a.bal - A["start"])) if best > 0 else need, cap_day)
                br = np.where(a.bal + W <= a.mll)[0]; hit = np.where(B >= tp + through * 0.5 * q)[0]
                ib = br[0] if len(br) else 10**9; ih = hit[0] if len(hit) else 10**9
                if ib <= ih and ib < 10**9:
                    return False, n
                dp = tp if ih < 10**9 else L["net"] * q
        a.bal += dp; dps.append(dp)
        if a.bal >= A["start"] + A["target"] and max(dps) <= .5 * (a.bal - A["start"]):
            return True, n
        a.eod()
    return False, len(seq)


def funded_run(seq, A, risk):
    a = Acct(A); qual = 0; cyc = 0.0; pays = []; first = None
    for n, L in enumerate(seq, 1):
        dp = 0.0
        if L is not None:
            prof = a.bal - A["start"]
            cap = max(c for lvl, c in A["tiers"] if prof >= lvl) if prof >= 0 else A["tiers"][0][1]
            q = size_q(L, risk, cap, min1=True)
            if q > 0:
                if a.bal + min(L["worst"].min() * q, L["net"] * q) <= a.mll:
                    return pays, True, first
                dp = L["net"] * q
        a.bal += dp; cyc += dp
        if dp >= A["qual"]:
            qual += 1
        a.eod()
        prof = a.bal - A["start"]
        if qual >= 5 and cyc > 0 and prof >= 1.5 * A["pay_max"]:
            amt = min(0.5 * prof, A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.mll = A["start"] + 100
                pays.append(amt); first = first or n; qual = 0; cyc = 0.0
                if len(pays) >= 5:
                    return pays, False, first
    return pays, False, first


def one(job):
    size, sname = job
    A = SIZES[size]; sc = SCEN[sname]
    lg = T.legs({k: v for k, v in sc.items() if k != "recent"}); g = T.G
    risk_e = 0.5 * A["mll"]; cap_day = 0.48 * A["target"]; risk_f = max(0.08 * A["mll"], 1.0)
    through = sc.get("through", 1)
    rng = np.random.default_rng(3)
    paths = []
    if sc.get("recent"):
        idx = np.where(g.days >= np.datetime64("2025-10-10").astype(np.int64))[0]
        for _ in range(1500):
            p = []
            while len(p) < 620:
                s = rng.integers(0, len(idx) - 10); p += list(idx[s:s + 10])
            paths.append(p)
    else:
        for s in np.where((g.year >= 2020) & (g.year <= 2025))[0]:
            paths.append(list(range(s, min(s + 620, g.D))))
    P = []; D = []; F = []; C = []; FB = []; FP = []
    for p in paths:
        seq = [lg.get(d) for d in p]
        ok, n = eval_run(seq[:120], A, risk_e, cap_day, through)
        P.append(ok); D.append(n)
        if ok:
            pays, blew, first = funded_run(seq[n:n + 500], A, risk_f)
            F.append(len(pays) > 0); C.append(0.9 * sum(pays)); FB.append(blew); FP.append(first)
    P = np.array(P); D = np.array(D)
    pp = P.mean(); pf = np.mean(F) if F else 0; cash = np.mean(C) if C else 0
    cut = np.datetime64("2025-10-10").astype(np.int64) if sc.get("recent") else None
    lgl = [v for d, v in lg.items() if (g.days[d] >= cut if cut is not None else g.year[d] >= 2020)]
    net = np.array([v["net"] for v in lgl]); PF = net[net > 0].sum() / -net[net < 0].sum()
    return dict(size=size, scen=sname, PF=PF, pass_=pp, days=np.median(D[P]) if P.any() else np.nan,
                d75=np.percentile(D[P], 75) if P.any() else np.nan, pay=pf, blow=np.mean(FB) if FB else np.nan,
                first=np.median([x for x in FP if x]) if any(FP) else np.nan, cash=cash,
                s2p=pp * pf, accts=1 / (pp * pf) if pp * pf > 0 else np.inf,
                net=pp * cash - A["price"])


if __name__ == "__main__":
    jobs = [(s, c) for c in SCEN for s in SIZES]
    with Pool(5) as pool:
        rows = pool.map(one, jobs)
    import json
    json.dump(rows, open("results/lucid_sizes.json", "w"), default=float)
    for c in SCEN:
        print(f"\n=== {c} ===")
        print(f"{'size':5} {'PF':>5} | {'pass':>5} {'med d':>5} {'75%':>4} | {'payout|funded':>13} {'blow':>5} {'1st pay d':>9} {'avg cash':>8} | {'start->payout':>13} {'accts/payout':>12} {'net $/eval':>10}")
        for r in rows:
            if r["scen"] != c: continue
            print(f"{r['size']:5} {r['PF']:5.2f} | {r['pass_']*100:4.0f}% {r['days']:5.0f} {r['d75']:4.0f} | {r['pay']*100:12.0f}% {r['blow']*100:4.0f}% {r['first']:9.0f} ${r['cash']:7.0f} | {r['s2p']*100:12.0f}% {r['accts']:12.1f} ${r['net']:9.0f}")
