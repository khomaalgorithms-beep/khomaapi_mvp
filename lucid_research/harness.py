"""Backtest harness: dense day x minute grids, contract specs, cost model, vectorized trade simulator.

Grid convention: one row per Lucid trading session. Column m = minutes since 18:00 ET of the prior
evening (m=0 -> 18:00, m=930 -> 09:30, m=1320 -> 16:00, m=1365 -> 16:45 flat time). FirstRate stamps
mark bar START, so the 09:30 bar covers 09:30:00-09:30:59. Missing minutes (no trades) are filled with
the prior close (o=h=l=c) and volume 0 — no fake prices beyond carrying the last trade.

Roll handling (unadjusted continuous data): FirstRate switches contracts at a calendar-day boundary —
00:00 ET inside the session on weekdays (detected exactly, verified 19/19 vs the NQ adjusted file, and
back-adjusted), or across the weekend break (not separable from real weekend gaps). Sessions where
multi-day LEVEL features may be wrong are flagged in `g.suspect`; level-based strategies skip them.
"""
import os
import numpy as np

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
M = 1380                      # 18:00 .. 16:59
FLAT = 1365                   # 16:45 ET -> must be flat before this bar

def t(hhmm):
    """'09:30' -> grid minute index."""
    h, mm = map(int, hhmm.split(":"))
    return ((h * 60 + mm) - 18 * 60) % 1440


# ---------------------------------------------------------------- contract specs (traded contract)
# tick: price tick of the quoted series; dpp: $ per 1.0 price point of the TRADED contract;
# comm: Lucid round-trip commission of the traded contract; slip: ticks per side (market/stop fills);
# micro: name of the traded contract; open/close: the product's main day-session anchor (ET).
SPEC = {
    # equity index -> micros (MES/MNQ/M2K/MYM) for sizing granularity
    "ES":  dict(tick=0.25,   dpp=5.0,      comm=1.00, slip=1, trade="MES", open="09:30", close="16:00", cls="EQ"),
    "NQ":  dict(tick=0.25,   dpp=2.0,      comm=1.00, slip=1, trade="MNQ", open="09:30", close="16:00", cls="EQ"),
    "RTY": dict(tick=0.10,   dpp=5.0,      comm=1.00, slip=1, trade="M2K", open="09:30", close="16:00", cls="EQ"),
    "YM":  dict(tick=1.0,    dpp=0.5,      comm=1.00, slip=1, trade="MYM", open="09:30", close="16:00", cls="EQ"),
    "NKD": dict(tick=5.0,    dpp=5.0,      comm=3.50, slip=2, trade="NKD", open="09:30", close="16:00", cls="EQ"),
    # energy
    "CL":  dict(tick=0.01,   dpp=100.0,    comm=1.00, slip=1, trade="MCL", open="09:00", close="14:30", cls="EN"),
    "NG":  dict(tick=0.001,  dpp=10000.0,  comm=4.00, slip=1, trade="NG",  open="09:00", close="14:30", cls="EN"),
    "QG":  dict(tick=0.005,  dpp=2500.0,   comm=2.60, slip=1, trade="QG",  open="09:00", close="14:30", cls="EN"),
    # metals
    "GC":  dict(tick=0.10,   dpp=10.0,     comm=1.60, slip=1, trade="MGC", open="08:20", close="13:30", cls="ME"),
    "SI":  dict(tick=0.005,  dpp=1000.0,   comm=3.20, slip=1, trade="SIL", open="08:25", close="13:25", cls="ME"),
    "HG":  dict(tick=0.0005, dpp=25000.0,  comm=4.60, slip=1, trade="HG",  open="08:10", close="13:00", cls="ME"),
    "PL":  dict(tick=0.10,   dpp=50.0,     comm=4.60, slip=2, trade="PL",  open="08:20", close="13:05", cls="ME"),
    # FX
    "A6":  dict(tick=0.00005, dpp=100000.0, comm=4.80, slip=1, trade="6A", open="08:20", close="15:00", cls="FX"),
    "B6":  dict(tick=0.0001,  dpp=62500.0,  comm=4.80, slip=1, trade="6B", open="08:20", close="15:00", cls="FX"),
    "E6":  dict(tick=0.00005, dpp=125000.0, comm=4.80, slip=1, trade="6E", open="08:20", close="15:00", cls="FX"),
    "J1":  dict(tick=0.0000005, dpp=12500000.0, comm=4.80, slip=1, trade="6J", open="08:20", close="15:00", cls="FX"),
    "E1":  dict(tick=0.00005, dpp=125000.0, comm=4.80, slip=2, trade="6S", open="08:20", close="15:00", cls="FX"),
    "N6":  dict(tick=0.00005, dpp=100000.0, comm=4.80, slip=2, trade="6N", open="08:20", close="15:00", cls="FX"),
    # grains / livestock (cents quoted; dpp = $ per 1 cent)
    "ZC":  dict(tick=0.25,  dpp=50.0,  comm=5.60, slip=1, trade="ZC", open="09:30", close="14:15", cls="AG"),
    "ZS":  dict(tick=0.25,  dpp=50.0,  comm=5.60, slip=1, trade="ZS", open="09:30", close="14:15", cls="AG"),
    "ZW":  dict(tick=0.25,  dpp=50.0,  comm=5.60, slip=1, trade="ZW", open="09:30", close="14:15", cls="AG"),
    "ZL":  dict(tick=0.01,  dpp=600.0, comm=5.60, slip=1, trade="ZL", open="09:30", close="14:15", cls="AG"),
    "ZM":  dict(tick=0.10,  dpp=100.0, comm=5.60, slip=1, trade="ZM", open="09:30", close="14:15", cls="AG"),
    "HE":  dict(tick=0.025, dpp=400.0, comm=5.60, slip=2, trade="HE", open="09:30", close="14:05", cls="AG"),
    "LE":  dict(tick=0.025, dpp=400.0, comm=5.60, slip=2, trade="LE", open="09:30", close="14:05", cls="AG"),
}
TRADABLE = list(SPEC)


def cost_pts(sym, slip_mult=1.0, exit_slips=1.0):
    """Round-trip cost in price points: commission + entry slip + exit slip (stop/market exits)."""
    s = SPEC[sym]
    return s["comm"] / s["dpp"] + s["tick"] * s["slip"] * slip_mult * (1.0 + exit_slips)


# ---------------------------------------------------------------- grid
class Grid:
    pass


_cache = {}


def load(sym, start="2008-01-01"):
    key = (sym, start)
    if key in _cache:
        return _cache[key]
    z = np.load(os.path.join(DATA, f"{sym}.npz"))
    ts = z["ts"].astype(np.int64)
    tod = ts % 1440                                  # minute of day (wall clock ET)
    day = ts // 1440                                 # calendar day number
    m = (tod - 1080) % 1440
    sess_day = day + (tod >= 1080)                   # 18:00+ belongs to next calendar day
    wd = (sess_day + 3) % 7                          # 0=Mon .. 6=Sun (1970-01-01 was Thursday)
    sess_day = np.where(wd == 5, sess_day + 2, np.where(wd == 6, sess_day + 1, sess_day))  # Sat/Sun evening -> Monday
    ok = (m < M) & (sess_day >= np.datetime64(start, "D").astype(np.int64))
    ts, m, sess_day = ts[ok], m[ok], sess_day[ok]
    o, h, l, c, v = (z[k][ok].astype(np.float64) for k in "ohlcv")
    # --- roll handling: FirstRate switches contracts at a calendar-day boundary. When that boundary is
    # inside a session (midnight), back-adjust everything before it by the jump (detected exactly).
    cal = ts // 1440
    bi = np.where(np.diff(cal) != 0)[0] + 1
    step = np.abs(np.diff(c)); med1 = np.median(step[step > 0]) if (step > 0).any() else 1.0
    contig = (ts[bi] - ts[bi - 1]) <= 90
    jump = o[bi] - c[bi - 1]
    is_roll = contig & (np.abs(jump) > 10 * med1)
    adj = np.zeros(len(c))
    for i, j in zip(bi[is_roll], jump[is_roll]):
        adj[:i] += j
    raw_c = c.copy()
    o += adj; h += adj; l += adj; c += adj
    roll_sess = np.unique(sess_day[bi[is_roll]])
    days, di = np.unique(sess_day, return_inverse=True)
    D = len(days)
    O = np.full((D, M), np.nan, np.float32); H = O.copy(); L = O.copy(); C = O.copy()
    V = np.zeros((D, M), np.float32)
    O[di, m] = o; H[di, m] = h; L[di, m] = l; C[di, m] = c; V[di, m] = v
    RC = np.full((D, M), np.nan, np.float32); RC[di, m] = np.abs(raw_c)
    raw_px = np.nanmedian(RC, axis=1).astype(np.float64)     # unadjusted price level per session
    del RC
    real = ~np.isnan(C)
    # forward-fill close within the session; leading gaps take the first real open
    idx = np.where(real, np.arange(M)[None, :], 0)
    np.maximum.accumulate(idx, axis=1, out=idx)
    Cf = C[np.arange(D)[:, None], idx]
    first = np.argmax(real, axis=1)
    lead = np.arange(M)[None, :] < first[:, None]
    fo = O[np.arange(D), first]
    Cf = np.where(lead, fo[:, None], Cf)
    O = np.where(real, O, Cf); H = np.where(real, H, Cf); L = np.where(real, L, Cf); C = Cf
    g = Grid()
    g.sym, g.D, g.days = sym, D, days
    g.dates = days.astype("datetime64[D]")
    g.wd = ((days + 3) % 7).astype(np.int8)
    g.year = g.dates.astype("datetime64[Y]").astype(int) + 1970
    g.O, g.H, g.L, g.C, g.V, g.real = O, H, L, C, V, real
    g.spec = SPEC.get(sym, dict(tick=0.01, dpp=1.0, comm=0, slip=0, open="09:30", close="16:00", cls="SIG"))
    g.nbars = real.sum(1)
    g.px = raw_px
    # day validity: enough bars in the main session
    a, b = t(g.spec["open"]), t(g.spec["close"])
    g.sess_ok = real[:, a:b].sum(1) >= 0.6 * (b - a) if g.spec["cls"] in ("EQ", "EN", "ME") else real[:, a:b].sum(1) >= 0.3 * (b - a)
    # session gap at 18:00 (roll jump + tiny maintenance-break move)
    last_close = C[:, M - 1]
    g.gap18 = np.zeros(D, np.float32)
    g.gap18[1:] = O[1:, 0] - last_close[:-1]
    g.roll_detected = np.isin(days, roll_sess)
    _features(g)
    # suspect sessions for multi-day LEVEL features: detected rolls, equity quarterly roll window
    # (weekend rolls are not separable from real weekend gaps), and abnormal 18:00 gaps.
    dd = g.dates.astype(object)
    mon = np.array([x.month for x in dd]); dom = np.array([x.day for x in dd])
    eqwin = (g.spec["cls"] == "EQ") & np.isin(mon, [3, 6, 9, 12]) & (dom >= 11) & (dom <= 17)
    big = np.abs(g.gap18) > np.maximum(8 * np.median(np.abs(g.gap18)), 0.4 * np.nan_to_num(g.atr14, nan=1e9))
    g.suspect = g.roll_detected | eqwin | big
    _cache[key] = g
    return g


def _features(g):
    """Daily features, all aligned so feature[d] uses ONLY information available before day d's
    main-session open unless the name says otherwise. Price levels are roll-rebased."""
    a, b = t(g.spec["open"]), t(g.spec["close"])
    D = g.D
    g.a, g.b = a, b
    g.s_open = g.O[:, a]                                  # main-session open (unadjusted, today)
    g.s_high = g.H[:, a:b].max(1); g.s_low = g.L[:, a:b].min(1); g.s_close = g.C[:, b - 1]
    g.f_high = g.H.max(1); g.f_low = g.L.min(1); g.f_close = g.C[:, FLAT - 1]   # full-session
    g.on_high = g.H[:, :a].max(1); g.on_low = g.L[:, :a].min(1)                 # overnight (18:00 -> open)
    rng = (g.s_high - g.s_low).astype(np.float64)
    # adjusted prior-day levels: shift yesterday's levels by today's 18:00 gap (removes roll jumps)
    sh = np.zeros(D)        # levels are raw (detected rolls already back-adjusted); see g.suspect
    def prev(x):
        p = np.full(D, np.nan); p[1:] = x[:-1]; return p
    g.p_high = prev(g.s_high) + sh; g.p_low = prev(g.s_low) + sh; g.p_close = prev(g.s_close) + sh
    g.p_range = prev(rng)
    g.p_open = prev(g.s_open) + sh
    # ATR of main-session range (roll-safe), known before today
    g.atr14 = _sma_prev(rng, 14); g.atr5 = _sma_prev(rng, 5)
    # roll-safe daily return series: session close/open over the 18:00->16:59 session
    # (points / price level, not logs: back-adjusted series can go negative, e.g. CL)
    sess_ret = (g.C[:, M - 1] - g.O[:, 0]).astype(np.float64) / g.px
    g.cum = np.cumsum(sess_ret)                          # roll-free cumulative % path at day end
    g.cum_prev = np.concatenate([[np.nan], g.cum[:-1]])
    # trend: close vs SMA(n) of adjusted log closes, known at prior close
    for n in (10, 20, 50):
        sma = _sma_prev(g.cum, n)                        # mean of cum[d-n..d-1]
        setattr(g, f"trend{n}", np.sign(g.cum_prev - sma))
    g.ret1 = np.concatenate([[np.nan], sess_ret[:-1]])   # yesterday's session return
    # narrow-range patterns (on main-session ranges, known before today)
    pr = g.p_range
    def lagged_min(k):
        out = np.full(D, np.nan)
        for d in range(k + 1, D):
            out[d] = rng[d - k - 1:d - 1].min()
        return out
    g.nr4 = pr < lagged_min(3); g.nr7 = pr < lagged_min(6)
    ph, pl = prev(g.s_high), prev(g.s_low)
    pph = np.concatenate([[np.nan], ph[:-1]]); ppl = np.concatenate([[np.nan], pl[:-1]])
    g.inside = (ph < pph) & (pl > ppl)
    # realized vol percentile (expanding, of ATR14/price) — regime filter
    vol = g.atr14 / g.px
    g.volpct = _expanding_pct(vol)


def _sma_prev(x, n):
    """mean of x[d-n .. d-1] (strictly prior)."""
    x = np.asarray(x, np.float64)
    c = np.concatenate([[0.0], np.nancumsum(x)])
    out = np.full(len(x), np.nan)
    out[n:] = (c[n:-1] - c[:-n - 1]) / n
    return out


def _expanding_pct(x, minn=250):
    """percentile of x[d] among all earlier values (x[d] itself is already known before day d)."""
    import bisect
    out = np.full(len(x), np.nan)
    hist = []
    for i in range(len(x)):
        if np.isnan(x[i]):
            continue
        if len(hist) >= minn:
            out[i] = bisect.bisect_left(hist, x[i]) / len(hist)
        bisect.insort(hist, x[i])
    return out


# ---------------------------------------------------------------- trade simulator
def simulate(g, d, e, side, entry_px, stop_dist, tgt_dist=None, exit_m=None, trail=None,
             be_at=None, slip_mult=1.0, chunk=600):
    """Chunked wrapper (bounded memory) around _simulate."""
    n = len(d)
    if n <= chunk:
        return _simulate(g, d, e, side, entry_px, stop_dist, tgt_dist, exit_m, trail, be_at, slip_mult)
    def sl(x, i, j):
        if x is None or np.ndim(x) == 0:
            return x
        return np.asarray(x)[i:j]
    parts = [_simulate(g, sl(d, i, i + chunk), sl(e, i, i + chunk), sl(side, i, i + chunk),
                       sl(entry_px, i, i + chunk), sl(stop_dist, i, i + chunk), sl(tgt_dist, i, i + chunk),
                       sl(exit_m, i, i + chunk), trail, be_at, slip_mult) for i in range(0, n, chunk)]
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


def _simulate(g, d, e, side, entry_px, stop_dist, tgt_dist=None, exit_m=None, trail=None,
              be_at=None, slip_mult=1.0):
    """Vectorized exits for a batch of trades.
    d: day idx; e: entry bar index (fill already happened inside bar e at entry_px);
    side: +1/-1; stop_dist: price distance (>0) for initial stop; tgt_dist: limit target distance or nan;
    exit_m: time exit -> exit at open of bar exit_m (default FLAT); trail: (trigger_R, dist_R) chandelier
    trail once open profit >= trigger_R; be_at: move stop to entry once profit >= be_at R.
    Bars are scanned from e (entry bar: only its close can stop us out) to exit_m-1.
    Same-bar stop & target -> stop first (conservative). Returns dict of arrays (points, per 1 contract)."""
    d = np.asarray(d); e = np.asarray(e); side = np.asarray(side, np.float64)
    n = len(d)
    if n == 0:
        z = np.zeros(0)
        return dict(gross=z, net=z, mae=z, R=z, d=np.zeros(0, int), e=np.zeros(0, int), hold=np.zeros(0, int), why=np.zeros(0, int), stop=z, side=z, mfe=z, mae_first=np.zeros(0, bool))
    entry_px = np.asarray(entry_px, np.float64)
    stop_dist = np.asarray(stop_dist, np.float64)
    tgt = np.full(n, np.nan) if tgt_dist is None else np.broadcast_to(np.asarray(tgt_dist, np.float64), (n,)).copy()
    xm = np.full(n, FLAT) if exit_m is None else np.minimum(np.broadcast_to(np.asarray(exit_m), (n,)), FLAT)
    sp = g.spec; tick = sp["tick"]; slip = tick * sp["slip"] * slip_mult
    entry_fill = entry_px + side * slip
    Lmax = int(max(1, (xm - e).max()))
    cols = e[:, None] + np.arange(Lmax)[None, :]
    valid = cols < xm[:, None]
    cols = np.minimum(cols, M - 1)
    rows = d[:, None]
    Hh = g.H[rows, cols].astype(np.float64); Ll = g.L[rows, cols].astype(np.float64); Cc = g.C[rows, cols].astype(np.float64)
    # favourable / adverse excursion series in "side" space
    fav = np.where(side[:, None] > 0, Hh - entry_fill[:, None], entry_fill[:, None] - Ll)
    adv = np.where(side[:, None] > 0, Ll - entry_fill[:, None], entry_fill[:, None] - Hh)   # <=0 when adverse
    closeR = np.where(side[:, None] > 0, Cc - entry_fill[:, None], entry_fill[:, None] - Cc)
    fav[:, 0] = closeR[:, 0]; adv[:, 0] = closeR[:, 0]  # entry bar: only its close counts
    fav = np.where(valid, fav, -np.inf); adv = np.where(valid, adv, np.inf)
    # dynamic stop level (in side-space P&L): -stop initially, raised by BE / trail using prior-bar peak
    stop_lvl = np.broadcast_to(-stop_dist[:, None], fav.shape).copy()
    peak = np.maximum.accumulate(np.where(valid, fav, -np.inf), axis=1)
    peak_prev = np.concatenate([np.full((n, 1), -np.inf), peak[:, :-1]], axis=1)
    if be_at is not None:
        stop_lvl = np.where(peak_prev >= be_at * stop_dist[:, None], np.maximum(stop_lvl, 0.0), stop_lvl)
    if trail is not None:
        trg, dist = trail
        tl = peak_prev - dist * stop_dist[:, None]
        stop_lvl = np.where(peak_prev >= trg * stop_dist[:, None], np.maximum(stop_lvl, tl), stop_lvl)
    hit_stop = adv <= stop_lvl
    hit_tgt = (fav >= (tgt[:, None] + tick)) if tgt_dist is not None else np.zeros_like(hit_stop)
    hit_tgt = hit_tgt & ~np.isnan(tgt)[:, None]
    big = Lmax + 10
    i_stop = np.where(hit_stop.any(1), hit_stop.argmax(1), big)
    i_tgt = np.where(hit_tgt.any(1), hit_tgt.argmax(1), big)
    i_time = valid.sum(1)                                     # exit at open of bar xm
    gross = np.empty(n); why = np.empty(n, int); ex = np.empty(n, int)
    for k in range(n):
        a_, b_, c_ = i_stop[k], i_tgt[k], i_time[k]
        if a_ <= b_ and a_ < c_:
            lvl = stop_lvl[k, a_]
            # gap through stop: fill at bar open if worse
            op = g.O[d[k], min(e[k] + a_, M - 1)]
            opR = (op - entry_fill[k]) * side[k]
            px = min(lvl, opR) if a_ > 0 else min(lvl, closeR[k, 0])
            gross[k] = px - slip; why[k] = 0; ex[k] = a_
        elif b_ < c_:
            gross[k] = tgt[k]; why[k] = 1; ex[k] = b_
        else:
            xo = g.O[d[k], min(xm[k], M - 1)] if xm[k] < M else g.C[d[k], M - 1]
            gross[k] = (xo - entry_fill[k]) * side[k] - slip; why[k] = 2; ex[k] = c_
    # MAE up to exit (side space, <=0), relative to fill
    upto = np.arange(Lmax)[None, :] <= ex[:, None]
    mae = np.minimum(np.where(upto & valid, adv, np.inf).min(1), 0.0)
    mae = np.minimum(mae, gross)
    mfe = np.maximum(np.where(upto & valid, fav, -np.inf).max(1), 0.0)   # best open profit before exit
    # ordering flag: did the worst excursion come BEFORE the best one? (conservative tie -> True)
    i_mae = np.where(upto & valid, adv, np.inf).argmin(1); i_mfe = np.where(upto & valid, fav, -np.inf).argmax(1)
    mae_first = i_mae <= i_mfe
    comm_pts = sp["comm"] / sp["dpp"]
    # gross already includes slippage; subtract the commission and the entry slip already in fill
    net = gross - comm_pts
    R = net / stop_dist
    return dict(gross=gross, net=net, mae=mae - comm_pts, R=R, d=d, e=e, hold=ex, why=why, stop=stop_dist, side=side,
                mfe=mfe - comm_pts - slip, mae_first=mae_first)
