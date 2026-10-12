"""Parity: the live resting-order ORB logic (orb_levels + first-break rule, as MultiAutopilot runs it) vs the research
touch rule, last 500 sessions."""
import sys, numpy as np
sys.path.insert(0, "..")
from datetime import datetime
import orb_lab as OL
from app import orb_momentum as MOM
from app.orb_selective import Bar
g = OL.G("NQ"); t = OL.trades(g, n=5, entry="touch", cutoff=60, filt=("pdl",), stop="atr", stop_k=0.2)
res = {int(d): int(s) for d, s in zip(t["d"], t["side"])}
a = g.a; agree = dis = 0; bad = []
days = [d for d in range(g.D - 560, g.D) if not g.suspect[d] and np.isfinite(g.atr14[d]) and np.isfinite(g.p_high[d]) and g.sess_ok[d]]
for d in days[-500:]:
    date = str(g.dates[d])
    bars = [Bar(datetime.fromisoformat(f"{date} {(570+m-a)//60:02d}:{(570+m-a)%60:02d}"), float(g.O[d, m]), float(g.H[d, m]), float(g.L[d, m]), float(g.C[d, m]), 0) for m in range(a, a + 66) if not np.isnan(g.O[d, m])]
    ctx = MOM.DayContext(date, float(g.atr14[d]), 1, float(g.p_high[d]), float(g.p_low[d]))
    lv = MOM.orb_levels(bars, ctx, 0.25); live = 0
    if lv and not MOM.short_session(date):
        hi, lo, vl, vs = lv
        for b in bars:
            m = b.dt.hour * 60 + b.dt.minute
            if not (MOM.ORB_END_MIN <= m < MOM.ORB_LAST_MIN): continue
            up, dn = b.h >= hi + 0.25, b.l <= lo - 0.25
            if up and dn: break                         # ambiguous bar: research takes neither (side 0)
            if up: live = 1 if vl else 0; break
            if dn: live = -1 if vs else 0; break
    ref = res.get(d, 0)
    if live == ref: agree += 1
    else: dis += 1; bad.append((date, live, ref))
print(f"parity on {agree+dis} sessions: {agree} identical, {dis} differ"); print(bad[:12])
