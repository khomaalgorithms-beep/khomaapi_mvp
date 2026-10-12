"""Live 10:30 rule vs NQ ORB+PDL vs BOTH together — same LucidFlex 50K simulator, merged intraday P&L paths."""
import sys, numpy as np
sys.path.insert(0, "..")
from multiprocessing import Pool
import orb_lab as OL, harness as Hn, dashboard_data as DDm
from lucid_sizes import SIZES
A = SIZES["50K"]
SYS = dict(n=5, entry="touch", cutoff=60, filt=("pdl",), stop="atr", stop_k=0.2, be=1.0)   # LIVE-IMPLEMENTED rule: resting stop entry 1 tick beyond the 09:30-09:34 range
def orb_legs():
    g = OL.G("NQ"); t = OL.trades(g, **{k: v for k, v in SYS.items() if k != "be"})
    r = Hn.simulate(g, t["d"], t["e"], t["side"], t["px"], t["stop"], None, np.full(len(t["d"]), g.b), None, SYS["be"])
    slip = g.spec["tick"] * g.spec["slip"]; cm = g.spec["comm"]; L = {}
    for i in range(len(r["d"])):
        d = int(r["d"][i])
        if g.suspect[d]: continue
        ei = int(r["e"][i]); x = ei + int(r["hold"][i]); sd = float(r["side"][i]); fill = float(t["px"][i]) + sd * slip
        H = g.H[d, ei:x + 1].astype(float); Lo = g.L[d, ei:x + 1].astype(float); C = g.C[d, ei:x + 1].astype(float)
        best = (np.where(sd > 0, H, -Lo) - sd * fill) * 2.0; worst = (np.where(sd > 0, Lo, -H) - sd * fill) * 2.0
        best[0] = worst[0] = (C[0] - fill) * sd * 2.0
        L[d] = dict(best=best - cm, worst=worst - cm, net=float(r["net"][i]) * 2.0, rct=float(r["stop"][i]) * 2.0, e=ei)
    return L
def live_legs():
    L, g = DDm.lg(); return {d: dict(v, e=int(v["e"])) for d, v in L.items()}
def with_tp(v, k):
    """Funded-Safe take-profit k x stop on one leg (path already ends at the leg's natural exit)."""
    tp = k * v["rct"]; hit = np.where(v["best"] >= tp + 0.5)[0]
    if not len(hit) or (hit[0] == len(v["best"]) - 1 and v["net"] < 0): return v
    i = hit[0]; return dict(v, best=v["best"][:i + 1].copy(), worst=v["worst"][:i + 1].copy(), net=tp - 0.5)
def qty(risk, rct, floor1):
    q = int(risk // rct)
    return 1 if q == 0 and (floor1 or rct <= 1.5 * risk) else q
def merge(parts):
    """parts: [(leg, q)] -> one $ leg (rct=1 so the simulator trades it at q=1)."""
    parts = [(v, q) for v, q in parts if v is not None and q > 0]
    if not parts: return None
    s = min(v["e"] for v, _ in parts); end = max(v["e"] + len(v["best"]) for v, _ in parts); T = end - s
    B = np.zeros(T); W = np.zeros(T); net = 0.0
    for v, q in parts:
        o = v["e"] - s; n = len(v["best"])
        B[o:o + n] += q * v["best"]; W[o:o + n] += q * v["worst"]; B[o + n:] += q * v["net"]; W[o + n:] += q * v["net"]; net += q * v["net"]
    return dict(best=B, worst=W, net=net, rct=1.0)
def build(mode, risk, tp=None, floor1=False):
    """Daily merged legs for mode in LIVE/ORB/BOTH at $risk per strategy-trade."""
    out = {}
    use = {"LIVE": [LV], "ORB": [OR], "BOTH": [LV, OR]}[mode]
    for d in set().union(*[set(x) for x in use]):
        parts = []
        for X in use:
            v = X.get(d)
            if v is None: continue
            if tp: v = with_tp(v, tp)
            parts.append((v, qty(risk, v["rct"], floor1)))
        m = merge(parts)
        if m: out[d] = m
    return out
def funded(seq_full, seq_half, pol):
    a = DDm.DD(A, "eod"); qual = 0; cyc = 0.0; pays = []
    for n in range(len(seq_full)):
        L = (seq_half if (pol == "E" and pays) else seq_full)[n]; dp = 0.0
        if L is not None:
            B, W = L["best"], L["worst"]
            if len(np.where(a.bal + W <= a.path_floor(B))[0]): return pays, True
            dp = L["net"]
        a.bal += dp; cyc += dp
        if dp >= A["qual"]: qual += 1
        a.after_day(None); prof = a.bal - A["start"]
        small = pol == "E" and not pays
        trig = (prof >= 1000) if small else (prof >= A["mll"] + 300 if not pays else a.bal - a.floor >= 1300 + 4 * 120)
        if qual >= 5 and cyc > 0 and trig:
            amt = min(500 if small else 1000, 0.5 * max(prof, 0), A["pay_max"])
            if amt >= 500:
                a.bal -= amt; a.locked = True; a.floor = A["start"] + 100; pays.append(amt); qual = 0; cyc = 0.0
                if len(pays) >= 5: return pays, False
    return pays, False
def job(a):
    mode, er, fr = a
    global LV, OR
    LV, OR = live_legs(), orb_legs(); g = OL.G("NQ")
    E = build(mode, er); out = {}
    for w, (x, y) in {"2008-26": ("2008-03-01", "2026-10-09"), "2020-26": ("2020-01-01", "2026-10-09"), "last12m": ("2025-10-12", "2026-10-09")}.items():
        st = np.where((g.days >= np.datetime64(x).astype(np.int64)) & (g.days <= np.datetime64(y).astype(np.int64)))[0]
        res = [DDm.eval_run([E.get(i) for i in range(s, min(s + 180, g.D))], A, 1.0, "eod") for s in st]
        p = np.array([r[0] for r in res]); d = np.array([r[1] for r in res])
        out[w] = (p.mean(), float(np.median(d[p])) if p.any() else 0)
    st = np.where((g.year >= 2008) & (g.year <= 2025))[0][::2]
    for nm, tp, pol in (("std", None, "A"), ("safe", 2.0, "E")):
        F, Fh = build(mode, fr, tp, True), build(mode, fr / 2, tp, True)
        R = [funded([F.get(i) for i in range(s, min(s + 250, g.D))], [Fh.get(i) for i in range(s, min(s + 250, g.D))], pol) for s in st]
        out[nm] = (np.mean([len(r[0]) > 0 for r in R]), np.mean([r[1] and not r[0] for r in R]), np.mean([0.9 * sum(r[0]) for r in R]))
    return a, out
def port_stats():
    global LV, OR
    LV, OR = live_legs(), orb_legs(); g = OL.G("NQ")
    days = sorted(set(LV) | set(OR))
    a = {d: LV[d]["net"] / LV[d]["rct"] for d in LV}; b = {d: OR[d]["net"] / OR[d]["rct"] for d in OR}
    for nm, f in (("LIVE 10:30 only", lambda d: a.get(d, 0)), ("ORB+PDL only", lambda d: b.get(d, 0)), ("BOTH (1R each)", lambda d: a.get(d, 0) + b.get(d, 0))):
        r = np.array([f(d) for d in days]); eq = np.cumsum(r); dd = -(eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min()
        yr = g.year[np.array(days)]; ys = {y: r[yr == y].sum() for y in sorted(set(yr))}
        t = r[r != 0]
        print(f"  {nm:16} total {r.sum():+6.1f}R | PF {t[t>0].sum()/-t[t<0].sum():.2f} | maxDD {dd:5.1f}R | return/DD {r.sum()/dd:5.2f} | losing years {sum(v<0 for v in ys.values())}/19 | worst year {min(ys.values()):+.0f}R ({min(ys, key=ys.get)})")
if __name__ == "__main__":
    print("PORTFOLIO (R per unit of risk, 2008-2026):"); port_stats()
    jobs = [("LIVE", 600, 240), ("ORB", 600, 240), ("BOTH", 600, 240), ("BOTH", 300, 120), ("BOTH", 400, 160)]
    with Pool(5) as pool: R = pool.map(job, jobs)
    print("\nLUCIDFLEX 50K (EOD). Eval risk per trade per strategy / funded risk per strategy:")
    for (m, er, fr), o in R:
        print(f"  {m:4} eval ${er} | pass " + " | ".join(f"{k} {o[k][0]*100:3.0f}% ({o[k][1]:.0f}d)" for k in ("2008-26", "2020-26", "last12m")) +
              f" || funded ${fr}: Std paid {o['std'][0]*100:.0f}% blown {o['std'][1]*100:.0f}% yr1 ${o['std'][2]:.0f} | Safe paid {o['safe'][0]*100:.0f}% blown {o['safe'][1]*100:.0f}% yr1 ${o['safe'][2]:.0f}")
