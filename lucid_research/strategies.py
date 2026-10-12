"""Parametric strategy families. Each family(g, **p) returns a trade batch dict for harness.simulate:
d (day), e (entry bar; fill happens inside bar e at px), side, px, stop (price distance), optional tgt
(price distance), xm (time-exit bar), trail, be. Signals only use information available at the entry
bar (bar closes strictly before e, or a resting stop/limit price touched inside bar e).
Every (family, params, symbol) run counts as one trial for multiple-testing control."""
import itertools
import numpy as np
from harness import t, FLAT, M

INF = 10 ** 9


def _base_days(g, level=False):
    ok = g.sess_ok & ~np.isnan(g.atr14) & (g.atr14 > 0)
    if level:
        ok &= ~g.suspect & ~np.isnan(g.p_high)
    return ok


def _first(mask, start, end):
    """first column index in [start, end) where mask is True (per row); start/end arrays; -1 if none."""
    cols = np.arange(mask.shape[1])[None, :]
    m = mask & (cols >= np.asarray(start)[:, None]) & (cols < np.asarray(end)[:, None])
    has = m.any(1)
    return np.where(has, m.argmax(1), -1)


def _pack(d, e, side, px, stop, tgt=None, xm=None, trail=None, be=None, tf=None, g=None):
    d = np.asarray(d, int); e = np.asarray(e, int); side = np.asarray(side, float)
    px = np.asarray(px, float); stop = np.asarray(stop, float)
    keep = (stop > 0) & np.isfinite(stop) & np.isfinite(px) & (side != 0) & (e >= 0)
    if tf is not None:                             # trend filter: trade only with the n-day trend
        tr = getattr(g, f"trend{tf}")[d]
        keep &= (np.sign(tr) == side)
    sel = lambda x: None if x is None else (np.asarray(x)[keep] if np.ndim(x) else x)
    return dict(d=d[keep], e=e[keep], side=side[keep], px=px[keep], stop=stop[keep],
                tgt=sel(tgt), xm=sel(xm), trail=trail, be=be)


def _tgt(R, stop):
    return None if R is None else R * stop


# ------------------------------------------------------------------ 1. opening-range breakout
def orb(g, n=15, stopmode="opp", R=None, cutoff=180, wmin=0.0, wmax=9.0, tf=None, trail=None, filt=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=filt in ("nr7", "inside", "nr4")))[0]
    if filt:
        days = days[getattr(g, filt)[days]]
    O, H, L = g.O[days], g.H[days], g.L[days]
    orh = H[:, a:a + n].max(1); orl = L[:, a:a + n].min(1); w = orh - orl
    tick = g.spec["tick"]
    st = np.full(len(days), a + n); en = np.full(len(days), min(a + n + cutoff, b - 1))
    ju = _first(H >= (orh + tick)[:, None], st, en); jd = _first(L <= (orl - tick)[:, None], st, en)
    ju = np.where(ju < 0, INF, ju); jd = np.where(jd < 0, INF, jd)
    e = np.minimum(ju, jd); side = np.where(ju < jd, 1, np.where(jd < ju, -1, 0))
    ok = (e < INF) & (w >= wmin * g.atr14[days]) & (w <= wmax * g.atr14[days]) & (w > 0)
    days, e, side, orh, orl, w = days[ok], e[ok], side[ok], orh[ok], orl[ok], w[ok]
    ob = g.O[days, e]
    px = np.where(side > 0, np.maximum(orh + tick, ob), np.minimum(orl - tick, ob))
    lvl = orl if stopmode == "opp" else (orh + orl) / 2
    lvl = np.where(side > 0, lvl, orh if stopmode == "opp" else (orh + orl) / 2)
    stop = np.abs(px - lvl)
    return _pack(days, e, side, px, stop, _tgt(R, stop), np.full(len(days), b), trail, None, tf, g)


# ------------------------------------------------------------------ 2. first-candle direction (Zarattini ORB)
def first_candle(g, n=5, R=10, tf=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g))[0]
    op = g.O[days, a]; cl = g.C[days, a + n - 1]
    hi = g.H[days, a:a + n].max(1); lo = g.L[days, a:a + n].min(1)
    side = np.sign(cl - op)
    side[np.abs(cl - op) < 0.05 * (hi - lo)] = 0          # doji skip
    e = np.full(len(days), a + n); px = g.O[days, a + n]
    stop = np.where(side > 0, px - lo, hi - px)
    return _pack(days, e, side, px, stop, _tgt(R, stop), np.full(len(days), b), None, None, tf, g)


# ------------------------------------------------------------------ 3. time-of-day momentum (decide at T, hold to close)
def tod_mom(g, T=60, k=0.0, s=0.5, ref="open", tf=None, trail=None, xmode="close"):
    """at anchor+T minutes: side = sign(price - ref); |move| >= k*ATR; stop = s*ATR; exit at session close."""
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=(ref == "pclose")))[0]
    e = a + T
    if e >= b - 5:
        return _pack([], [], [], [], [])
    refp = g.O[days, a] if ref == "open" else (g.p_close[days] if ref == "pclose" else g.O[days, 0])
    p = g.C[days, e - 1]
    mv = p - refp
    side = np.sign(mv) * (np.abs(mv) >= k * g.atr14[days])
    px = g.O[days, e]; stop = s * g.atr14[days]
    xm = np.full(len(days), b if xmode == "close" else min(e + 60, b))
    return _pack(days, np.full(len(days), e), side, px, stop, None, xm, trail, None, tf, g)


# ------------------------------------------------------------------ 4. Gao et al.: first half-hour predicts last half-hour
def gao_last(g, first=30, last=30, ref="pclose", s=0.3, tf=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=(ref == "pclose")))[0]
    refp = g.p_close[days] if ref == "pclose" else g.O[days, 0]
    r1 = g.C[days, a + first - 1] - refp
    side = np.sign(r1)
    e = b - last
    px = g.O[days, e]
    return _pack(days, np.full(len(days), e), side, px, s * g.atr14[days], None, np.full(len(days), b), None, None, tf, g)


# ------------------------------------------------------------------ 5. noise-area momentum (Zarattini-Aziz-Barbon)
def noise_area(g, look=14, step=30, mult=1.0, s=0.5, tf=None, trail=(1.0, 1.0)):
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=True))[0]
    D = g.D
    span = b - a
    move = np.abs(g.C[:, a:b] / np.where(g.O[:, a:a + 1] == 0, np.nan, g.O[:, a:a + 1]) - 1)   # |C(t)/open - 1|
    move = np.where(np.isfinite(move), move, np.nan)
    # sigma(t): mean over prior `look` sessions at the same minute
    cs = np.nancumsum(np.nan_to_num(move), axis=0)
    sig = np.full_like(move, np.nan)
    sig[look:] = (cs[look - 1:-1] - np.vstack([np.zeros((1, span)), cs[:-look - 1]])) / look
    sig = sig[days]
    op = g.O[days, a]; pc = g.p_close[days]
    ub = np.maximum(op, pc)[:, None] * (1 + mult * sig); lb = np.minimum(op, pc)[:, None] * (1 - mult * sig)
    checks = np.arange(step, span - 5, step)
    C = g.C[days, a:b]
    up = (C[:, checks - 1] > ub[:, checks - 1]); dn = (C[:, checks - 1] < lb[:, checks - 1])
    any_ = up | dn
    first = np.where(any_.any(1), any_.argmax(1), -1)
    ok = first >= 0
    days, first = days[ok], first[ok]
    side = np.where(up[ok, first], 1, -1)
    e = a + checks[first]
    px = g.O[days, e]
    return _pack(days, e, side, px, s * g.atr14[days], None, np.full(len(days), b), trail, None, tf, g)


# ------------------------------------------------------------------ 6. VWAP sigma-band fade (mean reversion)
def vwap_fade(g, k=2.0, start=30, stopk=1.0, end_before=60, tgtmode="vwap", tf=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g))[0]
    Hh, Ll, Cc, Vv = (x[days, a:b].astype(np.float64) for x in (g.H, g.L, g.C, g.V))
    tp = (Hh + Ll + Cc) / 3; vv = np.maximum(Vv, 1e-9)
    cv = np.cumsum(vv, 1); vwap = np.cumsum(tp * vv, 1) / cv
    var = np.maximum(np.cumsum(tp * tp * vv, 1) / cv - vwap ** 2, 0); sd = np.sqrt(var)
    span = b - a
    st = np.full(len(days), start); en = np.full(len(days), span - end_before)
    ju = _first(Cc > vwap + k * sd, st, en); jd = _first(Cc < vwap - k * sd, st, en)
    ju = np.where(ju < 0, INF, ju); jd = np.where(jd < 0, INF, jd)
    j = np.minimum(ju, jd); ok = j < INF
    days, j, ju_ = days[ok], j[ok], ju[ok]
    side = np.where(ju_ == j, -1, 1)
    r = np.arange(len(days))
    e = a + j + 1
    px = g.O[days, e]
    sdj = sd[ok][r, j]; vw = vwap[ok][r, j]
    stop = np.maximum(stopk * sdj, 2 * g.spec["tick"])
    tgt = np.maximum((vw - px) * side, 0) if tgtmode == "vwap" else None
    tgt = None if tgt is None else np.where(tgt > 0, tgt, np.nan)
    return _pack(days, e, side, px, stop, tgt, np.full(len(days), b), None, None, tf, g)


# ------------------------------------------------------------------ 7. prior-day high/low sweep & reclaim (turtle soup)
def pd_sweep(g, within=30, R=2.0, start=0, tgtmode="R", tf=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=True))[0]
    Hh, Ll, Cc = g.H[days, a:b], g.L[days, a:b], g.C[days, a:b]
    ph, pl = g.p_high[days], g.p_low[days]
    tick = g.spec["tick"]
    span = b - a
    st = np.full(len(days), start); en = np.full(len(days), span - 60)
    su = _first(Hh > (ph + tick)[:, None], st, en); sd_ = _first(Ll < (pl - tick)[:, None], st, en)
    out = []
    for side, sw, lvl in ((-1, su, ph), (1, sd_, pl)):
        ok = sw >= 0
        dd, sw2, lv = days[ok], sw[ok], lvl[ok]
        Hs, Ls, Cs = Hh[ok], Ll[ok], Cc[ok]
        back = (Cs < lv[:, None]) if side < 0 else (Cs > lv[:, None])
        j = _first(back, sw2, np.minimum(sw2 + within, span - 30))
        ok2 = j >= 0
        dd, sw2, j, lv = dd[ok2], sw2[ok2], j[ok2], lv[ok2]
        Hs, Ls = Hs[ok2], Ls[ok2]
        cols = np.arange(span)[None, :]
        win = (cols >= sw2[:, None]) & (cols <= j[:, None])
        ext = np.where(win, Hs, -np.inf).max(1) if side < 0 else np.where(win, Ls, np.inf).min(1)
        e = a + j + 1
        px = g.O[dd, e]
        stop = np.abs(ext - px) + tick
        out.append((dd, e, np.full(len(dd), side), px, stop))
    dd, e, sd2, px, stop = (np.concatenate([o[i] for o in out]) for i in range(5))
    # one trade per day: earliest
    order = np.lexsort((e, dd)); dd, e, sd2, px, stop = dd[order], e[order], sd2[order], px[order], stop[order]
    firstk = np.ones(len(dd), bool); firstk[1:] = dd[1:] != dd[:-1]
    dd, e, sd2, px, stop = dd[firstk], e[firstk], sd2[firstk], px[firstk], stop[firstk]
    return _pack(dd, e, sd2, px, stop, _tgt(R, stop), np.full(len(dd), b), None, None, tf, g)


# ------------------------------------------------------------------ 8. gap fade / gap go
def gap(g, lo=0.1, hi=0.6, mode="fade", wait=0, s=1.0, tf=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=True))[0]
    gp = g.O[days, a] - g.p_close[days]
    at = g.atr14[days]
    ok = (np.abs(gp) >= lo * at) & (np.abs(gp) <= hi * at)
    days, gp, at = days[ok], gp[ok], at[ok]
    e = a + wait
    px = g.O[days, e]
    if mode == "fade":
        side = -np.sign(gp)
        tgt = np.maximum((g.p_close[days] - px) * side, 0); tgt = np.where(tgt > 0, tgt, np.nan)
        stop = np.maximum(s * np.abs(gp), 0.1 * at)
    else:
        side = np.sign(gp) * (np.sign(g.C[days, e - 1] - g.O[days, a]) == np.sign(gp) if wait > 0 else np.sign(gp))
        side = np.sign(gp) * (side != 0)
        tgt = None; stop = s * 0.5 * at
    return _pack(days, np.full(len(days), e), side, px, stop, tgt, np.full(len(days), b), None, None, tf, g)


# ------------------------------------------------------------------ 9. overnight / intra-session drift windows
def window_drift(g, start="18:00", end="09:25", direction="long", s=1.0):
    days = np.where(_base_days(g))[0]
    e0, x0 = t(start), t(end)
    if x0 <= e0:
        return _pack([], [], [], [], [])
    if direction == "long":
        side = np.ones(len(days))
    elif direction == "trend":
        side = g.trend20[days]
    else:
        side = -np.ones(len(days))
    keep = ~g.roll_detected[days]                       # never span an intra-session roll
    days, side = days[keep], side[keep]
    px = g.O[days, e0]
    return _pack(days, np.full(len(days), e0), side, px, s * g.atr14[days], None, np.full(len(days), x0), None, None, None, g)


# ------------------------------------------------------------------ 10. Williams volatility breakout / Crabel stretch
def vol_breakout(g, k=0.5, basis="prange", stopk=None, filt=None, tf=None, cutoff=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=(basis == "prange" or filt is not None)))[0]
    if filt:
        days = days[getattr(g, filt)[days]]
    if basis == "prange":
        dist = k * g.p_range[days]
    else:  # crabel stretch: 10-day avg of min(H-O, O-L) of main session
        st = np.minimum(g.s_high - g.s_open, g.s_open - g.s_low).astype(float)
        from harness import _sma_prev
        dist = k * _sma_prev(st, 10)[days]
    op = g.O[days, a]
    up, dn = op + dist, op - dist
    span_end = b - 30 if cutoff is None else min(a + cutoff, b - 30)
    st_ = np.full(len(days), a + 1); en = np.full(len(days), span_end)
    ju = _first(g.H[days] >= up[:, None], st_, en); jd = _first(g.L[days] <= dn[:, None], st_, en)
    ju = np.where(ju < 0, INF, ju); jd = np.where(jd < 0, INF, jd)
    e = np.minimum(ju, jd); side = np.where(ju < jd, 1, np.where(jd < ju, -1, 0))
    ok = (e < INF) & np.isfinite(dist) & (dist > 0)
    days, e, side, up, dn, dist = days[ok], e[ok], side[ok], up[ok], dn[ok], dist[ok]
    ob = g.O[days, e]
    px = np.where(side > 0, np.maximum(up, ob), np.minimum(dn, ob))
    stop = (2 * dist + np.abs(px - np.where(side > 0, up, dn))) if stopk is None else stopk * g.atr14[days]
    return _pack(days, e, side, px, stop, None, np.full(len(days), b), None, None, tf, g)


# ------------------------------------------------------------------ 11. session range breakout (London / Asian / overnight range)
def range_break(g, r0="03:00", r1="08:00", until="11:30", exitt=None, stopmode="opp", R=None, tf=None):
    a, b = g.a, g.b
    c0, c1, cu = t(r0), t(r1), t(until)
    if not (c0 < c1 < cu):
        return _pack([], [], [], [], [])
    days = np.where(_base_days(g))[0]
    rh = g.H[days, c0:c1].max(1); rl = g.L[days, c0:c1].min(1); w = rh - rl
    tick = g.spec["tick"]
    st = np.full(len(days), c1); en = np.full(len(days), cu)
    ju = _first(g.H[days] >= (rh + tick)[:, None], st, en); jd = _first(g.L[days] <= (rl - tick)[:, None], st, en)
    ju = np.where(ju < 0, INF, ju); jd = np.where(jd < 0, INF, jd)
    e = np.minimum(ju, jd); side = np.where(ju < jd, 1, np.where(jd < ju, -1, 0))
    ok = (e < INF) & (w > 0) & (w < 2 * g.atr14[days])
    days, e, side, rh, rl = days[ok], e[ok], side[ok], rh[ok], rl[ok]
    ob = g.O[days, e]
    px = np.where(side > 0, np.maximum(rh + tick, ob), np.minimum(rl - tick, ob))
    lvl = np.where(side > 0, rl, rh) if stopmode == "opp" else (rh + rl) / 2
    stop = np.abs(px - lvl)
    xm = np.full(len(days), t(exitt) if exitt else b)
    return _pack(days, e, side, px, stop, _tgt(R, stop), xm, None, None, tf, g)


# ------------------------------------------------------------------ 12. opening-drive fade / reversal at T
def drive_fade(g, T=30, k=0.5, R=1.0, tf=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g))[0]
    mv = g.C[days, a + T - 1] - g.O[days, a]
    side = -np.sign(mv) * (np.abs(mv) >= k * g.atr14[days])
    hi = g.H[days, a:a + T].max(1); lo = g.L[days, a:a + T].min(1)
    e = a + T; px = g.O[days, e]
    stop = np.where(side < 0, hi - px, px - lo) + g.spec["tick"]
    tgt = 0.5 * np.abs(mv) if R is None else R * stop
    return _pack(days, np.full(len(days), e), side, px, stop, tgt, np.full(len(days), b), None, None, tf, g)


# ------------------------------------------------------------------ 13. ICT Silver Bullet (mechanical)
def silver_bullet(g, w0="10:00", w1="11:00", R=2.0, minfvg=0.0):
    c0, c1 = t(w0), t(w1)
    days = np.where(_base_days(g))[0]
    ph = g.H[days, c0 - 60:c0].max(1); pl = g.L[days, c0 - 60:c0].min(1)
    Hh, Ll, Cc = g.H[days, c0:c1], g.L[days, c0:c1], g.C[days, c0:c1]
    span = c1 - c0
    res = []
    for k in range(len(days)):
        h, l, c = Hh[k], Ll[k], Cc[k]
        swept_hi = np.where(h > ph[k])[0]; swept_lo = np.where(l < pl[k])[0]
        s_hi = swept_hi[0] if len(swept_hi) else INF; s_lo = swept_lo[0] if len(swept_lo) else INF
        if s_hi == s_lo:
            continue
        side = -1 if s_hi < s_lo else 1
        s0 = min(s_hi, s_lo)
        ext = None
        for j in range(s0 + 2, span - 2):
            ext = h[s0:j + 1].max() if side < 0 else l[s0:j + 1].min()
            if side < 0 and h[j] < l[j - 2] - minfvg * g.atr14[days[k]]:     # bearish FVG
                lim = h[j]                                               # near edge of gap
                f = np.where(h[j + 1:] >= lim)[0]
                if len(f):
                    e = j + 1 + f[0]
                    res.append((days[k], c0 + e, -1, lim, ext - lim + g.spec["tick"]))
                break
            if side > 0 and l[j] > h[j - 2] + minfvg * g.atr14[days[k]]:
                lim = l[j]
                f = np.where(l[j + 1:] <= lim)[0]
                if len(f):
                    e = j + 1 + f[0]
                    res.append((days[k], c0 + e, 1, lim, lim - ext + g.spec["tick"]))
                break
    if not res:
        return _pack([], [], [], [], [])
    d, e, sd, px, st = map(np.array, zip(*res))
    # limit entry: no entry slippage is modelled by harness (it adds slip); acceptable conservative
    return _pack(d, e, sd, px, st, R * st, np.full(len(d), min(c1 + 60, FLAT)), None, None, None, g)


# ------------------------------------------------------------------ 14. intraday EMA trend (positive skew)
def ema_trend(g, fast=9, slow=21, tf_min=5, s=1.0, trail=(1.0, 1.5), start=15, until=240, tf=None):
    """one trade/day: first fast/slow EMA cross (on tf_min bars) after anchor+start, before anchor+until."""
    a, b = g.a, g.b
    days = np.where(_base_days(g))[0]
    C = g.C[days, a:b].astype(np.float64)
    n = C.shape[1] // tf_min
    Cb = C[:, tf_min - 1::tf_min][:, :n]
    def ema(x, p):
        al = 2 / (p + 1); out = np.empty_like(x); out[:, 0] = x[:, 0]
        for i in range(1, x.shape[1]):
            out[:, i] = al * x[:, i] + (1 - al) * out[:, i - 1]
        return out
    f, sl = ema(Cb, fast), ema(Cb, slow)
    diff = np.sign(f - sl)
    cross = np.zeros_like(diff, bool); cross[:, 1:] = (diff[:, 1:] != diff[:, :-1]) & (diff[:, 1:] != 0)
    st = np.full(len(days), start // tf_min); en = np.full(len(days), min(until // tf_min, n - 2))
    j = _first(cross, st, en)
    ok = j >= 0
    days, j = days[ok], j[ok]
    side = diff[ok, j]
    e = a + (j + 1) * tf_min
    px = g.O[days, e]
    return _pack(days, e, side, px, s * g.atr14[days], None, np.full(len(days), b), trail, None, tf, g)


# ------------------------------------------------------------------ 15. fix flows (FX 4pm London fix = 11:00 ET; gold PM fix 10:00 ET)
def fix_reversal(g, fix="11:00", pre=60, hold=60, mode="revert", s=0.3):
    c = t(fix)
    days = np.where(_base_days(g))[0]
    mv = g.C[days, c - 1] - g.O[days, c - pre]
    side = -np.sign(mv) if mode == "revert" else np.sign(mv)
    e = c; px = g.O[days, e]
    return _pack(days, np.full(len(days), e), side, px, s * g.atr14[days], None, np.full(len(days), c + hold), None, None, None, g)


# ------------------------------------------------------------------ 16. event-day range break (EIA: CL Wed / NG Thu 10:30)
def event_break(g, wd=2, r0="10:00", r1="10:31", until="12:00", R=2.0):
    days = np.where(_base_days(g) & (g.wd == wd))[0]
    c0, c1, cu = t(r0), t(r1), t(until)
    rh = g.H[days, c0:c1].max(1); rl = g.L[days, c0:c1].min(1)
    tick = g.spec["tick"]
    st = np.full(len(days), c1); en = np.full(len(days), cu)
    ju = _first(g.H[days] >= (rh + tick)[:, None], st, en); jd = _first(g.L[days] <= (rl - tick)[:, None], st, en)
    ju = np.where(ju < 0, INF, ju); jd = np.where(jd < 0, INF, jd)
    e = np.minimum(ju, jd); side = np.where(ju < jd, 1, np.where(jd < ju, -1, 0))
    ok = e < INF
    days, e, side, rh, rl = days[ok], e[ok], side[ok], rh[ok], rl[ok]
    ob = g.O[days, e]
    px = np.where(side > 0, np.maximum(rh + tick, ob), np.minimum(rl - tick, ob))
    stop = np.abs(px - (rh + rl) / 2)
    return _pack(days, e, side, px, stop, R * stop, np.full(len(days), g.b), None, None, None, g)


# ------------------------------------------------------------------ 17. overnight-move -> session continuation / reversal
def on_transfer(g, mode="cont", k=0.2, s=0.5, T=0, tf=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g))[0]
    mv = g.O[days, a] - g.O[days, 0]
    side = np.sign(mv) * (np.abs(mv) >= k * g.atr14[days]) * (1 if mode == "cont" else -1)
    keep = ~g.roll_detected[days]
    days, side = days[keep], side[keep]
    e = a + T; px = g.O[days, e]
    return _pack(days, np.full(len(days), e), side, px, s * g.atr14[days], None, np.full(len(days), b), None, None, tf, g)


# ------------------------------------------------------------------ 18. IB (initial balance) break / failure
def ib_break(g, n=60, mode="break", R=None, maxw=0.6, tf=None):
    a, b = g.a, g.b
    days = np.where(_base_days(g))[0]
    ih = g.H[days, a:a + n].max(1); il = g.L[days, a:a + n].min(1); w = ih - il
    ok = w <= maxw * g.atr14[days]
    days, ih, il, w = days[ok], ih[ok], il[ok], w[ok]
    tick = g.spec["tick"]
    st = np.full(len(days), a + n); en = np.full(len(days), b - 60)
    if mode == "break":
        ju = _first(g.H[days] >= (ih + tick)[:, None], st, en); jd = _first(g.L[days] <= (il - tick)[:, None], st, en)
        ju = np.where(ju < 0, INF, ju); jd = np.where(jd < 0, INF, jd)
        e = np.minimum(ju, jd); side = np.where(ju < jd, 1, np.where(jd < ju, -1, 0))
        ok = e < INF
        days, e, side, ih, il = days[ok], e[ok], side[ok], ih[ok], il[ok]
        ob = g.O[days, e]
        px = np.where(side > 0, np.maximum(ih + tick, ob), np.minimum(il - tick, ob))
        stop = np.abs(px - (ih + il) / 2)
        return _pack(days, e, side, px, stop, _tgt(R, stop), np.full(len(days), b), None, None, tf, g)
    # failure: break out then close back inside within 15 bars -> fade toward the other side
    ju = _first(g.H[days] >= (ih + tick)[:, None], st, en)
    ok = ju >= 0
    days, ju, ih, il = days[ok], ju[ok], ih[ok], il[ok]
    back = _first(g.C[days] < ih[:, None], ju, np.minimum(ju + 15, b - 30))
    ok = back >= 0
    days, back, ih, il = days[ok], back[ok], ih[ok], il[ok]
    e = back + 1; px = g.O[days, e]
    cols = np.arange(M)[None, :]
    hi = np.where((cols >= (back - 15)[:, None]) & (cols <= back[:, None]), g.H[days], -np.inf).max(1)
    stop = hi - px + tick
    return _pack(days, e, -np.ones(len(days)), px, stop, _tgt(R or 1.0, stop), np.full(len(days), b), None, None, tf, g)


# ------------------------------------------------------------------ registry with parameter grids
def grid_of(**kw):
    keys = list(kw)
    return [dict(zip(keys, vals)) for vals in itertools.product(*[kw[k] for k in keys])]


FAMILIES = {
    "orb": (orb, grid_of(n=[5, 15, 30, 60], stopmode=["opp", "mid"], R=[1.0, 2.0, None], tf=[None, 20], filt=[None, "nr7"])),
    "first_candle": (first_candle, grid_of(n=[5, 15, 30], R=[2, 5, 10], tf=[None, 20])),
    "tod_mom": (tod_mom, grid_of(T=[30, 60, 90, 150, 240], k=[0.0, 0.15, 0.3], s=[0.3, 0.6], ref=["open", "globex"], tf=[None, 20], trail=[None, (2.0, 1.0)])),
    "gao_last": (gao_last, grid_of(first=[30, 60], last=[30, 60], ref=["pclose", "globex"], s=[0.3], tf=[None, 20])),
    "noise_area": (noise_area, grid_of(step=[30, 60], mult=[1.0, 1.5], s=[0.5], tf=[None, 20], trail=[None, (1.0, 1.0)])),
    "vwap_fade": (vwap_fade, grid_of(k=[1.5, 2.0, 2.5, 3.0], start=[30, 60], stopk=[0.75, 1.5], tf=[None, 20])),
    "pd_sweep": (pd_sweep, grid_of(within=[15, 60], R=[1.0, 2.0, 3.0], tf=[None, 20])),
    "gap": (gap, grid_of(lo=[0.1, 0.3], hi=[0.5, 1.0], mode=["fade", "go"], wait=[0, 15], tf=[None, 20])),
    "window_drift": (window_drift, grid_of(start=["18:00", "20:00", "00:00", "03:00"], end=["03:00", "08:00", "09:25"], direction=["long", "trend"])),
    "vol_breakout": (vol_breakout, grid_of(k=[0.3, 0.5, 0.75], basis=["prange", "stretch"], filt=[None, "nr7", "inside"], tf=[None, 20])),
    "range_break": (range_break, grid_of(r0=["19:00", "03:00", "00:00"], r1=["02:00", "08:00", "09:30"], until=["11:30", "14:00"], R=[1.0, 2.0, None], tf=[None, 20])),
    "drive_fade": (drive_fade, grid_of(T=[15, 30, 60], k=[0.3, 0.5, 0.8], R=[1.0, None], tf=[None, 20])),
    "silver_bullet": (silver_bullet, grid_of(w0=["03:00", "10:00", "14:00"], R=[1.0, 2.0, 3.0])),
    "ema_trend": (ema_trend, grid_of(fast=[9, 20], slow=[21, 50], tf_min=[5, 15], s=[0.5, 1.0], tf=[None, 20])),
    "fix_reversal": (fix_reversal, grid_of(fix=["10:00", "11:00", "15:00"], pre=[30, 60, 120], hold=[30, 60, 120], mode=["revert", "cont"])),
    "event_break": (event_break, grid_of(wd=[2, 3], R=[1.0, 2.0, 3.0])),
    "on_transfer": (on_transfer, grid_of(mode=["cont", "rev"], k=[0.0, 0.2, 0.4], s=[0.3, 0.6], T=[0, 30], tf=[None, 20])),
    "ib_break": (ib_break, grid_of(n=[30, 60], mode=["break", "fail"], R=[1.0, 2.0, None], maxw=[0.4, 0.7, 2.0], tf=[None, 20])),
}


def fix_w1(p):
    """silver bullet window end = start + 1h"""
    if "w0" in p:
        h, m = map(int, p["w0"].split(":"))
        p = dict(p, w1=f"{h + 1:02d}:{m:02d}")
    return p


# families that only make sense for some asset classes (avoid junk trials)
ONLY = {"event_break": {"EN"}, "fix_reversal": {"FX", "ME"}}


# ------------------------------------------------------------------ batch 2 (from academic catalog)
def baltussen(g, last=30, ref="pclose", s=0.3, tf=None):
    """Market intraday momentum (Baltussen et al. 2021): return from prior close (or 18:00 open) to
    the start of the last `last` minutes predicts the last `last` minutes of the session."""
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=(ref == "pclose")))[0]
    if ref == "pclose":
        keep = ~g.suspect[days]; days = days[keep]
    refp = g.p_close[days] if ref == "pclose" else g.O[days, 0]
    e = b - last
    side = np.sign(g.C[days, e - 1] - refp)
    keep = ~g.roll_detected[days] if ref != "pclose" else np.ones(len(days), bool)
    days, side = days[keep], side[keep]
    return _pack(days, np.full(len(days), e), side, g.O[days, e], s * g.atr14[days], None, np.full(len(days), b), None, None, tf, g)


def window_side(g, start="19:00", end="03:00", side=-1, s=1.0, wd=None):
    """fixed-direction intra-session window (e.g. home currency weakens in its local hours)."""
    days = np.where(_base_days(g) & ~g.roll_detected)[0]
    if wd is not None:
        days = days[g.wd[days] == wd]
    e0, x0 = t(start), t(end)
    if x0 <= e0:
        return _pack([], [], [], [], [])
    return _pack(days, np.full(len(days), e0), np.full(len(days), float(side)), g.O[days, e0], s * g.atr14[days], None, np.full(len(days), x0), None, None, None, g)


def tom(g, before=1, after=3, start="open", s=1.0):
    """turn-of-month long: last `before` and first `after` trading days of the month."""
    days = np.where(_base_days(g) & ~g.roll_detected)[0]
    mo = g.dates.astype("datetime64[M]").astype(int)
    D = g.D
    idx_in = np.zeros(D, int); idx_rev = np.zeros(D, int)
    for m in np.unique(mo):
        w = np.where(mo == m)[0]
        idx_in[w] = np.arange(len(w)); idx_rev[w] = np.arange(len(w))[::-1]
    sel = (idx_in < after) | (idx_rev < before)
    days = days[sel[days]]
    e0 = g.a if start == "open" else 0
    return _pack(days, np.full(len(days), e0), np.ones(len(days)), g.O[days, e0], s * g.atr14[days], None, np.full(len(days), g.b), None, None, None, g)


FAMILIES.update({
    "baltussen": (baltussen, grid_of(last=[15, 30, 60], ref=["pclose", "globex"], s=[0.2, 0.4], tf=[None, 20])),
    "window_side": (window_side, grid_of(start=["18:00", "19:00", "02:00", "03:00", "08:00"], end=["02:00", "03:00", "08:00", "11:00", "14:00"], side=[-1, 1])),
    "tom": (tom, grid_of(before=[1, 2], after=[1, 3, 4], start=["open", "globex"])),
})


# ------------------------------------------------------------------ batch 3 (from practitioner catalog)
def _prev_value_area(g, pct=0.70, bins=60):
    """prior main-session value area (volume-at-price from 1-min bars, typical price), unadjusted levels."""
    a, b = g.a, g.b
    vah = np.full(g.D, np.nan); val = np.full(g.D, np.nan)
    for d in range(1, g.D):
        H, L, C, V = g.H[d - 1, a:b], g.L[d - 1, a:b], g.C[d - 1, a:b], g.V[d - 1, a:b]
        if V.sum() <= 0:
            continue
        tp = (H + L + C) / 3
        lo, hi = L.min(), H.max()
        if hi <= lo:
            continue
        hist, edges = np.histogram(tp, bins=bins, range=(lo, hi), weights=V)
        poc = hist.argmax(); i0 = i1 = poc; tot = hist[poc]; target = pct * hist.sum()
        while tot < target and (i0 > 0 or i1 < bins - 1):
            left = hist[i0 - 1] if i0 > 0 else -1; right = hist[i1 + 1] if i1 < bins - 1 else -1
            if right >= left:
                i1 += 1; tot += hist[i1]
            else:
                i0 -= 1; tot += hist[i0]
        vah[d] = edges[i1 + 1]; val[d] = edges[i0]
    return vah, val


def eighty_rule(g, accept=30, R=None, tf=None):
    """Market Profile 80% rule: open outside prior value area, then trade back inside and hold for
    `accept` minutes -> target the other side of value; stop beyond the re-entry extreme."""
    if not hasattr(g, "vah"):
        g.vah, g.val = _prev_value_area(g)
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=True) & ~np.isnan(g.vah))[0]
    op = g.O[days, a]; vah, val = g.vah[days], g.val[days]
    above = op > vah; below = op < val
    res = []
    for side, msk, edge, far in ((-1, above, vah, val), (1, below, val, vah)):
        dd = days[msk]; ed = edge[msk]; fr = far[msk]
        C = g.C[dd]
        inside = (C < ed[:, None]) if side < 0 else (C > ed[:, None])
        # first bar where the last `accept` bars were all inside
        cs = np.cumsum(inside, 1)
        ok_run = np.zeros_like(inside); ok_run[:, accept:] = (cs[:, accept:] - cs[:, :-accept]) == accept
        j = _first(ok_run, np.full(len(dd), a + accept), np.full(len(dd), b - 60))
        k = j >= 0
        dd, j, ed, fr = dd[k], j[k], ed[k], fr[k]
        e = j + 1; px = g.O[dd, e]
        ext = np.array([g.H[d_, a:e_].max() if side < 0 else g.L[d_, a:e_].min() for d_, e_ in zip(dd, e)])
        stop = np.abs(ext - px) + g.spec["tick"]
        tgt = np.abs(fr - px) if R is None else R * stop
        res.append((dd, e, np.full(len(dd), side), px, stop, tgt))
    dd, e, sd, px, st, tg = (np.concatenate([r[i] for r in res]) for i in range(6))
    return _pack(dd, e, sd, px, st, tg, np.full(len(dd), b), None, None, tf, g)


def camarilla(g, mode="fade", R=1.5, tf=None):
    """Camarilla pivots from prior session: fade first touch of H3/L3 (stop H4/L4) or break of H4/L4."""
    a, b = g.a, g.b
    days = np.where(_base_days(g, level=True))[0]
    rng = g.p_high[days] - g.p_low[days]; pc = g.p_close[days]
    h3, l3 = pc + rng * 1.1 / 4, pc - rng * 1.1 / 4
    h4, l4 = pc + rng * 1.1 / 2, pc - rng * 1.1 / 2
    tick = g.spec["tick"]
    st = np.full(len(days), a + 1); en = np.full(len(days), b - 60)
    if mode == "fade":
        up_l, dn_l = h3, l3
    else:
        up_l, dn_l = h4, l4
    ju = _first(g.H[days] >= up_l[:, None], st, en); jd = _first(g.L[days] <= dn_l[:, None], st, en)
    ju = np.where(ju < 0, INF, ju); jd = np.where(jd < 0, INF, jd)
    e = np.minimum(ju, jd); hit_up = ju < jd
    ok = (e < INF) & (ju != jd) & (g.O[days, a] < up_l) & (g.O[days, a] > dn_l)
    days, e, hit_up, h3, l3, h4, l4 = days[ok], e[ok], hit_up[ok], h3[ok], l3[ok], h4[ok], l4[ok]
    ob = g.O[days, e]
    if mode == "fade":
        side = np.where(hit_up, -1, 1)
        px = np.where(hit_up, np.maximum(h3, ob), np.minimum(l3, ob))
        stop = np.where(hit_up, h4 - px, px - l4) + tick
    else:
        side = np.where(hit_up, 1, -1)
        px = np.where(hit_up, np.maximum(h4, ob), np.minimum(l4, ob))
        stop = np.where(hit_up, px - h3, l3 - px)
    return _pack(days, e, side, px, stop, R * stop, np.full(len(days), b), None, None, tf, g)


def _bars(g, days, tf_min):
    a, b = g.a, g.b
    n = (b - a) // tf_min
    sl = slice(a, a + n * tf_min)
    H = g.H[days, sl].reshape(len(days), n, tf_min).max(2).astype(float)
    L = g.L[days, sl].reshape(len(days), n, tf_min).min(2).astype(float)
    C = g.C[days, sl].reshape(len(days), n, tf_min)[:, :, -1].astype(float)
    return H, L, C, n


def squeeze(g, tf_min=5, n=20, s=1.0, trail=(1.0, 1.0), tf=None):
    """TTM-style squeeze: Bollinger(n,2) inside Keltner(n,1.5*ATR) then release -> trade momentum sign."""
    a, b = g.a, g.b
    days = np.where(_base_days(g))[0]
    H, L, C, nb = _bars(g, days, tf_min)
    def roll_mean(x):
        c = np.cumsum(x, 1); out = np.full_like(x, np.nan); out[:, n - 1:] = (c[:, n - 1:] - np.hstack([np.zeros((len(x), 1)), c[:, :-n]])) / n; return out
    m = roll_mean(C); sd = np.sqrt(np.maximum(roll_mean(C * C) - m * m, 0))
    tr = np.maximum(H - L, np.abs(H - np.roll(C, 1, 1))); tr[:, 0] = H[:, 0] - L[:, 0]
    atr = roll_mean(tr)
    sq = (m + 2 * sd < m + 1.5 * atr) & (m - 2 * sd > m - 1.5 * atr)
    rel = np.zeros_like(sq); rel[:, 1:] = sq[:, :-1] & ~sq[:, 1:]
    j = _first(rel, np.full(len(days), n), np.full(len(days), nb - 6))
    ok = j >= 0
    days, j = days[ok], j[ok]
    r = np.arange(len(days))
    side = np.sign(C[ok][r, j] - m[ok][r, j])
    e = a + (j + 1) * tf_min
    return _pack(days, e, side, g.O[days, e], s * g.atr14[days], None, np.full(len(days), b), trail, None, tf, g)


def supertrend(g, tf_min=15, per=10, mult=3.0, s=1.0, trail=(1.0, 1.5), tf=None):
    """first Supertrend flip (on tf_min bars, ATR seeded from prior-day ATR) of the session."""
    a, b = g.a, g.b
    days = np.where(_base_days(g))[0]
    H, L, C, nb = _bars(g, days, tf_min)
    atr = g.atr14[days][:, None] * np.sqrt(tf_min / max(b - a, 1)) * 1.0 + 0 * C
    hl2 = (H + L) / 2
    ub = hl2 + mult * atr; lb = hl2 - mult * atr
    dirn = np.ones_like(C); fu = ub.copy(); fl = lb.copy()
    for i in range(1, nb):
        fu[:, i] = np.where((ub[:, i] < fu[:, i - 1]) | (C[:, i - 1] > fu[:, i - 1]), ub[:, i], fu[:, i - 1])
        fl[:, i] = np.where((lb[:, i] > fl[:, i - 1]) | (C[:, i - 1] < fl[:, i - 1]), lb[:, i], fl[:, i - 1])
        dirn[:, i] = np.where(C[:, i] > fu[:, i - 1], 1, np.where(C[:, i] < fl[:, i - 1], -1, dirn[:, i - 1]))
    flip = np.zeros_like(dirn, bool); flip[:, 1:] = dirn[:, 1:] != dirn[:, :-1]
    j = _first(flip, np.full(len(days), 1), np.full(len(days), nb - 2))
    ok = j >= 0
    days, j = days[ok], j[ok]
    side = dirn[ok][np.arange(len(days)), j]
    e = a + (j + 1) * tf_min
    return _pack(days, e, side, g.O[days, e], s * g.atr14[days], None, np.full(len(days), b), trail, None, tf, g)


def judas(g, asia0="19:00", asia1="00:00", until="10:00", R=2.0, exitt="12:00"):
    """ICT Power-of-3 / Judas swing: London sweeps one side of the Asian range, then closes back
    inside -> trade toward the other side (stop beyond the sweep extreme)."""
    c0, c1, cu = t(asia0), t(asia1), t(until)
    days = np.where(_base_days(g) & ~g.roll_detected)[0]
    ah = g.H[days, c0:c1].max(1); al = g.L[days, c0:c1].min(1)
    tick = g.spec["tick"]
    st = np.full(len(days), c1); en = np.full(len(days), cu)
    su = _first(g.H[days] > (ah + tick)[:, None], st, en); sd_ = _first(g.L[days] < (al - tick)[:, None], st, en)
    su = np.where(su < 0, INF, su); sd_ = np.where(sd_ < 0, INF, sd_)
    first = np.minimum(su, sd_); ok = (first < INF) & (su != sd_)
    days, first, su, ah, al = days[ok], first[ok], su[ok], ah[ok], al[ok]
    side = np.where(su == first, -1, 1)
    lvl = np.where(side < 0, ah, al)
    back = _first(np.where(side[:, None] < 0, g.C[days] < lvl[:, None], g.C[days] > lvl[:, None]), first, np.full(len(days), cu))
    ok = back >= 0
    days, back, side, first = days[ok], back[ok], side[ok], first[ok]
    e = back + 1; px = g.O[days, e]
    cols = np.arange(M)[None, :]; win = (cols >= first[:, None]) & (cols <= back[:, None])
    ext = np.where(side < 0, np.where(win, g.H[days], -np.inf).max(1), np.where(win, g.L[days], np.inf).min(1))
    stop = np.abs(ext - px) + tick
    return _pack(days, e, side, px, stop, R * stop, np.full(len(days), t(exitt)), None, None, None, g)


FAMILIES.update({
    "eighty_rule": (eighty_rule, grid_of(accept=[15, 30, 60], R=[None, 1.5], tf=[None, 20])),
    "camarilla": (camarilla, grid_of(mode=["fade", "break"], R=[1.0, 2.0], tf=[None, 20])),
    "squeeze": (squeeze, grid_of(tf_min=[5, 15], n=[20], s=[0.5, 1.0], trail=[None, (1.0, 1.0)], tf=[None, 20])),
    "supertrend": (supertrend, grid_of(tf_min=[5, 15], mult=[2.0, 3.0], s=[0.5, 1.0], trail=[None, (1.0, 1.5)], tf=[None, 20])),
    "judas": (judas, grid_of(asia0=["19:00", "20:00"], asia1=["00:00", "02:00"], until=["08:00", "10:00"], R=[1.0, 2.0, 3.0], exitt=["11:00", "14:00"])),
})
