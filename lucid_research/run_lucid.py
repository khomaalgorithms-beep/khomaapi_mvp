"""LucidFlex 50K pass/payout simulation for Spec A on real NQ trades (traded as MNQ)."""
import numpy as np, harness as Hn, strategies as S
from account_mc import build_days, rolling, summarize, block_bootstrap
g = Hn.load("NQ")
tr = S.tod_mom(g, T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None)
r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], None, tr["xm"])
trade = dict(sym="NQ", date=g.days[r["d"]], e=r["e"], net=r["net"], mae=r["mae"], stop=r["stop"])
dates = g.days
per_ct = r["stop"] * 2.0
yrs = g.year[r["d"]]
print(f"1 MNQ risk per trade (stop x $2): median ${np.median(per_ct[yrs>=2020]):.0f}, p10 ${np.percentile(per_ct[yrs>=2020],10):.0f}, p90 ${np.percentile(per_ct[yrs>=2020],90):.0f}")
R = r["R"][yrs >= 2020]; pnl1 = r["net"][yrs >= 2020] * 2
print(f"per 1 MNQ 2020-26: avg ${pnl1.mean():.1f}/trade, PF {pnl1[pnl1>0].sum()/-pnl1[pnl1<0].sum():.2f}, {len(pnl1)/6.78:.0f} trades/yr\n")

def show(lbl, s):
    print(f"{lbl:34} pass {s['pass_rate']*100:5.1f}% | blow {s['breach_rate']*100:5.1f}% | timeout {s['timeout']*100:4.1f}% | "
          f"median days to pass {s['med_days_pass']:5.0f} | funded: >=1 payout {s.get('p_payout',np.nan)*100:5.1f}% "
          f"blow {s.get('funded_blow',np.nan)*100:5.1f}% avg payouts {s.get('avg_payouts',np.nan):.2f} cash ${s.get('avg_cash',np.nan):6.0f} | net/eval ${s.get('net_per_eval_bought',np.nan):6.0f}")

print("=== EVAL sizing sweep (funded: 1 MNQ-risk, payout at $2k profit), rolling starts 2020-2025, eval max 120 days ===")
for ev_risk in (250, 450, 700, 950):
    days_e, sk = build_days([trade], ev_risk, dates)
    days_f, _ = build_days([trade], 250, dates)
    # funded uses its own sizing: splice -> run eval on days_e, funded on days_f
    from lucid_sim import run_eval, run_funded
    recs = []
    starts = np.where((g.year >= 2020) & (g.year <= 2025))[0]
    for s in starts:
        ev = run_eval(days_e[s:s + 120])
        rec = dict(passed=ev.passed, breached=ev.breached, edays=ev.days)
        if ev.passed:
            f0 = s + ev.days
            fr = run_funded(days_f[f0:f0 + 250], payout_trigger_profit=2000)
            rec.update(fblow=fr.breached, npay=len(fr.payouts), cash=fr.trader_cash, first_pay=(fr.payout_days[0] if fr.payout_days else None), grad=fr.graduated)
        recs.append(rec)
    show(f"eval risk ${ev_risk} (skip {sk*100:.0f}%)", summarize(recs))
