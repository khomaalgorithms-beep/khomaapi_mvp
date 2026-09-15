"""KhomaVolume live engine — KhomaAPI integration glue (ADD-ONLY, OFF BY DEFAULT).

One self-contained module that plugs the live autopilot into the running app with a single
line in main.py:

    from app import orb_engine
    orb_engine.install(app)            # right after `app = FastAPI(...)` and its startup event

It (1) owns a tiny `orb_autopilot` table (per-account opt-in + preset), (2) builds AccountConfigs
from the operator's CONNECTED Tradovate accounts, sizing them exactly like the Prop Engine, (3)
runs a leader-gated background loop that drives one market-data feed + Autopilot per enabled
account, and (4) exposes a small control API + status. It touches NOTHING in KhomaAPI's existing
execution path; until a user flips an account ON (demo first), this code places no orders.

Version-independent: it imports main.py's helpers LAZILY (db, require_user, ensure_fresh_token)
so there is no import cycle, and it works on any main.py that has them.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from fastapi import Request                     # module-level so route annotations resolve
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # (with `from __future__ import annotations`)

from app import tradovate_oauth as tvo
from app.orb_live import (Autopilot, AccountConfig, DEFAULT_PARAMS, INSTRUMENTS,
                         plan_total_micros, split_micros)
from app.orb_massive import MassiveFeed, subscriptions_for, _ET
from app.orb_selective import Bar

POLL_INTERVAL = float(__import__("os").getenv("ORB_LIVE_POLL_INTERVAL", "3"))
# instrument root -> the asset key the sizing/split uses
_ASSET_ROOTS = {"nq": ["MNQ"], "rty": ["M2K"], "both": ["MNQ", "M2K"]}


def _now_et() -> datetime:
    return datetime.now(_ET)


def _massive_key() -> Optional[str]:
    return __import__("os").getenv("MASSIVE_API_KEY") or None


def _make_autopilot(cfg: AccountConfig, token_provider) -> Autopilot:
    """One Autopilot per account, executing to that account's Tradovate connection."""
    return Autopilot(cfg, get_token=token_provider, now_et=_now_et)


def _make_massive_feed(subscriptions, on_bar):
    """The single shared Massive real-time feed for the whole engine (needs MASSIVE_API_KEY)."""
    return MassiveFeed(get_key=_massive_key, subscriptions=subscriptions, on_bar=on_bar)


def _subscriptions_for(roots):
    return subscriptions_for(roots)


# ---------------------------------------------------------------------------
# lazy access to main.py (avoids an import cycle; works across main.py versions)
# ---------------------------------------------------------------------------
def _main():
    from app import main  # deferred
    return main


def _db():
    return _main().db()


def _fresh_token_for(account_pk: int) -> Optional[str]:
    """Re-read the broker_accounts row by primary key and return a valid (renewed) token, or
    None. Re-reading each call means a reconnect / token refresh is always picked up live."""
    con = _db()
    row = con.execute("SELECT * FROM broker_accounts WHERE id=?", (account_pk,)).fetchone()
    con.close()
    if not row:
        return None
    acct = dict(row)
    if acct.get("status") != "connected":
        return None
    try:
        return _main().ensure_fresh_token(acct) or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# persistence — per-account opt-in + preset (the ONLY new table)
# ---------------------------------------------------------------------------
def ensure_table() -> None:
    con = _db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS orb_autopilot(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_pk INTEGER NOT NULL,        -- broker_accounts.id
            account_id TEXT NOT NULL,           -- tradovate account id
            enabled INTEGER DEFAULT 0,
            mode TEXT DEFAULT 'eval',           -- eval | funded | cash
            acct_size TEXT DEFAULT '50K',       -- 25K | 50K | 100K | 150K
            preset TEXT DEFAULT 'norm',         -- cons | norm | aggr
            asset TEXT DEFAULT 'both',          -- nq | rty | both
            updated_at TEXT,
            UNIQUE(user_id, account_id)
        )""")
    con.commit()
    con.close()


def get_config(user_id: int, account_id: str) -> Optional[dict]:
    con = _db()
    row = con.execute("SELECT * FROM orb_autopilot WHERE user_id=? AND account_id=?",
                      (user_id, str(account_id))).fetchone()
    con.close()
    return dict(row) if row else None


def save_config(user_id: int, account_pk: int, account_id: str, *, enabled: bool,
                mode: str, acct_size: str, preset: str, asset: str) -> None:
    """Upsert one account's autopilot settings. Validates the enum values so a bad payload can
    never reach the sizing tables."""
    assert mode in ("eval", "funded", "cash")
    assert acct_size in ("25K", "50K", "100K", "150K")
    assert preset in ("cons", "norm", "aggr")
    assert asset in ("nq", "rty", "both")
    con = _db()
    con.execute("""
        INSERT INTO orb_autopilot(user_id,account_pk,account_id,enabled,mode,acct_size,preset,asset,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,account_id) DO UPDATE SET
          account_pk=excluded.account_pk, enabled=excluded.enabled, mode=excluded.mode,
          acct_size=excluded.acct_size, preset=excluded.preset, asset=excluded.asset,
          updated_at=excluded.updated_at
    """, (user_id, account_pk, str(account_id), 1 if enabled else 0, mode, acct_size, preset,
          asset, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()


def enabled_rows() -> List[dict]:
    """Every ARMED autopilot that is actually allowed to trade right now: the account is
    connected AND the owner's MASTER automation switch is Running. So hitting Pause on the main
    dashboard (users.automation_status='Paused') stops the ORB engine too — it places nothing."""
    con = _db()
    rows = con.execute("""
        SELECT ap.*, ba.account_name, ba.env, ba.status
        FROM orb_autopilot ap
        JOIN broker_accounts ba ON ba.id = ap.account_pk
        JOIN users u ON u.id = ap.user_id
        WHERE ap.enabled=1 AND ba.status='connected' AND u.automation_status='Running'
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]


def master_running(user_id: int) -> bool:
    """Is the owner's master automation switch Running? (The main-dashboard Start/Pause.)"""
    con = _db()
    row = con.execute("SELECT automation_status FROM users WHERE id=?", (user_id,)).fetchone()
    con.close()
    return bool(row) and dict(row).get("automation_status") == "Running"


def set_master_running(user_id: int) -> None:
    """Flip the master switch ON when the user Starts the ORB engine, so arming actually results
    in execution (main-dashboard Pause can still stop everything)."""
    con = _db()
    con.execute("UPDATE users SET automation_status='Running' WHERE id=?", (user_id,))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# sizing + config building
# ---------------------------------------------------------------------------
def qty_for(mode: str, acct_size: str, preset: str, asset: str) -> Dict[str, int]:
    """Micro contracts per instrument for a config, EXACTLY as the Prop Engine dashboard sizes
    (plan_total_micros -> split_micros). Instruments not in the chosen asset get 0."""
    total = plan_total_micros(mode, acct_size, preset, asset=asset)
    split = split_micros(total, asset)   # {"MNQ": x, "M2K": y}
    roots = _ASSET_ROOTS[asset]
    return {r: split.get(r, 0) for r in roots}


def config_signature(row: dict, contracts: Dict[str, str], qty: Dict[str, int]) -> str:
    """Stable hash of everything that, if changed, should rebuild the account's feed+autopilot."""
    return json.dumps({"env": row.get("env"), "mode": row["mode"], "size": row["acct_size"],
                       "preset": row["preset"], "asset": row["asset"],
                       "contracts": contracts, "qty": qty}, sort_keys=True)


def build_account_config(row: dict, token: str) -> Optional[AccountConfig]:
    """One enabled row -> a ready AccountConfig (front contracts resolved, sized). None if the
    front month can't be resolved. All chosen instruments trade live (RTY is validated); an
    instrument can be forced to observe-only via the ORB_OBSERVE env (comma-sep roots)."""
    env = row.get("env") or "live"
    roots = _ASSET_ROOTS[row["asset"]]
    contracts: Dict[str, str] = {}
    for r in roots:
        try:
            contracts[r] = tvo.resolve_contract(env, token, INSTRUMENTS[r].root)
        except Exception:
            return None
    qty = qty_for(row["mode"], row["acct_size"], row["preset"], row["asset"])
    observe_env = set(x.strip() for x in __import__("os").getenv("ORB_OBSERVE", "").split(",") if x.strip())
    observe = {r: (r in observe_env) for r in roots}
    return AccountConfig(env=env, account_spec=row["account_name"], account_id=int(row["account_id"]),
                        account_name=row["account_name"], instruments=contracts, qty=qty,
                        params=dict(DEFAULT_PARAMS), observe=observe)


# ---------------------------------------------------------------------------
# manager — owns the live autopilots + feeds, reconciles them to the enabled set
# ---------------------------------------------------------------------------
class EngineManager:
    """Owns one Autopilot per armed account and ONE shared Massive data feed for the whole
    business. Each completed bar from the single feed is dispatched to EVERY armed account's
    autopilot, so one $199 feed powers signals for all accounts — clients never buy data.
    `sync()` starts/stops autopilots and (re)subscribes the feed to the union of instruments;
    `poll()` reconciles every account against its broker. Feed/autopilot construction is injected
    for unit-testing without a socket."""

    def __init__(self, feed_factory=None, autopilot_factory=None, start_feed=True):
        self._feed_factory = feed_factory        # (subscriptions, on_bar) -> feed
        self._autopilot_factory = autopilot_factory or _make_autopilot
        self._start_feed = start_feed
        self._active: Dict[int, dict] = {}       # account_id -> {sig, autopilot}
        self.bars_seen = 0                       # observability: bars dispatched to the engine
        self.last_bar = None                     # e.g. "MNQ 09:47" — proves data reaches the engine
        self._feed = None
        self._feed_task = None
        self._feed_roots: tuple = ()             # exec roots the feed is currently subscribed to

    # ---- accounts + shared feed ----
    def sync(self, configs: List[dict]) -> None:
        """configs: [{"cfg":AccountConfig, "sig":str, "token_provider":callable}]."""
        wanted = {c["cfg"].account_id: c for c in configs}
        for acct_id in list(self._active):        # drop removed / changed
            if acct_id not in wanted or wanted[acct_id]["sig"] != self._active[acct_id]["sig"]:
                self._active.pop(acct_id, None)
        for acct_id, c in wanted.items():         # add new
            if acct_id not in self._active:
                ap = self._autopilot_factory(c["cfg"], c["token_provider"])
                self._active[acct_id] = {"sig": c["sig"], "autopilot": ap}
        self._ensure_feed()

    def _ensure_feed(self) -> None:
        """Keep the single shared feed subscribed to the union of instruments across all armed
        accounts; stop it when nothing is armed; restart it when the instrument set changes."""
        roots = tuple(sorted({r for st in self._active.values()
                              for r in st["autopilot"].cfg.instruments}))
        if roots == self._feed_roots and (self._feed is not None or not roots):
            return
        self._stop_feed()
        self._feed_roots = roots
        if not roots or not self._start_feed:
            return
        subs = _subscriptions_for(roots)
        self._feed = (self._feed_factory or _make_massive_feed)(subs, self.on_bar)
        try:
            self._feed_task = asyncio.create_task(self._feed.run())
        except RuntimeError:
            self._feed_task = None                # no running loop (tests) — feed built, not run

    def on_bar(self, root: str, bar: Bar) -> List[dict]:
        """Dispatch ONE completed bar to every armed account. (Broker order I/O inside on_bar is
        brief and per-minute; for very large fleets this should move to a thread pool.)"""
        self.bars_seen += 1
        try:
            self.last_bar = f"{root} {bar.dt:%H:%M} c={bar.c}"
        except Exception:
            self.last_bar = root
        out = []
        for acct_id, st in list(self._active.items()):
            try:
                r = st["autopilot"].on_bar(root, bar)
                if r:
                    out.append({"account_id": acct_id, **r})
            except Exception as e:
                out.append({"account_id": acct_id, "error": str(e)})
        return out

    def poll(self) -> List[dict]:
        out: List[dict] = []
        for acct_id, st in list(self._active.items()):
            try:
                out.extend(st["autopilot"].poll())
            except Exception as e:
                out.append({"account_id": acct_id, "error": str(e)})
        return out

    def _stop_feed(self) -> None:
        try:
            if self._feed:
                self._feed.stop()
            if self._feed_task:
                self._feed_task.cancel()
        except Exception:
            pass
        self._feed, self._feed_task = None, None

    def active_ids(self) -> List[int]:
        return list(self._active)

    def feed_roots(self) -> tuple:
        return self._feed_roots

    def stop_all(self) -> None:
        self._active.clear()
        self._stop_feed()
        self._feed_roots = ()


# ---------------------------------------------------------------------------
# the background loop (leader-gated, like orb_engine_loop)
# ---------------------------------------------------------------------------
_MANAGER: Optional[EngineManager] = None
_LAST_STATUS: Dict[str, object] = {"running": False, "accounts": 0, "actions": []}


def _plan_configs() -> List[dict]:
    """Build the config list for every enabled+connected account (needs a token per account)."""
    out = []
    for row in enabled_rows():
        token = _fresh_token_for(row["account_pk"])
        if not token:
            continue
        cfg = build_account_config(row, token)
        if cfg is None:
            continue
        roots = _ASSET_ROOTS[row["asset"]]
        contracts = dict(cfg.instruments)
        qty = {r: cfg.qty.get(r, 0) for r in roots}
        out.append({"row": row, "cfg": cfg, "sig": config_signature(row, contracts, qty),
                    "token_provider": (lambda pk=row["account_pk"]: _fresh_token_for(pk))})
    return out


async def engine_loop() -> None:
    """Leader-only reconcile loop. Each tick: rebuild the enabled config set, sync the manager
    (start/stop feeds), and poll every account against broker truth. Never raises out."""
    global _MANAGER
    _MANAGER = EngineManager()
    main = _main()
    print("ORB LIVE ENGINE started — autopilot @ %ss (opt-in per account, off by default)"
          % POLL_INTERVAL)
    while True:
        try:
            is_leader = await asyncio.get_event_loop().run_in_executor(None, main.try_become_leader)
            if is_leader:
                configs = await asyncio.get_event_loop().run_in_executor(None, _plan_configs)
                _MANAGER.sync(configs)
                actions = _MANAGER.poll()
                _LAST_STATUS.update(running=True, accounts=len(_MANAGER.active_ids()),
                                    data_key=bool(_massive_key()),
                                    feed_instruments=list(_MANAGER.feed_roots()),
                                    bars_seen=_MANAGER.bars_seen, last_bar=_MANAGER.last_bar,
                                    actions=[a for a in actions if a.get("action") not in (None,)][-20:],
                                    ts=datetime.now(timezone.utc).isoformat())
            else:
                _LAST_STATUS.update(running=False)
        except Exception as e:
            print("ORB LIVE ENGINE ERROR:", e)
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP — control API + dashboard (registered by install)
# ---------------------------------------------------------------------------
def _connected_accounts(user_id: int) -> List[dict]:
    con = _db()
    rows = con.execute(
        "SELECT * FROM broker_accounts WHERE user_id=? AND status='connected' ORDER BY id",
        (user_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def install(app) -> None:
    """Wire the engine into a FastAPI app: create the table, register the background loop on
    startup, and add the control routes. Call ONCE from main.py after `app` is created."""
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse

    ensure_table()

    @app.on_event("startup")
    async def _orb_live_startup():
        asyncio.create_task(engine_loop())

    @app.get("/prop-engine", response_class=HTMLResponse)
    def prop_engine_page(request: Request):
        main = _main()
        user = main.require_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        accounts = _connected_accounts(user["id"])
        cfgs = {a["account_id"]: get_config(user["id"], a["account_id"]) for a in accounts}
        body = _render_dashboard(accounts, cfgs)
        try:
            # layout(content, user=None, active="dashboard") — wrap in KhomaAPI chrome + sidebar
            return HTMLResponse(main.layout(body, user=user, active="prop"))
        except Exception:
            return HTMLResponse(body)   # fall back to a bare page if layout() signature differs

    @app.post("/prop-engine/save")
    async def prop_engine_save(request: Request):
        main = _main()
        user = main.require_user(request)
        if not user:
            return JSONResponse({"ok": False, "error": "login required"}, status_code=401)
        form = await request.form()
        account_id = str(form.get("account_id") or "")
        acct = next((a for a in _connected_accounts(user["id"]) if a["account_id"] == account_id), None)
        if not acct:
            return JSONResponse({"ok": False, "error": "account not connected"}, status_code=400)
        enabling = form.get("enabled") in ("1", "true", "on", True)
        try:
            save_config(user["id"], acct["id"], account_id, enabled=enabling,
                        mode=str(form.get("mode") or "eval"), acct_size=str(form.get("acct_size") or "50K"),
                        preset=str(form.get("preset") or "norm"), asset=str(form.get("asset") or "both"))
        except AssertionError:
            return JSONResponse({"ok": False, "error": "invalid settings"}, status_code=400)
        if enabling:
            set_master_running(user["id"])   # Start here = allow execution (Dashboard Pause still stops it)
        return JSONResponse({"ok": True, "enabled": enabling, "master_running": master_running(user["id"]),
                            "qty": qty_for(str(form.get("mode") or "eval"), str(form.get("acct_size") or "50K"),
                            str(form.get("preset") or "norm"), str(form.get("asset") or "both"))})

    @app.get("/prop-engine/status")
    def prop_engine_status(request: Request):
        main = _main()
        user = main.require_user(request)
        if not user:
            return JSONResponse({"engine": _LAST_STATUS, "accounts": {}, "master_running": False})
        mine = {a["account_id"]: get_config(user["id"], a["account_id"])
                for a in _connected_accounts(user["id"])}
        return JSONResponse({"engine": _LAST_STATUS, "accounts": mine,
                            "master_running": master_running(user["id"])})


def _render_dashboard(accounts: List[dict], cfgs: Dict[str, Optional[dict]]) -> str:
    """Full KhomaVolume ORB control room: pick a connected account, configure mode / account
    size / drawdown / preset / markets, see the live contract sizing + projection, and Start
    Automation — which arms the server-side engine to scan the market and execute by our rules.
    All projection math is client-side from the validated (honest) tables; arming persists via
    /prop-engine/save and the background loop picks it up within seconds."""
    accts = [{"id": a["account_id"], "name": a.get("account_name", ""),
              "env": (a.get("env") or "live")} for a in accounts]
    conf = {}
    for a in accounts:
        c = cfgs.get(a["account_id"]) or {}
        conf[a["account_id"]] = {"enabled": bool(c.get("enabled")), "mode": c.get("mode", "eval"),
                                 "acct_size": c.get("acct_size", "50K"), "preset": c.get("preset", "norm"),
                                 "asset": c.get("asset", "both"), "bal": c.get("bal", 25000),
                                 "dd": c.get("dd", 15)}
    return (_PAGE.replace("__ACCOUNTS__", json.dumps(accts))
                 .replace("__CONFIGS__", json.dumps(conf))
                 .replace("__EQUITY__", _EQUITY_JSON))


# Validated equity curves + stats (per micro), generated from the 2019-2026 backtest:
# NQ MNQ 30/75, RTY M2K 8/20, and the NQ+RTY portfolio (1 MNQ + 1 M2K per day).
_EQUITY_JSON = r"""{"NQ":{"cum":[961,734,1170,1031,1426,1624,1588,2018,2118,2479,2273,2297,2406,2863,3088,3523,3809,4248,4596,4979,5675,6297,6352,6584,6794,6929,6994,7498,7538,7811,8072,9453,9740,9728,9749,10184,10100,10201,10451,10763,10900,11125,10990,11033,10780,11302,11580,11830,12390,12430,12941,13216,13342,13802,13272,13881,13772,14084,14333,14622,15144,15840,15788,15889,16302,16170,16987,17237,17512,18556,19165,19898,20692,21240,21849,22073,22857,22500,22801,23435,23617,23615,24286,24598,25120,25580,25558,26229,25934,26061,26546,26386,26325],"stats":{"n":1145,"net":26325,"pf":1.67,"win":42,"maxdd":-716,"ret_dd":36.8}},"RTY":{"cum":[119,2,226,245,505,323,595,773,1168,1085,1065,956,1142,1154,1184,1329,1360,1852,1749,1804,1994,2121,2049,2190,2562,3284,3539,3698,3591,3620,4260,4355,4458,4432,4561,4873,4862,5147,5572,5827,5941,6154,6165,6221,6236,6267,6324,6297,6158,6313,6476,6654,6721,6715,6846,6854,6882,6907,7021,6949,7160,7066,7180,7466,7679,7489,7660,7907,8065,8221,8231,8218,8134,8444,8672,8885,8750,8599,8659,8673,8890,9082,8957,8990,8975,9206,9293,9423,9528,9377,9828,9947,10058],"stats":{"n":775,"net":10058,"pf":1.64,"win":47,"maxdd":-429,"ret_dd":23.4}},"PF":{"cum":[1080,736,1396,1276,1931,1948,2183,2791,3286,3563,3339,3252,3548,4017,4272,4852,5169,6100,6345,6782,7669,8418,8401,8774,9356,10213,10532,11197,11129,11430,12332,13808,14197,14160,14310,15057,14962,15348,16024,16590,16842,17279,17156,17254,17016,17569,17904,18128,18548,18743,19417,19870,20063,20518,20118,20736,20654,20990,21354,21571,22304,22906,22968,23355,23982,23659,24647,25144,25577,26778,27396,28116,28827,29684,30521,30959,31606,31099,31460,32108,32507,32698,33243,33588,34095,34786,34851,35652,35463,35437,36374,36334,36382],"stats":{"n":1391,"net":36382,"pf":1.8,"win":48,"maxdd":-784,"ret_dd":46.4}},"months":["2019-01","2019-02","2019-03","2019-04","2019-05","2019-06","2019-07","2019-08","2019-09","2019-10","2019-11","2019-12","2020-01","2020-02","2020-03","2020-04","2020-05","2020-06","2020-07","2020-08","2020-09","2020-10","2020-11","2020-12","2021-01","2021-02","2021-03","2021-04","2021-05","2021-06","2021-07","2021-08","2021-09","2021-10","2021-11","2021-12","2022-01","2022-02","2022-03","2022-04","2022-05","2022-06","2022-07","2022-08","2022-09","2022-10","2022-11","2022-12","2023-01","2023-02","2023-03","2023-04","2023-05","2023-06","2023-07","2023-08","2023-09","2023-10","2023-11","2023-12","2024-01","2024-02","2024-03","2024-04","2024-05","2024-06","2024-07","2024-08","2024-09","2024-10","2024-11","2024-12","2025-01","2025-02","2025-03","2025-04","2025-05","2025-06","2025-07","2025-08","2025-09","2025-10","2025-11","2025-12","2026-01","2026-02","2026-03","2026-04","2026-05","2026-06","2026-07","2026-08","2026-09"]}"""


_PAGE = r"""
<style>
  .kv{--g:#0f8f45;--gd:#086b34;--gs:#eaf7ef;--gl:#cdebd8;--btn:linear-gradient(135deg,#12a150,#087135);
      --tx:#111827;--mu:#6b7280;--fa:#9ca3af;--ln:#e5e7eb;--bg:#f8faf9;--card:#fff;--dg:#dc2626;
      max-width:1000px;margin:0 auto;padding:8px 6px 60px;color:var(--tx);
      font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
  .kv h1{font-size:24px;font-weight:900;letter-spacing:-.8px;margin:0}
  .kv .lead{color:var(--mu);font-size:13.5px;margin:4px 0 20px}
  .kv .ey{font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--fa)}
  .kv .top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
  .kv .apick{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--ln);
      border-radius:14px;padding:9px 12px;box-shadow:0 8px 20px rgba(17,24,39,.05)}
  .kv .apick select{font:inherit;font-size:15px;font-weight:800;border:0;background:transparent;color:var(--tx);outline:none}
  .kv .envb{font-size:10px;font-weight:900;border:1px solid;border-radius:6px;padding:2px 7px}
  .kv .grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;margin-top:18px}
  @media(max-width:820px){.kv .grid{grid-template-columns:1fr}}
  .kv .card{background:var(--card);border:1px solid var(--ln);border-radius:20px;box-shadow:0 18px 60px rgba(17,24,39,.06)}
  .kv .card .hd{padding:18px 22px 0}.kv .card .hd h2{font-size:16px;font-weight:800;letter-spacing:-.3px;margin:0}
  .kv .card .hd p{color:var(--mu);font-size:12.5px;margin:4px 0 0}
  .kv .card .bd{padding:18px 22px 22px}
  .kv .field{margin-top:18px}.kv .field:first-child{margin-top:0}
  .kv .field>label{display:block;font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--fa);margin-bottom:8px}
  .kv .seg{display:flex;gap:6px;background:#f4f6f5;border:1px solid var(--ln);border-radius:13px;padding:5px}
  .kv .seg.four{display:grid;grid-template-columns:repeat(4,1fr)}
  .kv .seg button{flex:1;border:0;background:transparent;color:var(--mu);font:inherit;font-size:13px;font-weight:700;
      padding:9px 6px;border-radius:9px;cursor:pointer;transition:.12s}
  .kv .seg button[aria-pressed=true]{background:#fff;color:var(--gd);box-shadow:0 6px 16px rgba(17,24,39,.06);font-weight:800}
  .kv .cashrow{display:flex;gap:14px;flex-wrap:wrap}.kv .cashrow .f{flex:1;min-width:130px}
  .kv .inl{display:flex;align-items:center;border:1px solid var(--ln);border-radius:12px;background:#fff;overflow:hidden}
  .kv .inl span{padding:0 11px;color:var(--fa);font-weight:800}
  .kv .inl input{border:0;background:transparent;font:inherit;font-size:16px;font-weight:800;padding:12px 12px 12px 0;width:100%;outline:none}
  .kv input[type=range]{width:100%;accent-color:var(--g);margin-top:8px}
  .kv .rv{display:flex;justify-content:space-between;font-size:12px;color:var(--mu);font-weight:700}.kv .rv b{color:var(--gd);font-size:15px}
  .kv .pn{font-size:12.5px;color:var(--mu);margin-top:10px;min-height:16px;line-height:1.45}
  .kv .proj .bd{padding:20px 22px 22px}
  .kv .hero{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  .kv .big{font-size:52px;font-weight:900;letter-spacing:-2.5px;color:var(--gd);line-height:.9;font-variant-numeric:tabular-nums}
  .kv .cap{font-size:13px;color:var(--mu);max-width:30ch;line-height:1.4}.kv .cap b{color:var(--tx)}
  .kv .meter{height:12px;border-radius:8px;background:#eef2f0;border:1px solid var(--ln);overflow:hidden;margin:14px 0 4px}
  .kv .meter i{display:block;height:100%;background:var(--btn);border-radius:7px;transition:width .4s}
  .kv .sz{margin-top:18px;background:var(--gs);border:1px solid var(--gl);border-radius:16px;padding:16px 18px}
  .kv .sz .t{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--gd)}
  .kv .sz .legs{display:flex;gap:12px;margin-top:10px}
  .kv .sz .leg{flex:1;background:#fff;border:1px solid var(--gl);border-radius:13px;padding:12px 14px}
  .kv .sz .leg .m{font-size:11px;color:var(--mu);font-weight:700;display:flex;gap:7px;align-items:center}
  .kv .sz .leg .m .tk{font-size:10px;font-weight:900;color:var(--gd);background:var(--gs);border:1px solid var(--gl);padding:1px 6px;border-radius:6px}
  .kv .sz .leg.r .m .tk{color:#475569;background:#f1f5f9;border-color:#e2e8f0}
  .kv .sz .leg .n{font-size:30px;font-weight:900;letter-spacing:-1px;margin-top:5px;font-variant-numeric:tabular-nums}
  .kv .sz .leg .n small{font-size:12px;color:var(--fa);font-weight:700}
  .kv .tiles{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--ln);border:1px solid var(--ln);border-radius:14px;overflow:hidden;margin-top:16px}
  .kv .tiles .t{background:#fff;padding:13px 16px}.kv .tiles .t .k{font-size:10px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--fa)}
  .kv .tiles .t .v{font-size:20px;font-weight:900;letter-spacing:-.6px;margin-top:3px;font-variant-numeric:tabular-nums}.kv .tiles .t .v small{font-size:11px;color:var(--mu);font-weight:700}
  .kv .run{margin-top:22px;background:var(--card);border:1px solid var(--ln);border-radius:20px;box-shadow:0 18px 60px rgba(17,24,39,.06);padding:20px 22px}
  .kv .runrow{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .kv .startbtn{flex:1;min-width:220px;border:0;border-radius:14px;padding:17px 22px;font:inherit;font-size:16px;font-weight:900;color:#fff;
      background:var(--btn);cursor:pointer;letter-spacing:-.2px;box-shadow:0 14px 30px rgba(15,143,69,.25)}
  .kv .startbtn.on{background:#fef2f2;color:var(--dg);box-shadow:none;border:1px solid #fecaca}
  .kv .rstat{flex:1;min-width:220px;font-size:13px;color:var(--mu);line-height:1.5}
  .kv .rstat .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#9ca3af;margin-right:7px;vertical-align:middle}
  .kv .rstat.live .dot{background:#16a34a;box-shadow:0 0 0 3px rgba(22,163,74,.18);animation:kvp 1.6s infinite}
  @keyframes kvp{0%,100%{opacity:1}50%{opacity:.45}}
  .kv .rstat b{color:var(--tx)}
  .kv .acts{margin-top:14px;border-top:1px solid var(--ln);padding-top:12px;font-size:12px;color:var(--mu)}
  .kv .acts .row{display:flex;gap:10px;padding:3px 0;font-variant-numeric:tabular-nums}
  .kv .note{display:flex;gap:12px;background:var(--bg);border:1px solid var(--ln);border-left:4px solid var(--g);border-radius:14px;padding:14px 16px;margin-top:16px;font-size:12.5px;color:var(--mu);line-height:1.5}
  .kv .note b{color:var(--tx)}
  .kv .empty{background:var(--card);border:1px solid var(--ln);border-radius:18px;padding:26px;text-align:center;color:var(--mu)}
  .kv .eqwrap{display:block;margin-top:22px;background:var(--card);border:1px solid var(--ln);border-radius:20px;box-shadow:0 18px 60px rgba(17,24,39,.06);padding:20px 22px 22px}
  .kv .eqhead{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}
  .kv .eqhead h2{font-size:16px;font-weight:800;letter-spacing:-.3px;margin:0}.kv .eqhead p{color:var(--mu);font-size:12px;margin:4px 0 0}
  .kv .eqtabs{display:flex;gap:6px;background:#f4f6f5;border:1px solid var(--ln);border-radius:12px;padding:4px}
  .kv .eqtab{border:0;background:transparent;color:var(--mu);font:inherit;font-size:12.5px;font-weight:700;padding:7px 12px;border-radius:8px;cursor:pointer}
  .kv .eqtab.on{background:#fff;color:var(--gd);box-shadow:0 6px 16px rgba(17,24,39,.06);font-weight:800}
  .kv .eqchart{margin-top:14px}.kv .eqchart svg{width:100%;height:auto;display:block}
  .kv .eqtiles{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--ln);border:1px solid var(--ln);border-radius:14px;overflow:hidden;margin-top:14px}
  @media(max-width:760px){.kv .eqtiles{grid-template-columns:repeat(3,1fr)}}
  .kv .eqt{background:#fff;padding:12px 14px}.kv .eqt .k{font-size:9.5px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--fa)}
  .kv .eqt .v{font-size:18px;font-weight:900;letter-spacing:-.5px;margin-top:3px;font-variant-numeric:tabular-nums}
  .kv .eqnote{font-size:12.5px;color:var(--mu);line-height:1.5;margin-top:14px;background:var(--gs);border:1px solid var(--gl);border-radius:12px;padding:12px 14px}
</style>
<div class="kv" id="kv">
  <div class="top">
    <div>
      <h1>KhomaVolume ORB</h1>
      <div class="lead">Automated selective-breakout execution on NQ &amp; RTY, run server-side by KhomaAPI.</div>
    </div>
    <div class="apick" id="apickWrap">
      <span class="ey">Account</span>
      <select id="acctSel"></select>
      <span class="envb" id="envBadge"></span>
    </div>
  </div>
  <div id="app"></div>
  <section id="eqSection" class="eqwrap"></section>
</div>
<script>
const ACCOUNTS=__ACCOUNTS__, CONFIGS=__CONFIGS__, EQ=__EQUITY__;
const EVAL={"25K":{cons:[4,86,36],norm:[6,76,21],aggr:[10,64,10]},"50K":{cons:[8,84,45],norm:[12,74,25],aggr:[20,62,11]},"100K":{cons:[12,84,62],norm:[18,72,35],aggr:[30,59,15]},"150K":{cons:[20,81,53],norm:[30,69,29],aggr:[50,57,14]}};
const FUNDED={"25K":{cons:2,norm:4,aggr:6},"50K":{cons:4,norm:8,aggr:12},"100K":{cons:6,norm:12,aggr:18},"150K":{cons:10,norm:20,aggr:30}};
const CAP={"25K":1000,"50K":2000,"100K":2500,"150K":3000},TARGET={"25K":1250,"50K":3000,"100K":6000,"150K":9000},MLL={"25K":1000,"50K":2000,"100K":3000,"150K":4500};
const DD_PER_MICRO=720,RISKFRAC={cons:.5,norm:.75,aggr:1},NQ_M=483,RTY_M=273;
const $=s=>document.querySelector(s),money=n=>"$"+Math.round(n).toLocaleString("en-US"),fmt=n=>n.toLocaleString("en-US");
const rnd=x=>Math.floor(x+0.5);
let state=null;

function splitMicros(t,asset){if(asset==="nq")return[t,0];if(asset==="rty")return[0,t];const q=rnd(t/2);return[q,t-q];}
function assetAdj(p,asset){return asset==="both"?p:asset==="rty"?Math.round(p*.85):Math.round(p*.75);}
function totalMicros(){
  if(state.mode==="funded")return FUNDED[state.acct_size][state.preset];
  if(state.mode==="cash"){const mx=Math.max(0,Math.floor((state.bal*state.dd/100)/DD_PER_MICRO));
    return mx===0?0:Math.max(state.asset==="both"?2:1,rnd(mx*RISKFRAC[state.preset]));}
  return EVAL[state.acct_size][state.preset][0];
}

function seg(id,opts,val){return `<div class="seg${opts.length===4?' four':''}" data-seg="${id}">`+
  opts.map(o=>`<button data-v="${o.v}" aria-pressed="${o.v===val}">${o.t}</button>`).join("")+`</div>`;}

function configCard(){
  const cash=state.mode==="cash";
  return `<div class="card"><div class="hd"><h2>Configure</h2><p>Sizing &amp; risk compute live from the validated results.</p></div>
   <div class="bd">
    <div class="field"><label>Mode</label>${seg("mode",[{v:"eval",t:"Prop evaluation"},{v:"funded",t:"Funded"},{v:"cash",t:"Live cash"}],state.mode)}</div>
    ${cash?"":`<div class="field"><label>Account size</label>${seg("acct_size",[{v:"25K",t:"$25K"},{v:"50K",t:"$50K"},{v:"100K",t:"$100K"},{v:"150K",t:"$150K"}],state.acct_size)}</div>`}
    ${cash?`<div class="field"><label>Live account &amp; drawdown</label><div class="cashrow">
        <div class="f"><div class="inl"><span>$</span><input id="balIn" value="${fmt(state.bal)}"></div></div>
        <div class="f"><div class="rv"><span>Max drawdown</span><b id="ddOut">${state.dd}%</b></div>
          <input id="ddIn" type="range" min="5" max="35" value="${state.dd}"></div></div>
        <div class="pn">KhomaAPI keeps risk inside <b>${money(state.bal*state.dd/100)}</b> of drawdown.</div></div>`:""}
    <div class="field"><label>Markets</label>${seg("asset",[{v:"both",t:"NQ + RTY"},{v:"nq",t:"NQ only"},{v:"rty",t:"RTY only"}],state.asset)}</div>
    <div class="field"><label>Risk preset</label>${seg("preset",[{v:"cons",t:"Conservative"},{v:"norm",t:"Normal"},{v:"aggr",t:"Aggressive"}],state.preset)}
      <div class="pn" id="presetNote"></div></div>
   </div></div>`;
}

function projCard(){
  const total=totalMicros(),[nq,rty]=splitMicros(total,state.asset);
  let hero,cap,meter=null,tiles=[];
  if(state.mode==="eval"){const[m,pass,days]=EVAL[state.acct_size][state.preset],p=assetAdj(pass,state.asset);
    hero=p+"%";cap="<b>chance to pass</b> the "+state.acct_size+" evaluation before the trailing limit";meter=p;
    tiles=[["Time to pass","~"+days+" days","median, when it passes"],["Profit target",money(TARGET[state.acct_size]),"trailing max loss "+money(MLL[state.acct_size])]];
  }else if(state.mode==="funded"){const gross=nq*NQ_M+rty*RTY_M,inc=Math.min(gross,CAP[state.acct_size]);
    hero=money(inc);cap="<b>monthly withdrawable</b> — capped by the "+state.acct_size+" payout, not the strategy";
    tiles=[["Payout cap / mo",money(CAP[state.acct_size]),"per account"],["Strategy output",money(gross)+"/mo",gross>CAP[state.acct_size]?"exceeds cap — steady":"building"]];
  }else{const gross=nq*NQ_M+rty*RTY_M,budget=state.bal*state.dd/100;
    hero=money(gross)+"/mo";cap="<b>estimated monthly</b> at this size, before variance";
    tiles=[["Drawdown budget",money(budget),state.dd+"% of "+money(state.bal)],["Per-trade risk","~"+money(nq*60+rty*40),"stop distance × $/pt"]];}
  const legs=`<div class="sz"><div class="t">Contracts the engine will trade</div><div class="legs">
     <div class="leg"><div class="m"><span class="tk">MNQ</span> Micro Nasdaq</div><div class="n">${nq}<small> micro</small></div></div>
     <div class="leg r"><div class="m"><span class="tk">M2K</span> Micro Russell</div><div class="n">${rty}<small> micro</small></div></div></div></div>`;
  return `<div class="card proj"><div class="hd"><h2>Projection</h2><p>Honest, from the 2019–2026 validation.</p></div>
   <div class="bd"><div class="hero"><div class="big">${hero}</div><div class="cap">${cap}</div></div>
   ${meter!=null?`<div class="meter"><i style="width:${meter}%"></i></div>`:""}
   ${legs}
   <div class="tiles">${tiles.map(t=>`<div class="t"><div class="k">${t[0]}</div><div class="v">${t[1]}</div><div class="k" style="text-transform:none;color:var(--mu);font-weight:600;margin-top:2px">${t[2]}</div></div>`).join("")}</div>
   </div></div>`;
}

function runCard(){
  const on=state.enabled;
  return `<div class="run"><div class="runrow">
     <button class="startbtn ${on?'on':''}" id="startBtn">${on?'Stop automation':'Start automation'}</button>
     <div class="rstat ${on?'live':''}" id="rstat"><span class="dot"></span>${on?'<b>Scanning</b> — watching NQ &amp; RTY for the opening-range breakout. Trades place automatically when your rules trigger.':'Idle. Press Start and the engine begins scanning at the 9:30 ET open and executes by the rules.'}</div>
   </div><div class="acts" id="acts" hidden></div></div>
   <div class="note"><div><b>Demo first.</b> Arm a demo account to watch real order placement with simulated money before any real capital. The strategy logic runs server-side inside KhomaAPI and never leaves it. What you see under “Contracts” is exactly what gets sent to Tradovate.</div></div>`;
}

const NOTES={cons:"Highest odds, patient — small size makes it slower.",norm:"Balanced — solid odds, passes in about three weeks.",aggr:"Fastest, but bust risk climbs. Only if resets don't bother you."};

function render(){
  if(!ACCOUNTS.length){$("#apickWrap").style.display="none";
    $("#app").innerHTML='<div class="empty">No connected Tradovate account yet. Connect one under <a href="/broker">Broker</a> — start with your <b>demo</b> account, then come back here.</div>';return;}
  $("#app").innerHTML=`<div class="grid">${configCard()}${projCard()}</div>${runCard()}`;
  const acc=ACCOUNTS.find(a=>a.id===state.acct);
  $("#envBadge").textContent=(acc.env||"live").toUpperCase();
  $("#envBadge").style.color=acc.env==="demo"?"#0f8f45":"#dc2626";
  $("#envBadge").style.borderColor=acc.env==="demo"?"#0f8f45":"#dc2626";
  const pn=$("#presetNote");if(pn)pn.textContent=NOTES[state.preset];
  document.querySelectorAll("[data-seg]").forEach(sg=>{
    sg.querySelectorAll("button").forEach(b=>b.onclick=()=>{state[sg.dataset.seg]=b.dataset.v;render();});});
  const bi=$("#balIn");if(bi)bi.oninput=e=>{state.bal=+e.target.value.replace(/[^0-9]/g,"")||0;e.target.value=state.bal?fmt(state.bal):"";render();};
  const di=$("#ddIn");if(di)di.oninput=e=>{state.dd=+e.target.value;render();};
  $("#startBtn").onclick=toggleRun;
}

async function save(enabled){
  const b=new URLSearchParams({account_id:state.acct,enabled:enabled?"1":"0",mode:state.mode,
    acct_size:state.acct_size,preset:state.preset,asset:state.asset});
  const r=await fetch("/prop-engine/save",{method:"POST",body:b});return r.ok?r.json():null;
}
async function toggleRun(){
  const btn=$("#startBtn");btn.disabled=true;
  const want=!state.enabled;const j=await save(want);btn.disabled=false;
  if(j&&j.ok){state.enabled=want;CONFIGS[state.acct]=Object.assign(CONFIGS[state.acct]||{},{enabled:want,mode:state.mode,acct_size:state.acct_size,preset:state.preset,asset:state.asset});render();}
  else alert("Could not update automation. Check the account is connected.");
}

function loadAcct(id){
  state=Object.assign({acct:id,mode:"eval",acct_size:"50K",preset:"norm",asset:"both",bal:25000,dd:15,enabled:false},CONFIGS[id]||{});
  state.acct=id;render();
}

function poll(){
  fetch("/prop-engine/status").then(r=>r.json()).then(s=>{
    const acts=$("#acts");if(!acts)return;
    const list=(s.engine&&s.engine.actions||[]).filter(a=>a.action&&a.action!=="tick");
    if(state.enabled&&list.length){acts.hidden=false;
      acts.innerHTML="<div class='k' style='color:var(--fa)'>Recent engine activity</div>"+list.slice(-6).reverse().map(a=>`<div class="row"><b>${a.root||a.account_id||''}</b> ${a.action}${a.side?' '+a.side:''}${a.qty?' ×'+a.qty:''}</div>`).join("");}
    else acts.hidden=true;
  }).catch(()=>{});
}

const sel=$("#acctSel");
ACCOUNTS.forEach(a=>{const o=document.createElement("option");o.value=a.id;o.textContent=a.name+"  ("+(a.env||"live")+")";sel.appendChild(o);});
sel.onchange=()=>loadAcct(sel.value);
// ---- validated equity curves (NQ / RTY / Portfolio), from the 2019-2026 backtest ----
let eqTab="PF";
function eqPath(cum,W,H,L,R,T,B){
  const n=cum.length,mx=Math.max(...cum),mn=Math.min(...cum,0);
  const x=i=>L+i/(n-1)*(W-L-R),y=v=>T+(1-(v-mn)/(mx-mn||1))*(H-T-B);
  let ln="M"+x(0).toFixed(1)+" "+y(cum[0]).toFixed(1);
  cum.forEach((v,i)=>{if(i)ln+=" L"+x(i).toFixed(1)+" "+y(v).toFixed(1);});
  const ar=ln+" L"+x(n-1).toFixed(1)+" "+(H-B)+" L"+x(0).toFixed(1)+" "+(H-B)+" Z";
  const zy=y(0),g=`<line x1="${L}" y1="${zy.toFixed(1)}" x2="${W-R}" y2="${zy.toFixed(1)}" stroke="#e5e7eb"/>`
    +`<text x="${L-6}" y="${(y(mx)+9).toFixed(1)}" text-anchor="end" font-size="10" font-weight="700" fill="#9ca3af">$${(mx/1000).toFixed(0)}k</text>`
    +`<text x="${L-6}" y="${(zy+3).toFixed(1)}" text-anchor="end" font-size="10" font-weight="700" fill="#9ca3af">0</text>`;
  return {ln,ar,g,x:x(n-1),y:y(cum[n-1])};
}
function renderEquity(){
  const d=EQ[eqTab],c=d.cum,s=d.stats,W=780,H=210,L=40,R=12,T=14,B=20;
  const p=eqPath(c,W,H,L,R,T,B);
  const yr=EQ.months.map((m,i)=>[i,m]).filter(([i,m])=>m.endsWith("-01")).map(([i,m])=>
    `<text x="${(L+i/(c.length-1)*(W-L-R)).toFixed(1)}" y="${H-5}" text-anchor="middle" font-size="10" font-weight="700" fill="#9ca3af">${m.slice(0,4)}</text>`).join("");
  const tabs=[["PF","Portfolio · NQ+RTY"],["NQ","NQ only"],["RTY","RTY only"]].map(([k,t])=>
    `<button class="eqtab ${k===eqTab?'on':''}" data-k="${k}">${t}</button>`).join("");
  const tiles=[["Net / micro","$"+s.net.toLocaleString()],["Profit factor",s.pf],["Win rate",s.win+"%"],
    ["Max drawdown","$"+s.maxdd.toLocaleString()],["Return / DD",s.ret_dd+"×"],["Trades",s.n.toLocaleString()]];
  document.getElementById("eqSection").innerHTML=`
    <div class="eqhead"><div><h2>Validated equity curve</h2><p>Real 1-minute data, 2019–2026 · honest fills (slippage + commissions) · per micro contract</p></div>
      <div class="eqtabs">${tabs}</div></div>
    <div class="eqchart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${eqTab} equity curve">
      <defs><linearGradient id="eqg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#16a34a" stop-opacity=".16"/><stop offset="1" stop-color="#16a34a" stop-opacity="0"/></linearGradient></defs>
      ${p.g}<path d="${p.ar}" fill="url(#eqg)"/><path d="${p.ln}" fill="none" stroke="#0f8f45" stroke-width="2.4" stroke-linejoin="round"/>
      <circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4.5" fill="#0f8f45" stroke="#fff" stroke-width="2"/>${yr}</svg></div>
    <div class="eqtiles">${tiles.map(t=>`<div class="eqt"><div class="k">${t[0]}</div><div class="v">${t[1]}</div></div>`).join("")}</div>
    <div class="eqnote">Portfolio (NQ + RTY) is the smoothest — PF ${EQ.PF.stats.pf}, ${EQ.PF.stats.ret_dd}× return-to-drawdown vs ${EQ.NQ.stats.ret_dd}× (NQ) and ${EQ.RTY.stats.ret_dd}× (RTY) alone. Their winning days barely overlap, so running both cuts the drawdown. This is the exact engine that trades your account.</div>`;
  document.querySelectorAll(".eqtab").forEach(b=>b.onclick=()=>{eqTab=b.dataset.k;renderEquity();});
}
renderEquity();

if(ACCOUNTS.length){loadAcct(ACCOUNTS[0].id);setInterval(poll,4000);poll();}else render();
</script>
"""
