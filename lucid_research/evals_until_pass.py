"""How many LucidFlex 50K evaluations (Normal preset, one at a time, back-to-back) until the first pass —
real sequences (losing streaks cluster), starts every trading day. Cost at $90.20 per eval."""
import sys; sys.path.insert(0, ".."); import numpy as np, dashboard_data as DDm
from lucid_sizes import SIZES
from app.orb_validated import DATA
L, g = DDm.lg(); A = SIZES["50K"]; risk = DATA["presets"]["50K"]["eod"]["norm"]["risk"]
for lab, a, b in (("2008-2026 (long run)", "2008-03-01", "2025-10-09"), ("2020-2026 (good markets)", "2020-01-01", "2025-10-09"), ("2025 starts (current)", "2025-01-01", "2025-10-09")):
    st = np.where((g.days >= np.datetime64(a).astype(np.int64)) & (g.days <= np.datetime64(b).astype(np.int64)))[0]
    N = []; D = []
    for s in st:
        t = s; n = 0
        while t < g.D - 1 and n < 30:
            n += 1; ok, d = DDm.eval_run([L.get(x) for x in range(t, g.D)], A, risk, "eod"); t += d
            if ok: break
        N.append(n); D.append(t - s)
    N = np.array(N); c = N * A["price"]
    print(f"{lab}: evals avg {N.mean():.1f}, median {np.median(N):.0f}, 75% {np.percentile(N,75):.0f}, 90% {np.percentile(N,90):.0f}, 95% {np.percentile(N,95):.0f}, worst {N.max()} | "
          f"cost avg ${c.mean():.0f}, median ${np.median(c):.0f}, 90% ${np.percentile(c,90):.0f}, 95% ${np.percentile(c,95):.0f}, worst ${c.max():.0f} | "
          f"P(>6 evals) {np.mean(N>6)*100:.0f}% | trading days to funded median {np.median(D):.0f}, 90% {np.percentile(D,90):.0f}")
