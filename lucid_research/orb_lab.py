"""ORB lab (see ORB_PREREGISTRATION.md). Builds opening-range trades with configurable entry/filters/exits
and simulates them with harness.simulate (real per-market costs)."""
import numpy as np, harness as Hn
from strategies import _base_days, _first, INF
MARKETS = ["NQ", "ES", "YM", "RTY", "GC", "SI", "CL", "E6"]
PER = {"SEL": ("2016-01-01", "2023-12-31"), "BACK": ("2008-03-01", "2015-12-31"), "HO": ("2024-01-01", "2026-10-31")}
_G = {}
def G(sym):
    if sym not in _G: _G[sym] = Hn.load(sym)
    return _G[sym]

def trades(g, n=15, entry="touch", cutoff=120, filt=(), stop="opp", stop_k=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=True))[0]
    O, H, L, C = g.O[days], g.H[days], g.L[days], g.C[days]
    orh = H[:, a:a + n].max(1); orl = L[:, a:a + n].min(1); w = orh - orl; tick = g.spec["tick"]
    st = np.full(len(days), a + n); en = np.full(len(days), min(a + n + cutoff, b - 2))
    if entry == "touch":
        ju = _first(H >= (orh + tick)[:, None], st, en); jd = _first(L <= (orl - tick)[:, None], st, en)
    else:  # close beyond the edge, enter next bar's open
        ju = _first(C > orh[:, None], st, en); jd = _first(C < orl[:, None], st, en)
    ju = np.where(ju < 0, INF, ju); jd = np.where(jd < 0, INF, jd)
    j = np.minimum(ju, jd); side = np.where(ju < jd, 1, np.where(jd < ju, -1, 0))
    ok = (j < INF) & (w > 0) & (side != 0)
    if entry == "touch":
        e = np.where(ok, j, 0)
        ob = O[np.arange(len(days)), np.minimum(e, Hn.M - 1)]
        px = np.where(side > 0, np.maximum(orh + tick, ob), np.minimum(orl - tick, ob))
    else:
        e = np.where(ok, j + 1, 0)
        px = O[np.arange(len(days)), np.minimum(e, Hn.M - 1)]
    atr = g.atr14[days]
    # ---- confluences (all known at/before entry) ----
    f = np.ones(len(days), bool)
    for k in filt:
        if k in ("trend20", "trend50"): f &= np.sign(getattr(g, k)[days]) == side
        elif k == "gap": f &= np.sign(g.s_open[days] - g.p_close[days]) == side
        elif k == "pdl": f &= np.where(side > 0, px > g.p_high[days], px < g.p_low[days])
        elif k == "onl": f &= np.where(side > 0, px > g.on_high[days], px < g.on_low[days])
        elif k == "narrow": f &= w < 0.25 * atr
        elif k == "wide": f &= w > 0.5 * atr
        elif k in ("nr7", "inside"): f &= getattr(g, k)[days].astype(bool)
        elif k == "hivol": f &= g.volpct[days] > 0.5
        elif k == "lovol": f &= g.volpct[days] < 0.5
        elif k == "firstbar": f &= np.sign(C[:, a + 4] - O[:, a]) == side
        else: raise ValueError(k)
    ok &= f & np.isfinite(px)
    # ---- stop distance ----
    if stop == "opp": sd = np.where(side > 0, px - orl, orh - px)
    elif stop == "mid": sd = np.abs(px - (orh + orl) / 2)
    else: sd = stop_k * atr
    ok &= np.isfinite(sd) & (sd >= 2 * tick)
    return dict(d=days[ok], e=e[ok], side=side[ok], px=px[ok], stop=sd[ok], w=w[ok], atr=atr[ok])

def run(sym, tgt_R=None, be=None, trail=None, slip=1.0, rand=None, **kw):
    g = G(sym); t = trades(g, **kw)
    side = t["side"]
    if rand is not None:
        side = np.random.default_rng(rand).choice([-1.0, 1.0], len(side))
    tg = None if tgt_R is None else tgt_R * t["stop"]
    r = Hn.simulate(g, t["d"], t["e"], side, t["px"], t["stop"], tg, np.full(len(t["d"]), g.b), trail, be, slip_mult=slip)
    keep = ~g.suspect[r["d"]]
    return dict(day=g.days[r["d"]][keep], R=r["R"][keep])

def stats(res, per):
    a, b = PER[per] if isinstance(per, str) else per
    m = (res["day"] >= np.datetime64(a).astype(np.int64)) & (res["day"] <= np.datetime64(b).astype(np.int64))
    r = res["R"][m]; n = len(r)
    if n < 20: return dict(n=n, mean=np.nan, t=np.nan, pf=np.nan, win=np.nan, tot=0.0, dd=np.nan, rd=np.nan, tpy=0)
    eq = np.cumsum(r); dd = -(eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min()
    yrs = (np.datetime64(b) - np.datetime64(a)).astype(int) / 365.25
    return dict(n=n, mean=r.mean(), t=r.mean() / r.std(ddof=1) * np.sqrt(n), pf=r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
                win=(r > 0).mean(), tot=r.sum(), dd=dd, rd=r.sum() / max(dd, 1e-9), tpy=n / yrs)
