"""Stress-test the final NQ fast-pass logic. Exact minute replay of the LucidFlex 50K eval.
Each scenario rebuilds the chain trades under the stress, then reports:
  trade level (per MNQ, 2020-26): avg $/trade, PF, win%   |   Lucid: pass %, median days, edge vs luck."""
import sys, json, numpy as np
from multiprocessing import Pool
import harness as Hn, strategies as S
from lucid_sim import Account, START, TARGET

BASE_CHAIN = [("tod_mom", dict(T=60, k=0.0, s=0.3, ref="open", tf=20, trail=None), 1.0),
              ("tod_mom", dict(T=90, k=0.0, s=0.3, ref="open", tf=20, trail=None), 1.0),
              ("tod_mom", dict(T=150, k=0.0, s=0.3, ref="open", tf=20, trail=None), 1.0),
              ("noise_area", dict(step=30, mult=1.0, s=0.5, tf=20, trail=(1.0, 1.0)), 1.0),
              ("tod_mom", dict(T=60, k=0.0, s=0.3, ref="open", tf=None, trail=None), 0.5)]
G = None
# FINAL system after the look-ahead fix: Spec A only (10:30 with-trend). The multi-signal list is NOT used.
FINAL = BASE_CHAIN[:1]


def legs(cfg):
    """cfg: comm (RT $ per MNQ), slip (ticks/side), delay (min), skip (prob), seed, chain, direction ('real'|'random'),
    drop_top (fraction of best trades removed), years (tuple) for trade stats."""
    global G
    if G is None:
        G = Hn.load("NQ")
    g = G; dpp = 2.0; tick = 0.25
    g.spec = dict(g.spec, comm=cfg.get("comm", 1.0))
    slip = tick * cfg.get("slip", 1)
    rng = np.random.default_rng(cfg.get("seed", 0))
    taken = {}
    for fam, p, mult in cfg.get("chain", FINAL):
        tr = getattr(S, fam)(g, **p)
        if len(tr["d"]) == 0:
            continue
        e = tr["e"] + cfg.get("delay", 0)
        px = g.O[tr["d"], np.minimum(e, Hn.M - 1)] if cfg.get("delay", 0) else tr["px"]
        side = tr["side"]
        if cfg.get("direction") == "random":
            side = rng.choice([-1.0, 1.0], len(side))
        if cfg.get("time") == "random":       # same days/sides, random entry minute AFTER the signal (no look-ahead)
            e = np.minimum(e + rng.integers(0, 181, len(e)), Hn.t("15:00")); px = g.O[tr["d"], e]
        r = Hn.simulate(g, tr["d"], e, side, px, tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"],
                        slip_mult=cfg.get("slip", 1))
        for i, d in enumerate(r["d"]):
            d = int(d)
            ei = int(r["e"][i]); x = ei + int(r["hold"][i]); sd = float(r["side"][i])
            # REAL-TIME selection: the signal that fires FIRST in time wins (priority only breaks exact ties).
            # (The old priority-first rule kept low-priority early trades only when later signals did not
            #  fire — look-ahead. Set cfg["lookahead_chain"]=True only to reproduce that biased version.)
            if d in taken and (cfg.get("lookahead_chain") or taken[d]["e"] <= ei):
                continue
            fill = float(px[i]) + sd * slip
            H = g.H[d, ei:x + 1].astype(float); L = g.L[d, ei:x + 1].astype(float); C = g.C[d, ei:x + 1].astype(float)
            best = (np.where(sd > 0, H, -L) - sd * fill) * dpp; worst = (np.where(sd > 0, L, -H) - sd * fill) * dpp
            best[0] = worst[0] = (C[0] - fill) * sd * dpp
            cm = cfg.get("comm", 1.0)
            taken[d] = dict(e=ei, x=x, best=best - cm, worst=worst - cm, net=float(r["net"][i]) * dpp,
                            rct=float(r["stop"][i]) * dpp, mult=mult, R=float(r["R"][i]))
    if cfg.get("skip", 0) > 0:
        taken = {d: v for d, v in taken.items() if rng.random() >= cfg["skip"]}
    if cfg.get("drop_top", 0) > 0:
        ds = [d for d in taken if g.year[d] >= 2020]
        nets = np.array([taken[d]["net"] / taken[d]["rct"] for d in ds])
        cut = np.quantile(nets, 1 - cfg["drop_top"])
        for d, v in zip(ds, nets):
            if v >= cut:
                taken[d] = dict(taken[d], best=np.minimum(taken[d]["best"], 0) * 0, worst=taken[d]["worst"] * 0, net=0.0)  # day removed -> flat
    if cfg.get("zero_edge"):
        ds = [d for d in taken if g.year[d] >= 2020]
        mu = np.mean([taken[d]["net"] / taken[d]["rct"] for d in ds])
        taken = {d: dict(v, best=v["best"] - mu * v["rct"], worst=v["worst"] - mu * v["rct"], net=v["net"] - mu * v["rct"]) for d, v in taken.items()}
    return taken


def day(L, budget, a, dps, cap, through, dll, breach_any, cons=0.5):
    if L is None:
        return 0.0, False
    r_ = budget * L["mult"]
    q = int(r_ // L["rct"]); q = 1 if q == 0 and L["rct"] <= 1.5 * r_ else q; q = min(q, 40)
    if q <= 0:
        return 0.0, False
    B = L["best"] * q; W = L["worst"] * q
    need = START + TARGET - a.balance; best = max(dps) if dps else 0.0
    tp = min(max(need, best / cons - (a.balance - START)) if best > 0 else need, cap, cons * TARGET * 0.97)
    floor = a.mll if not breach_any else a.balance - 1e9
    br = np.where(a.balance + W <= a.mll)[0]
    hit = np.where(B >= tp + through * 0.5 * q)[0]
    ib = br[0] if len(br) else 10 ** 9; ih = hit[0] if len(hit) else 10 ** 9
    if dll is not None:                                   # optional daily loss limit: soft stop at -dll
        dl = np.where(W <= -dll)[0]; idl = dl[0] if len(dl) else 10 ** 9
        if idl < min(ib, ih):
            return -dll, False
    if ib <= ih and ib < 10 ** 9:
        return None, True
    if ih < 10 ** 9:
        return tp, False
    return L["net"] * q, False


def run_eval(legs_, days, s0, budget=1000, cap=1450, through=1, cons=0.5, dll=None, breach_any=False, maxd=120):
    a = Account(); dps = []
    for n in range(1, maxd + 1):
        i = s0 + n - 1
        if i >= len(days):
            break
        dp, br = day(legs_.get(days[i]), budget, a, dps, cap, through, dll, breach_any, cons)
        if br:
            return False, True, n
        a.balance += dp; dps.append(dp)
        if a.balance >= START + TARGET and max(dps) <= cons * (a.balance - START):
            return True, False, n
        a.eod_update()
    return False, False, maxd


def scenario(cfg):
    global G
    lg = legs(cfg)
    g = G
    y0, y1 = cfg.get("years", (2020, 2026))
    ds = [d for d in sorted(lg) if y0 <= g.year[d] <= y1]
    net = np.array([lg[d]["net"] / max(1, int(1000 * lg[d]["mult"] // lg[d]["rct"]) or 1) * 0 + lg[d]["net"] for d in ds])  # per 1 MNQ
    pf = net[net > 0].sum() / -net[net < 0].sum() if (net < 0).any() else np.nan
    out = dict(name=cfg["name"], n=len(net), avg=float(net.mean()), pf=float(pf), win=float((net > 0).mean()))
    days = np.arange(g.D)
    starts = np.where((g.year >= y0) & (g.year <= y1))[0]
    kw = {k: cfg[k] for k in ("budget", "cap", "through", "cons", "dll", "breach_any") if k in cfg}
    res = [run_eval(lg, days, s, **kw) for s in starts]
    p = np.array([r[0] for r in res]); dd = np.array([r[2] for r in res])
    out.update(pass_=float(p.mean()), med=float(np.median(dd[p])) if p.any() else np.nan)
    if cfg.get("luck", True):
        ps = []
        for sd in range(1, 6):
            z = legs(dict(cfg, direction="random", seed=100 + sd))
            ps.append(np.mean([run_eval(z, days, s, **kw)[0] for s in starts]))
        out["luck"] = float(np.mean(ps))
    return out


def fmt(o):
    lk = o.get("luck", np.nan)
    return (f"{o['name']:46} | {o['n']:5d} trades  ${o['avg']:+7.2f}/MNQ  PF {o['pf']:4.2f}  win {o['win']*100:3.0f}% | "
            f"PASS {o['pass_']*100:3.0f}%  {o['med']:3.0f}d  luck {lk*100:3.0f}%  edge {(o['pass_']-lk)*100:+4.0f}")


if __name__ == "__main__":
    grp = sys.argv[1]
    C = []
    if grp == "costs":
        for cm in (1.0, 1.5, 2.0, 3.0):
            for sl in (1, 2, 4, 6, 8, 10):
                C.append(dict(name=f"comm ${cm:.2f} RT, slip {sl} tick/side", comm=cm, slip=sl, luck=(sl in (1, 4, 8))))
        for th in (2, 4):
            C.append(dict(name=f"TP needs {th}-tick trade-through", through=th, luck=False))
        C.append(dict(name="WORST: $3 RT + 8 ticks + 4-tick TP", comm=3.0, slip=8, through=4))
    elif grp == "exec":
        for dl in (1, 5, 15):
            C.append(dict(name=f"enter {dl} min late", delay=dl, luck=False))
        for sk in (0.1, 0.3, 0.5):
            for sd in (1, 2, 3):
                C.append(dict(name=f"skip {sk*100:.0f}% of signals (seed {sd})", skip=sk, seed=sd, luck=False))
    elif grp == "params":
        ch = lambda **o: [(f, dict(p, **{k: v for k, v in o.items() if k in p and f == "tod_mom"}), m) for f, p, m in BASE_CHAIN]
        for s_ in (0.2, 0.25, 0.35, 0.4, 0.5):
            C.append(dict(name=f"stop {s_}xATR (all tod legs)", chain=ch(s=s_), luck=False))
        for tf in (10, 50):
            C.append(dict(name=f"trend filter {tf}d", chain=[(f, dict(p, tf=(tf if p.get('tf') else None)), m) for f, p, m in BASE_CHAIN], luck=False))
        C.append(dict(name="first signal 10:15 instead of 10:30", chain=[(f, dict(p, T=45) if (f == 'tod_mom' and p['T'] == 60) else p, m) for f, p, m in BASE_CHAIN], luck=False))
        C.append(dict(name="first signal 10:45 instead of 10:30", chain=[(f, dict(p, T=75) if (f == 'tod_mom' and p['T'] == 60) else p, m) for f, p, m in BASE_CHAIN], luck=False))
        C.append(dict(name="no counter-trend leg", chain=BASE_CHAIN[:4], luck=False))
        C.append(dict(name="counter-trend leg full size", chain=BASE_CHAIN[:4] + [(BASE_CHAIN[4][0], BASE_CHAIN[4][1], 1.0)], luck=False))
        for b in (800, 900, 1100, 1200):
            C.append(dict(name=f"risk ${b}", budget=b, luck=False))
        for cp in (1000, 1250, 1500):
            C.append(dict(name=f"daily TP ${cp}", cap=cp, luck=False))
    elif grp == "placebo":
        for sd in range(1, 6):
            C.append(dict(name=f"RANDOM direction (seed {sd})", direction="random", seed=sd, luck=False))
        for sd in range(1, 4):
            C.append(dict(name=f"RANDOM entry time (seed {sd})", time="random", seed=sd, luck=False))
        C.append(dict(name="remove top 5% best trades", drop_top=0.05, luck=False))
        C.append(dict(name="remove top 10% best trades", drop_top=0.10, luck=False))
    elif grp == "regime":
        for y in range(2008, 2027):
            C.append(dict(name=f"year {y}", years=(y, y), luck=False))
        C.append(dict(name="2008-2019 (pre-regime)", years=(2008, 2019)))
    elif grp == "fix":
        for sd in range(1, 4):
            C.append(dict(name=f"RANDOM entry 0-3h AFTER signal (seed {sd})", time="random", seed=sd, luck=False))
        C.append(dict(name="consistency 40% (TP adapted)", cons=0.40, luck=False))
        C.append(dict(name="WORST costs: $3 RT + 8 ticks + 4-tick TP", comm=3.0, slip=8, through=4))
        C.append(dict(name="REALISTIC BAD: $2 RT + 4 ticks + 30% missed + 1min late", comm=2.0, slip=4, skip=0.3, delay=1, seed=1))
    elif grp == "rules":
        C.append(dict(name="consistency 40% (stricter)", cons=0.40, luck=False))
        C.append(dict(name="optional DLL $1,200", dll=1200, luck=False))
        C.append(dict(name="optional DLL $800", dll=800, luck=False))
    C.insert(0, dict(name="BASELINE (Lucid $1.00 RT, 1 tick)"))
    with Pool(5) as pool:
        for o in pool.imap(scenario, C):
            print(fmt(o), flush=True)
