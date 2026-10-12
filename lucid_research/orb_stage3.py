import json, itertools, numpy as np
from multiprocessing import Pool
import orb_lab as OL
S2 = json.load(open("results/orb_stage2.json"))
STOPS = [("opp", None), ("mid", None), ("atr", 0.1), ("atr", 0.2), ("atr", 0.3), ("atr", 0.5)]
TGTS = [None, 1.0, 1.5, 2.0, 3.0]; BES = [None, 1.0]; TRAILS = [None, (1.0, 1.0)]
def best2(sym):
    X = [r for r in S2 if r["sym"] == sym and r["SEL"]["tpy"] >= 40 and r["SEL"]["mean"] == r["SEL"]["mean"]]
    X.sort(key=lambda r: -r["SEL"]["mean"])
    return dict(**X[0]["base"], filt=tuple(X[0]["filt"]))
def job(sym):
    sysd = best2(sym); out = []
    for (sm, sk), tg, be, tr in itertools.product(STOPS, TGTS, BES, TRAILS):
        if be and tr: continue
        res = OL.run(sym, stop=sm, stop_k=sk, tgt_R=tg, be=be, trail=tr, **sysd)
        out.append(dict(sym=sym, sys=dict(sysd, filt=list(sysd["filt"])), stop=sm, stop_k=sk, tgt=tg, be=be, trail=tr,
                        SEL=OL.stats(res, "SEL"), BACK=OL.stats(res, "BACK")))
    return out
if __name__ == "__main__":
    with Pool(8) as pool: R = [x for xs in pool.map(job, OL.MARKETS) for x in xs]
    json.dump(R, open("results/orb_stage3.json", "w"), default=float)
    lab = lambda r: f"stop {r['stop']}{'' if r['stop_k'] is None else r['stop_k']} tgt {r['tgt'] or 'close'} {'BE' if r['be'] else ''}{'trail' if r['trail'] else ''}"
    for sym in OL.MARKETS:
        X = [r for r in R if r["sym"] == sym and r["SEL"]["rd"] == r["SEL"]["rd"]]
        X.sort(key=lambda r: (-r["SEL"]["rd"], -r["SEL"]["pf"]))
        s0 = X[0]["sys"]
        print(f"\n{sym} [{s0['n']}m {s0['entry']} +{'+'.join(s0['filt'])}] — best exits by SELECT return/drawdown:")
        for r in X[:4]:
            s, bk = r["SEL"], r["BACK"]
            print(f"   {lab(r):34} | SEL R {s['mean']:+.3f} t {s['t']:+.1f} PF {s['pf']:.2f} win {s['win']*100:.0f}% ret/DD {s['rd']:.2f} | BACK PF {bk['pf']:.2f}")
        good = np.mean([r["SEL"]["pf"] > 1 for r in X]) * 100
        print(f"   ({good:.0f}% of {len(X)} exit variants profitable in SELECT)")
