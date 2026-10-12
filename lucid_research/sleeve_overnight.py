import numpy as np, dashboard_data as DDm
from sleeve_test import daily_R
L, g = DDm.lg(); base = {int(g.days[d]): L[d]["net"] / L[d]["rct"] for d in L}
on = daily_R("window_drift", "NQ", {"start": "18:00", "end": "08:00", "direction": "long"})
yrs = {}
for k in sorted(set(base) | set(on)):
    y = str(np.datetime64(k, "D"))[:4]; b, o = base.get(k, 0), on.get(k, 0)
    a = yrs.setdefault(y, [0, 0, 0]); a[0] += b; a[1] += o; a[2] += 0.5 * b + 0.5 * o
print("year  live  overnight  50/50")
for y, (b, o, p) in yrs.items(): print(f"{y} {b:+6.1f} {o:+8.1f} {p:+7.1f}")
