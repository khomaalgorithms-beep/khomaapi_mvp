import sys, numpy as np
sys.path.insert(0, "..")
import orb_combo as OC, orb_lab as OL, dashboard_data as DDm
from lucid_sizes import SIZES
A = SIZES["50K"]; g = OL.G("NQ")
for entry in ("touch", "close"):
    OC.SYS = dict(OC.SYS, entry=entry); OC.LV, OC.OR = OC.live_legs(), OC.orb_legs()
    for risk in (400, 600):
        E = OC.build("ORB", risk); row = []
        for lab, x, y in (("long run", "2008-03-01", "2026-10-09"), ("since Jul-25", "2025-07-01", "2026-10-09"), ("since Oct-25", "2025-10-12", "2026-10-09"), ("2024-26", "2024-01-01", "2026-10-09")):
            st = np.where((g.days >= np.datetime64(x).astype(np.int64)) & (g.days <= np.datetime64(y).astype(np.int64)))[0]
            p = np.mean([DDm.eval_run([E.get(i) for i in range(s, min(s + 180, g.D))], A, 1.0, "eod")[0] for s in st])
            row.append(f"{lab} {p*100:3.0f}%")
        res = OL.run("NQ", **{k: v for k, v in OC.SYS.items()})
        s25 = OL.stats(res, ("2025-07-01", "2026-10-31"))
        print(f"{entry:5} ${risk}: " + " | ".join(row) + f" || trades since Jul-25: {s25['n']} PF {s25['pf']:.2f} total {s25['tot']:+.1f}R")
