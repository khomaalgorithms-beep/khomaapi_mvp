"""Can the 10:30 NQ momentum engine be made LESS regime dependent?  PRE-REGISTERED candidates (fixed before running,
no parameter search): each is judged on 2008-26 per-day R (risk split so total risk/day = 1R), real costs.
  B   base: 10:30 with-trend, stop 0.2 ATR (live rule)
  C1  time diversification: B + same rule at 12:00 + Gao last-half-hour momentum (1/3 risk each)
  C2  counter-regime sleeve: B + VWAP 2-sigma fade (mean reversion) 50/50
  C3  equity-curve filter: take B only if B's last 50 (shadow) trades sum > 0
  C4  volatility filter: take B only if ATR14/price >= its trailing 250-day median
  C5  multi-market: same rule on NQ + ES + YM (1/3 risk each) [if data exists]
  C6  vol-targeted size: risk scaled by median(vol)/vol (cap 0.5x-1.5x), normalized to mean 1
Regime metrics: PF 2008-19 / 2020-26 / last 12m, losing years, worst 12-month R, t-stat."""
import numpy as np, harness as Hn, stress as T

COST = dict(comm=1.0, slip=1)
def R_of(chain, grid=None):
    if grid is not None: T.G = grid
    lg = T.legs(dict(chain=chain, **COST)); return {d: v["net"] / v["rct"] for d, v in lg.items()}

def mom(Tm=60, s=0.2): return [("tod_mom", dict(T=Tm, k=0.0, s=s, ref="open", tf=20, trail=None), 1.0)]

NQ = Hn.load("NQ"); T.G = NQ; g = NQ; D = g.D
base = R_of(mom())
def blend(parts):
    out = {}
    for R, w in parts:
        for d, r in R.items(): out[d] = out.get(d, 0.0) + w * r
    return out

C = {"B base (live)": base}
C["C1 time-diversified"] = blend([(base, 1/3), (R_of(mom(150)), 1/3), (R_of([("gao_last", dict(first=30, last=30, ref="pclose", s=0.3, tf=20), 1.0)]), 1/3)])
C["C2 + mean-reversion sleeve"] = blend([(base, .5), (R_of([("vwap_fade", dict(k=2.0, start=30, stopk=1.0, end_before=60, tgtmode="vwap", tf=None), 1.0)]), .5)])
# C3 equity-curve filter (uses only completed prior trades)
ds = sorted(base); keep = {}; hist = []
for d in ds:
    if len(hist) < 50 or sum(hist[-50:]) > 0: keep[d] = base[d]
    hist.append(base[d])
C["C3 equity-curve filter"] = keep
# C4 vol filter
v = g.atr14 / g.px; med = np.full(D, np.nan)
for i in range(250, D):
    w = v[i-250:i]; w = w[np.isfinite(w)]; med[i] = np.median(w) if len(w) else np.nan
C["C4 high-vol only"] = {d: r for d, r in base.items() if np.isfinite(med[d]) and v[d] >= med[d]}
# C6 vol-targeted size
sc = {d: np.clip(med[d] / v[d], .5, 1.5) if np.isfinite(med[d]) and v[d] > 0 else 1.0 for d in base}
m = np.mean(list(sc.values())); C["C6 vol-targeted size"] = {d: base[d] * sc[d] / m for d in base}
# C5 multi-market (map other grids' days onto NQ day ids by date)
try:
    parts = [(base, 1/3)]
    for sym in ("ES", "YM"):
        gs = Hn.load(sym); Rs = R_of(mom(), gs); idx = {int(x): i for i, x in enumerate(NQ.days)}
        parts.append(({idx[int(gs.days[d])]: r for d, r in Rs.items() if int(gs.days[d]) in idx}, 1/3))
    T.G = NQ; C["C5 NQ+ES+YM"] = blend(parts)
except Exception as e:
    T.G = NQ; print("C5 skipped:", e)

def stats(R):
    ds = np.array(sorted(R)); r = np.array([R[d] for d in ds]); yr = g.year[ds]
    pf = lambda x: x[x > 0].sum() / max(1e-9, -x[x < 0].sum())
    last = ds >= np.searchsorted(g.days, np.datetime64("2025-10-10").astype(np.int64))
    ys = {y: r[yr == y].sum() for y in range(2008, 2027)}
    # worst 252-session window (calendar sessions, not trades)
    full = np.zeros(D); full[ds] = r; cs = np.cumsum(full); w12 = (cs[252:] - cs[:-252]).min()
    t = r.mean() / r.std() * np.sqrt(len(r))
    return dict(pf=pf(r), pf_old=pf(r[yr <= 2019]), pf_new=pf(r[yr >= 2020]), pf_12m=pf(r[last]),
                lose=sum(1 for y, x in ys.items() if x < 0 and y < 2026), r26=ys[2026], w12=w12, t=t, n=len(r), ys=ys)

print(f"{'candidate':28}{'PF all':>7}{'08-19':>7}{'20-26':>7}{'12m':>6}{'lose yrs':>9}{'2026 R':>8}{'worst12m R':>11}{'t':>6}")
S = {}
for k, R in C.items():
    s = S[k] = stats(R)
    print(f"{k:28}{s['pf']:7.2f}{s['pf_old']:7.2f}{s['pf_new']:7.2f}{s['pf_12m']:6.2f}{s['lose']:6d}/18{s['r26']:8.1f}{s['w12']:11.1f}{s['t']:6.2f}")
print("\nyearly R:"); print("year  " + "".join(f"{k[:3]:>7}" for k in C))
for y in range(2008, 2027): print(f"{y}  " + "".join(f"{S[k]['ys'][y]:7.1f}" for k in C))
