import sys; sys.path.insert(0, ".."); import numpy as np, dashboard_data as DDm
from lucid_sizes import SIZES
from app.orb_validated import DATA
L, g = DDm.lg(); A = SIZES["50K"]; risk = DATA["presets"]["50K"]["eod"]["norm"]["risk"]
for lab, a, b in (("2008-2026", "2008-03-01", "2025-10-09"), ("2020-2026", "2020-01-01", "2025-10-09")):
    st = np.where((g.days >= np.datetime64(a).astype(np.int64)) & (g.days <= np.datetime64(b).astype(np.int64)))[0]
    N = []
    for s in st:
        t = s; n = 0
        while t < g.D - 1 and n < 40:
            n += 1; ok, d = DDm.eval_run([L.get(x) for x in range(t, g.D)], A, risk, "eod"); t += d
            if ok: break
        N.append(n)
    N = np.array(N); extra = np.maximum(N - 5, 0); c = extra * A["price"]
    print(f"{lab}: P(no pass in first 5) {np.mean(N>5)*100:.1f}% | you pay per client avg ${c.mean():.0f} | when triggered: avg {extra[extra>0].mean():.1f} evals (${c[c>0].mean():.0f}), 90% {np.percentile(extra[extra>0],90):.0f} evals, worst {extra.max()} (${c.max():.0f}) | client's own spend avg ${np.minimum(N,5).mean()*A['price']:.0f}")
