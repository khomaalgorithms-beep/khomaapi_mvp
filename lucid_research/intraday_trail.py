"""Same 50K eval/fast-pass logic under INTRADAY trailing (floor = highest open equity - $2,000, updated every
minute from the prior minute's high; locks at $50,100) vs Lucid's EOD trailing. Real costs (Lucid comm + 1 tick)."""
import numpy as np, stress as T
from lucid_sizes import SIZES
lg = T.legs(dict(comm=1.0, slip=1)); g = T.G
def run(seq, A, intraday, risk, cap_day=1440):
    S = A["start"]; bal = S; floor = S - A["mll"]; peak = S; locked = False; dps = []
    for n, L in enumerate(seq, 1):
        dp = 0.0
        if L is not None:
            q = int(risk * L["mult"] // L["rct"]); q = 1 if q == 0 and L["rct"] <= 1.5 * risk * L["mult"] else q; q = min(q, A["eval_micros"])
            if q > 0:
                B = L["best"] * q; W = L["worst"] * q
                need = S + A["target"] - bal; best = max(dps) if dps else 0.0
                tp = min(max(need, best / .5 - (bal - S)) if best > 0 else need, cap_day)
                fl = np.full(len(B), floor)
                if intraday and not locked:
                    hw = np.maximum.accumulate(np.r_[peak, bal + B[:-1]])          # prior-minute high-water
                    fl = np.minimum(np.maximum(floor, hw - A["mll"]), S + 100)
                br = np.where(bal + W <= fl)[0]; hit = np.where(B >= tp + 0.5 * q)[0]
                ib = br[0] if len(br) else 10**9; ih = hit[0] if len(hit) else 10**9
                if ib <= ih and ib < 10**9: return False, n
                dp = tp if ih < 10**9 else L["net"] * q
                if intraday and not locked:
                    upto = (ih if ih < 10**9 else len(B) - 1)
                    peak = max(peak, bal + B[:upto + 1].max()); floor = min(max(floor, peak - A["mll"]), S + 100)
        bal += dp; dps.append(dp)
        if bal >= S + A["target"] and max(dps) <= .5 * (bal - S): return True, n
        peak = max(peak, bal)
        if locked or peak > S + A["mll"] + 100: locked = True; floor = S + 100
        else: floor = max(floor, peak - A["mll"])
    return False, len(seq)
A = SIZES["50K"]
for lab, (y0, y1) in {"2020-26": (2020, 2026), "2024-26": (2024, 2026)}.items():
    st = np.where((g.year >= y0) & (g.year <= y1))[0]
    for risk in (1000, 700, 500):
        for intr in (False, True):
            r = [run([lg.get(d) for d in range(s, min(s + 120, g.D))], A, intr, risk) for s in st]
            p = np.array([x[0] for x in r]); dd = np.array([x[1] for x in r])
            print(f"{lab} risk ${risk:4d} {'INTRADAY trailing' if intr else 'EOD trailing (Lucid)':22}: pass {p.mean()*100:4.1f}%  median {np.median(dd[p]):.0f}d")
