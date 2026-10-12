"""Target: median 5-7 trading days to pass with a strong pass rate.
One trade per day (no stacking), first qualifying signal from a priority list of validated NQ momentum
streams, each with a size multiplier. Rule-aware exits: pass take-profit + daily take-profit cap.
Design on starts 2020-23, check on starts 2024-26."""
import sys, numpy as np, harness as Hn, strategies as S
from lucid_sim import Account, START, TARGET

g = Hn.load("NQ")
SPECS = {
    "A1030":  (S.tod_mom, dict(T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None)),
    "M1100":  (S.tod_mom, dict(T=90, k=0.0, s=0.3, ref="open", tf=20, trail=None)),
    "M1200":  (S.tod_mom, dict(T=150, k=0.0, s=0.3, ref="open", tf=20, trail=None)),
    "NOISE":  (S.noise_area, dict(step=30, mult=1.0, s=0.5, tf=20, trail=(1.0, 1.0))),
    "A1030nf": (S.tod_mom, dict(T=60, k=0.0, s=0.3, ref="open", tf=None, trail=None)),   # no trend filter
}
RAW = {}
for k, (f, p) in SPECS.items():
    tr = f(g, **p)
    r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"])
    RAW[k] = {int(d): (r["stop"][i] * 2, r["net"][i] * 2, min(r["mae"][i], 0) * 2, max(r["mfe"][i], 0) * 2, bool(r["mae_first"][i]))
              for i, d in enumerate(r["d"])}


def schedule(prio):
    """prio: list of (stream, size_mult). Per day pick the first stream that has a trade."""
    out = {}
    for d in range(g.D):
        for k, mult in prio:
            x = RAW[k].get(d)
            if x is not None:
                out[d] = x + (mult,); break
    return out


def run(sched, s0, risk, day_cap=1450, cons=0.50, maxd=120):
    a = Account(); dps = []
    for n in range(1, maxd + 1):
        d = s0 + n - 1
        if d >= g.D:
            break
        x = sched.get(d); dp = 0.0
        if x is not None:
            rct, net, mae, mfe, mf, mult = x
            rk = risk * mult
            q = int(rk // rct); q = 1 if q == 0 and rct <= 1.5 * rk else q; q = min(q, 40)
            if q > 0:
                pnl, mae, mfe = net * q, mae * q, mfe * q
                need = START + TARGET - a.balance
                best = max(dps) if dps else 0.0
                need_c = max(need, best / cons - (a.balance - START)) if best > 0 else need
                tp = min(need_c, day_cap) if day_cap else need_c
                hit = tp > 0 and mfe >= tp + 0.5 * q
                if a.balance + min(mae, pnl) <= a.mll and (not hit or mf):
                    return False, True, n
                if hit:
                    pnl = tp
                a.balance += pnl; dp = pnl
        dps.append(dp)
        if a.balance >= START + TARGET and max(dps) <= cons * (a.balance - START):
            return True, False, n
        a.eod_update()
    return False, False, maxd


def ev(sched, y0, y1, **kw):
    starts = np.where((g.year >= y0) & (g.year <= y1))[0]
    res = [run(sched, s, **kw) for s in starts]
    p = np.array([x[0] for x in res]); b = np.array([x[1] for x in res]); dd = np.array([x[2] for x in res])
    q = lambda v: np.percentile(dd[p], v) if p.any() else np.nan
    return p.mean(), b.mean(), q(50), q(75), q(25)


PRIOS = {
    "A only":                  [("A1030", 1.0)],
    "A>11>12":                 [("A1030", 1.0), ("M1100", 1.0), ("M1200", 1.0)],
    "A>11>12>NOISE":           [("A1030", 1.0), ("M1100", 1.0), ("M1200", 1.0), ("NOISE", 1.0)],
    "A>11>12>NOISE>ctr(0.5)":  [("A1030", 1.0), ("M1100", 1.0), ("M1200", 1.0), ("NOISE", 1.0), ("A1030nf", 0.5)],
    "A>ctr(0.5)":              [("A1030", 1.0), ("A1030nf", 0.5)],
    "A>ctr(1.0)":              [("A1030", 1.0), ("A1030nf", 1.0)],
}

if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "design"
    y0, y1 = {"design": (2020, 2023), "check": (2024, 2026), "full": (2020, 2026)}[period]
    print(f"=== {period} starts {y0}-{y1} | one trade/day, pass-TP + daily TP ===")
    for name, prio in PRIOS.items():
        sch = schedule(prio)
        cov = np.mean([d in sch for d in np.where((g.year >= y0) & (g.year <= y1))[0]])
        print(f"{name}  (trade on {cov*100:.0f}% of days)")
        for risk in (800, 1000, 1300, 1600, 2000):
            for cap in (1450, 1000):
                p, b, md, q3, q1 = ev(sch, y0, y1, risk=risk, day_cap=cap)
                print(f"   risk ${risk:5d} cap ${cap}: pass {p*100:3.0f}%  blow {b*100:3.0f}%  days 25th/median/75th {q1:3.0f}/{md:3.0f}/{q3:3.0f}")
