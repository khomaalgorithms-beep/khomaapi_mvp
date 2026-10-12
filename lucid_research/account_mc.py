"""Stage 3: size selected strategies into contracts and run the full LucidFlex 50K path.

Rolling-start simulation: an evaluation is started on EVERY trading day of the chosen period and run
on the real subsequent days until pass / breach / timeout; each pass then starts a funded account on
the next day. This keeps real autocorrelation, losing streaks and regimes (no i.i.d. shuffling).
A stationary block bootstrap is provided as a second opinion."""
import numpy as np
import harness as Hn
from lucid_sim import run_eval, run_funded, SPLIT

MICRO = {"ES", "NQ", "RTY", "YM", "CL", "GC", "SI"}     # traded as micros (count 1/10 of a mini)


def build_days(strats, risk, all_dates, cap_minis=4.0, min_fill=0.67):
    """strats: list of trade dicts (from g1 pickle) each with 'sym' added. risk: $ risk per trade (or list).
    Returns list (aligned to all_dates) of [(pnl$, mae$)] sorted by entry time, plus stats."""
    pos = {d: i for i, d in enumerate(all_dates)}
    per = [[] for _ in all_dates]
    skipped = 0; total = 0
    for k, tr in enumerate(strats):
        rk = risk[k] if isinstance(risk, (list, tuple, np.ndarray)) else risk
        sp = Hn.SPEC[tr["sym"]]
        per_ct = tr["stop"] * sp["dpp"]                 # $ risk of one traded contract
        n = np.floor(rk / per_ct)
        n = np.where((n == 0) & (per_ct <= rk / min_fill), 1, n)   # allow 1 lot if risk <= 1.5x budget
        unit = 0.1 if tr["sym"] in MICRO else 1.0
        n = np.minimum(n, cap_minis / unit)
        pnl = tr["net"] * sp["dpp"] * n; mae = np.minimum(tr["mae"], 0) * sp["dpp"] * n
        for i in range(len(n)):
            total += 1
            if n[i] <= 0:
                skipped += 1; continue
            di = pos.get(tr["date"][i])
            if di is not None:
                per[di].append((int(tr["e"][i]), float(pnl[i]), float(mae[i])))
    days = [[(p, m) for _, p, m in sorted(x)] for x in per]
    return days, skipped / max(total, 1)


def rolling(days, dates, y0, y1, eval_kw=None, funded_kw=None, eval_max=120, funded_max=250, step=1):
    eval_kw = eval_kw or {}; funded_kw = funded_kw or {}
    yrs = np.array([int(str(d)[:4]) for d in dates])
    starts = np.where((yrs >= y0) & (yrs <= y1))[0][::step]
    out = []
    for s in starts:
        ev = run_eval(days[s:s + eval_max], **eval_kw)
        rec = dict(start=s, passed=ev.passed, breached=ev.breached, edays=ev.days)
        if ev.passed:
            f0 = s + ev.days
            fr = run_funded(days[f0:f0 + funded_max], **funded_kw)
            rec.update(fblow=fr.breached, fdays=fr.days, npay=len(fr.payouts), cash=fr.trader_cash,
                       first_pay=(fr.payout_days[0] if fr.payout_days else None), grad=fr.graduated,
                       fcomplete=(f0 + funded_max <= len(days)) or fr.breached or fr.graduated)
        out.append(rec)
    return out


def summarize(recs, fee=140.0, label=""):
    n = len(recs)
    p = np.array([r["passed"] for r in recs]); b = np.array([r["breached"] for r in recs])
    ed = np.array([r["edays"] for r in recs])
    fu = [r for r in recs if r["passed"]]
    pp = p.mean()
    s = dict(label=label, n=n, pass_rate=pp, breach_rate=b.mean(), timeout=(~p & ~b).mean(),
             med_days_pass=float(np.median(ed[p])) if p.any() else np.nan,
             med_days_breach=float(np.median(ed[b])) if b.any() else np.nan)
    if fu:
        s["funded_blow"] = np.mean([r["fblow"] for r in fu])
        s["p_payout"] = np.mean([r["npay"] > 0 for r in fu])
        s["avg_payouts"] = np.mean([r["npay"] for r in fu])
        s["avg_cash"] = np.mean([r["cash"] for r in fu])
        fp = [r["first_pay"] for r in fu if r["first_pay"]]
        s["med_first_pay_days"] = float(np.median(fp)) if fp else np.nan
        # economics of one "pass attempt chain": expected evals to pass = 1/p, each costs fee
        s["evals_per_pass"] = 1 / pp if pp > 0 else np.inf
        s["net_per_funded"] = s["avg_cash"] - fee / pp if pp > 0 else -fee
        s["net_per_eval_bought"] = pp * s["avg_cash"] - fee
    return s


def fmt(s):
    keys = ["label", "n", "pass_rate", "breach_rate", "timeout", "med_days_pass", "funded_blow", "p_payout",
            "avg_payouts", "avg_cash", "med_first_pay_days", "net_per_eval_bought"]
    out = []
    for k in keys:
        v = s.get(k, float("nan"))
        out.append(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}")
    return " ".join(out)


def block_bootstrap(days, n_paths=2000, block=10, horizon=370, seed=0, eval_kw=None, funded_kw=None,
                    eval_max=120, funded_max=250):
    rng = np.random.default_rng(seed)
    N = len(days); recs = []
    for _ in range(n_paths):
        path = []
        while len(path) < horizon:
            s = rng.integers(0, N - block); path += days[s:s + block]
        ev = run_eval(path[:eval_max], **(eval_kw or {}))
        rec = dict(passed=ev.passed, breached=ev.breached, edays=ev.days)
        if ev.passed:
            fr = run_funded(path[ev.days:ev.days + funded_max], **(funded_kw or {}))
            rec.update(fblow=fr.breached, npay=len(fr.payouts), cash=fr.trader_cash,
                       first_pay=(fr.payout_days[0] if fr.payout_days else None), grad=fr.graduated)
        recs.append(rec)
    return recs
