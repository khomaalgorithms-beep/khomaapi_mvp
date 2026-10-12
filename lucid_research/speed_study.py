"""Speed up the LucidFlex 50K pass while keeping ~49% pass rate.
Levers: (1) state-dependent sizing, (2) more trades from the validated NQ momentum family.
Choose on rolling starts 2020-2023 (DESIGN), confirm on starts 2024-2026 (CHECK). Eval window 120 days."""
import sys, numpy as np, harness as Hn, strategies as S
from lucid_sim import Account, START, TARGET

g = Hn.load("NQ")
STREAMS = {
    "A1030": (S.tod_mom, dict(T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None)),
    "M1000": (S.tod_mom, dict(T=30, k=0.0, s=0.3, ref="open", tf=20, trail=None)),
    "M1100": (S.tod_mom, dict(T=90, k=0.0, s=0.3, ref="open", tf=20, trail=None)),
    "M1200": (S.tod_mom, dict(T=150, k=0.0, s=0.3, ref="open", tf=20, trail=None)),
    "NOISE": (S.noise_area, dict(step=30, mult=1.0, s=0.5, tf=20, trail=(1.0, 1.0))),
}
TR = {}
for k, (f, p) in STREAMS.items():
    tr = f(g, **p)
    r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"])
    TR[k] = r


def day_lists(names):
    """per day: list of (entry bar, per-contract $risk, per-contract $net, per-contract $mae) for MNQ ($2/pt)."""
    per = [[] for _ in range(g.D)]
    for k in names:
        r = TR[k]
        for i in range(len(r["d"])):
            per[r["d"][i]].append((int(r["e"][i]), r["stop"][i] * 2, r["net"][i] * 2, min(r["mae"][i], 0) * 2))
    return [sorted(x) for x in per]


def run_eval_dyn(days, sizer, consistency=0.50, cap=40, day_cap=None):
    """No look-ahead: all of a day's trades are sized from the START-OF-DAY account state (positions
    overlap until 16:00). Breach check is conservative: the day's worst open excursions are assumed to
    coincide (sum of per-trade MAEs). Pass is checked at the end of the day."""
    a = Account(); dps = []; n = 0
    for trades in days:
        n += 1
        budget = sizer(a, 0.0)
        tot_pnl = 0.0; tot_mae = 0.0; used = 0
        for _, risk_ct, net, mae in trades:
            q = int(budget // risk_ct)
            if q == 0 and risk_ct <= 1.5 * budget:
                q = 1
            q = min(q, cap - used)
            if q <= 0:
                continue
            used += q
            tot_pnl += net * q; tot_mae += min(mae, net) * q
        if trades and used:
            if a.balance + min(tot_mae, tot_pnl) <= a.mll:
                return False, True, n
            a.balance += tot_pnl
        dps.append(tot_pnl)
        prof = a.balance - START
        if a.balance >= START + TARGET and max(dps) <= consistency * prof:
            return True, False, n
        a.eod_update()
    return False, False, n


def evaluate(names, sizer, y0, y1, day_cap=None, maxd=120):
    days = day_lists(names)
    starts = np.where((g.year >= y0) & (g.year <= y1))[0]
    res = [run_eval_dyn(days[s:s + maxd], sizer, day_cap=day_cap) for s in starts]
    p = np.array([x[0] for x in res]); b = np.array([x[1] for x in res]); d = np.array([x[2] for x in res])
    return p.mean(), b.mean(), (np.median(d[p]) if p.any() else np.nan), (np.percentile(d[p], 75) if p.any() else np.nan)


# ---- sizing rules -------------------------------------------------------------------------------
def fixed(r):
    return lambda a, dp: r

def cushion(f, floor=150):
    """risk a fraction f of the distance to the MLL (shrinks after losses, grows after wins)."""
    return lambda a, dp: max(floor, f * (a.balance + min(dp, 0) * 0 - a.mll))

def to_target(f, minr=150, maxr=1500):
    """risk a fraction of what's still needed to pass (aggressive early, gentle near the target)."""
    return lambda a, dp: float(np.clip(f * (START + TARGET - a.balance), minr, maxr))

def hybrid(fc, ft, minr=150):
    """min of cushion- and target-based budgets: never risk much near the floor OR near the target."""
    return lambda a, dp: max(minr, min(fc * (a.balance - a.mll), ft * (START + TARGET - a.balance)))


if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "design"
    y0, y1 = (2020, 2023) if period == "design" else (2024, 2026)
    print(f"=== {period.upper()} rolling starts {y0}-{y1} ===")
    # correlation of daily P&L between streams (per contract R)
    names = list(STREAMS)
    rows = {}
    for k in names:
        v = np.full(g.D, np.nan); v[TR[k]["d"]] = TR[k]["R"]; rows[k] = v
    m = g.year >= 2020
    print("daily R correlation 2020+:")
    for k in names:
        print(f"  {k:6}", " ".join(f"{np.corrcoef(*[np.nan_to_num(rows[k][m]), np.nan_to_num(rows[j][m])])[0,1]:+.2f}" for j in names),
              f"| meanR {np.nanmean(rows[k][m]):+.3f}")
    combos = {"A only": ["A1030"], "A+NOISE": ["A1030", "NOISE"], "A+11:00": ["A1030", "M1100"],
              "A+10:00+11:00": ["M1000", "A1030", "M1100"], "A+11:00+NOISE": ["A1030", "M1100", "NOISE"],
              "all 5": names}
    sizers = {"fixed $450": fixed(450), "fixed $600": fixed(600), "fixed $800": fixed(800),
              "cushion 25%": cushion(0.25), "cushion 33%": cushion(0.33), "cushion 45%": cushion(0.45),
              "to-target 25%": to_target(0.25), "to-target 40%": to_target(0.40),
              "hybrid 33%/35%": hybrid(0.33, 0.35), "hybrid 45%/50%": hybrid(0.45, 0.50)}
    print(f"\n{'streams':16} {'sizing':16} | {'pass':>5} {'blow':>5} | days to pass median / 75th")
    for cn, cs in combos.items():
        for sn, sz in sizers.items():
            p, b, md, q3 = evaluate(cs, sz, y0, y1)
            print(f"{cn:16} {sn:16} | {p*100:4.0f}% {b*100:4.0f}% | {md:5.0f} / {q3:4.0f}")
        print()
