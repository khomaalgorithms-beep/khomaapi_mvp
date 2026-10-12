"""Run strategy families over the universe. Pre-registered protocol:
  IS (discovery) 2008-2019 | OOS (validation) 2020-2023 | HOLDOUT 2024-2026-10 (locked: stats are
  computed and stored but never used for selection; the search report does not print them).
Gate 1 (IS): N >= 150, mean net R > 0, t(R) >= 2.5  -> trades saved for later stages.
Every (family, params, symbol) run is logged as a trial (needed for deflated-Sharpe / multiple testing)."""
import sys, os, json, time, traceback
import numpy as np
from multiprocessing import Pool
import harness as Hn
import strategies as S

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
PERIODS = {"IS": (2008, 2019), "OOS": (2020, 2023), "HO": (2024, 2026)}


def stats(R):
    n = len(R)
    if n < 2:
        return dict(n=n, mean=float(np.mean(R)) if n else 0.0, t=0.0, win=0.0, pf=0.0)
    m = R.mean(); sd = R.std(ddof=1)
    pos = R[R > 0].sum(); neg = -R[R < 0].sum()
    return dict(n=n, mean=float(m), t=float(m / sd * np.sqrt(n)) if sd > 0 else 0.0,
                win=float((R > 0).mean()), pf=float(pos / neg) if neg > 0 else 99.0)


def run_symbol(args):
    sym, fams = args
    rows = []; saved = {}
    try:
        g = Hn.load(sym)
    except Exception as ex:
        return rows, saved
    yr = g.year
    for fam in fams:
        fn, grid = S.FAMILIES[fam]
        if fam in S.ONLY and g.spec["cls"] not in S.ONLY[fam]:
            continue
        for p in grid:
            p = S.fix_w1(p) if fam == "silver_bullet" else p
            key = f"{fam}|{json.dumps(p, sort_keys=True)}|{sym}"
            try:
                tr = fn(g, **p)
                if len(tr["d"]) < 30:
                    rows.append(dict(key=key, fam=fam, sym=sym, err="few")); continue
                r = Hn.simulate(g, tr["d"], tr["e"], tr["side"], tr["px"], tr["stop"], tr["tgt"], tr["xm"], tr["trail"], tr["be"])
            except Exception as ex:
                rows.append(dict(key=key, fam=fam, sym=sym, err=repr(ex)[:200])); continue
            R = np.clip(r["R"], -5, 50)      # guard against tiny-stop outliers
            y = yr[r["d"]]
            row = dict(key=key, fam=fam, sym=sym, params=p)
            for nm, (y0, y1) in PERIODS.items():
                st = stats(R[(y >= y0) & (y <= y1)])
                for k2, v in st.items():
                    row[f"{nm}_{k2}"] = v
            # IS per-year positivity
            isy = [R[y == yy].mean() for yy in range(2008, 2020) if (y == yy).sum() >= 10]
            row["IS_yrs_pos"] = float(np.mean([v > 0 for v in isy])) if isy else 0.0
            rows.append(row)
            if row["IS_n"] >= 150 and row["IS_mean"] > 0 and row["IS_t"] >= 2.5:
                saved[key] = {k: r[k] for k in ("d", "e", "R", "net", "mae", "stop", "side", "hold", "why")}
                saved[key]["date"] = g.days[r["d"]]
    Hn._cache.clear()
    return rows, saved


if __name__ == "__main__":
    fams = sys.argv[1].split(",") if len(sys.argv) > 1 and sys.argv[1] != "all" else list(S.FAMILIES)
    syms = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] != "all" else Hn.TRADABLE
    tag = sys.argv[3] if len(sys.argv) > 3 else "run"
    t0 = time.time()
    allrows = []; allsaved = {}
    with Pool(int(os.environ.get("NPROC", 5))) as pool:
        for rows, saved in pool.imap_unordered(run_symbol, [(s, fams) for s in syms]):
            allrows += rows; allsaved.update(saved)
            print(f"{rows[0]['sym'] if rows else '?'} done: {len(rows)} trials, {len(saved)} pass G1  ({time.time()-t0:.0f}s)", flush=True)
    with open(os.path.join(OUT, f"trials_{tag}.jsonl"), "w") as f:
        for r in allrows:
            f.write(json.dumps(r) + "\n")
    import pickle
    with open(os.path.join(OUT, f"g1trades_{tag}.pkl"), "wb") as f:
        pickle.dump(allsaved, f)
    print(f"TOTAL trials {len(allrows)}, G1 survivors {len(allsaved)}, {time.time()-t0:.0f}s")
