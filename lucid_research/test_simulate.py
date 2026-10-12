"""Cross-check the vectorized simulator against a slow bar-by-bar reference on random trades."""
import numpy as np, harness as Hn

def ref(g, d, e, side, px, stop, tgt, xm, trail, slip_mult=1.0):
    sp = g.spec; slip = sp["tick"] * sp["slip"] * slip_mult; tick = sp["tick"]
    fill = px + side * slip
    peak = -np.inf; lvl = -stop
    for j in range(e, xm):
        h, l, c, o = (float(x[d, j]) for x in (g.H, g.L, g.C, g.O))
        if j == e:
            fav = adv = (c - fill) * side
        else:
            fav = (h - fill) if side > 0 else (fill - l)
            adv = (l - fill) if side > 0 else (fill - h)
        if trail is not None and peak >= trail[0] * stop:
            lvl = max(lvl, peak - trail[1] * stop)
        if adv <= lvl:
            if j == e:
                return min(lvl, adv) - slip
            opR = (float(g.O[d, j]) - fill) * side
            return min(lvl, opR) - slip
        if tgt is not None and fav >= tgt + tick:
            return tgt
        peak = max(peak, fav)
    return (float(g.O[d, xm]) - fill) * side - slip

g = Hn.load("ES")
rng = np.random.default_rng(1)
n = 400
d = rng.choice(np.where(g.sess_ok)[0], n); e = rng.integers(Hn.t("09:30"), Hn.t("15:00"), n)
side = rng.choice([-1.0, 1.0], n); px = g.O[d, e].astype(float); stop = rng.uniform(1, 15, n)
xm = np.full(n, Hn.t("16:00"))
for tgtR, trail in [(None, None), (2.0, None), (None, (1.0, 1.0)), (3.0, (2.0, 1.0))]:
    tg = None if tgtR is None else tgtR * stop
    r = Hn.simulate(g, d, e, side, px, stop, tg, xm, trail)
    want = np.array([ref(g, d[k], e[k], side[k], px[k], stop[k], None if tg is None else tg[k], xm[k], trail) for k in range(n)])
    bad = np.abs(r["gross"] - want) > 1e-3
    print(tgtR, trail, "mismatches", bad.sum(), "of", n, "| mean gross", round(r["gross"].mean(), 3))
