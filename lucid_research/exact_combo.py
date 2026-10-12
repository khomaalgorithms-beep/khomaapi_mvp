"""Exact minute-by-minute account replay for one or more markets in ONE LucidFlex 50K eval.
Each leg = the fast-pass chain trade of that market (one per day per market). Every minute we compute the
account's combined open P&L: best case (bar highs for longs / lows for shorts) and worst case (opposite).
Rules: breach if balance + combined WORST open P&L <= MLL; account-level take-profit: flatten ALL legs when
combined BEST open P&L >= tp (tp = min($ still needed to pass with best-day<=50%, $1,450)). If both happen
in the same minute -> breach (conservative). Legs exit on their own stop/time exit otherwise."""
import sys, numpy as np, harness as Hn, bundle_test as B
from lucid_sim import Account, START, TARGET

UNIT = {"NQ": .1, "ES": .1, "GC": .1, "SI": .1, "CL": .1, "PL": 1.0}


def legs_for(sym):
    """per session-day-number: (entry m, exit m, side, fill, $ risk per ct, dpp, mult, grid ref)."""
    g = Hn.load(sym); sp = g.spec; dpp = sp["dpp"]; slip = sp["tick"] * sp["slip"]
    out = {}
    import strategies as S
    taken = set()
    for fam, p, mult in B.CHAIN:
        tr = getattr(S, fam)(g, **p)
        if len(tr["d"]) == 0:
            continue
        r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"])
        for i, d in enumerate(r["d"]):
            d = int(d)
            if d in taken:
                continue
            taken.add(d)
            e = int(r["e"][i]); x = e + int(r["hold"][i]); side = float(r["side"][i])
            fill = float(tr["px"][i]) + side * slip
            # per-contract minute paths from e..x (inclusive); after exit frozen at realized net
            H = g.H[d, e:x + 1].astype(float); L = g.L[d, e:x + 1].astype(float); Cc = g.C[d, e:x + 1].astype(float)
            best = (np.where(side > 0, H, -L) - side * fill) * dpp
            worst = (np.where(side > 0, L, -H) - side * fill) * dpp
            best[0] = worst[0] = (Cc[0] - fill) * side * dpp            # entry bar: close only
            comm = sp["comm"]
            out[int(g.days[d])] = dict(e=e, x=x, best=best - comm, worst=worst - comm,
                                       net=float(r["net"][i]) * dpp, rct=float(r["stop"][i]) * dpp, mult=mult, unit=UNIT.get(sym, 1.0))
    Hn._cache.clear()
    return out


def day_result(legs_today, budget_by_sym, a, dps, cap):
    """returns (day pnl, breached)."""
    used = 0.0; lg = []
    for sym, L in legs_today:
        r_ = budget_by_sym[sym] * L["mult"]
        q = int(r_ // L["rct"]); q = 1 if q == 0 and L["rct"] <= 1.5 * r_ else q
        q = min(q, int(round((4 - used) / L["unit"])))
        if q <= 0:
            continue
        used += q * L["unit"]; lg.append((L, q))
    if not lg:
        return 0.0, False
    m0 = min(L["e"] for L, _ in lg); m1 = max(L["x"] for L, _ in lg)
    span = m1 - m0 + 1
    BEST = np.zeros(span); WORST = np.zeros(span)
    for L, q in lg:
        i0 = L["e"] - m0; n = L["x"] - L["e"] + 1
        BEST[i0:i0 + n] += L["best"] * q; WORST[i0:i0 + n] += L["worst"] * q
        BEST[i0 + n:] += L["net"] * q; WORST[i0 + n:] += L["net"] * q          # closed leg: realized
    need = START + TARGET - a.balance; best = max(dps) if dps else 0.0
    tp = min(max(need, best / .5 - (a.balance - START)) if best > 0 else need, cap)
    br = np.where(a.balance + WORST <= a.mll)[0]; hit = np.where(BEST >= tp + 1)[0]
    ib = br[0] if len(br) else 10 ** 9; ih = hit[0] if len(hit) else 10 ** 9
    if ib <= ih and ib < 10 ** 9:
        return a.mll - a.balance, True
    if ih < 10 ** 9:
        return tp, False
    return sum(L["net"] * q for L, q in lg), False


def run(alld, legs, s0, budget, cap=1450, maxd=120):
    a = Account(); dps = []
    for n in range(1, maxd + 1):
        i = s0 + n - 1
        if i >= len(alld):
            break
        d = alld[i]
        today = [(s, legs[s][d]) for s in budget if d in legs[s]]
        dp, br = day_result(today, budget, a, dps, cap)
        if br:
            return False, True, n
        a.balance += dp; dps.append(dp)
        if a.balance >= START + TARGET and max(dps) <= .5 * (a.balance - START):
            return True, False, n
        a.eod_update()
    return False, False, maxd


def zero_edge(legs, y0=2020):
    z = {}
    for s, L in legs.items():
        ds = [d for d in L if int(str(np.datetime64(d, 'D'))[:4]) >= y0]
        mu = np.mean([L[d]["net"] / L[d]["rct"] for d in ds])
        z[s] = {d: dict(v, best=v["best"] - mu * v["rct"], worst=v["worst"] - mu * v["rct"], net=v["net"] - mu * v["rct"]) for d, v in L.items()}
    return z


if __name__ == "__main__":
    syms = ["NQ", "GC", "SI", "ES"]
    legs = {s: legs_for(s) for s in syms}
    alld = sorted(set().union(*[set(v) for v in legs.values()]))
    yr = np.array([int(str(np.datetime64(d, "D"))[:4]) for d in alld])
    Z = zero_edge(legs)
    def ev(bud, y0, y1, L=legs):
        st = np.where((yr >= y0) & (yr <= y1))[0]
        res = [run(alld, L, s, bud) for s in st]
        p = np.array([r[0] for r in res]); dd = np.array([r[2] for r in res])
        return p.mean(), np.percentile(dd[p], 25), np.median(dd[p]), np.percentile(dd[p], 75)
    print("EXACT minute-level replay, account-level flatten at take-profit")
    for lab, bud in [("NQ $1000", {"NQ": 1000}), ("NQ $1150", {"NQ": 1150}), ("NQ 800 + GC 400", {"NQ": 800, "GC": 400}),
                     ("NQ 900 + GC 450", {"NQ": 900, "GC": 450}), ("NQ 1000 + GC 500", {"NQ": 1000, "GC": 500}),
                     ("NQ 800 + GC 300 + SI 300", {"NQ": 800, "GC": 300, "SI": 300}), ("NQ 700 + ES 400", {"NQ": 700, "ES": 400})]:
        a = ev(bud, 2020, 2026); b = ev(bud, 2024, 2026); z = ev(bud, 2020, 2026, Z)
        print(f"{lab:26} 2020-26 pass {a[0]*100:3.0f}% days {a[1]:.0f}/{a[2]:.0f}/{a[3]:.0f} | 2024-26 {b[0]*100:3.0f}% {b[2]:.0f}d | luck {z[0]*100:3.0f}% -> edge {(a[0]-z[0])*100:+3.0f}")
