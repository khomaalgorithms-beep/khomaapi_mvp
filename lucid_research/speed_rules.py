"""Rule-aware exits for the eval (single stream Spec A, MNQ):
  pass-TP : a take-profit at exactly the $ still needed to pass (Lucid upgrades on hitting target)
  day-cap : a take-profit at the max day profit that keeps best-day <= 50% of the eventual total
If a trade's best open profit (MFE) reached the TP, it exits there; if its worst excursion came first and
would breach, the breach wins (conservative). Choose on 2020-23 starts, check on 2024-26 starts."""
import sys, numpy as np, harness as Hn, strategies as S
from lucid_sim import Account, START, TARGET
g = Hn.load("NQ")
tr = S.tod_mom(g, T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None)
r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], None, tr["xm"])
by_day = {int(d): i for i, d in enumerate(r["d"])}
RISK, NET, MAE, MFE, MF = r["stop"] * 2, r["net"] * 2, np.minimum(r["mae"], 0) * 2, np.maximum(r["mfe"], 0) * 2, r["mae_first"]
TP_SLIP = 2.0 * 0.25 * 0   # limit TP: no slippage, but require MFE to exceed TP by 1 tick ($0.50/MNQ)

def run(s0, risk, pass_tp, day_cap, cons=0.50, maxd=120, cap_frac=0.5):
    a = Account(); dps = []
    for n in range(1, maxd + 1):
        d = s0 + n - 1
        if d >= g.D: break
        i = by_day.get(d); dp = 0.0
        if i is not None:
            q = int(risk // RISK[i]); q = 1 if q == 0 and RISK[i] <= 1.5 * risk else q; q = min(q, 40)
            if q > 0:
                pnl, mae, mfe = NET[i] * q, MAE[i] * q, MFE[i] * q
                tps = []
                if pass_tp:
                    need = START + TARGET - a.balance
                    best = max(dps) if dps else 0.0
                    # profit needed so that total >= 3000 AND best day <= cons*total
                    need_c = max(need, best / cons - (a.balance - START)) if best > 0 else need
                    tps.append(need_c)
                if day_cap is not None:
                    tps.append(day_cap)
                tp = min(tps) if tps else None
                hit_tp = tp is not None and tp > 0 and mfe >= tp + 0.5 * q
                breach = a.balance + min(mae, pnl) <= a.mll
                if breach and (not hit_tp or MF[i]):
                    return False, True, n
                if hit_tp:
                    pnl = tp
                a.balance += pnl; dp = pnl
        dps.append(dp)
        prof = a.balance - START
        if a.balance >= START + TARGET and max(dps) <= cons * prof:
            return True, False, n
        a.eod_update()
    return False, False, maxd

def ev(y0, y1, **kw):
    starts = np.where((g.year >= y0) & (g.year <= y1))[0]
    res = [run(s, **kw) for s in starts]
    p = np.array([x[0] for x in res]); b = np.array([x[1] for x in res]); dd = np.array([x[2] for x in res])
    return p.mean(), b.mean(), np.median(dd[p]) if p.any() else np.nan, np.percentile(dd[p], 75) if p.any() else np.nan

if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "design"
    y0, y1 = (2020, 2023) if period == "design" else (2024, 2026)
    print(f"=== {period} starts {y0}-{y1}: Spec A single stream ===")
    print(f"{'risk':>5} {'pass-TP':>7} {'day cap':>7} | pass  blow | median/75th days")
    for risk in (450, 600, 800, 1000, 1300):
        for ptp in (False, True):
            for cap in (None, 1450, 1000, 750):
                p, b, md, q3 = ev(y0, y1, risk=risk, pass_tp=ptp, day_cap=cap)
                print(f"{risk:5d} {str(ptp):>7} {str(cap):>7} | {p*100:3.0f}% {b*100:4.0f}% | {md:4.0f} / {q3:4.0f}")
        print()
