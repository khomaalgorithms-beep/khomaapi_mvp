import json, itertools, numpy as np
from multiprocessing import Pool
import orb_lab as OL
FILTERS = ["trend20", "trend50", "gap", "pdl", "onl", "narrow", "wide", "nr7", "inside", "hivol", "lovol", "firstbar"]
S1 = json.load(open("results/orb_stage1.json"))
def top3(sym):
    X = sorted([r for r in S1 if r["sym"] == sym], key=lambda r: -(r["SEL"]["mean"] if r["SEL"]["mean"] == r["SEL"]["mean"] else -9))
    return [dict(n=r["n"], entry=r["entry"], cutoff=r["cutoff"]) for r in X[:3]]
def job(sym):
    out = []
    for base in top3(sym):
        b0 = OL.stats(OL.run(sym, **base), "SEL")
        kept = []
        for f in FILTERS:
            s = OL.stats(OL.run(sym, filt=(f,), **base), "SEL")
            row = dict(sym=sym, base=base, filt=[f], SEL=s, BACK=OL.stats(OL.run(sym, filt=(f,), **base), "BACK"), base_SEL=b0)
            out.append(row)
            if s["n"] > 0 and s["mean"] == s["mean"] and s["mean"] > b0["mean"] and s["tpy"] >= 40: kept.append((s["mean"], f))
        kept.sort(reverse=True)
        if len(kept) >= 2:
            fs = (kept[0][1], kept[1][1]); res = OL.run(sym, filt=fs, **base)
            out.append(dict(sym=sym, base=base, filt=list(fs), SEL=OL.stats(res, "SEL"), BACK=OL.stats(res, "BACK"), base_SEL=b0))
    return out
if __name__ == "__main__":
    with Pool(8) as pool: R = [x for xs in pool.map(job, OL.MARKETS) for x in xs]
    json.dump(R, open("results/orb_stage2.json", "w"), default=float)
    for sym in OL.MARKETS:
        X = [r for r in R if r["sym"] == sym and r["SEL"]["tpy"] >= 40 and r["SEL"]["mean"] == r["SEL"]["mean"]]
        X.sort(key=lambda r: -r["SEL"]["mean"])
        b = X[0]["base_SEL"] if X else None
        print(f"\n{sym}: best base SEL R {max(r['base_SEL']['mean'] for r in R if r['sym']==sym):+.3f}. Best filtered (>=40 trades/yr):")
        for r in X[:4]:
            s, bk = r["SEL"], r["BACK"]
            print(f"   {r['base']['n']:2}m {r['base']['entry']:5} c{r['base']['cutoff']:3} + {'+'.join(r['filt']):18} | SEL {s['tpy']:3.0f}/yr R {s['mean']:+.3f} t {s['t']:+.1f} PF {s['pf']:.2f} win {s['win']*100:.0f}% | BACK PF {bk['pf']:.2f}")
    # filter usefulness across all markets: how often each filter improves its base
    print("\nFilter helps (raises SELECT R) in % of the 24 market-base cases:")
    for f in FILTERS:
        Y = [r for r in R if r["filt"] == [f]]
        print(f"   {f:9} {np.mean([r['SEL']['mean'] > r['base_SEL']['mean'] for r in Y if r['SEL']['mean']==r['SEL']['mean']])*100:4.0f}%  | median R change {np.median([r['SEL']['mean']-r['base_SEL']['mean'] for r in Y if r['SEL']['mean']==r['SEL']['mean']]):+.3f}")
