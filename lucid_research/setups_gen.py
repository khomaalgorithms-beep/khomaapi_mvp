"""Validated numbers for the two NEW setups (ORB+PDL close-entry, and BOTH = ORB + 10:30 momentum on one account),
for every account size x drawdown type x preset (eval) and Standard/Safe (funded), plus equity curve and live-cash
stats. Written into app/orb_validated.py as DATA['setups'] — the existing 10:30 tables are left untouched."""
import sys, json, numpy as np
sys.path.insert(0, "..")
from multiprocessing import Pool
import orb_combo as OC, orb_lab as OL, dashboard_data as DDm, orb_matrix as OM
from lucid_sizes import SIZES
FR = {"orb": {"cons": 0.2, "norm": 0.3, "aggr": 0.4}, "both": {"cons": 0.15, "norm": 0.2, "aggr": 0.3}}
FUND = {"orb": 0.12, "both": 0.08}
MODE = {"orb": "ORB", "both": "BOTH"}
KINDS = ("eod", "intraday", "static")
WIN = {"full": ("2008-03-01", "2026-10-09"), "good": ("2020-01-01", "2026-10-09"), "now": ("2025-07-01", "2026-10-09")}
def init(): OC.LV, OC.OR = OC.live_legs(), OC.orb_legs()
def legs_for(setup, risk):
    """day -> [(per-contract leg, qty)] for the setup at $risk per strategy-trade."""
    use = [OC.OR] if setup == "orb" else [OC.OR, OC.LV]
    out = {}
    for X in use:
        for d, v in X.items():
            q = OC.qty(risk, v["rct"], False)
            if q > 0: out.setdefault(d, []).append((v, q))
    return out
def eval_perleg(seq, A, kind, cap_frac=0.48):
    """LucidFlex evaluation where EACH leg carries its own resting take-profit = min($ still needed, daily cap),
    computed from the start-of-day balance (as the engine does); legs are then merged on the minute timeline."""
    a = DDm.DD(A, kind); dps = []; cap_day = cap_frac * A["target"]
    for n, legs in enumerate(seq, 1):
        dp = 0.0; a.bal_start = a.bal; Bup = None
        if legs:
            need = A["start"] + A["target"] - a.bal; best = max(dps) if dps else 0.0
            tp = min(max(need, best / .5 - (a.bal - A["start"])) if best > 0 else need, cap_day)
            parts = []
            for v, q in legs:
                hit = np.where(v["best"] * q >= tp + 0.5 * q)[0]
                if len(hit) and not (hit[0] == len(v["best"]) - 1 and v["net"] < 0):
                    i = hit[0]; v = dict(v, best=v["best"][:i + 1].copy(), worst=v["worst"][:i + 1].copy(), net=tp / q)
                parts.append((v, q))
            m = OC.merge(parts); B, W = m["best"], m["worst"]
            if len(np.where(a.bal + W <= a.path_floor(B))[0]): return False, n
            dp = m["net"]; Bup = B
        a.bal += dp; dps.append(dp)
        if a.bal >= A["start"] + A["target"] and max(dps) <= .5 * (a.bal - A["start"]): return True, n
        a.after_day(Bup if kind == "intraday" else None)
    return False, len(seq)
def ev(a):
    setup, size, kind, pr = a; A = SIZES[size]; g = OL.G("NQ"); f = FR[setup][pr]
    E = legs_for(setup, f * A["mll"]); o = dict(risk=round(f * A["mll"]), frac=f)
    for w, (x, y) in WIN.items():
        st = np.where((g.days >= np.datetime64(x).astype(np.int64)) & (g.days <= np.datetime64(y).astype(np.int64)))[0]
        res = [eval_perleg([E.get(i) for i in range(s, min(s + 180, g.D))], A, kind) for s in st]
        p = np.array([r[0] for r in res]); d = np.array([r[1] for r in res])
        o["pass_" + w] = round(float(p.mean()) * 100)
        if w == "full":
            o["days_full"] = float(np.median(d[p])) if p.any() else None; o["p75_full"] = float(np.percentile(d[p], 75)) if p.any() else None
    return a, o
def fu(a):
    setup, size, kind = a; A = SIZES[size]; g = OL.G("NQ"); r = FUND[setup] * A["mll"]; out = dict(risk=round(r), risk_after=round(r / 2))
    for nm, tp, pol in (("std", None, "A"), ("safe", 2.0, "E")):
        F, Fh = OC.build(MODE[setup], r, tp, True), OC.build(MODE[setup], r / 2, tp, True)
        for lab, y0, y1 in (("full", 2008, 2025), ("good", 2020, 2025)):
            st = np.where((g.year >= y0) & (g.year <= y1))[0][::2]
            R = [funded_kind([F.get(i) for i in range(s, min(s + 250, g.D))], [Fh.get(i) for i in range(s, min(s + 250, g.D))], pol, A, r / 2, kind) for s in st]
            fs = [x[2] for x in R if x[2]]
            out[f"{nm}_paid_{lab}"] = round(float(np.mean([len(x[0]) > 0 for x in R])) * 100)
            if lab == "full":
                out[f"{nm}_bb"] = round(float(np.mean([x[1] and not x[0] for x in R])) * 100)
                out[f"{nm}_cash"] = round(float(np.mean([0.9 * sum(x[0]) for x in R])))
                out[f"{nm}_first"] = float(np.median(fs)) if fs else None
    return a, out
def funded_kind(seqF, seqH, pol, A, rhalf, kind):
    a = DDm.DD(A, kind); qual = 0; cyc = 0.0; pays = []; first = None
    for n in range(len(seqF)):
        L = (seqH if (pol == "E" and pays) else seqF)[n]; dp = 0.0; Bup = None
        if L is not None:
            a.bal_start = a.bal
            if len(np.where(a.bal + L["worst"] <= a.path_floor(L["best"]))[0]): return pays, True, first
            dp = L["net"]; Bup = L["best"]
        else:
            a.bal_start = a.bal
        a.bal += dp; cyc += dp
        if dp >= A["qual"]: qual += 1
        a.after_day(Bup if kind == "intraday" else None); prof = a.bal - A["start"]; small = pol == "E" and not pays
        trig = (prof >= 1000) if small else (prof >= A["mll"] + 300 if not pays else a.bal - a.floor >= 1300 + 4 * rhalf)
        if qual >= 5 and cyc > 0 and trig:
            amt = min(500 if small else 1000, 0.5 * max(prof, 0), A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.floor = A["start"] + 100; pays.append(amt); first = first or n + 1; qual = 0; cyc = 0.0
                if len(pays) >= 5: return pays, False, first
    return pays, False, first
def series(setup):
    a = {d: v["net"] / v["rct"] for d, v in OC.LV.items()}; b = {d: v["net"] / v["rct"] for d, v in OC.OR.items()}
    days = sorted(set(b) if setup == "orb" else set(a) | set(b))
    return days, np.array([b.get(d, 0) + (a.get(d, 0) if setup == "both" else 0) for d in days])
def equity_and_cash(setup):
    g = OL.G("NQ"); days, R = series(setup)
    months = [str(g.dates[d])[:7] for d in days]; ms = sorted(set(months)); mo = np.array(months); cum = []; c = 0.0
    for m in ms: c += R[mo == m].sum(); cum.append(round(c, 2))
    eq = np.cumsum(R); dd = float(-(eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min())
    yr = {}
    for y in range(2008, 2027):
        x = R[g.year[np.array(days)] == y]; t = x[x != 0]
        if len(t): yr[y] = dict(n=int(len(t)), r=round(float(x.sum()), 1), pf=round(float(t[t > 0].sum() / max(1e-9, -t[t < 0].sum())), 2), win=round(float((t > 0).mean()) * 100))
    t = R[R != 0]
    equity = dict(months=ms, cumR=cum, n=int(len(t)), win=round(float((t > 0).mean()) * 100), pf=round(float(t[t > 0].sum() / -t[t < 0].sum()), 2),
                  totalR=round(float(R.sum()), 1), maxddR=round(-dd, 1), years=yr, start=ms[0], end=ms[-1])
    cash = {}
    for lab, lo in (("full", "2008-03-01"), ("ten", "2016-10-10")):
        k = np.array([g.days[d] >= np.datetime64(lo).astype(np.int64) for d in days]); r = R[k]; ds = np.array(days)[k]
        e2 = np.cumsum(r); ddr = float(-(e2 - np.maximum.accumulate(np.r_[0, e2])[1:]).min())
        mm = {}
        for d, v in zip(ds, r): key = str(g.dates[d])[:7]; mm[key] = mm.get(key, 0) + v
        y0, m0 = map(int, str(g.dates[ds[0]])[:7].split("-")); y1, m1 = map(int, str(g.dates[ds[-1]])[:7].split("-")); nm = (y1 - y0) * 12 + (m1 - m0) + 1
        mr = np.array(list(mm.values()) + [0.0] * (nm - len(mm)))
        full = np.zeros(g.D); full[ds] = r; cs = np.cumsum(full[ds[0]:]); w12 = float((cs[252:] - cs[:-252]).min())
        last12 = float(r[np.array([g.days[d] >= np.datetime64("2025-10-10").astype(np.int64) for d in ds])].sum())
        cash[lab] = dict(ddR=round(ddr, 1), perMonthR=round(float(r.sum()) / nm, 3), p10=round(float(np.percentile(mr, 10)), 2), p50=round(float(np.percentile(mr, 50)), 2),
                         p90=round(float(np.percentile(mr, 90)), 2), loseM=round(float(np.mean(mr < 0)) * 100), worst12R=round(w12, 1), last12R=round(last12, 1), months=nm)
    return equity, cash
if __name__ == "__main__":
    init()
    EJ = [(s, z, k, p) for s in FR for z in SIZES for k in KINDS for p in ("cons", "norm", "aggr")]
    FJ = [(s, z, k) for s in FR for z in SIZES for k in KINDS]
    with Pool(8, initializer=init) as pool:
        ER = pool.map(ev, EJ); FU = pool.map(fu, FJ)
    from app.orb_validated import DATA
    setups = {}
    for (s, z, k, p), o in ER: setups.setdefault(s, {}).setdefault("presets", {}).setdefault(z, {}).setdefault(k, {})[p] = o
    for (s, z, k), o in FU: setups[s].setdefault("funded", {}).setdefault(z, {})[k] = o
    for s in FR:
        eq, cash = equity_and_cash(s); setups[s]["equity"] = eq; setups[s]["cash"] = cash
    setups["orb"]["rule"] = dict(range="09:30-09:35 ET", entry="resting stop order 1 tick beyond the range (09:35-10:35), only where that price is beyond the prior-day high (long) / low (short); the first break of either side decides the day", stop_atr=0.2, breakeven_R=1.0, exit="15:59 ET")
    DATA["setups"] = setups
    open("../app/orb_validated.py", "w").write('"""Validated KhomaVolume numbers (generated by lucid_research scripts incl. setups_gen.py — do not hand-edit)."""\nDATA = ' + json.dumps(DATA, default=float) + "\n")
    for s in FR:
        for z in SIZES:
            p = setups[s]["presets"][z]["eod"]; f = setups[s]["funded"][z]["eod"]
            print(f"{s:4} {z:5} eval " + " | ".join(f"{pr} ${p[pr]['risk']}: {p[pr]['pass_full']}%/{p[pr]['pass_good']}%/{p[pr]['pass_now']}% {p[pr]['days_full'] or 0:.0f}d" for pr in ("cons", "norm", "aggr")) +
                  f" || funded ${f['risk']}: Std {f['std_paid_full']}% paid {f['std_bb']}% blown ${f['std_cash']} | Safe {f['safe_paid_full']}% paid {f['safe_bb']}% blown ${f['safe_cash']} {f['safe_first'] or 0:.0f}d")
        print(f"{s} equity: PF {setups[s]['equity']['pf']} total {setups[s]['equity']['totalR']}R maxDD {setups[s]['equity']['maxddR']}R | cash full ddR {setups[s]['cash']['full']['ddR']} {setups[s]['cash']['full']['perMonthR']}R/mo")
