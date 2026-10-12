"""Which days should the NQ fast-pass logic NOT trade? Pre-registered filter menu + acceptance rule.

Unit: per-trade R of the chain (net / $ risk), so years with different NQ price levels are comparable.
A filter = a set of days to SKIP. ACCEPT only if, in EACH of 2008-19, 2020-23 and 2024-26:
   mean R of skipped trades < 0  AND  < mean R of kept trades,
and the pooled (2008-26) difference kept-minus-skipped has a one-sided p < 0.05 (Welch t),
and at least 20 skipped trades per period. Everything is known before the first entry (10:30), except
calendar events, which are scheduled in advance."""
import os, csv, math, numpy as np
import harness as Hn, stress as T

PER = {"2008-19": (2008, 2019), "2020-23": (2020, 2023), "2024-26": (2024, 2026)}


def trades():
    lg = T.legs({}); g = T.G
    d = np.array(sorted(lg)); R = np.array([lg[x]["net"] / lg[x]["rct"] for x in d])
    e = np.array([lg[x]["e"] for x in d])
    return g, d, R, e


def calendar(g):
    dates = g.dates.astype(object)
    F = {}
    yy = np.array([x.year for x in dates]); mm = np.array([x.month for x in dates]); dd = np.array([x.day for x in dates])
    wd = g.wd
    third_fri = (wd == 4) & (dd >= 15) & (dd <= 21)
    F["monthly OPEX (3rd Fri)"] = third_fri & ~np.isin(mm, [3, 6, 9, 12])
    F["quad witching"] = third_fri & np.isin(mm, [3, 6, 9, 12])
    F["OPEX week (Mon-Fri)"] = np.zeros(g.D, bool)
    for i in np.where(third_fri)[0]:
        F["OPEX week (Mon-Fri)"][max(0, i - 4):i + 1] = True
    # holiday-adjacent: a weekday gap in sessions (holiday) before/after, or a short session today
    gapb = np.zeros(g.D, bool); gapa = np.zeros(g.D, bool)
    dn = g.days
    for i in range(1, g.D):
        cal = dn[i] - dn[i - 1]
        exp = 3 if wd[i] == 0 else 1
        if cal > exp:
            gapb[i] = True; gapa[i - 1] = True
    F["day after a holiday"] = gapb
    F["day before a holiday"] = gapa
    short = g.real[:, Hn.t("13:15"):Hn.t("16:00")].sum(1) < 60
    F["half-day session"] = short
    mo = g.dates.astype("datetime64[M]")
    first = np.r_[True, mo[1:] != mo[:-1]]; last = np.r_[mo[1:] != mo[:-1], True]
    F["first trading day of month"] = first
    F["last trading day of month"] = last
    for k, nm in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]):
        F[f"{nm}"] = wd == k
    return F


def market_state(g):
    F = {}
    a = g.a
    # 08:30 news shock seen in price (known before 10:30): range of 08:30-08:34 vs its 60-day median
    r830 = (g.H[:, Hn.t("08:30"):Hn.t("08:35")].max(1) - g.L[:, Hn.t("08:30"):Hn.t("08:35")].min(1)).astype(float)
    med = np.array([np.median(r830[max(0, i - 60):i]) if i > 20 else np.nan for i in range(g.D)])
    F["08:30 shock > 3x normal"] = r830 > 3 * med
    F["08:30 quiet (< 1x normal)"] = r830 < med
    vp = g.volpct
    F["vol regime: lowest third"] = vp < 1 / 3
    F["vol regime: highest third"] = vp > 2 / 3
    gap = np.abs(g.O[:, a] - g.C[:, a - 1 - 0]) / g.atr14          # 09:29 close -> 09:30 open is tiny; use overnight move
    on = np.abs(g.O[:, a] - g.O[:, 0]) / g.atr14
    F["big overnight move (> 0.5 ATR)"] = on > 0.5
    F["tiny overnight move (< 0.1 ATR)"] = on < 0.1
    dist = np.abs(g.cum_prev - np.r_[[np.nan] * 20, [np.mean(g.cum[i - 20:i]) for i in range(20, g.D)]]) / (g.atr14 / g.px)
    F["choppy: near 20d average (< 1 ATR)"] = dist < 1.0
    big = np.r_[False, (np.abs(g.s_close - g.s_open) > 1.0 * g.atr14)[:-1]]
    F["day after a big trend day"] = big
    F["after inside day"] = g.inside
    F["after NR7 day"] = g.nr7
    return F


def events(g):
    p = os.path.join(Hn.DATA, "events.csv")
    F = {}
    if not os.path.exists(p):
        return F
    ev = {}
    for row in csv.DictReader(open(p)):
        ev.setdefault(row["event"].strip().upper(), set()).add(np.datetime64(row["date"].strip(), "D").astype(np.int64))
    dn = g.days
    def mark(s):
        return np.isin(dn, list(s))
    def shift(m, k):
        out = np.zeros_like(m)
        if k > 0: out[k:] = m[:-k]
        else: out[:k] = m[-k:]
        return out
    fomc = set().union(*(v for k, v in ev.items() if k.startswith("FOMC")))
    F["FOMC day"] = mark(fomc)
    F["day before FOMC"] = shift(mark(fomc), -1)
    F["day after FOMC"] = shift(mark(fomc), 1)
    for k in ("CPI", "NFP", "PPI"):
        if k in ev:
            F[f"{k} day"] = mark(ev[k])
    if all(k in ev for k in ("CPI", "NFP", "PPI")):
        F["any 08:30 tier-1 (CPI/NFP/PPI)"] = mark(ev["CPI"] | ev["NFP"] | ev["PPI"])
        F["any tier-1 incl FOMC"] = F["any 08:30 tier-1 (CPI/NFP/PPI)"] | F["FOMC day"]
    return F


def welch_p(a, b):
    if len(a) < 3 or len(b) < 3:
        return 1.0
    t = (a.mean() - b.mean()) / math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return 1 - 0.5 * (1 + math.erf(t / math.sqrt(2)))


def study():
    g, d, R, e = trades()
    y = g.year[d]
    allF = {}
    for src in (calendar(g), market_state(g), events(g)):
        allF.update(src)
    print(f"{'filter (days to SKIP)':38} " + " | ".join(f"{p:^26}" for p in PER) + " | pooled  verdict")
    print(f"{'':38} " + " | ".join(f"{'n':>4} {'skipR':>7} {'keepR':>7} {'%':>4}" for _ in PER))
    rows = []
    for name, F in allF.items():
        f = F[d].astype(bool)
        ok = True; cells = []
        for p, (y0, y1) in PER.items():
            m = (y >= y0) & (y <= y1)
            s, k = R[m & f], R[m & ~f]
            cells.append(f"{len(s):4d} {s.mean() if len(s) else np.nan:+7.3f} {k.mean():+7.3f} {len(s)/max(m.sum(),1)*100:3.0f}%")
            ok &= len(s) >= 20 and s.mean() < 0 and s.mean() < k.mean()
        pv = welch_p(R[~f], R[f])
        verdict = "SKIP ✔" if ok and pv < 0.05 else ("(weak)" if ok else "keep")
        rows.append((name, ok, pv))
        print(f"{name:38} " + " | ".join(cells) + f" | p={pv:5.3f}  {verdict}")
    return rows


if __name__ == "__main__":
    study()
