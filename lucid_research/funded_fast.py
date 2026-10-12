"""FUNDED mode per size: fast first payout without blowing. Same NQ signals (chain), real costs, exact replay.
Pre-declared rule: among playbooks with median days-to-first-payout <= 50 and P(blow before 1st payout) <= 20%
(design 2020-23), pick max year-1 trader cash; then check 2024-25 and last-12m bootstrap."""
import sys, json, numpy as np
from multiprocessing import Pool
import stress as T
from lucid_sizes import SIZES, Acct

LG = None


def lg_():
    global LG
    if LG is None:
        LG = T.legs(dict(comm=1.0, slip=1))
    return LG, T.G


def funded(seq, A, risk_lock, cush, day_tp, trig):
    a = Acct(A); qual = 0; cyc = 0.0; pays = []; first = None
    for n, L in enumerate(seq, 1):
        dp = 0.0
        if L is not None:
            prof = a.bal - A["start"]
            cap = max(c for lvl, c in A["tiers"] if prof >= lvl) if prof >= 0 else A["tiers"][0][1]
            risk = risk_lock if not a.locked else max(risk_lock, cush * (a.bal - a.mll))
            q = max(1, int(risk * L["mult"] // L["rct"])); q = min(q, cap)
            B = L["best"] * q; W = L["worst"] * q
            br = np.where(a.bal + W <= a.mll)[0]
            hit = np.where(B >= day_tp + 0.5 * q)[0] if day_tp else np.array([], int)
            ib = br[0] if len(br) else 10**9; ih = hit[0] if len(hit) else 10**9
            if ib <= ih and ib < 10**9:
                return pays, True, first, n
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
                pays.append(amt); first = first or n; qual = 0; cyc = 0.0
                if len(pays) >= 5:
                    return pays, False, first, n
    return pays, False, first, len(seq)


def grid(size):
    A = SIZES[size]; m = A["mll"]
    out = []
    for rl in (1, 2):                      # MNQ-equivalents of risk before lock (x ~$140)
        for cu in (0.0, 0.15):
            for tp_f in (None, 0.15, 0.25):  # daily TP as fraction of MLL
                for tr in (1000, 1.5):  # payout trigger: $1,000 (minimum) or multiples of max payout
                    trig = tr if tr >= 1000 else tr * A["pay_max"]
                    tp = None if tp_f is None else max(A["qual"] * 1.5, tp_f * m)
                    out.append((size, rl * 140, cu, tp, trig))
    return out


def job(args):
    size, risk, cu, tp, trig, period = args
    lg, g = lg_(); A = SIZES[size]; rng = np.random.default_rng(4)
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
    R = [funded([lg.get(d) for d in p], A, risk, cu, tp, trig) for p in paths]
    firsts = np.array([r[2] if r[2] else np.nan for r in R])
    blow_before = np.mean([r[1] and r[2] is None for r in R])
    return dict(size=size, risk=risk, cu=cu, tp=tp, trig=trig, period=period,
                cash=float(np.mean([0.9 * sum(r[0]) for r in R])), p1=float(np.mean([len(r[0]) > 0 for r in R])),
                p1_60=float(np.mean(firsts <= 60)), med_first=float(np.nanmedian(firsts)) if (~np.isnan(firsts)).any() else np.nan,
                blow_before=float(blow_before), blow=float(np.mean([r[1] for r in R])))


if __name__ == "__main__":
    sizes = sys.argv[1].split(",") if len(sys.argv) > 1 else list(SIZES)
    jobs = [g_ + ("design",) for s in sizes for g_ in grid(s)]
    with Pool(3) as pool:
        rows = pool.map(job, jobs)
    best = {}
    for s in sizes:
        ok = [r for r in rows if r["size"] == s and r["med_first"] <= 50 and r["blow_before"] <= 0.20]
        best[s] = max(ok, key=lambda r: r["cash"]) if ok else None
    checks = [(b["size"], b["risk"], b["cu"], b["tp"], b["trig"], p) for b in best.values() if b for p in ("check", "recent")]
    with Pool(5) as pool:
        ch = pool.map(job, checks)
    json.dump(dict(grid=rows, best=best, checks=ch), open("results/funded_fast.json", "w"), default=float)
    for s in sizes:
        b = best[s]
        if not b:
            print(s, "no playbook meets the rule"); continue
        print(f"\n{s} FUNDED: risk ${b['risk']:.0f} before lock, then {b['cu']*100:.0f}% of cushion | daily TP {b['tp']} | payout when profit >= ${b['trig']:.0f}")
        for r in [b] + [c for c in ch if c["size"] == s]:
            print(f"   {r['period']:7}: 1st payout median {r['med_first']:4.0f}d, within 60d {r['p1_60']*100:3.0f}% | >=1 payout {r['p1']*100:3.0f}% | blown before 1st payout {r['blow_before']*100:3.0f}% | yr-1 cash ${r['cash']:,.0f} | blown within yr {r['blow']*100:3.0f}%")
