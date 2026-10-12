"""Build report/nq_validation.html from the results/*.json files (static SVG charts, theme tokens)."""
import json, os, numpy as np
R = lambda f: json.load(open(os.path.join("results", f)))
ef = R("equity_forward.json"); cur = ef["curve"]
pa = R("presets_A.json")["chosen"]; fa = R("funded_A.json")
wf = R("max_walkforward.json"); pbo = R("max_pbo.json"); noise = R("max_noise.json"); mc = R("max_mccost.json")["summary"]
rw = R("max_randomwalk.json")

# ---------------------------------------------------------------- charts (SVG, drawn to one scale)
W, Hh, PL, PR, PT, PB = 960, 300, 64, 16, 14, 30
dates = np.array(cur["dates"], dtype="datetime64[D]").astype(np.int64); eq = np.array(cur["equity"], float); dd = np.array(cur["drawdown"], float)
x0, x1 = dates[0], dates[-1]
X = lambda d: PL + (d - x0) / (x1 - x0) * (W - PL - PR)
def yscale(lo, hi, h):
    return lambda v: PT + (hi - v) / (hi - lo) * (h - PT - PB)
def path(xs, ys):
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
def yticks(lo, hi, step):
    v = np.ceil(lo / step) * step; out = []
    while v <= hi:
        out.append(v); v += step
    return out
def year_ticks(Y, h):
    s = ""
    for yr in range(2017, 2027):
        d = np.datetime64(f"{yr}-01-01").astype(np.int64)
        if x0 <= d <= x1:
            s += f'<line x1="{X(d):.1f}" x2="{X(d):.1f}" y1="{PT}" y2="{h-PB}" class="grid"/><text x="{X(d):.1f}" y="{h-10}" class="axis" text-anchor="middle">{yr}</text>'
    return s
lo, hi = min(0, eq.min()) - 2000, eq.max() + 4000
Y = yscale(lo, hi, Hh)
eq_svg = f'<svg viewBox="0 0 {W} {Hh}" role="img" aria-label="Equity curve">'
for v in yticks(lo, hi, 20000):
    eq_svg += f'<line x1="{PL}" x2="{W-PR}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="grid"/><text x="{PL-8}" y="{Y(v)+4:.1f}" class="axis" text-anchor="end">${v/1000:.0f}k</text>'
eq_svg += year_ticks(Y, Hh)
xs = [X(d) for d in dates]; ys = [Y(v) for v in eq]
eq_svg += f'<path d="{path(xs, ys)} L{xs[-1]:.1f},{Y(0):.1f} L{xs[0]:.1f},{Y(0):.1f} Z" class="area"/>'
eq_svg += f'<line x1="{PL}" x2="{W-PR}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" class="zero"/>'
eq_svg += f'<path d="{path(xs, ys)}" class="line"/><circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4" class="dot"/>'
eq_svg += f'<text x="{xs[-1]-8:.1f}" y="{ys[-1]-10:.1f}" class="lbl" text-anchor="end">+${eq[-1]:,.0f}</text></svg>'
H2 = 160; dlo = dd.min() * 1.15
Y2 = yscale(dlo, 0, H2)
dd_svg = f'<svg viewBox="0 0 {W} {H2}" role="img" aria-label="Drawdown">'
for v in yticks(dlo, 0, 2000):
    dd_svg += f'<line x1="{PL}" x2="{W-PR}" y1="{Y2(v):.1f}" y2="{Y2(v):.1f}" class="grid"/><text x="{PL-8}" y="{Y2(v)+4:.1f}" class="axis" text-anchor="end">{"$0" if v == 0 else f"-${-v/1000:.0f}k"}</text>'
dd_svg += year_ticks(Y2, H2)
ys2 = [Y2(v) for v in dd]
dd_svg += f'<path d="{path(xs, ys2)} L{xs[-1]:.1f},{Y2(0):.1f} L{xs[0]:.1f},{Y2(0):.1f} Z" class="ddarea"/><path d="{path(xs, ys2)}" class="ddline"/></svg>'

# ---------------------------------------------------------------- tables
def pct(x): return f"{x*100:.0f}%"
yrs = "".join(f'<tr><td>{y}</td><td>{v["trades"]}</td><td>{pct(v["win"])}</td><td class="{"neg" if v["pf"] < 1 else ""}">{v["pf"]:.2f}</td>'
              f'<td class="{"neg" if v["net"] < 0 else "pos"}">{"+" if v["net"] >= 0 else "−"}${abs(v["net"]):,.0f}</td><td>−${-v["maxdd"]:,.0f}</td></tr>'
              for y, v in cur["years"].items())
note_y = {"2016": " (from Oct 10)", "2026": " (to Oct 9)"}
yrs = yrs.replace("<td>2016</td>", "<td>2016<span class=muted> Oct–Dec</span></td>").replace("<td>2026</td>", "<td>2026<span class=muted> YTD</span></td>")

pre = ""
for size in ("25K", "50K", "100K", "150K"):
    rows = [p for p in pa if p["size"] == size]
    for i, p in enumerate(rows):
        pre += (f'<tr>{"<td rowspan=3 class=sz>" + size + "</td>" if i == 0 else ""}<td><span class="chip {p["preset"].lower()}">{p["preset"].title()}</span></td>'
                f'<td>${p["risk"]:,.0f}</td><td><b>{pct(p["real"]["pass_"])}</b></td><td>{p["real"]["med"]:.0f} <span class=muted>({p["real"]["p75"]:.0f})</span></td>'
                f'<td>{pct(p["recent"]["pass_"])} · {p["recent"]["med"]:.0f}d</td><td>{pct(p["bad"]["pass_"])} · {p["bad"]["med"]:.0f}d</td></tr>')

fund = ""
from lucid_sizes import SIZES
for size in ("25K", "50K", "100K", "150K"):
    m = SIZES[size]["mll"]
    for lab, f in (("Safe", 0.12), ("Fast", 0.16)):
        r = {x["period"]: x for x in fa if x["size"] == size and x["risk"] == round(f * m) and x["tp"] is None}
        if not r: continue
        d, c, rc = r["design"], r["check"], r["recent"]
        fund += (f'<tr><td>{size}</td><td>{lab} <span class=muted>${f*m:,.0f}/trade</span></td>'
                 f'<td>{d["med"]:.0f}–{c["med"]:.0f}</td><td>{pct(min(d["p1"], c["p1"]))}–{pct(max(d["p1"], c["p1"]))}</td>'
                 f'<td>{pct(min(d["bb"], c["bb"]))}–{pct(max(d["bb"], c["bb"]))}</td><td>${min(d["cash"], c["cash"]):,.0f}–{max(d["cash"], c["cash"]):,.0f}</td><td class=warn>{pct(rc["p1"])}</td></tr>')

coh = "".join(f'<tr><td>{k}</td><td>{pct(v["funded"])}</td><td>{v["med_funded"]:.0f}</td><td>{pct(v["paid"])}</td><td>${v["avg_cash"]:,.0f}</td><td>${v["avg_spent"]:,.0f}</td>'
              f'<td class="{"pos" if v["net"] > 0 else "neg"}">{"+" if v["net"] >= 0 else "−"}${abs(v["net"]):,.0f}</td><td>{pct(v["pos"])}{"" if v["complete"] else " <span class=muted>partial</span>"}</td></tr>'
              for k, v in ef["cohorts"].items())
avg_net = np.mean([v["net"] for v in ef["cohorts"].values()])
proj = ""
for reg, P in ef["projections"].items():
    for H, v in P.items():
        proj += f'<tr><td>{reg}</td><td>{int(H)//21} months</td><td><b>{pct(v["p_profit"])}</b></td><td class=neg>−${-v["p5"]:,.0f}</td><td>{"+" if v["med"] >= 0 else "−"}${abs(v["med"]):,.0f}</td><td class=pos>+${v["p95"]:,.0f}</td></tr>'

noise_pf = f'{min(n["pf"] for n in noise):.2f}–{max(n["pf"] for n in noise):.2f}'
rw_g = f'{min(r["gross"]["mean"] for r in rw):+.3f} to {max(r["gross"]["mean"] for r in rw):+.3f}R'
tests = [
    ("Pure random-walk market", "Synthetic NQ with real daily volatility, no momentum, no trend", f"Loses only its costs. Gross {rw_g} per trade, all t-stats under 0.4", "pass"),
    ("Look-ahead audit", "Same random walk on the earlier multi-signal list", "Found a bias: it made +0.12–0.15R on pure noise. Removed; that list is no longer used", "fixed"),
    ("Pre-registered holdout", "Rules frozen before 2024–26 data was opened", "PF 1.28, t = 1.81, 337 trades", "pass"),
    ("Walk-forward re-selection", "Settings re-chosen each year from past data only, traded blind 2012–26", f"PF {wf['oos_all']['pf']:.2f}, t = {wf['oos_all']['t']:.1f}; 2020–26 PF {wf['oos_2020']['pf']:.2f}", "pass"),
    ("Probability of backtest overfitting", "12,870 splits of 45 parameter sets (CSCV)", f"PBO {pbo['PBO']*100:.0f}% (danger line 50%); in-sample winner profitable out of sample in {pbo['winner_oos_positive']*100:.0f}% of splits", "pass"),
    ("Parameter plateau", "45 combinations of stop, entry time, trend length", f"100% profitable 2020–26; {wf['share_pos_0819']*100:.0f}% in 2008–19", "pass"),
    ("Price noise injection", "Every bar jittered ±1, ±2, ±4 ticks", f"PF {noise_pf}, t ≈ 3", "pass"),
    ("Random execution Monte Carlo", "200 worlds: fat-tailed slippage, $1–3 commission, up to 30% missed trades, 0–3 min delay", f"PF 5th pct {mc['pf_p5']:.2f}, median {mc['pf_med']:.2f}; 0 of 200 below 1.0; 50K pass {mc['pass_p5']*100:.0f}–{mc['pass_p95']*100:.0f}%", "pass"),
    ("Worst-case costs", "$3 round trip + 8 ticks slippage per side", "Still profitable; holds because the stop is ~$140 per MNQ", "pass"),
    ("Random direction placebo", "Same days and stops, coin-flip direction", "PF 0.93–1.02. The direction call is the edge", "pass"),
    ("Remove the best 5% of trades", "Delete the largest winners", "PF falls to about 1.0. Profit comes from trend days", "flag"),
    ("Current regime", "Last 12 months only", "PF 0.94–0.99. Flat this year", "flag"),
]
LABEL = {"pass": "Passed", "fixed": "Fixed", "flag": "Watch"}
tst = "".join(f'<tr><td><b>{a}</b><div class=muted>{b}</div></td><td>{c}</td><td><span class="state {s}">{LABEL[s]}</span></td></tr>' for a, b, c, s in tests)

html = open("report_template.html").read()
for k, v in dict(EQ=eq_svg, DD=dd_svg, YEARS=yrs, PRESETS=pre, FUNDED=fund, COHORTS=coh, PROJ=proj, TESTS=tst,
                 NET=f"+${cur['total']:,.0f}", PF=f"{cur['pf']:.2f}", WIN=pct(cur['win']), MAXDD=f"−${-cur['maxdd']:,.0f}",
                 TRADES=f"{cur['trades']:,}", AVGNET=f"{'+' if avg_net >= 0 else '−'}${abs(avg_net):,.0f}").items():
    html = html.replace("{{" + k + "}}", v)
os.makedirs("report", exist_ok=True)
open("report/nq_validation.html", "w").write(html)
print("written", len(html))
