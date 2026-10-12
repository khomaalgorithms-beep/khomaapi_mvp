"""Parity: live orb_decision (app.orb_momentum) vs the research rule (orb_lab, close entry) on the last 500 sessions."""
import sys, numpy as np
sys.path.insert(0, "..")
from datetime import datetime
import orb_lab as OL, harness as Hn
from app import orb_momentum as MOM
from app.orb_selective import Bar
g = OL.G("NQ"); SYS = dict(n=5, entry="close", cutoff=60, filt=("pdl",), stop="atr", stop_k=0.2)
t = OL.trades(g, **SYS); res = {int(d): (int(s), float(sd)) for d, s, sd in zip(t["d"], t["side"], t["stop"])}
a = g.a; days = [d for d in range(g.D - 520, g.D) if not g.suspect[d] and np.isfinite(g.atr14[d]) and np.isfinite(g.p_high[d])]
agree = dis = 0; bad = []
for d in days[-500:]:
    date = str(g.dates[d]); bars = []
    for m in range(a, a + 66):
        if np.isnan(g.O[d, m]): continue
        mins = 9 * 60 + 30 + (m - a); dt = datetime.fromisoformat(f"{date} {mins//60:02d}:{mins%60:02d}")
        bars.append(Bar(dt, float(g.O[d, m]), float(g.H[d, m]), float(g.L[d, m]), float(g.C[d, m]), 0.0))
    ctx = MOM.DayContext(date, float(g.atr14[d]), 1, float(g.p_high[d]), float(g.p_low[d]))
    st, sig = MOM.orb_decision(bars, ctx)
    live = (1 if sig.side == "long" else -1) if st == "signal" else 0
    ref = res.get(d, (0, 0))[0]
    if live == ref: agree += 1
    else: dis += 1; bad.append((date, live, ref, st, sig if st != "signal" else ""))
print(f"parity on {agree+dis} sessions: {agree} identical decisions, {dis} differ")
for b in bad[:10]: print("  ", b)
