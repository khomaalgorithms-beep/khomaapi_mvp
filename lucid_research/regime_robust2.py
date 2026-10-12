"""Robustness of the two survivors (neighbouring settings, not a search) + LucidFlex 50K pass impact."""
import numpy as np, harness as Hn, stress as T, dashboard_data as DDm
from lucid_sizes import SIZES
NQ = Hn.load("NQ"); T.G = NQ; g = NQ; D = g.D
lg = T.legs(dict(chain=[("tod_mom", dict(T=60, k=0.0, s=0.2, ref="open", tf=20, trail=None), 1.0)], comm=1.0, slip=1))
base = {d: v["net"] / v["rct"] for d, v in lg.items()}
v = g.atr14 / g.px
def volmask(win, pct):
    ok = set()
    for d in base:
        if d < win: continue
        w = v[d-win:d]; w = w[np.isfinite(w)]
        if len(w) and np.isfinite(v[d]) and v[d] >= np.percentile(w, pct): ok.add(d)
    return ok
def eqmask(n):
    ok = set(); h = []
    for d in sorted(base):
        if len(h) < n or sum(h[-n:]) > 0: ok.add(d)
        h.append(base[d])
    return ok
pf = lambda x: x[x > 0].sum() / max(1e-9, -x[x < 0].sum())
def st(keep):
    ds = np.array(sorted(keep)); r = np.array([base[d] for d in ds]); yr = g.year[ds]
    full = np.zeros(D); full[ds] = r; cs = np.cumsum(full)
    return f"PF {pf(r):.2f} | 08-19 {pf(r[yr<=2019]):.2f} | 20-26 {pf(r[yr>=2020]):.2f} | 2026 {r[yr==2026].sum():5.1f}R | worst12m {(cs[252:]-cs[:-252]).min():6.1f} | t {r.mean()/r.std()*np.sqrt(len(r)):.2f} | trades/yr {len(r)/18.6:.0f}"
print("BASE                 ", st(set(base)))
for win in (120, 250, 500):
    for pct in (40, 50, 60):
        print(f"vol win {win:3} pct {pct}   ", st(volmask(win, pct)))
for n in (30, 50, 100):
    print(f"equity filter {n:3}     ", st(eqmask(n)))
V = volmask(250, 50); E = eqmask(50)
print("vol250/50 + eq50      ", st(V & E))
# Lucid 50K Normal ($600) EOD pass impact
A = SIZES["50K"]
def lucid(keep):
    L = {d: lg[d] for d in keep}; out = []
    for a, b in (("2008-03-01", "2026-10-09"), ("2020-01-01", "2026-10-09"), ("2025-10-12", "2026-10-09")):
        st_ = np.where((g.days >= np.datetime64(a).astype(np.int64)) & (g.days <= np.datetime64(b).astype(np.int64)))[0]
        res = [DDm.eval_run([L.get(x) for x in range(s, min(s + 180, D))], A, 600, "eod") for s in st_]
        p = np.array([x[0] for x in res]); dd = np.array([x[1] for x in res])
        out.append(f"{p.mean()*100:3.0f}% ({np.median(dd[p]):.0f}d)")
    return "  full " + out[0] + " | 2020-26 " + out[1] + " | last12m " + out[2]
print("\nLucid 50K Normal $600, EOD:")
for k, keep in (("BASE", set(base)), ("vol filter", V), ("equity filter", E), ("both", V & E)):
    print(f"{k:14}", lucid(keep))
