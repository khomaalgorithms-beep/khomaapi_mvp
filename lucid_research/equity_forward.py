"""(1) 10-year equity curve of the FINAL system (Spec A) at a fixed $500 risk per trade, real costs.
(2) Forward-test simulator: client cohorts launched on past dates with rules frozen (no re-fitting), each client
    on LucidFlex 50K NORMAL preset (eval $800 risk) -> on pass switch to FUNDED-safe ($240 risk, lock then $1,000
    payouts) -> on eval breach buy another eval (max 5 evals). Follow each client 6 months (126 trading days).
(3) Forward projection: 3/6/12-month outcome distribution by block bootstrap from (a) 2020-26 and (b) last 12 months.
Output: results/equity_forward.json"""
import json, numpy as np
import stress as T, funded_hybrid as FH
from lucid_sizes import SIZES, eval_run

lg = T.legs(dict(comm=1.0, slip=1)); g = T.G
A = SIZES["50K"]
EVAL_RISK, FUND_RISK, FEE = 800, 240, 90.2

# ---------------- (1) equity curve, fixed $500 risk
start = np.datetime64("2016-10-10").astype(np.int64)
days = [d for d in sorted(lg) if g.days[d] >= start]
pnl = np.array([500 * lg[d]["net"] / lg[d]["rct"] for d in days])
dates = [str(g.dates[d]) for d in days]
eq = np.cumsum(pnl); peak = np.maximum.accumulate(np.r_[0, eq])[1:]; dd = eq - peak
years = {}
for y in range(2016, 2027):
    m = np.array([int(s[:4]) == y for s in dates])
    x = pnl[m]
    if len(x) == 0: continue
    e = np.cumsum(x); ddy = (e - np.maximum.accumulate(np.r_[0, e])[1:]).min()
    years[y] = dict(trades=int(len(x)), win=float((x > 0).mean()), pf=float(x[x > 0].sum() / -x[x < 0].sum()),
                    net=float(x.sum()), maxdd=float(ddy))
curve = dict(dates=dates, equity=eq.round(0).tolist(), drawdown=dd.round(0).tolist(), years=years,
             total=float(eq[-1]), maxdd=float(dd.min()), trades=len(pnl), win=float((pnl > 0).mean()),
             pf=float(pnl[pnl > 0].sum() / -pnl[pnl < 0].sum()),
             longest_flat_days=int(max(np.diff(np.r_[0, np.where(dd == 0)[0], len(dd) - 1])) if (dd == 0).any() else len(dd)))
print(f"10y equity @ $500/trade: +${eq[-1]:,.0f}, {len(pnl)} trades, win {curve['win']*100:.0f}%, PF {curve['pf']:.2f}, max DD ${dd.min():,.0f}")
for y, v in years.items():
    print(f"  {y}: {v['trades']:3d} trades win {v['win']*100:3.0f}% PF {v['pf']:.2f} net ${v['net']:+8,.0f} maxDD ${v['maxdd']:,.0f}")

# ---------------- (2) forward cohorts
def client(s0, horizon=252):
    d = s0; evals = 0; spent = 0.0
    while evals < 5 and d < min(s0 + horizon, g.D):
        evals += 1; spent += FEE
        ok, n = eval_run([lg.get(x) for x in range(d, min(d + 180, g.D))], A, EVAL_RISK, 1440, 1)
        if ok:
            fd = d + n
            left = s0 + horizon - fd
            if left <= 0:
                return dict(funded=True, paid=False, cash=0.0, spent=spent, evals=evals, days_to_funded=fd - s0, first_pay=None)
            pays, blew, first = FH.funded([lg.get(x) for x in range(fd, min(fd + left, g.D))], A, FUND_RISK, None, A["mll"] + 300, 1000)
            return dict(funded=True, paid=bool(pays), cash=0.9 * sum(pays), spent=spent, evals=evals, days_to_funded=fd - s0,
                        first_pay=(fd - s0 + first) if first else None)
        d += n
    return dict(funded=False, paid=False, cash=0.0, spent=spent, evals=evals, days_to_funded=None, first_pay=None)

launches = ["2020-01-02", "2020-07-01", "2021-01-04", "2021-07-01", "2022-01-03", "2022-07-01", "2023-01-03", "2023-07-03", "2024-01-02", "2024-07-01", "2025-01-02", "2025-07-01", "2025-10-01"]
cohorts = {}
for L in launches:
    i0 = int(np.searchsorted(g.days, np.datetime64(L).astype(np.int64)))
    cl = [client(s) for s in range(i0, min(i0 + 63, g.D - 5), 3)]   # 21 clients spread over the first 3 months
    full = (i0 + 63 + 252) <= g.D
    cohorts[L] = dict(n=len(cl), funded=float(np.mean([c["funded"] for c in cl])), paid=float(np.mean([c["paid"] for c in cl])),
                      avg_cash=float(np.mean([c["cash"] for c in cl])), avg_spent=float(np.mean([c["spent"] for c in cl])),
                      net=float(np.mean([c["cash"] - c["spent"] for c in cl])),
                      pos=float(np.mean([c["cash"] - c["spent"] > 0 for c in cl])),
                      med_funded=float(np.median([c["days_to_funded"] for c in cl if c["days_to_funded"] is not None])) if any(c["funded"] for c in cl) else None,
                      complete=full)
    c = cohorts[L]
    print(f"launch {L}: funded {c['funded']*100:3.0f}% (median {c['med_funded']} d) | paid within 12m {c['paid']*100:3.0f}% | avg cash ${c['avg_cash']:,.0f} - fees ${c['avg_spent']:.0f} = net ${c['net']:+,.0f} | {c['pos']*100:.0f}% of clients net positive {'' if full else '(partial window)'}")

# ---------------- (3) forward projection by block bootstrap
rng = np.random.default_rng(21)
def proj(src_from, label, n=3000):
    idx = np.where(g.days >= np.datetime64(src_from).astype(np.int64))[0]
    out = {}
    for H in (63, 126, 252):
        tot = []
        for _ in range(n):
            p = []
            while len(p) < H:
                s = rng.integers(0, len(idx) - 10); p += list(idx[s:s + 10])
            tot.append(sum(500 * lg[d]["net"] / lg[d]["rct"] for d in p[:H] if d in lg))
        tot = np.array(tot)
        out[H] = dict(p_profit=float((tot > 0).mean()), p5=float(np.percentile(tot, 5)), med=float(np.median(tot)), p95=float(np.percentile(tot, 95)))
        print(f"  {label} next {H//21:2d} months @ $500/trade: P(profit) {out[H]['p_profit']*100:3.0f}%  5th ${out[H]['p5']:+,.0f}  median ${out[H]['med']:+,.0f}  95th ${out[H]['p95']:+,.0f}")
    return out
print("Forward projection:")
projections = {"2020-26 regime": proj("2020-01-01", "2020-26 regime"), "last 12 months regime": proj("2025-10-10", "last-12m regime")}
for c in cl: pass
json.dump(dict(curve=curve, cohorts=cohorts, projections=projections), open("results/equity_forward.json", "w"), default=float)
