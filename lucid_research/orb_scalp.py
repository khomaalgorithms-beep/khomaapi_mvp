"""User's ORB scalp idea on NQ (traded as MNQ), 1-min FirstRate data 2008-2026.
Range 09:30-09:59. From 10:00 to 15:00, OCO orders at the range high and low; first fill cancels the other.
  A = BREAKOUT: buy stop at ORH / sell stop at ORL (entry slips `slip` pts)
  B = FADE (true limit): sell limit at ORH / buy limit at ORL (fills at price only if traded 1 tick through)
SL = 3 pts (stop: slips `slip` pts), TP = 5 or 7 pts (limit: needs 1 tick through). Flat 15:59. Commission 0.5 pt RT.
Intrabar ambiguity (SL and TP inside one 1-min bar): 'worst' = SL first (honest), 'best' = TP first."""
import numpy as np, harness as Hn
g = Hn.load("NQ"); a = Hn.t("09:30"); e0 = Hn.t("10:00"); e1 = Hn.t("15:00"); fl = Hn.t("15:59")
TICK = 0.25; COMM = 0.5
def day(d, mode, sl, tp, slip, order):
    H = g.H[d]; L = g.L[d]; C = g.C[d]
    if np.isnan(H[a:e0]).any() or np.isnan(H[e0:fl]).all(): return None
    hi = np.nanmax(H[a:e0]); lo = np.nanmin(L[a:e0])
    for i in range(e0, e1):
        if np.isnan(H[i]): continue
        up = H[i] >= hi + (TICK if mode == "fade" else 0); dn = L[i] <= lo - (TICK if mode == "fade" else 0)
        if up and dn: return -(sl + slip) - COMM                     # both sides in one bar: count as a loss
        if not (up or dn): continue
        if mode == "break":
            side = 1 if up else -1; ent = (hi + slip) if up else (lo - slip)
        else:
            side = -1 if up else 1; ent = hi if up else lo
        stop = ent - side * sl; tgt = ent + side * tp
        for j in range(i, fl + 1):
            if np.isnan(H[j]): continue
            if j == i:      # entry bar: its high/low may have printed BEFORE the fill -> judge it by the close only
                if (C[j] <= stop) if side > 0 else (C[j] >= stop):
                    return -(sl + slip) - COMM
                continue
            hit_s = (L[j] <= stop) if side > 0 else (H[j] >= stop)
            hit_t = ((H[j] >= tgt + TICK) if side > 0 else (L[j] <= tgt - TICK)) and j > i   # (entry bar handled above)
            if hit_s and hit_t:
                return (tp if order == "best" else -(sl + slip)) - COMM
            if hit_s: return -(sl + slip) - COMM
            if hit_t: return tp - COMM
        return side * (C[fl] - ent) - COMM
    return None
def run(mode, sl, tp, slip, order):
    out = {}
    for d in range(g.D):
        if g.wd[d] > 4 or g.suspect[d]: continue
        r = day(d, mode, sl, tp, slip, order)
        if r is not None: out[d] = r
    return out
def show(name, R):
    ds = np.array(sorted(R)); r = np.array([R[x] for x in ds]); yr = g.year[ds]
    w = r > 0; pf = r[w].sum() / max(1e-9, -r[~w].sum())
    last = r[g.days[ds] >= np.datetime64("2025-10-10").astype(np.int64)]
    ys = {int(y): r[yr == y].sum() for y in sorted(set(yr))}
    print(f"{name:42} trades {len(r):5} | win {w.mean()*100:4.1f}% | avg {r.mean():+.2f} pt | PF {pf:4.2f} | total {r.sum():+8.0f} pt (${r.sum()*2:+,.0f}/MNQ) | last 12m {last.sum():+6.0f} pt | losing yrs {sum(v<0 for v in ys.values())}/{len(ys)}")
    return ys
if __name__ == "__main__":
    for mode, nm in (("break", "A BREAKOUT (stop entry)"), ("fade", "B FADE (limit entry)")):
        print(f"\n===== {nm} =====")
        for tp in (5, 7):
            for slip in (1, 2):
                for order in ("worst", "best"):
                    ys = show(f"SL 3 / TP {tp} / slip {slip} / {order}-case", run(mode, 3, tp, slip, order))
                    if order == "worst" and slip == 1:
                        print("    yearly pts: " + " ".join(f"{y}:{v:+.0f}" for y, v in ys.items()))
