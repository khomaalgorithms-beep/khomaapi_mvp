"""Second-sleeve test (pre-registered): rebuild the top-10 candidates' trades (real costs via each market's spec),
measure correlation with the live NQ 10:30 rule and the effect of a 50/50 risk portfolio.
PASS criteria (fixed before running): candidate PF > 1.0 in 2008-19 AND in 2020-26; |daily correlation| < 0.2;
the 50/50 portfolio has a smaller max drawdown (in R) than the live rule alone AND no lower total R per unit DD."""
import json, numpy as np, harness as Hn, strategies as S, dashboard_data as DDm
from multiprocessing import Pool
def daily_R(fam, sym, params):
    g = Hn.load(sym); tr = getattr(S, fam)(g, **params)
    if len(tr["d"]) == 0: return {}
    r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"])
    out = {}
    for d, R in zip(r["d"], r["R"]):
        if g.suspect[int(d)]: continue
        k = int(g.days[int(d)]); out[k] = out.get(k, 0.0) + float(R)
    return out
def job(c):
    try: return c, daily_R(c["fam"], c["sym"], c["params"])
    except Exception as e: return c, {"err": str(e)}
if __name__ == "__main__":
    L, g = DDm.lg(); base = {int(g.days[d]): L[d]["net"] / L[d]["rct"] for d in L}
    cands = json.load(open("results/sleeve_candidates.json"))
    with Pool(5) as pool: res = pool.map(job, cands)
    allk = sorted(set(int(x) for x in g.days if x >= np.datetime64("2008-03-01").astype(np.int64)))
    cut = np.datetime64("2020-01-01").astype(np.int64)
    def stats(series):
        r = np.array([series.get(k, 0.0) for k in allk]); eq = np.cumsum(r); dd = -(eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min()
        pf = lambda x: x[x > 0].sum() / max(1e-9, -x[x < 0].sum())
        early = np.array(allk) < cut
        yrs = {}
        for k, v in zip(allk, r): y = str(np.datetime64(k, "D"))[:4]; yrs[y] = yrs.get(y, 0) + v
        return r, dict(pf_old=pf(r[early]), pf_new=pf(r[~early]), tot=r.sum(), dd=dd, ratio=r.sum() / dd, lose=sum(v < 0 for v in yrs.values()))
    rb, sb = stats(base)
    print(f"LIVE RULE alone          | PF 08-19 {sb['pf_old']:.2f} | PF 20-26 {sb['pf_new']:.2f} | total {sb['tot']:6.1f}R | maxDD {sb['dd']:5.1f}R | total/DD {sb['ratio']:.2f} | losing yrs {sb['lose']}/19")
    for c, ser in res:
        if "err" in ser or not ser: print(c["sym"], c["fam"], "ERROR", ser.get("err") if isinstance(ser, dict) else ""); continue
        rc, sc = stats(ser)
        both = (rb != 0) & (rc != 0); corr = np.corrcoef(rb[both], rc[both])[0, 1] if both.sum() > 30 else np.corrcoef(rb, rc)[0, 1]
        corr_all = np.corrcoef(rb, rc)[0, 1]
        port = {k: 0.5 * base.get(k, 0.0) + 0.5 * ser.get(k, 0.0) for k in allk}; rp, sp = stats(port)
        ok = sc["pf_old"] > 1 and sc["pf_new"] > 1 and abs(corr_all) < 0.2 and sp["dd"] < sb["dd"] * 0.5 + 1e-9 * 0 + sp["dd"] * 0 and True
        verdict = "PASS" if (sc["pf_old"] > 1 and sc["pf_new"] > 1 and abs(corr_all) < 0.2 and sp["ratio"] >= sb["ratio"] and sp["dd"] * 2 < sb["dd"] * 2) else "fail"
        print(f"{c['sym']:3} {c['fam']:12} | PF 08-19 {sc['pf_old']:.2f} | PF 20-26 {sc['pf_new']:.2f} | total {sc['tot']:6.1f}R | corr {corr_all:+.2f} (same-day {corr:+.2f}) || 50/50 PORT total/DD {sp['ratio']:.2f} (live {sb['ratio']:.2f}) maxDD {sp['dd']:5.1f}R losing yrs {sp['lose']}/19 | {verdict}")
