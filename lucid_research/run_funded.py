"""Funded-phase optimisation for Spec A: start a funded LucidFlex 50K on every day 2020-2024 and run
up to 500 trading days (or to 5 payouts / breach). Sweep risk sizing and payout trigger."""
import numpy as np, harness as Hn, strategies as S
from account_mc import build_days
from lucid_sim import run_funded
g = Hn.load("NQ")
tr = S.tod_mom(g, T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None)
r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], None, tr["xm"])
trade = dict(sym="NQ", date=g.days[r["d"]], e=r["e"], net=r["net"], mae=r["mae"], stop=r["stop"])
starts = np.where((g.year >= 2020) & (g.year <= 2024))[0]
print(f"{'risk':>5} {'trigger':>7} | {'>=1 payout':>10} {'>=3':>5} {'all 5':>6} {'blow':>6} | {'avg cash':>8} {'median 1st payout (days)':>25}")
for risk in (150, 250, 400):
    days, _ = build_days([trade], risk, g.days, min_fill=0.5 if risk == 150 else 0.67)
    for trig in (1000, 2000, 3000, 4000):
        res = [run_funded(days[s:s + 500], payout_trigger_profit=trig) for s in starts]
        npay = np.array([len(x.payouts) for x in res]); cash = np.array([x.trader_cash for x in res])
        blow = np.mean([x.breached for x in res]); fp = [x.payout_days[0] for x in res if x.payout_days]
        print(f"{risk:5d} {trig:7d} | {np.mean(npay>=1)*100:9.1f}% {np.mean(npay>=3)*100:4.0f}% {np.mean(npay>=5)*100:5.0f}% {blow*100:5.1f}% | ${cash.mean():7.0f} {np.median(fp) if fp else float('nan'):25.0f}")
