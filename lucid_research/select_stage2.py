"""Stage 2 selection over G1 survivors (pre-registered; holdout 2024-26 is NOT looked at here).
G2  OOS 2020-23: mean R > 0 and Holm-corrected one-sided p < 0.10 across all G1 survivors.
G3  robustness: (a) still positive OOS at DOUBLE slippage, (b) parameter plateau: the median IS mean R
    of one-step neighbours (same family+symbol) >= 50% of the candidate's, (c) >= 70% of years
    2008-2023 positive.
Usage: python3 select_stage2.py tag1[,tag2...]"""
import sys, os, json, pickle, math
import numpy as np
import harness as Hn

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def load(tags):
    trials, trades = [], {}
    for tg in tags:
        with open(os.path.join(RES, f"trials_{tg}.jsonl")) as f:
            trials += [json.loads(l) for l in f]
        with open(os.path.join(RES, f"g1trades_{tg}.pkl"), "rb") as f:
            trades.update(pickle.load(f))
    return trials, trades


def neighbours(row, by_fs):
    p = row["params"]; out = []
    for r in by_fs.get((row["fam"], row["sym"]), []):
        q = r["params"]
        if q == p or set(q) != set(p):
            continue
        if sum(q[k] != p[k] for k in p) == 1 and "IS_mean" in r:
            out.append(r["IS_mean"])
    return out


def main(tags):
    trials, trades = load(tags)
    ok = [r for r in trials if "IS_mean" in r]
    print(f"trials logged: {len(trials)} (valid {len(ok)}); G1 survivors: {len(trades)}")
    by_fs = {}
    for r in ok:
        by_fs.setdefault((r["fam"], r["sym"]), []).append(r)
    rows = {r["key"]: r for r in ok}
    cands = []
    for k, tr in trades.items():
        r = rows[k]
        y = tr["date"].astype("datetime64[D]").astype("datetime64[Y]").astype(int) + 1970
        R = np.clip(tr["R"], -5, 50)
        oos = R[(y >= 2020) & (y <= 2023)]
        if len(oos) < 30:
            continue
        m, sd = oos.mean(), oos.std(ddof=1)
        tt = m / sd * np.sqrt(len(oos)) if sd > 0 else 0
        sp = Hn.SPEC[r["sym"]]
        extra = 2 * sp["tick"] * sp["slip"] / tr["stop"]          # one more slip tick per side
        oos2 = (R - extra)[(y >= 2020) & (y <= 2023)]
        yrs = [R[y == yy].mean() for yy in range(2008, 2024) if (y == yy).sum() >= 10]
        nb = neighbours(r, by_fs)
        cands.append(dict(key=k, fam=r["fam"], sym=r["sym"], IS_n=r["IS_n"], IS_mean=r["IS_mean"], IS_t=r["IS_t"],
                          OOS_n=len(oos), OOS_mean=float(m), OOS_t=float(tt), p=1 - phi(tt),
                          OOS_mean_2x=float(oos2.mean()), yrs_pos=float(np.mean([v > 0 for v in yrs])),
                          nb_ratio=float(np.median(nb) / r["IS_mean"]) if nb else float("nan"),
                          trades_per_yr=float(len(R) / max(1, len(set(y.tolist())))),
                          avg_stop=float(np.mean(tr["stop"])), win=float((R > 0).mean())))
    # Holm across ALL G1 survivors
    cands.sort(key=lambda c: c["p"])
    mtot = len(cands); alpha = 0.10
    alive = True
    for i, c in enumerate(cands):
        thr = alpha / (mtot - i)
        alive = alive and (c["p"] <= thr) and c["OOS_mean"] > 0
        c["G2"] = alive
        c["G3"] = c["G2"] and c["OOS_mean_2x"] > 0 and c["yrs_pos"] >= 0.70 and (np.isnan(c["nb_ratio"]) or c["nb_ratio"] >= 0.5)
    soft = [c for c in cands if c["OOS_mean"] > 0 and c["OOS_t"] >= 1.5]
    print(f"OOS positive & t>=1.5 (uncorrected): {len(soft)}; G2 (Holm 10%): {sum(c['G2'] for c in cands)}; G3: {sum(c['G3'] for c in cands)}")
    hdr = f"{'family':13} {'sym':4} {'IS_n':>5} {'ISmR':>6} {'ISt':>5} {'OOSn':>5} {'OOSmR':>6} {'OOSt':>5} {'2xslip':>6} {'yrs+':>5} {'nb':>5} {'win':>4} G2 G3  params"
    print(hdr)
    for c in sorted(soft, key=lambda c: -c["OOS_t"])[:80]:
        print(f"{c['fam']:13} {c['sym']:4} {c['IS_n']:5d} {c['IS_mean']:6.3f} {c['IS_t']:5.2f} {c['OOS_n']:5d} {c['OOS_mean']:6.3f} {c['OOS_t']:5.2f} "
              f"{c['OOS_mean_2x']:6.3f} {c['yrs_pos']:5.2f} {c['nb_ratio']:5.2f} {c['win']:4.2f} {'Y' if c['G2'] else '.'}  {'Y' if c['G3'] else '.'}  {c['key'].split('|')[1]}")
    with open(os.path.join(RES, f"stage2_{'_'.join(tags)}.json"), "w") as f:
        json.dump(cands, f)
    # family-level summary: how many symbol/param trials per family and how many pass
    fam = {}
    for r in ok:
        f_ = fam.setdefault(r["fam"], [0, 0, 0])
        f_[0] += 1
    for c in cands:
        fam[c["fam"]][1] += 1
        fam[c["fam"]][2] += c["G3"]
    print("\nfamily: trials / G1 / G3")
    for k, v in sorted(fam.items(), key=lambda x: -x[1][1]):
        print(f"  {k:14} {v[0]:6d} {v[1]:5d} {v[2]:4d}")


if __name__ == "__main__":
    main(sys.argv[1].split(","))
