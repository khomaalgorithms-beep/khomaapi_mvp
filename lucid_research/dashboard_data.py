"""Generate app/orb_validated.py: every number the KhomaVolume dashboard + engine use, computed on the EXACT
live rule (adaptive Spec A; current setting since Apr-2020 = 10:30 / stop 0.2xATR14 / 20d trend), real costs.
For each drawdown type (eod = LucidFlex end-of-day trailing, intraday = trails open-equity highs, static = fixed floor)
x account size x preset: eval risk/trade (pre-declared selection rule), pass %, days, 2024-26 pass, current-regime pass;
funded stats; 10-year equity curve in R (client scales by its risk)."""
import json, numpy as np
from multiprocessing import Pool
import harness as Hn, stress as T
from lucid_sizes import SIZES

LIVE_CHAIN = [("tod_mom", dict(T=60, k=0.0, s=0.2, ref="open", tf=20, trail=None), 1.0)]
FR = [0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.65, 0.8, 1.0]
DDT = ("eod", "intraday", "static")
_LG = None


def lg():
    global _LG
    if _LG is None:
        _LG = T.legs(dict(chain=LIVE_CHAIN, comm=1.0, slip=1))
    return _LG, T.G


class DD:
    """Account floor under three drawdown regimes (lock at start+$100 for trailing types)."""
    def __init__(s, A, kind):
        s.A, s.kind, s.S = A, kind, A["start"]
        s.bal = s.S; s.peak = s.S; s.floor = s.S - A["mll"]; s.locked = False

    def path_floor(s, B):
        if s.kind != "intraday" or s.locked:
            return np.full(len(B), s.floor)
        hw = np.maximum.accumulate(np.r_[s.peak, s.bal + B[:-1]])
        return np.minimum(np.maximum(s.floor, hw - s.A["mll"]), s.S + 100)

    def after_day(s, B_upto=None):
        if s.kind == "static":
            return
        if s.kind == "intraday" and B_upto is not None and len(B_upto):
            s.peak = max(s.peak, s.bal_start + B_upto.max())
        s.peak = max(s.peak, s.bal)
        if s.locked or s.peak > s.S + s.A["mll"] + 100:
            s.locked = True; s.floor = s.S + 100
        else:
            s.floor = max(s.floor, s.peak - s.A["mll"])


def eval_run(seq, A, risk, kind, cap_frac=0.48):
    a = DD(A, kind); dps = []; cap_day = cap_frac * A["target"]
    for n, L in enumerate(seq, 1):
        dp = 0.0; a.bal_start = a.bal; Bupto = None
        if L is not None:
            q = int(risk // L["rct"]); q = 1 if q == 0 and L["rct"] <= 1.5 * risk else q
            q = min(q, A["eval_micros"])
            if q > 0:
                B = L["best"] * q; W = L["worst"] * q
                need = A["start"] + A["target"] - a.bal; best = max(dps) if dps else 0.0
                tp = min(max(need, best / .5 - (a.bal - A["start"])) if best > 0 else need, cap_day)
                fl = a.path_floor(B)
                br = np.where(a.bal + W <= fl)[0]; hit = np.where(B >= tp + 0.5 * q)[0]
                ib = br[0] if len(br) else 10**9; ih = hit[0] if len(hit) else 10**9
                if ib <= ih and ib < 10**9:
                    return False, n
                dp = tp if ih < 10**9 else L["net"] * q
                Bupto = B[:(ih + 1 if ih < 10**9 else len(B))]
        a.bal += dp; dps.append(dp)
        if a.bal >= A["start"] + A["target"] and max(dps) <= .5 * (a.bal - A["start"]):
            return True, n
        a.after_day(Bupto)
    return False, len(seq)


def funded_run(seq, A, risk, kind, trig_buf=300, amt_fixed=1000):
    a = DD(A, kind); qual = 0; cyc = 0.0; pays = []; first = None
    for n, L in enumerate(seq, 1):
        dp = 0.0; a.bal_start = a.bal; Bupto = None
        if L is not None:
            prof = a.bal - A["start"]
            cap = max(c for lvl, c in A["tiers"] if prof >= lvl) if prof >= 0 else A["tiers"][0][1]
            q = min(max(1, int(risk // L["rct"])), cap)
            B = L["best"] * q; W = L["worst"] * q
            if len(np.where(a.bal + W <= a.path_floor(B))[0]):
                return pays, True, first
            dp = L["net"] * q; Bupto = B
        a.bal += dp; cyc += dp
        if dp >= A["qual"]:
            qual += 1
        a.after_day(Bupto)
        prof = a.bal - A["start"]
        if qual >= 5 and cyc > 0 and prof >= A["mll"] + trig_buf:
            amt = min(amt_fixed, 0.5 * prof, A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.floor = A["start"] + 100
                pays.append(amt); first = first or n; qual = 0; cyc = 0.0
                if len(pays) >= 5:
                    return pays, False, first
    return pays, False, first


def eval_job(args):
    size, kind, frac = args
    L, g = lg(); A = SIZES[size]; out = {}
    for lab, a, b in (("all", "2020-01-01", "2026-10-09"), ("y2426", "2024-01-01", "2026-10-09"), ("now", "2025-07-01", "2026-10-09")):
        st = np.where((g.days >= np.datetime64(a).astype(np.int64)) & (g.days <= np.datetime64(b).astype(np.int64)))[0]
        res = [eval_run([L.get(x) for x in range(s, min(s + 180, g.D))], A, frac * A["mll"], kind) for s in st]
        p = np.array([r[0] for r in res]); d = np.array([r[1] for r in res])
        out[lab] = dict(p=float(p.mean()), med=float(np.median(d[p])) if p.any() else None, p75=float(np.percentile(d[p], 75)) if p.any() else None)
    return size, kind, frac, out


def funded_job(args):
    size, kind = args
    L, g = lg(); A = SIZES[size]; risk = max(0.12 * A["mll"], 1.0); rng = np.random.default_rng(5); out = {}
    for lab in ("all", "now"):
        if lab == "now":
            idx = np.where(g.days >= np.datetime64("2025-10-10").astype(np.int64))[0]; paths = []
            for _ in range(400):
                p = []
                while len(p) < 250:
                    s = rng.integers(0, len(idx) - 10); p += list(idx[s:s + 10])
                paths.append(p[:250])
        else:
            paths = [list(range(s, min(s + 250, g.D))) for s in np.where((g.year >= 2020) & (g.year <= 2025))[0][::2]]
        R = [funded_run([L.get(x) for x in p], A, risk, kind) for p in paths]
        fs = np.array([r[2] if r[2] else np.nan for r in R])
        out[lab] = dict(p1=float(np.mean([len(r[0]) > 0 for r in R])), first=float(np.nanmedian(fs)) if (~np.isnan(fs)).any() else None,
                        bb=float(np.mean([r[1] and r[2] is None for r in R])), cash=float(np.mean([0.9 * sum(r[0]) for r in R])))
    return size, kind, risk, out


def equity():
    import adaptive_lucid as AL
    lgA, _ = AL.build(); g = T.G
    start = np.datetime64("2016-10-10").astype(np.int64)
    days = [d for d in sorted(lgA) if g.days[d] >= start]
    R = np.array([lgA[d]["net"] / lgA[d]["rct"] for d in days])
    months = [str(g.dates[d])[:7] for d in days]
    ms = sorted(set(months)); cum = []; c = 0.0
    for m in ms:
        c += R[[i for i, x in enumerate(months) if x == m]].sum(); cum.append(round(c, 2))
    eq = np.cumsum(R); dd = (eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min()
    yr = {}
    for y in range(2017, 2027):
        x = R[np.array([int(m[:4]) == y for m in months])]
        yr[y] = dict(n=int(len(x)), r=round(float(x.sum()), 1), pf=round(float(x[x > 0].sum() / -x[x < 0].sum()), 2), win=round(float((x > 0).mean()) * 100))
    return dict(months=ms, cumR=cum, n=int(len(R)), win=round(float((R > 0).mean()) * 100), pf=round(float(R[R > 0].sum() / -R[R < 0].sum()), 2),
                totalR=round(float(R.sum()), 1), maxddR=round(float(dd), 1), years=yr)


if __name__ == "__main__":
    with Pool(6) as pool:
        ev = pool.map(eval_job, [(s, k, f) for s in SIZES for k in DDT for f in FR])
        fd = pool.map(funded_job, [(s, k) for s in SIZES for k in DDT])
    presets = {}
    for s in SIZES:
        for k in DDT:
            R = [(f, o) for (s2, k2, f, o) in ev if s2 == s and k2 == k]
            def pick(cond):
                c = [x for x in R if x[1]["all"]["med"] is not None and cond(x[1]["all"])]
                return max(c, key=lambda x: x[1]["all"]["p"]) if c else max(R, key=lambda x: -abs((x[1]["all"]["med"] or 99) - 20))
            ch = {"cons": pick(lambda o: o["med"] <= 30 and o["p75"] <= 50), "norm": pick(lambda o: o["med"] <= 15), "aggr": pick(lambda o: o["med"] <= 10)}
            for pr, (f, o) in ch.items():
                presets.setdefault(s, {}).setdefault(k, {})[pr] = dict(risk=round(f * SIZES[s]["mll"]), frac=f,
                    pass_=round(o["all"]["p"] * 100), days=o["all"]["med"], p75=o["all"]["p75"],
                    pass2426=round(o["y2426"]["p"] * 100), pass_now=round(o["now"]["p"] * 100), days_now=o["now"]["med"])
    funded = {}
    for s, k, risk, o in fd:
        funded.setdefault(s, {})[k] = dict(risk=round(risk), p1=round(o["all"]["p1"] * 100), first=o["all"]["first"], bb=round(o["all"]["bb"] * 100),
                                           cash=round(o["all"]["cash"]), p1_now=round(o["now"]["p1"] * 100), first_now=o["now"]["first"])
    eqd = equity()
    data = dict(presets=presets, funded=funded, equity=eqd, rule=dict(entry="10:30 ET", stop_atr=0.2, trend_days=20, exit="15:59 ET"),
                generated="2026-10-11", note="LucidFlex rules read 2026-10-11; real costs (Lucid $1.00 RT/MNQ + 1 tick); rolling starts.")
    for s in SIZES:
        for k in DDT:
            p = presets[s][k]
            print(f"{s:5} {k:8} " + " | ".join(f"{pr} ${p[pr]['risk']}: {p[pr]['pass_']}%/{p[pr]['days']:.0f}d (24-26 {p[pr]['pass2426']}%, now {p[pr]['pass_now']}%)" for pr in ("cons", "norm", "aggr")))
        print("      funded " + " | ".join(f"{k}: 1st {funded[s][k]['first']}d paid {funded[s][k]['p1']}% bb {funded[s][k]['bb']}% ${funded[s][k]['cash']} now {funded[s][k]['p1_now']}%" for k in DDT))
    print("equity:", {k: eqd[k] for k in ("n", "win", "pf", "totalR", "maxddR")})
    src = '"""Validated KhomaVolume numbers (generated by lucid_research/dashboard_data.py — do not hand-edit)."""\nDATA = ' + json.dumps(data, default=float) + "\n"
    open("../app/orb_validated.py", "w").write(src)
    print("wrote app/orb_validated.py", len(src))
