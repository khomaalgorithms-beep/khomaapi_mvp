"""Does recent strategy performance predict the next trade? (equity-curve / cool-off filters)
Shadow-trade every signal; decide to take the real trade using ONLY past shadow results.
Pre-registered menu: rolling mean R over last N trades > 0 (N=10,20,40,60); pause K days after L consecutive
losses (L=3,4,5; K=3,5); same acceptance rule as filter_study (each period skipped<0 and < kept, pooled p<.05)."""
import math, numpy as np, filter_study as FS
g, d, R, e = FS.trades(); y = g.year[d]
def corr_report():
    for n in (5, 10, 20, 40):
        past = np.array([R[max(0, i - n):i].mean() if i >= n else np.nan for i in range(len(R))])
        m = ~np.isnan(past)
        print(f"corr(next trade R, mean of last {n:2d}) = {np.corrcoef(past[m], R[m])[0,1]:+.3f}")
def evaluate(name, skip):
    ok = True; cells = []
    for p, (y0, y1) in FS.PER.items():
        m = (y >= y0) & (y <= y1); s, k = R[m & skip], R[m & ~skip]
        cells.append(f"{len(s):4d} {s.mean() if len(s) else np.nan:+.3f} {k.mean():+.3f}")
        ok &= len(s) >= 20 and s.mean() < 0 and s.mean() < k.mean()
    pv = FS.welch_p(R[~skip], R[skip])
    print(f"{name:36} " + " | ".join(cells) + f" | p={pv:.3f} {'SKIP ✔' if ok and pv < .05 else ('(weak)' if ok else 'reject')}")
corr_report()
print(f"\n{'rule (skip when...)':36} " + " | ".join(f'{p:^20}' for p in FS.PER))
for n in (10, 20, 40, 60):
    past = np.array([R[max(0, i - n):i].mean() if i >= n else 1 for i in range(len(R))])
    evaluate(f"last {n} trades avg R <= 0", past <= 0)
for L in (3, 4, 5):
    for K in (3, 5):
        skip = np.zeros(len(R), bool); streak = 0; pause = 0
        for i in range(len(R)):
            if pause > 0:
                skip[i] = True; pause -= 1
            if R[i] < 0: streak += 1
            else: streak = 0
            if streak >= L and pause == 0:
                pause = K; streak = 0
        evaluate(f"pause {K} after {L} losses in a row", skip)
