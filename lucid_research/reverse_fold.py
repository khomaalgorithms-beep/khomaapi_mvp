"""Reverse fold: discover on 2020-23 (t>=2.5, N>=100), validate on 2008-19 with Holm (10%).
A real but recent edge would show up here; holdout 2024-26 stays locked."""
import sys, json, math
def phi(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
rows = []
for tg in sys.argv[1].split(","):
    rows += [json.loads(l) for l in open(f"results/trials_{tg}.jsonl")]
rows = [r for r in rows if "OOS_t" in r]
c = [r for r in rows if r["OOS_n"] >= 100 and r["OOS_mean"] > 0 and r["OOS_t"] >= 2.5]
print(f"trials {len(rows)}; discovered on 2020-23: {len(c)}")
for r in c: r["p"] = 1 - phi(r["IS_t"]) if r["IS_mean"] > 0 else 1.0
c.sort(key=lambda r: r["p"]); m = len(c); alive = True
for i, r in enumerate(c):
    alive = alive and r["p"] <= 0.10 / (m - i); r["holm"] = alive
print(f"{'family':13} {'sym':4} {'OOSn':>5} {'OOS_R':>6} {'OOS_t':>5} | {'ISn':>5} {'IS_R':>6} {'IS_t':>5} holm params")
for r in sorted(c, key=lambda r: -r["IS_t"])[:40]:
    print(f"{r['fam']:13} {r['sym']:4} {r['OOS_n']:5d} {r['OOS_mean']:6.3f} {r['OOS_t']:5.2f} | {r['IS_n']:5d} {r['IS_mean']:6.3f} {r['IS_t']:5.2f}  {'Y' if r['holm'] else '.'}  {json.dumps(r['params'])[:80]}")
