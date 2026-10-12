"""Live engine code vs backtest: replay the last N real sessions bar-by-bar through app.orb_live.Autopilot
(fake broker) and compare every day's decision (trade/no trade, side, stop) with the research legs."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from datetime import datetime, timedelta
import harness as Hn, stress as T
from app import orb_momentum as MOM
from app.orb_live import Autopilot, AccountConfig
from app.orb_selective import Bar

LG = T.legs(dict(chain=[("tod_mom", dict(T=60, k=0.0, s=0.2, ref="open", tf=20, trail=None), 1.0)], comm=1.0, slip=1)); g = T.G
class B:
    def __init__(s): s.calls = []
    def place_bracket_order(s, env, tok, spec, acct, action, sym, qty, stop_price=None, limit_price=None):
        s.calls.append((action, qty, stop_price, limit_price)); return {"orderId": 1}
    def get_cash_snapshot(s, *a): return {"totalCashValue": 50000}
N = int(sys.argv[1]) if len(sys.argv) > 1 else 250
match = mism = 0; rows = []
for d in range(g.D - N, g.D):
    if not g.sess_ok[d]:
        continue
    date = str(g.dates[d])
    rng = (g.s_high - g.s_low).astype(float)
    atr = rng[d - 14:d].mean(); trend = int(np.sign(g.cum[d - 1] - g.cum[d - 20:d].mean()))
    ctx = MOM.DayContext(date, float(atr), trend)
    cfg = AccountConfig(env="demo", account_spec="X", account_id=1, account_name="X", instruments={"MNQ": "MNQZ6"},
                        qty={"MNQ": 0}, strategy="momentum", mode="eval", risk_usd=600, max_micros=40,
                        acct_start=50000, acct_target=3000, day_tp_usd=1440)
    br = B(); ap = Autopilot(cfg, get_token=lambda: "t", broker=br, get_context=lambda r, dt: ctx)
    y, mo, dy = map(int, date.split("-"))
    for m in range(Hn.t("09:30"), Hn.t("10:45")):
        hh, mm = divmod(m + 18 * 60, 60); hh %= 24
        ap.on_bar("MNQ", Bar(datetime(y, mo, dy, hh, mm), float(g.O[d, m]), float(g.H[d, m]), float(g.L[d, m]), float(g.C[d, m]), 1))
    live = None
    if br.calls:
        a, q, sl, tp = br.calls[0]; live = ("long" if a == "buy" else "short", round(abs(float(g.C[d, Hn.t('10:29')]) - sl), 1))
    research = None
    if d in LG:
        v = LG[d]; side = "long" if (g.C[d, Hn.t("10:29")] - g.O[d, Hn.t("09:30")]) > 0 else "short"
        research = (side, round(v["rct"] / 2.0, 1))
    same = (live is None and research is None) or (live and research and live[0] == research[0] and abs(live[1] - research[1]) <= 0.5)
    match += bool(same); mism += (not same)
    if not same:
        rows.append((date, live, research))
print(f"days checked: {match + mism} | identical decisions: {match} | mismatches: {mism}")
for r in rows[:15]:
    print("  MISMATCH", r)
