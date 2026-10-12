"""FOMC handling for the NQ chain: (a) hold as usual, (b) skip FOMC days, (c) flatten 1 min before the statement.
Trades are re-simulated on FOMC days with a time exit at the statement minute (actual historical time)."""
import csv, os, numpy as np, harness as Hn, strategies as S, stress as T, filter_study as FS
g = Hn.load("NQ"); T.G = g
fomc = {}
for r in csv.DictReader(open(os.path.join(Hn.DATA, "events.csv"))):
    if r["event"].startswith("FOMC") and r["time_et"].strip() >= "10:31" and r["time_et"].strip() <= "15:59":
        fomc[int(np.datetime64(r["date"], "D").astype(np.int64))] = Hn.t(r["time_et"].strip())
fdays = np.where(np.isin(g.days, list(fomc)))[0]
taken = {}; early = {}
for fam, p, mult in T.BASE_CHAIN:
    tr = getattr(S, fam)(g, **p)
    r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"])
    xm = np.array([fomc.get(int(g.days[dd]), tr["xm"][i]) - 1 if int(g.days[dd]) in fomc else tr["xm"][i] for i, dd in enumerate(tr["d"])])
    xm = np.maximum(xm, tr["e"] + 1)
    r2 = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], tr["tgt"], xm, tr["trail"], tr["be"])
    for i, dd in enumerate(r["d"]):
        dd = int(dd)
        if dd in taken: continue
        taken[dd] = r["R"][i]; early[dd] = r2["R"][i]
d = np.array(sorted(taken)); y = g.year[d]; isf = np.isin(d, fdays)
R0 = np.array([taken[x] for x in d]); R1 = np.array([early[x] for x in d])
print(f"FOMC days with a trade: {isf.sum()} (entry before the statement: {np.mean([early[x]!=taken[x] for x in d[isf]])*100:.0f}% changed)")
print(f"{'period':8} | {'FOMC hold R':>11} {'FOMC flatten-before R':>22} | all-days avg R: hold / skip FOMC / flatten-before")
for p, (y0, y1) in FS.PER.items():
    m = (y >= y0) & (y <= y1)
    print(f"{p:8} | {R0[m & isf].mean():+11.3f} {R1[m & isf].mean():+22.3f} | {R0[m].mean():+.4f} / {R0[m & ~isf].sum()/m.sum():+.4f} / {np.where(isf, R1, R0)[m].mean():+.4f}")
diff = (R1 - R0)[isf]
print(f"flatten-before minus hold on FOMC days: mean {diff.mean():+.3f}R, t={diff.mean()/diff.std(ddof=1)*np.sqrt(len(diff)):.2f}, better in {np.mean(diff>0)*100:.0f}% / worse {np.mean(diff<0)*100:.0f}% of FOMC trades")
