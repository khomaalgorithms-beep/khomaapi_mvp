"""Final business numbers for the recommended configuration under three edge scenarios.
Eval: ~$450 risk/trade (2-3 MNQ). Funded: 1 MNQ, payout request at $3,000 profit (and $2,000 variant).
Eval window 120 trading days; funded window 500 trading days."""
import numpy as np, harness as Hn, strategies as S
from account_mc import build_days
from lucid_sim import run_eval, run_funded
g = Hn.load("NQ")
tr = S.tod_mom(g, T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None)
r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], None, tr["xm"])
yrs = g.year[r["d"]]; base = dict(sym="NQ", date=g.days[r["d"]], e=r["e"], net=r["net"], mae=r["mae"], stop=r["stop"])
m20 = yrs >= 2020
edge_pts = r["net"][m20].mean()
def scen(haircut):
    t = dict(base); t["net"] = base["net"] - haircut * edge_pts; t["mae"] = np.minimum(base["mae"], t["net"]); return t
rng = np.random.default_rng(11)
def funnel(trade, label, trig=3000, bootstrap_from=None, n_paths=3000):
    de, _ = build_days([trade], 450, g.days); df, _ = build_days([trade], 150, g.days, min_fill=0.5)
    out = []
    if bootstrap_from is None:
        starts = np.where((g.year >= 2020) & (g.year <= 2025))[0]
        for s in starts:
            ev = run_eval(de[s:s + 120]); rec = [ev.passed, ev.breached, ev.days, None]
            if ev.passed:
                f0 = s + ev.days; rec[3] = run_funded(df[f0:f0 + 500], payout_trigger_profit=trig)
            out.append(rec)
    else:
        idx = np.where(g.days >= bootstrap_from)[0]
        for _ in range(n_paths):
            path = []
            while len(path) < 640:
                s = rng.choice(idx[:-10]); path += list(range(s, s + 10))
            ev = run_eval([de[i] for i in path[:120]]); rec = [ev.passed, ev.breached, ev.days, None]
            if ev.passed:
                rec[3] = run_funded([df[i] for i in path[ev.days:ev.days + 500]], payout_trigger_profit=trig)
            out.append(rec)
    p = np.array([o[0] for o in out]); b = np.array([o[1] for o in out]); d = np.array([o[2] for o in out])
    fu = [o[3] for o in out if o[0]]
    pay = np.mean([len(f.payouts) > 0 for f in fu]) if fu else 0
    blow = np.mean([f.breached for f in fu]) if fu else 0
    cash = np.mean([f.trader_cash for f in fu]) if fu else 0
    fp = [f.payout_days[0] for f in fu if f.payout_days]
    fee = 140
    print(f"{label:44} pass {p.mean()*100:5.1f}% (blow {b.mean()*100:4.1f}%, unfinished {(~p&~b).mean()*100:4.1f}%) | days to pass median {np.median(d[p]) if p.any() else np.nan:4.0f} / 75th pct {np.percentile(d[p],75) if p.any() else np.nan:4.0f}"
          f" | funded: payout {pay*100:5.1f}%, blow {blow*100:5.1f}%, 1st payout median {np.median(fp) if fp else np.nan:4.0f}d, avg cash ${cash:5.0f}"
          f" | start->payout {p.mean()*pay*100:5.1f}% | net per $140 eval ${p.mean()*cash-fee:6.0f}")
for trig in (3000, 2000):
    print(f"\n--- funded payout trigger ${trig} ---")
    funnel(scen(0.0), "S1 history as it happened (2020-26)", trig)
    funnel(scen(0.5), "S2 edge cut in half", trig)
    funnel(scen(1.0), "S3 zero edge (costs only)", trig)
    funnel(scen(0.0), "S4 bootstrap of the last 12 months", trig, bootstrap_from=np.datetime64("2025-10-10").astype("datetime64[D]").astype(np.int64))
# trade-level stats for the record
for lab, m in [("2020-26", m20), ("holdout 2024-26", yrs >= 2024), ("last 12m", g.days[r["d"]] >= np.datetime64("2025-10-10").astype(np.int64))]:
    x = r["net"][m] * 2; print(f"\nPF per MNQ {lab}: {x[x>0].sum()/-x[x<0].sum():.2f}  win {np.mean(x>0)*100:.0f}%  avg ${x.mean():.1f}/trade  n={m.sum()}", end="")
print()
