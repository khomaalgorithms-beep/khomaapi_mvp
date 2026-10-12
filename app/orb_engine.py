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
from app import orb_momentum as MOM
from app.orb_validated import DATA as VALIDATED

POLL_INTERVAL = float(__import__("os").getenv("ORB_LIVE_POLL_INTERVAL", "3"))
# instrument root -> the asset key the sizing/split uses
# RTY is retired (no reliable edge, malfunctioned live): every stored asset value now trades MNQ only.
_ASSET_ROOTS = {"nq": ["MNQ"], "rty": ["MNQ"], "both": ["MNQ"]}


def _now_et() -> datetime:
    return datetime.now(_ET)


def _massive_key() -> Optional[str]:
    return __import__("os").getenv("MASSIVE_API_KEY") or None


def _make_autopilot(cfg: AccountConfig, token_provider) -> Autopilot:
    """One Autopilot per account, executing to that account's Tradovate connection."""
    return Autopilot(cfg, get_token=token_provider, now_et=_now_et, get_context=get_context)


# ---------------------------------------------------------------------------
# daily context (prior-session ATR14 + 20-day trend) — fetched OFF the event loop, cached per date
# ---------------------------------------------------------------------------
_CTX: Dict[tuple, "MOM.DayContext"] = {}
_CTX_TRIED: Dict[tuple, float] = {}


def get_context(root: str, date: str):
    """Cached context for (root, ET date); None if not fetched yet (the engine then sits the day out)."""
    return _CTX.get((root, date))


def refresh_context(roots, now=None, retry_sec: float = 600.0) -> List[dict]:
    """Fetch today's context for each root once (retry every 10 min on failure). Blocking network —
    call from an executor. Never raises."""
    import time
    from app.orb_massive import fetch_daily_context
    now = now or _now_et()
    date = now.strftime("%Y-%m-%d")
    out = []
    key = _massive_key()
    for root in roots:
        k = (root, date)
        if k in _CTX and _CTX[k].ok:
            continue
        if time.time() - _CTX_TRIED.get(k, 0) < retry_sec:
            continue
        _CTX_TRIED[k] = time.time()
        if not key:
            out.append({"root": root, "context": "no MASSIVE_API_KEY"})
            continue
        try:
            ranges, closes = fetch_daily_context(key, root, now)
            ctx = MOM.context_from(ranges, closes, date)
            _CTX[k] = ctx
            out.append({"root": root, "date": date, "atr": ctx.atr, "trend": ctx.trend,
                        "ranges": len(ranges), "closes": len(closes), "ok": ctx.ok})
        except Exception as e:
            out.append({"root": root, "context_error": str(e)})
    return out


def _make_massive_feed(subscriptions, on_bar):
    """The single shared Massive real-time feed for the whole engine (needs MASSIVE_API_KEY)."""
    return MassiveFeed(get_key=_massive_key, subscriptions=subscriptions, on_bar=on_bar)


def _subscriptions_for(roots):
    return subscriptions_for(roots)


def _fetch_today_bars(root):
    """Today's elapsed 1-min bars for a root via Massive REST (for opening-range backfill)."""
    from app.orb_massive import fetch_today_bars
    key = _massive_key()
    return fetch_today_bars(key, root) if key else []


def _fetch_daily_ranges(root):
    """Prior sessions' true ranges for a root via Massive REST (to seed the rolling ATR)."""
    from app.orb_massive import fetch_daily_ranges
    key = _massive_key()
    return fetch_daily_ranges(key, root) if key else []


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
    for col, decl in (("dd_type", "TEXT DEFAULT 'eod'"), ("cash_bal", "REAL DEFAULT 25000"), ("cash_dd", "REAL DEFAULT 15")):
        try:                                  # additive, one statement per connection (Postgres-safe)
            c2 = _db()
            c2.execute(f"ALTER TABLE orb_autopilot ADD COLUMN {col} {decl}")
            c2.commit()
            c2.close()
        except Exception:
            try:
                c2.close()
            except Exception:
                pass
    print("ORB schema:", "new columns OK" if _has_new_columns() else "new columns MISSING (save falls back to legacy columns)")


def _has_new_columns() -> bool:
    try:
        con = _db()
        con.execute("SELECT dd_type, cash_bal, cash_dd FROM orb_autopilot WHERE 1=0").fetchall()
        con.close()
        return True
    except Exception:
        try:
            con.close()
        except Exception:
            pass
        return False


def get_config(user_id: int, account_id: str) -> Optional[dict]:
    con = _db()
    row = con.execute("SELECT * FROM orb_autopilot WHERE user_id=? AND account_id=?",
                      (user_id, str(account_id))).fetchone()
    con.close()
    return dict(row) if row else None


def save_config(user_id: int, account_pk: int, account_id: str, *, enabled: bool,
                mode: str, acct_size: str, preset: str, asset: str, dd_type: str = "eod",
                cash_bal: float = 25000.0, cash_dd: float = 15.0) -> None:
    """Upsert one account's autopilot settings. Validates the enum values so a bad payload can
    never reach the sizing tables."""
    assert mode in ("eval", "funded", "cash")
    assert acct_size in ("25K", "50K", "100K", "150K")
    assert preset in ("cons", "norm", "aggr", "safe", "safe2")
    assert preset not in ("safe", "safe2") or mode == "funded"
    assert asset in ("nq", "rty", "both")
    assert dd_type in ("eod", "intraday", "static")
    cash_bal = float(cash_bal); cash_dd = float(cash_dd)
    assert 1000 <= cash_bal <= 10_000_000 and 1 <= cash_dd <= 50
    if not _has_new_columns():                # schema fallback: never let Start/Stop fail on a missing column
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
        return
    con = _db()
    con.execute("""
        INSERT INTO orb_autopilot(user_id,account_pk,account_id,enabled,mode,acct_size,preset,asset,dd_type,cash_bal,cash_dd,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,account_id) DO UPDATE SET
          account_pk=excluded.account_pk, enabled=excluded.enabled, mode=excluded.mode,
          acct_size=excluded.acct_size, preset=excluded.preset, asset=excluded.asset,
          dd_type=excluded.dd_type, cash_bal=excluded.cash_bal, cash_dd=excluded.cash_dd,
          updated_at=excluded.updated_at
    """, (user_id, account_pk, str(account_id), 1 if enabled else 0, mode, acct_size, preset,
          asset, dd_type, cash_bal, cash_dd, datetime.now(timezone.utc).isoformat()))
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


def _copy_config_rows(copy_accounts: List[dict], templates: Dict[int, dict]) -> List[dict]:
    """Pure: apply each owner's ORB template (the account size + preset they picked in the ORB
    section) UNIFORMLY to every account in their Copy Trading box, so 'drop accounts in the box'
    makes them all trade KhomaVolume ORB on those exact conditions. A copy account whose owner has
    NOT set an ORB config yet is skipped (there is nothing to copy). Sizing is uniform by design —
    group same-size accounts in a box; a smaller account in a larger-size box would be over-sized."""
    out = []
    for a in copy_accounts:
        t = templates.get(a["user_id"])
        if not t:
            continue
        out.append({"account_pk": a["account_pk"], "user_id": a["user_id"],
                    "account_name": a["account_name"], "env": a["env"],
                    "status": a.get("status", "connected"), "account_id": a["account_id"],
                    "enabled": 1, "mode": t["mode"], "acct_size": t["acct_size"],
                    "preset": t["preset"], "asset": t["asset"], "dd_type": t.get("dd_type") or "eod",
                    "cash_bal": t.get("cash_bal") or 25000, "cash_dd": t.get("cash_dd") or 15,
                    "source": "copy"})
    return out


def copy_rows() -> List[dict]:
    """Every Copy-Trading-box account (group_type='copy') whose owner has an ORB template config
    AND master switch Running, as a config row using the owner's template applied uniformly."""
    con = _db()
    templates: Dict[int, dict] = {}
    for r in con.execute("SELECT * FROM orb_autopilot WHERE enabled=1 ORDER BY updated_at ASC").fetchall():
        d = dict(r)
        templates[d["user_id"]] = d          # most-recently-updated enabled config = the template
    accts = [dict(r) for r in con.execute("""
        SELECT ba.id AS account_pk, ba.user_id, ba.account_name, ba.env, ba.status, ba.account_id
        FROM broker_accounts ba JOIN users u ON u.id = ba.user_id
        WHERE ba.status='connected' AND COALESCE(ba.group_type,'independent')='copy'
              AND u.automation_status='Running'
    """).fetchall()]
    con.close()
    return _copy_config_rows(accts, templates)


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
ACCT = {"25K": dict(start=25000, target=1250, mll=1000, eval_micros=20, funded_micros=10),
        "50K": dict(start=50000, target=3000, mll=2000, eval_micros=40, funded_micros=20),
        "100K": dict(start=100000, target=6000, mll=3000, eval_micros=60, funded_micros=30),
        "150K": dict(start=150000, target=9000, mll=4500, eval_micros=100, funded_micros=40)}
def cash_basis(preset: str) -> str:
    """Live cash plans against the FULL history (2008-26) on 'cons', otherwise the last 10 years."""
    return "full" if preset == "cons" else "ten"


def cash_risk_usd(cash_bal: float, cash_dd: float, preset: str) -> float:
    """$ risk per trade so that the strategy's worst historical drawdown (in R) equals the client's drawdown budget."""
    dd_r = float(VALIDATED["cash"][cash_basis(preset)]["ddR"])
    return round(float(cash_bal) * float(cash_dd) / 100.0 / dd_r, 2)


def risk_plan(mode: str, acct_size: str, preset: str, dd_type: str = "eod",
              cash_bal: float = 25000.0, cash_dd: float = 15.0) -> dict:
    """Validated sizing for a configuration: $ risked per trade, max micros, eval take-profit cap.
    eval   -> the preset's validated risk for this account size AND drawdown type (orb_validated)
    funded -> 12% of the max-loss limit, hold to close (validated funded mode)
    cash   -> drawdown budget (balance x dd%) / the strategy's worst historical drawdown in R
              (full 2008-26 history on 'cons', last 10 years otherwise); 0 micros if one micro is too big."""
    a = ACCT[acct_size]
    dd = dd_type if dd_type in ("eod", "intraday", "static") else "eod"
    if mode == "eval":
        risk = float(VALIDATED["presets"][acct_size][dd][preset]["risk"])
        return dict(risk_usd=risk, max_micros=a["eval_micros"], day_tp_usd=round(0.48 * a["target"]),
                    start=a["start"], target=a["target"], tp_r=None, min_micros=0)
    if mode == "funded":
        f = VALIDATED["funded"][acct_size][dd]
        if preset in ("safe", "safe2") and f.get("safe"):
            # Funded SAFE (validated): take-profit at 2x the stop; risk halves after the first payout ("safe2")
            sf = f["safe"]
            return dict(risk_usd=float(sf["risk_after"] if preset == "safe2" else sf["risk"]), max_micros=a["funded_micros"],
                        day_tp_usd=None, start=a["start"], target=0, tp_r=float(sf["tp_r"]), min_micros=1)
        return dict(risk_usd=float(f["risk"]), max_micros=a["funded_micros"],
                    day_tp_usd=None, start=a["start"], target=0, tp_r=None, min_micros=1)
    return dict(risk_usd=cash_risk_usd(cash_bal, cash_dd, preset),
                max_micros=40, day_tp_usd=None, start=0, target=0, tp_r=None, min_micros=0)


def qty_for(mode: str, acct_size: str, preset: str, asset: str, dd_type: str = "eod",
            cash_bal: float = 25000.0, cash_dd: float = 15.0, atr: Optional[float] = None) -> Dict[str, int]:
    """ESTIMATED micros at today's volatility (the engine sizes exactly at 10:30 from the live stop)."""
    plan = risk_plan(mode, acct_size, preset, dd_type, cash_bal, cash_dd)
    a = atr or (get_context("MNQ", _now_et().strftime("%Y-%m-%d")) or MOM.DayContext("", None, 0)).atr or 300.0
    q = MOM.micros_for_risk(plan["risk_usd"], MOM.STOP_ATR * a, 2.0, plan["max_micros"])
    if q <= 0 and plan.get("min_micros"):
        q = min(int(plan["min_micros"]), int(plan["max_micros"]))
    return {"MNQ": q}


def config_signature(row: dict, contracts: Dict[str, str], qty: Dict[str, int]) -> str:
    """Stable hash of everything that, if changed, should rebuild the account's feed+autopilot."""
    return json.dumps({"env": row.get("env"), "mode": row["mode"], "size": row["acct_size"],
                       "preset": row["preset"], "asset": row["asset"], "dd": row.get("dd_type") or "eod",
                       "cash": [row.get("cash_bal"), row.get("cash_dd")],
                       "contracts": contracts, "strategy": "momentum-v1"}, sort_keys=True)


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
    dd = row.get("dd_type") or "eod"
    cb, cd = row.get("cash_bal") or 25000, row.get("cash_dd") or 15
    plan = risk_plan(row["mode"], row["acct_size"], row["preset"], dd, cb, cd)
    qty = qty_for(row["mode"], row["acct_size"], row["preset"], row["asset"], dd, cb, cd)
    observe_env = set(x.strip() for x in __import__("os").getenv("ORB_OBSERVE", "").split(",") if x.strip())
    observe = {r: (r in observe_env) for r in roots}
    return AccountConfig(env=env, account_spec=row["account_name"], account_id=int(row["account_id"]),
                        account_name=row["account_name"], instruments=contracts, qty=qty,
                        params=dict(DEFAULT_PARAMS), observe=observe, strategy="momentum",
                        mode=row["mode"], risk_usd=plan["risk_usd"], max_micros=plan["max_micros"],
                        acct_start=plan["start"], acct_target=plan["target"], day_tp_usd=plan["day_tp_usd"],
                        tp_r=plan.get("tp_r"), min_micros=int(plan.get("min_micros") or 0))


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
        self.events: List[dict] = []             # trade events pending an email notification
        self.decisions: List[dict] = []          # rolling audit trail of 10:30 decisions (also printed to the log)
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
                # needs_backfill: on (re)start mid-session, rebuild today's opening range from REST
                self._active[acct_id] = {"sig": c["sig"], "autopilot": ap, "needs_backfill": True}
        self._ensure_feed()

    def run_backfills(self, fetch_bars, fetch_ranges=None) -> List[dict]:
        """For each newly-armed autopilot: seed the ATR from prior daily ranges, then backfill
        today's elapsed bars so the opening range survives a restart. One REST fetch per root,
        shared across accounts. fetch_bars(root)->[Bar], fetch_ranges(root)->[float]. Runs off the
        loop thread (network); never raises out."""
        pending = [(aid, st) for aid, st in self._active.items() if st.get("needs_backfill")]
        if not pending:
            return []
        roots = sorted({r for _, st in pending for r in st["autopilot"].cfg.instruments})
        cache, rcache = {}, {}
        for r in roots:
            try:
                cache[r] = fetch_bars(r) or []
            except Exception as e:
                print("ORB backfill fetch error:", r, e); cache[r] = []
            if fetch_ranges is not None:
                try:
                    rcache[r] = fetch_ranges(r) or []
                except Exception as e:
                    print("ORB atr fetch error:", r, e); rcache[r] = []
        out = []
        for aid, st in pending:
            for r in list(st["autopilot"].cfg.instruments):
                try:
                    if fetch_ranges is not None:
                        st["autopilot"].seed_atr(r, rcache.get(r, []))   # seed ATR before the OR check
                    out.append({"account_id": aid, **st["autopilot"].backfill(r, cache.get(r, []))})
                except Exception as e:
                    out.append({"account_id": aid, "root": r, "error": str(e)})
            st["needs_backfill"] = False
        return out

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
                out.append({"account_id": acct_id, "action": "error", "error": str(e)})
        for d in out:                                   # audit trail: every decision is logged (trade / skip / reject / error)
            if d.get("action") in (None, "wait", "backfill_ready"):
                continue
            d = {k: v for k, v in d.items() if k != "resp" or d.get("action") == "reject"}
            self.decisions.append({"ts": datetime.now(timezone.utc).isoformat(), **d})
            del self.decisions[:-200]
            print("ORB DECISION:", d)
        return out

    def poll(self) -> List[dict]:
        out: List[dict] = []
        for acct_id, st in list(self._active.items()):
            try:
                res = st["autopilot"].poll()
                out.extend({"account_id": acct_id, **r} for r in res)
                for r in res:
                    # email ONLY when a real position actually FILLS on the account — never on mere
                    # order submission, so a rejected order sends no "Trade Triggered" email.
                    if r.get("action") == "filled":
                        self.events.append({"account_id": acct_id, "root": r.get("root"), **r})
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
    """Config list for every account that should trade: accounts ARMED in the ORB section
    (enabled_rows) PLUS every account in a Copy Trading box, which trades the owner's ORB template
    conditions (copy_rows). Deduped by account — an armed account also in a box counts once."""
    out = []
    seen = set()
    for row in list(enabled_rows()) + copy_rows():
        pk = row["account_pk"]
        if pk in seen:
            continue
        seen.add(pk)
        token = _fresh_token_for(pk)
        if not token:
            continue
        cfg = build_account_config(row, token)
        if cfg is None:
            continue
        # KhomaAPI Risk Engine: a locked account (daily loss / drawdown breach) must never get a new entry
        cfg.risk_check = (lambda pk=pk, uid=row.get("user_id"):
                          _main()._orb_account_risk_locked({"id": pk, "user_id": uid}, fail_closed=True))
        roots = _ASSET_ROOTS[row["asset"]]
        contracts = dict(cfg.instruments)
        qty = {r: cfg.qty.get(r, 0) for r in roots}
        out.append({"row": row, "cfg": cfg, "sig": config_signature(row, contracts, qty),
                    "token_provider": (lambda pk=pk: _fresh_token_for(pk))})
    return out


def _ctx_status() -> dict:
    c = get_context("MNQ", _now_et().strftime("%Y-%m-%d"))
    if not c:
        return {"ready": False}
    return {"ready": c.ok, "date": c.date, "atr": round(c.atr, 1) if c.atr else None, "trend": c.trend,
            "stop_pts": round(MOM.STOP_ATR * c.atr, 2) if c.atr else None}


_LOGGED: set = set()


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
                # today's prior-session context (ATR14 + 20d trend) for the 10:30 decision — fetched off-loop,
                # once per day (retried every 10 min on failure). Without it the engine sits the day out.
                # always prefetched from 06:00 ET (cheap, once/day) so a Start pressed just before 10:30 still
                # trades and every dashboard can show today's plan
                if _MANAGER.active_ids() or _now_et().hour >= 6:
                    ctx_out = await asyncio.get_event_loop().run_in_executor(None, refresh_context, ["MNQ"])
                    for c in ctx_out:
                        print("ORB context:", c)
                # seed ATR + backfill today's opening range for any newly-armed account (restart-proof)
                bf = await asyncio.get_event_loop().run_in_executor(
                    None, _MANAGER.run_backfills, _fetch_today_bars, _fetch_daily_ranges)
                for a in bf:
                    if a.get("action") in ("backfill_ready", "backfill_missed"):
                        print("ORB backfill:", a)
                actions = _MANAGER.poll()
                for a in actions:                           # fills, exits, flattens, errors -> log (once per kind/day)
                    if a.get("action") not in (None, "wait") or a.get("error"):
                        key = (_now_et().date().isoformat(), a.get("account_id"), a.get("root"), a.get("action"), a.get("reason"))
                        if key not in _LOGGED:
                            _LOGGED.add(key)
                            if len(_LOGGED) > 5000:
                                _LOGGED.clear()
                            print("ORB EVENT:", a)
                _LAST_STATUS.update(running=True, accounts=len(_MANAGER.active_ids()),
                                    data_key=bool(_massive_key()),
                                    feed_instruments=list(_MANAGER.feed_roots()),
                                    bars_seen=_MANAGER.bars_seen, last_bar=_MANAGER.last_bar,
                                    context=_ctx_status(), decisions=_MANAGER.decisions[-20:],
                                    actions=[a for a in actions if a.get("action") not in (None,)][-20:],
                                    ts=datetime.now(timezone.utc).isoformat())
                # email the account owner for each trade that just fired (off the loop thread)
                while _MANAGER.events:
                    ev = _MANAGER.events.pop(0)
                    await asyncio.get_event_loop().run_in_executor(None, notify_trade, ev)
            else:
                _LAST_STATUS.update(running=False)
        except Exception as e:
            print("ORB LIVE ENGINE ERROR:", e)
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# trade email notifications ("Trade Triggered") — branded like every KhomaAPI email
# ---------------------------------------------------------------------------
_ROOT_NAME = {"MNQ": "NQ &middot; Micro Nasdaq-100", "M2K": "RTY &middot; Micro Russell 2000"}


def notify_trade(ev: dict) -> None:
    """Email the account owner a branded 'Trade Triggered' notice. Runs off the loop thread and
    never raises out — a failed email must never affect trading."""
    try:
        main = _main()
        con = _db()
        row = con.execute(
            "SELECT ba.account_name, u.email FROM broker_accounts ba JOIN users u ON u.id = ba.user_id "
            "WHERE ba.account_id = ? LIMIT 1", (str(ev.get("account_id")),)).fetchone()
        con.close()
        d = dict(row) if row else {}
        if not d.get("email"):
            return
        subject, heading, html = _trade_email(d.get("account_name") or "your account", ev)
        main.send_branded_email(d["email"], subject, heading, html,
                                button_label="Open KhomaVolume ORB",
                                button_url="https://app.khomaapi.com/prop-engine")
        print("ORB trade email sent ->", d["email"])
    except Exception as e:
        print("ORB trade email error:", e)


def _fmt(x) -> str:
    try:
        return f"{float(x):,.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)


def _trade_email(account_name: str, ev: dict):
    """(subject, heading, message_html) for a trade-triggered email."""
    root = ev.get("root", ""); side = str(ev.get("side", "")).upper(); qty = ev.get("qty", "")
    plan = ev.get("plan")
    entry = ev.get("entry")                              # actual fill price when known
    if isinstance(plan, dict):                           # validated momentum engine: {"sl","tp","qty","side"}
        sl, tp = plan.get("sl"), plan.get("tp")
    else:                                                # legacy selective EntryPlan object
        if entry is None:
            entry = getattr(plan, "entry_limit", None) or getattr(plan, "entry_stop", None)
        sl, tp = getattr(plan, "sl_price", None), getattr(plan, "tp_price", None)
    name = _ROOT_NAME.get(root, root)
    color = "#0f8f45" if side == "LONG" else "#dc2626"
    when = (_now_et().strftime("%I:%M %p ET").lstrip("0") + " &middot; "
            + _now_et().strftime("%b %d").replace(" 0", " "))

    def r(k, v):
        return (f'<tr><td style="padding:9px 2px;color:#6b7280;font-size:13px;">{k}</td>'
                f'<td style="padding:9px 2px;color:#111827;font-size:14px;font-weight:700;text-align:right;'
                f'font-family:Arial;">{v}</td></tr>')
    msg = (
        f'<p style="margin:0 0 4px;">Your <b>KhomaVolume</b> engine just filled today&rsquo;s 10:30 trade on '
        f'<b>{account_name}</b>. The protective stop is already working at the broker, and the position '
        f'closes by 3:59 PM ET at the latest. About 2 trades in 3 end at the stop; that is normal.</p>'
        f'<div style="margin:20px 0;border:1px solid #e8eae9;border-radius:14px;overflow:hidden;">'
        f'<div style="background:{color};padding:13px 18px;color:#fff;font-weight:800;font-size:15px;'
        f'letter-spacing:.3px;">{side} &nbsp;&middot;&nbsp; {name}</div>'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="padding:4px 18px 12px;">'
        f'{r("Contracts", f"{qty} {root}")}{r("Entry", _fmt(entry))}'
        f'{r("Stop-loss", _fmt(sl) if sl is not None else "-")}'
        f'{r("Take-profit", _fmt(tp) if tp is not None else "Held to the close")}{r("Triggered", when)}'
        f'</table></div>'
        f'<p style="margin:0;color:#9ca3af;font-size:13px;line-height:1.6;">You only receive this email '
        f'when a trade fires — <b>no email means the engine took no trade.</b></p>')
    return f"Trade Triggered - {side} {root} on {account_name}", "Trade Triggered", msg


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


# ---------------------------------------------------------------------------
# Daily morning health email — 9:00 AM ET to EVERY client running the algo.
# Confirms each account is ARMED + CONNECTED + automation Running, and ALERTS on
# anything disconnected, paused, unconfigured, or whose broker link has expired.
# Leader-only; sent once per day (guard table so restarts never double-send).
# ---------------------------------------------------------------------------
_HEALTH_HOUR_ET = int(__import__("os").getenv("ORB_HEALTH_HOUR_ET", "9"))
_APP_URL = __import__("os").getenv("APP_BASE_URL", "https://app.khomaapi.com")
# Per-client send-hour overrides (ET, 24h clock). Default is _HEALTH_HOUR_ET (9 AM). Nandan's two
# KhomaAPI accounts get their health email at 20:00 (8 PM ET) instead of the morning.
_HEALTH_HOUR_OVERRIDES = {"n@idealstor.com": 20, "narora@idealstor.com": 20}


def _health_hour_for(email) -> int:
    return _HEALTH_HOUR_OVERRIDES.get((email or "").lower().strip(), _HEALTH_HOUR_ET)


def _ensure_health_table() -> None:
    con = _db()
    con.execute("CREATE TABLE IF NOT EXISTS morning_health_sent("
                "date TEXT PRIMARY KEY, sent_at TEXT, recipients INTEGER)")
    # Per-(date, user) log so each client is emailed exactly once/day AT THEIR OWN hour.
    con.execute("CREATE TABLE IF NOT EXISTS orb_health_log("
                "date TEXT, user_id INTEGER, sent_at TEXT, PRIMARY KEY(date, user_id))")
    con.commit(); con.close()


def _algo_users() -> List[dict]:
    """Every client running the algo: has >=1 armed ORB config OR an account in the Copy box.
    Returned regardless of current health — we WANT to reach the disconnected/paused ones."""
    con = _db()
    rows = con.execute(
        "SELECT DISTINCT u.* FROM users u "
        "WHERE EXISTS(SELECT 1 FROM orb_autopilot o WHERE o.user_id=u.id AND o.enabled=1) "
        "   OR EXISTS(SELECT 1 FROM broker_accounts b WHERE b.user_id=u.id "
        "             AND COALESCE(b.group_type,'independent')='copy')").fetchall()
    con.close()
    return [dict(r) for r in rows]


def _algo_accounts(user_id: int) -> List[dict]:
    """The user's accounts the algo is meant to run (armed OR in the Copy box), with status."""
    con = _db()
    rows = con.execute(
        "SELECT ba.id AS pk, ba.account_id, ba.account_name, ba.status, "
        "       COALESCE(ba.group_type,'independent') AS grp, "
        "       COALESCE(ap.enabled,0) AS armed, ap.acct_size, ap.preset "
        "FROM broker_accounts ba "
        "LEFT JOIN orb_autopilot ap ON ap.account_pk=ba.id AND ap.user_id=ba.user_id "
        "WHERE ba.user_id=? AND (COALESCE(ap.enabled,0)=1 "
        "      OR COALESCE(ba.group_type,'independent')='copy')", (user_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _health_rows(user) -> tuple:
    """(lines[(name, ok, detail)], issues[], automation_running). Checks connected + broker
    token + armed for each of the user's algo accounts."""
    running = (user.get("automation_status") == "Running")
    accts = _algo_accounts(user["id"])
    has_template = any(a.get("armed") for a in accts)
    lines, issues = [], []
    for a in accts:
        name = a.get("account_name") or str(a.get("account_id"))
        connected = (a.get("status") == "connected")
        armed = bool(a.get("armed")) or (a.get("grp") == "copy" and has_template)
        token_ok = True
        if connected:
            try:
                token_ok = bool(_fresh_token_for(a["pk"]))
            except Exception:
                token_ok = True  # never raise a false alarm on a transient check error
        if not connected:
            lines.append((name, False, "broker DISCONNECTED - reconnect it")); issues.append(name)
        elif not token_ok:
            lines.append((name, False, "connection expired - reconnect your broker")); issues.append(name)
        elif not armed:
            lines.append((name, False, "no ORB preset set - pick account size + preset")); issues.append(name)
        else:
            size = f" - {a.get('acct_size')}/{a.get('preset')}" if a.get("acct_size") else ""
            lines.append((name, True, f"Armed & Connected{size}"))
    return lines, issues, running


def _send_health_email(main, user) -> bool:
    import re as _re
    email = user.get("email")
    if not email:
        return False
    # Only email clients who are actually entitled — active subscription or comp access.
    # (Canceled / dormant accounts are not nagged.)
    try:
        if not main.user_entitlements(user).active:
            return False
    except Exception:
        pass
    # This client's own send-hour, as a friendly label for the footer ("9:00 AM ET" / "8:00 PM ET").
    _hh = _health_hour_for(email)
    _when = f"{((_hh - 1) % 12) + 1}:00 {'AM' if _hh < 12 else 'PM'} ET"
    lines, issues, running = _health_rows(user)
    if not lines:
        # Armed for the algo but no connected broker account found — tell them to reconnect.
        subject = "⚠️ KhomaVolume ORB - Reconnect Your Account"
        heading = "Reconnect your broker to resume the algo"
        msg = ('<p style="margin:0 0 12px;color:#4b5563;line-height:1.6;">Your KhomaVolume ORB is set '
               'up, but we could <b>not find a connected broker account</b> on your profile. '
               'The algo cannot trade until a broker account is connected.</p>'
               '<div style="margin:14px 0;padding:12px 14px;background:#fef2f2;border:1px solid #fecaca;'
               'border-radius:8px;color:#991b1b;font-size:14px;line-height:1.6;">'
               'Open KhomaAPI and <b>reconnect your broker</b> so your account is armed and active again.</div>'
               f'<p style="margin:14px 0 0;color:#9ca3af;font-size:13px;line-height:1.6;">This is your '
               f'automatic daily status email from KhomaAPI, sent every day at {_when}.</p>')
        return main.send_branded_email(email, subject, heading, msg,
                                       "Reconnect Broker", f"{_APP_URL}/broker", heading)
    if not running:
        issues.append("automation")
    ok = (not issues)

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    row_html = ""
    for name, good, detail in lines:
        color = "#16a34a" if good else "#dc2626"
        mark = "&#9989;" if good else "&#9888;&#65039;"
        row_html += (f'<tr><td style="padding:9px 12px;border-bottom:1px solid #eef0ef;font-size:14px;">'
                     f'<span style="color:{color};">{mark}</span> '
                     f'<b style="color:#111827;">{esc(name)}</b>'
                     f'<span style="color:#6b7280;"> &mdash; {esc(detail)}</span></td></tr>')

    if ok:
        subject = "✅ KhomaVolume ORB - Daily Check: Armed & Active"
        heading = "Good morning - your algo is armed and running"
        intro = ("Your daily KhomaAPI status check is complete. Every account below is "
                 "<b>connected, armed, and set to trade</b> today. No action needed.")
        note = ""
    else:
        subject = "⚠️ KhomaVolume ORB - Action Needed on Your Account"
        heading = "Action needed to keep your algo running"
        intro = ("Your daily KhomaAPI status check found something that needs attention "
                 "<b>before the algo can trade today</b>. Please review the item(s) marked below.")
        fixes = []
        if not running:
            fixes.append("Your automation is <b>PAUSED</b> - open KhomaAPI and press <b>Start</b>.")
        if any(not g for _, g, _ in lines):
            fixes.append("Reconnect any <b>disconnected/expired</b> broker account, or set a preset on any <b>unconfigured</b> one.")
        note = ('<div style="margin:14px 0;padding:12px 14px;background:#fef2f2;border:1px solid #fecaca;'
                'border-radius:8px;color:#991b1b;font-size:14px;line-height:1.6;">' + "<br>".join(fixes) + "</div>")

    msg = (f'<p style="margin:0 0 12px;color:#4b5563;line-height:1.6;">{intro}</p>{note}'
           f'<table style="width:100%;border-collapse:collapse;margin:6px 0 4px;">{row_html}</table>'
           f'<p style="margin:14px 0 0;color:#9ca3af;font-size:13px;line-height:1.6;">'
           f'This is your automatic daily status email from KhomaAPI, sent every day at '
           f'{_when} so you always know your accounts are armed and connected.</p>')
    text = heading + "\n\n" + "\n".join(
        f"{'OK' if g else 'ACTION'}: {n} - {_re.sub('<[^>]+>', '', d)}" for n, g, d in lines)
    return main.send_branded_email(email, subject, heading, msg,
                                   "Open KhomaAPI", f"{_APP_URL}/prop-engine", text)


def _run_morning_health(today: str, hour: int) -> None:
    """Email each client whose OWN send-hour == `hour` and who hasn't been emailed today. Per-user
    guard (orb_health_log) => everyone gets exactly one email/day, each at their own configured time."""
    main = _main(); users = _algo_users(); sent = 0; due = 0
    for u in users:
        if _health_hour_for(u.get("email")) != hour:
            continue                              # not this client's send-hour
        due += 1
        con = _db()
        if con.execute("SELECT 1 FROM orb_health_log WHERE date=? AND user_id=?", (today, u["id"])).fetchone():
            con.close(); continue                 # already emailed this client today
        con.execute("INSERT INTO orb_health_log(date, user_id, sent_at) VALUES(?,?,?)",
                    (today, u["id"], datetime.now(timezone.utc).isoformat()))
        con.commit(); con.close()
        try:
            if _send_health_email(main, u):
                sent += 1
        except Exception as e:
            print("morning health email error for", u.get("email"), ":", e)
    if due:
        print(f"MORNING HEALTH: hour {hour} ET — emailed {sent}/{due} due clients for {today}")


async def morning_health_loop() -> None:
    _ensure_health_table()
    loop = asyncio.get_event_loop()
    print("HEALTH EMAIL loop started - default %d:00 ET; overrides %s" % (_HEALTH_HOUR_ET, _HEALTH_HOUR_OVERRIDES))
    while True:
        try:
            await asyncio.sleep(300)              # re-check every 5 minutes
            if not await loop.run_in_executor(None, _main().try_become_leader):
                continue                          # not the worker instance
            now = _now_et()
            # each tick, send any client whose configured hour == the current hour (per-user once/day)
            await loop.run_in_executor(None, _run_morning_health, now.strftime("%Y-%m-%d"), now.hour)
        except Exception as e:
            print("morning health loop error:", e)


def install(app) -> None:
    """Wire the engine into a FastAPI app: create the table, register the background loop on
    startup, and add the control routes. Call ONCE from main.py after `app` is created."""
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse

    ensure_table()
    _ensure_health_table()

    @app.on_event("startup")
    async def _orb_live_startup():
        asyncio.create_task(engine_loop())
        asyncio.create_task(morning_health_loop())

    @app.get("/prop-engine", response_class=HTMLResponse)
    def prop_engine_page(request: Request):
        main = _main()
        user = main.require_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        # KhomaVolume ORB is a separate product — hide the control room from anyone who isn't a
        # comp account or an ORB-plan buyer (they don't see the nav link either).
        if not main.has_orb_access(user):
            return RedirectResponse("/dashboard", status_code=302)
        accounts = _connected_accounts(user["id"])
        cfgs = {a["account_id"]: get_config(user["id"], a["account_id"]) for a in accounts}
        try:
            _email = str((user.get("email") if hasattr(user, "get") else user["email"]) or "")
        except Exception:
            _email = ""
        _legacy = False  # honest numbers shown to everyone (owner inflated view disabled)
        body = _render_dashboard(accounts, cfgs, legacy=_legacy)
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
        # Hard block: only comp accounts / ORB-plan buyers may arm KhomaVolume ORB.
        if not main.has_orb_access(user):
            return JSONResponse({"ok": False, "error": "KhomaVolume ORB is not included in your plan"}, status_code=403)
        form = await request.form()
        account_id = str(form.get("account_id") or "")
        acct = next((a for a in _connected_accounts(user["id"]) if a["account_id"] == account_id), None)
        if not acct:
            return JSONResponse({"ok": False, "error": "account not connected"}, status_code=400)
        enabling = form.get("enabled") in ("1", "true", "on", True)
        mode = str(form.get("mode") or "eval"); size = str(form.get("acct_size") or "50K")
        preset = str(form.get("preset") or "norm"); dd = str(form.get("dd_type") or "eod")
        if mode != "funded" and preset in ("safe", "safe2"):
            preset = "norm"
        try:
            cb = float(str(form.get("cash_bal") or "25000").replace(",", ""))
            cd = float(form.get("cash_dd") or 15)
            save_config(user["id"], acct["id"], account_id, enabled=enabling, mode=mode, acct_size=size,
                        preset=preset, asset="nq", dd_type=dd, cash_bal=cb, cash_dd=cd)
        except (AssertionError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid settings"}, status_code=400)
        if enabling:
            set_master_running(user["id"])   # Start here = allow execution (Dashboard Pause still stops it)
        plan = risk_plan(mode, size, preset, dd, cb, cd)
        return JSONResponse({"ok": True, "enabled": enabling, "master_running": master_running(user["id"]),
                            "risk_usd": plan["risk_usd"], "qty": qty_for(mode, size, preset, "nq", dd, cb, cd)})

    @app.get("/prop-engine/status")
    def prop_engine_status(request: Request):
        main = _main()
        user = main.require_user(request)
        if not user:
            return JSONResponse({"engine": _LAST_STATUS, "accounts": {}, "master_running": False})
        mine = {a["account_id"]: get_config(user["id"], a["account_id"])
                for a in _connected_accounts(user["id"])}
        return JSONResponse({"engine": _LAST_STATUS, "accounts": mine, "context": _ctx_status(),
                            "master_running": master_running(user["id"])})


def _render_dashboard(accounts: List[dict], cfgs: Dict[str, Optional[dict]], legacy: bool = False) -> str:
    """KhomaVolume NQ control room: pick a connected account, configure mode / account size / drawdown
    type / preset, see the validated odds + live sizing + full-history (2008-26) equity curve, and Start/Stop automation.
    All numbers come from app.orb_validated (generated from the research harness); arming persists via
    /prop-engine/save and the background loop picks it up within seconds."""
    accts = [{"id": a["account_id"], "name": a.get("account_name", ""),
              "env": (a.get("env") or "live")} for a in accounts]
    conf = {}
    for a in accounts:
        c = cfgs.get(a["account_id"]) or {}
        conf[a["account_id"]] = {"enabled": bool(c.get("enabled")), "mode": c.get("mode", "eval"),
                                 "acct_size": c.get("acct_size", "50K"), "preset": c.get("preset", "norm"),
                                 "dd_type": c.get("dd_type") or "eod", "cash_bal": c.get("cash_bal") or 25000,
                                 "cash_dd": c.get("cash_dd") or 15}
    return (_PAGE.replace("__ACCOUNTS__", json.dumps(accts))
                 .replace("__CONFIGS__", json.dumps(conf))
                 .replace("__DATA__", json.dumps(VALIDATED)))


# Validated equity curves + stats (per micro), generated from the 2019-2026 backtest:
# NQ MNQ 30/75, RTY M2K 8/20, and the NQ+RTY portfolio (1 MNQ + 1 M2K per day).
_EQUITY_JSON = r"""{"NQ":{"cum":[745,468,776,544,542,588,532,596,510,500,94,73,153,580,1240,1690,1990,2110,2260,2608,2637,2787,3058,3070,3286,3222,3312,3820,3460,3588,3648,4502,4714,4504,4530,4350,4080,4410,4260,4380,4530,4560,4230,3688,2818,2938,2808,2658,2822,2462,2372,2252,2192,2598,1878,2508,2418,2748,2298,2328,2658,3134,3044,2954,3457,3037,3028,2878,2338,2488,2488,3028,2998,3568,4408,4228,4198,3928,4105,4345,4298,4066,4396,4306,4332,4182,3552,3672,3672,3672,3672,3672,3672,3672],"stats":{"n":1081,"net":3672,"pf":1.09,"win":32,"maxdd":-3136,"ret_dd":1.2}},"RTY":{"cum":[-9,-119,48,23,295,73,290,461,817,686,696,559,665,570,330,350,110,348,243,146,308,419,346,356,690,1130,1170,1298,1142,1175,1607,1701,1437,1327,1552,1732,1731,1806,1826,1946,1923,2106,2118,2086,2106,2146,2066,2046,1899,2059,1982,2010,1862,1834,1952,1912,1941,1845,1965,1922,2004,1964,2052,2320,2399,2336,2452,2557,2257,2258,2257,2157,2083,2401,2501,2707,2549,2396,2424,2403,2662,3008,3028,3045,3034,3054,3154,3154,3284,3004,3124,3180,3097,3017],"stats":{"n":776,"net":3017,"pf":1.17,"win":39,"maxdd":-914,"ret_dd":3.3}},"PF":{"cum":[736,350,824,567,838,661,823,1057,1327,1186,791,632,818,1150,1570,2040,2100,2458,2502,2754,2945,3206,3404,3425,3976,4352,4482,5118,4601,4764,5256,6203,6152,5832,6082,6082,5811,6216,6086,6326,6453,6665,6348,5774,4924,5084,4874,4704,4721,4521,4353,4261,4053,4432,3830,4420,4359,4593,4263,4250,4662,5098,5096,5275,5856,5373,5480,5435,4595,4746,4745,5185,5081,5969,6909,6935,6747,6324,6529,6748,6960,7074,7424,7351,7366,7236,6706,6826,6956,6676,6796,6852,6769,6689],"stats":{"n":1857,"net":6689,"pf":1.11,"win":35,"maxdd":-3055,"ret_dd":2.2}},"months":["2019-01","2019-02","2019-03","2019-04","2019-05","2019-06","2019-07","2019-08","2019-09","2019-10","2019-11","2019-12","2020-01","2020-02","2020-03","2020-04","2020-05","2020-06","2020-07","2020-08","2020-09","2020-10","2020-11","2020-12","2021-01","2021-02","2021-03","2021-04","2021-05","2021-06","2021-07","2021-08","2021-09","2021-10","2021-11","2021-12","2022-01","2022-02","2022-03","2022-04","2022-05","2022-06","2022-07","2022-08","2022-09","2022-10","2022-11","2022-12","2023-01","2023-02","2023-03","2023-04","2023-05","2023-06","2023-07","2023-08","2023-09","2023-10","2023-11","2023-12","2024-01","2024-02","2024-03","2024-04","2024-05","2024-06","2024-07","2024-08","2024-09","2024-10","2024-11","2024-12","2025-01","2025-02","2025-03","2025-04","2025-05","2025-06","2025-07","2025-08","2025-09","2025-10","2025-11","2025-12","2026-01","2026-02","2026-03","2026-04","2026-05","2026-06","2026-07","2026-08","2026-09","2026-10"]}"""



# --- LEGACY (original, pre-honest-numbers) dashboard numbers — shown ONLY to the owner ---
_EQUITY_JSON_LEGACY = '{"NQ":{"cum":[961,734,1170,1031,1426,1624,1588,2018,2118,2479,2273,2297,2406,2863,3088,3523,3809,4248,4596,4979,5675,6297,6352,6584,6794,6929,6994,7498,7538,7811,8072,9453,9740,9728,9749,10184,10100,10201,10451,10763,10900,11125,10990,11033,10780,11302,11580,11830,12390,12430,12941,13216,13342,13802,13272,13881,13772,14084,14333,14622,15144,15840,15788,15889,16302,16170,16987,17237,17512,18556,19165,19898,20692,21240,21849,22073,22857,22500,22801,23435,23617,23615,24286,24598,25120,25580,25558,26229,25934,26061,26546,26386,26325],"stats":{"n":1145,"net":26325,"pf":1.67,"win":42,"maxdd":-716,"ret_dd":36.8}},"RTY":{"cum":[119,2,226,245,505,323,595,773,1168,1085,1065,956,1142,1154,1184,1329,1360,1852,1749,1804,1994,2121,2049,2190,2562,3284,3539,3698,3591,3620,4260,4355,4458,4432,4561,4873,4862,5147,5572,5827,5941,6154,6165,6221,6236,6267,6324,6297,6158,6313,6476,6654,6721,6715,6846,6854,6882,6907,7021,6949,7160,7066,7180,7466,7679,7489,7660,7907,8065,8221,8231,8218,8134,8444,8672,8885,8750,8599,8659,8673,8890,9082,8957,8990,8975,9206,9293,9423,9528,9377,9828,9947,10058],"stats":{"n":775,"net":10058,"pf":1.64,"win":47,"maxdd":-429,"ret_dd":23.4}},"PF":{"cum":[1080,736,1396,1276,1931,1948,2183,2791,3286,3563,3339,3252,3548,4017,4272,4852,5169,6100,6345,6782,7669,8418,8401,8774,9356,10213,10532,11197,11129,11430,12332,13808,14197,14160,14310,15057,14962,15348,16024,16590,16842,17279,17156,17254,17016,17569,17904,18128,18548,18743,19417,19870,20063,20518,20118,20736,20654,20990,21354,21571,22304,22906,22968,23355,23982,23659,24647,25144,25577,26778,27396,28116,28827,29684,30521,30959,31606,31099,31460,32108,32507,32698,33243,33588,34095,34786,34851,35652,35463,35437,36374,36334,36382],"stats":{"n":1391,"net":36382,"pf":1.8,"win":48,"maxdd":-784,"ret_dd":46.4}},"months":["2019-01","2019-02","2019-03","2019-04","2019-05","2019-06","2019-07","2019-08","2019-09","2019-10","2019-11","2019-12","2020-01","2020-02","2020-03","2020-04","2020-05","2020-06","2020-07","2020-08","2020-09","2020-10","2020-11","2020-12","2021-01","2021-02","2021-03","2021-04","2021-05","2021-06","2021-07","2021-08","2021-09","2021-10","2021-11","2021-12","2022-01","2022-02","2022-03","2022-04","2022-05","2022-06","2022-07","2022-08","2022-09","2022-10","2022-11","2022-12","2023-01","2023-02","2023-03","2023-04","2023-05","2023-06","2023-07","2023-08","2023-09","2023-10","2023-11","2023-12","2024-01","2024-02","2024-03","2024-04","2024-05","2024-06","2024-07","2024-08","2024-09","2024-10","2024-11","2024-12","2025-01","2025-02","2025-03","2025-04","2025-05","2025-06","2025-07","2025-08","2025-09","2025-10","2025-11","2025-12","2026-01","2026-02","2026-03","2026-04","2026-05","2026-06","2026-07","2026-08","2026-09"]}'
_EVAL_LEGACY = 'const EVAL={"25K":{cons:[4,86,36],norm:[6,76,21],aggr:[10,64,10]},"50K":{cons:[8,84,45],norm:[12,74,25],aggr:[20,62,11]},"100K":{cons:[12,84,62],norm:[18,72,35],aggr:[30,59,15]},"150K":{cons:[20,81,53],norm:[30,69,29],aggr:[50,57,14]}};'
_NOTES_LEGACY = 'const NOTES={cons:"Highest odds, patient — small size makes it slower.",norm:"Balanced — solid odds, passes in about three weeks.",aggr:"Fastest, but bust risk climbs. Only if resets don\'t bother you."};'
_EQNOTE_LEGACY = '<div class="eqnote">Portfolio (NQ + RTY) is the smoothest — PF ${EQ.PF.stats.pf}, ${EQ.PF.stats.ret_dd}× return-to-drawdown vs ${EQ.NQ.stats.ret_dd}× (NQ) and ${EQ.RTY.stats.ret_dd}× (RTY) alone. Their winning days barely overlap, so running both cuts the drawdown. This is the exact engine that trades your account.</div>'


_PAGE = r"""
<style>
  .kv{--g:#0f8f45;--gd:#086b34;--gs:#eaf7ef;--gl:#cdebd8;--btn:linear-gradient(135deg,#12a150,#087135);
      --tx:#111827;--mu:#6b7280;--fa:#9ca3af;--ln:#e5e7eb;--bg:#f8faf9;--card:#fff;--dg:#dc2626;--am:#b45309;
      max-width:1000px;margin:0 auto;padding:8px 6px 60px;color:var(--tx);
      font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
  .kv h1{font-size:24px;font-weight:900;letter-spacing:-.8px;margin:0}
  .kv .lead{color:var(--mu);font-size:13.5px;margin:4px 0 20px}
  .kv .ey{font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--fa)}
  .kv .top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
  .kv .apick{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--ln);border-radius:14px;padding:9px 12px;box-shadow:0 8px 20px rgba(17,24,39,.05)}
  .kv .apick select{font:inherit;font-size:15px;font-weight:800;border:0;background:transparent;color:var(--tx);outline:none;max-width:60vw}
  .kv .envb{font-size:10px;font-weight:900;border:1px solid;border-radius:6px;padding:2px 7px}
  .kv .grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;margin-top:18px}
  @media(max-width:820px){.kv .grid{grid-template-columns:1fr}}
  .kv .card{background:var(--card);border:1px solid var(--ln);border-radius:20px;box-shadow:0 18px 60px rgba(17,24,39,.06);min-width:0}
  .kv .card .hd{padding:18px 22px 0}.kv .card .hd h2{font-size:16px;font-weight:800;letter-spacing:-.3px;margin:0}
  .kv .card .hd p{color:var(--mu);font-size:12.5px;margin:4px 0 0}
  .kv .card .bd{padding:18px 22px 22px}
  .kv .field{margin-top:18px}.kv .field:first-child{margin-top:0}
  .kv .field>label{display:block;font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--fa);margin-bottom:8px}
  .kv .seg{display:flex;gap:6px;background:#f4f6f5;border:1px solid var(--ln);border-radius:13px;padding:5px;flex-wrap:wrap}
  .kv .seg.four{display:grid;grid-template-columns:repeat(4,1fr)}
  .kv .seg button{flex:1;border:0;background:transparent;color:var(--mu);font:inherit;font-size:13px;font-weight:700;padding:9px 6px;border-radius:9px;cursor:pointer;transition:.12s;min-width:0}
  .kv .seg button[aria-pressed=true]{background:#fff;color:var(--gd);box-shadow:0 6px 16px rgba(17,24,39,.06);font-weight:800}
  .kv .seg button:focus-visible,.kv .startbtn:focus-visible{outline:2px solid var(--g);outline-offset:2px}
  .kv .cashrow{display:flex;gap:14px;flex-wrap:wrap}.kv .cashrow .f{flex:1;min-width:130px}
  .kv .inl{display:flex;align-items:center;border:1px solid var(--ln);border-radius:12px;background:#fff;overflow:hidden}
  .kv .inl span{padding:0 11px;color:var(--fa);font-weight:800}
  .kv .inl input{border:0;background:transparent;font:inherit;font-size:16px;font-weight:800;padding:12px 12px 12px 0;width:100%;outline:none}
  .kv input[type=range]{width:100%;accent-color:var(--g);margin-top:8px}
  .kv .rv{display:flex;justify-content:space-between;font-size:12px;color:var(--mu);font-weight:700}.kv .rv b{color:var(--gd);font-size:15px}
  .kv .pn{font-size:12.5px;color:var(--mu);margin-top:10px;min-height:16px;line-height:1.45}
  .kv .hero{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  .kv .big{font-size:52px;font-weight:900;letter-spacing:-2.5px;color:var(--gd);line-height:.9;font-variant-numeric:tabular-nums}
  .kv .cap{font-size:13px;color:var(--mu);max-width:30ch;line-height:1.4}.kv .cap b{color:var(--tx)}
  .kv .meter{height:12px;border-radius:8px;background:#eef2f0;border:1px solid var(--ln);overflow:hidden;margin:14px 0 4px}
  .kv .meter i{display:block;height:100%;background:var(--btn);border-radius:7px;transition:width .4s}
  .kv .sz{margin-top:18px;background:var(--gs);border:1px solid var(--gl);border-radius:16px;padding:16px 18px}
  .kv .sz .t{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--gd)}
  .kv .sz .row{display:flex;gap:12px;margin-top:10px;flex-wrap:wrap}
  .kv .sz .leg{flex:1;min-width:120px;background:#fff;border:1px solid var(--gl);border-radius:13px;padding:12px 14px}
  .kv .sz .leg .m{font-size:11px;color:var(--mu);font-weight:700}
  .kv .sz .leg .n{font-size:26px;font-weight:900;letter-spacing:-1px;margin-top:5px;font-variant-numeric:tabular-nums}
  .kv .sz .leg .n small{font-size:12px;color:var(--fa);font-weight:700}
  .kv .tiles{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--ln);border:1px solid var(--ln);border-radius:14px;overflow:hidden;margin-top:16px}
  .kv .tiles .t{background:#fff;padding:13px 16px;min-width:0}.kv .tiles .t .k{font-size:10px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--fa)}
  .kv .tiles .t .v{font-size:20px;font-weight:900;letter-spacing:-.6px;margin-top:3px;font-variant-numeric:tabular-nums}
  .kv .tiles .t .s{font-size:11.5px;color:var(--mu);font-weight:600;margin-top:2px}
  .kv .tiles .t.warn .v{color:var(--am)}
  .kv .run{margin-top:22px;background:var(--card);border:1px solid var(--ln);border-radius:20px;box-shadow:0 18px 60px rgba(17,24,39,.06);padding:20px 22px}
  .kv .runrow{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .kv .startbtn{flex:1;min-width:220px;border:0;border-radius:14px;padding:17px 22px;font:inherit;font-size:16px;font-weight:900;color:#fff;background:var(--btn);cursor:pointer;letter-spacing:-.2px;box-shadow:0 14px 30px rgba(15,143,69,.25)}
  .kv .startbtn.on{background:#fef2f2;color:var(--dg);box-shadow:none;border:1px solid #fecaca}
  .kv .startbtn:disabled{opacity:.6;cursor:wait}
  .kv .rstat{flex:1;min-width:220px;font-size:13px;color:var(--mu);line-height:1.5}
  .kv .rstat .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#9ca3af;margin-right:7px;vertical-align:middle}
  .kv .rstat.live .dot{background:#16a34a;box-shadow:0 0 0 3px rgba(22,163,74,.18);animation:kvp 1.6s infinite}
  @keyframes kvp{0%,100%{opacity:1}50%{opacity:.45}}
  @media (prefers-reduced-motion:reduce){.kv .rstat.live .dot{animation:none}}
  .kv .rstat b{color:var(--tx)}
  .kv .today{margin-top:14px;border-top:1px solid var(--ln);padding-top:12px;font-size:12.5px;color:var(--mu);line-height:1.5}
  .kv .today b{color:var(--tx)}
  .kv .err{margin-top:10px;color:var(--dg);font-size:12.5px;font-weight:700}
  .kv .acts{margin-top:10px;font-size:12px;color:var(--mu)}
  .kv .acts .row{display:flex;gap:10px;padding:3px 0;font-variant-numeric:tabular-nums}
  .kv .note{display:flex;gap:12px;background:var(--bg);border:1px solid var(--ln);border-left:4px solid var(--g);border-radius:14px;padding:14px 16px;margin-top:16px;font-size:12.5px;color:var(--mu);line-height:1.5}
  .kv .note b{color:var(--tx)}
  .kv .empty{background:var(--card);border:1px solid var(--ln);border-radius:18px;padding:26px;text-align:center;color:var(--mu)}
  .kv .eqwrap{display:block;margin-top:22px;background:var(--card);border:1px solid var(--ln);border-radius:20px;box-shadow:0 18px 60px rgba(17,24,39,.06);padding:20px 22px 22px}
  .kv .eqhead h2{font-size:16px;font-weight:800;letter-spacing:-.3px;margin:0}.kv .eqhead p{color:var(--mu);font-size:12px;margin:4px 0 0}
  .kv .eqchart{margin-top:14px}.kv .eqchart svg{width:100%;height:auto;display:block}
  .kv .eqtiles{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--ln);border:1px solid var(--ln);border-radius:14px;overflow:hidden;margin-top:14px}
  @media(max-width:760px){.kv .eqtiles{grid-template-columns:repeat(3,1fr)}}
  .kv .eqt{background:#fff;padding:12px 14px;min-width:0}.kv .eqt .k{font-size:9.5px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--fa)}
  .kv .eqt .v{font-size:17px;font-weight:900;letter-spacing:-.5px;margin-top:3px;font-variant-numeric:tabular-nums}
  .kv .yrs{display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:6px;margin-top:12px}
  @media(max-width:760px){.kv .yrs{grid-template-columns:repeat(5,minmax(0,1fr))}}
  .kv .yr{border:1px solid var(--ln);border-radius:10px;padding:8px;text-align:center;font-variant-numeric:tabular-nums}
  .kv .yr .y{font-size:10.5px;font-weight:800;color:var(--fa)}.kv .yr .r{font-size:13px;font-weight:900;margin-top:2px}
  .kv .pos{color:var(--gd)}.kv .neg{color:var(--dg)}
  .kv .eqnote{font-size:12.5px;color:var(--mu);line-height:1.5;margin-top:14px;background:var(--gs);border:1px solid var(--gl);border-radius:12px;padding:12px 14px}
  .kv .howhd{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--fa);margin:22px 0 10px}
  .kv .steps{display:grid;gap:8px}
  .kv .step{display:flex;gap:12px;align-items:flex-start;background:#fff;border:1px solid var(--ln);border-radius:13px;padding:12px 14px;font-size:12.5px;color:var(--mu);line-height:1.45}
  .kv .step .sn{flex:none;width:22px;height:22px;border-radius:50%;background:var(--gs);border:1px solid var(--gl);color:var(--gd);font-weight:900;font-size:11px;display:grid;place-items:center;margin-top:1px}
  .kv .step b{color:var(--tx)}
  .kv .truths{list-style:none;padding:0;margin:20px 0 0;display:grid;gap:9px}
  .kv .truths li{display:flex;gap:10px;align-items:flex-start;font-size:12.5px;color:var(--tx);line-height:1.45}
  .kv .truths li::before{content:"";flex:none;width:6px;height:6px;border-radius:50%;background:var(--g);margin-top:7px}
</style>
<div class="kv" id="kv">
  <div class="top">
    <div>
      <h1>KhomaVolume NQ</h1>
      <div class="lead">One decision a day at 10:30 ET on micro Nasdaq (MNQ), executed server-side by KhomaAPI.</div>
    </div>
    <div class="apick" id="apickWrap">
      <span class="ey">Account</span>
      <select id="acctSel" aria-label="Trading account"></select>
      <span class="envb" id="envBadge"></span>
    </div>
  </div>
  <div id="app"></div>
  <section id="eqSection" class="eqwrap"></section>
  <section class="eqwrap">
    <div class="eqhead"><h2>How your account is traded</h2><p>The exact rules the engine runs. Tested on 18 years of 1-minute NQ data (2008 to 2026) with realistic fills.</p></div>
    <div class="steps" style="margin-top:14px">
      <div class="step"><span class="sn">1</span><div><b>One market, one decision.</b> At 10:30 ET the engine checks the move from the 9:30 open. It trades that direction only when it agrees with the 20-day trend. On the other days it sits out and risks nothing.</div></div>
      <div class="step"><span class="sn">2</span><div><b>Risk-sized hard stop.</b> The stop is 0.2&times; the average daily range. Contracts are sized so a stop-out costs about your risk per trade. The stop rests at the exchange the moment the entry fills.</div></div>
      <div class="step"><span class="sn">3</span><div><b>Evaluation take-profit.</b> In an evaluation the trade closes at whichever comes first: the dollars still needed to pass, or the daily cap that keeps you inside the 50% consistency rule. Funded accounts hold to the close.</div></div>
      <div class="step"><span class="sn">4</span><div><b>Flat by 15:59 ET, never overnight.</b> News days are traded: CPI, jobs and PPI days have tested better than average.</div></div>
    </div>
    <ul class="truths">
      <li><b>A real but thin edge.</b> Profit factor about 1.16 since 2008 (1.30 over the last 10 years) with a 34% win rate. A few strong trend days pay for many small losses: without the best 5% of trades it would lose money.</li>
      <li><b>Long flat periods.</b> From 2009 to 2019 it made no new high for almost 10 years. Losing streaks of up to 19 trades have happened.</li>
      <li><b>Most evaluations fail.</b> Over the long run about 1 in 3 evaluations pass, and about 1 in 5 to 1 in 7 evaluations ends in a payout, and about half of funded accounts blow before their first payout. Budget for several evaluations, not one.</li>
      <li><b>Flat stretches happen.</b> From mid-2025 to today results have been close to breakeven, and the current-market figures above reflect that.</li>
      <li><b>Stagger accounts.</b> Accounts that start on the same day take the same trades and win or lose together. Start them a week or more apart.</li>
      <li><b>No guarantees.</b> Some evaluations fail and some funded accounts blow. The edge shows up across many accounts and months, not on any single one.</li>
    </ul>
  </section>
</div>
<script>
const ACCOUNTS=__ACCOUNTS__, CONFIGS=__CONFIGS__, DATA=__DATA__;
const SIZES=["25K","50K","100K","150K"],DDN={eod:"End-of-day trailing",intraday:"Intraday trailing",static:"Static"};
const TARGET={"25K":1250,"50K":3000,"100K":6000,"150K":9000},MLL={"25K":1000,"50K":2000,"100K":3000,"150K":4500};
const QUAL={"25K":100,"50K":150,"100K":200,"150K":250};
const EVALMAX={"25K":20,"50K":40,"100K":60,"150K":100},FUNDMAX={"25K":10,"50K":20,"100K":30,"150K":40};
const $=s=>document.querySelector(s),money=n=>(n<0?"-":"")+"$"+Math.abs(Math.round(n)).toLocaleString("en-US");
let state=null,CTX={ready:false},lastErr="";

function riskUsd(){
  if(state.mode==="eval")return DATA.presets[state.acct_size][state.dd_type][state.preset].risk;
  if(state.mode==="funded"){const f=DATA.funded[state.acct_size][state.dd_type];
    if(isSafe()&&f.safe)return state.preset==="safe2"?f.safe.risk_after:f.safe.risk;return f.risk;}
  return state.cash_bal*state.cash_dd/100/DATA.cash[cashBasis()].ddR;
}
function cashBasis(){return state.preset==="cons"?"full":"ten";}
function isSafe(){return state.mode==="funded"&&(state.preset==="safe"||state.preset==="safe2");}
function stopPts(){return CTX.ready&&CTX.stop_pts?CTX.stop_pts:null;}
function micros(){const sp=stopPts()||60,per=sp*2,r=riskUsd(),cap=state.mode==="eval"?EVALMAX[state.acct_size]:state.mode==="funded"?FUNDMAX[state.acct_size]:40;
  let q=Math.floor(r/per);if(q===0&&(per<=1.5*r||state.mode==="funded"))q=1;return Math.max(0,Math.min(q,cap));}
function seg(id,opts,val){return `<div class="seg${opts.length===4?' four':''}" data-seg="${id}" role="group">`+
  opts.map(o=>`<button type="button" data-v="${o.v}" aria-pressed="${o.v===val}">${o.t}</button>`).join("")+`</div>`;}

function safePlan(){const f=DATA.funded[state.acct_size][state.dd_type].safe;if(!f)return "";
  return state.preset==="safe"?`<b>Payout plan.</b> Request <b>$500</b> as soon as your firm allows it (LucidFlex: profit of at least $1,000 and 5 winning days of ${money(QUAL[state.acct_size])}+). Then set <b>First payout</b> to <b>Taken</b>: the engine halves its risk to protect the account.`:
  `<b>After your first payout</b> the engine risks ${money(f.risk_after)} per trade. Request <b>$1,000</b> payouts only when your balance is at least <b>${money(f.cushion)}</b> above your account floor, so one bad week cannot end the account.`;}
function configCard(){
  const cash=state.mode==="cash";
  return `<div class="card"><div class="hd"><h2>Configure</h2><p>Every number on this page comes from the validated backtest for these exact settings.</p></div>
   <div class="bd">
    <div class="field"><label>Mode</label>${seg("mode",[{v:"eval",t:"Evaluation"},{v:"funded",t:"Funded"},{v:"cash",t:"Live cash"}],state.mode)}</div>
    ${cash?`<div class="field"><label>Account balance &amp; max drawdown</label><div class="cashrow">
        <div class="f"><div class="inl"><span>$</span><input id="balIn" inputmode="numeric" aria-label="Account balance" value="${Math.round(state.cash_bal).toLocaleString("en-US")}"></div></div>
        <div class="f"><div class="rv"><span>Max drawdown</span><b id="ddOut">${state.cash_dd}%</b></div>
          <input id="ddIn" type="range" min="5" max="35" value="${state.cash_dd}" aria-label="Max drawdown percent"></div></div></div>`:
      `<div class="field"><label>Account size</label>${seg("acct_size",SIZES.map(s=>({v:s,t:"$"+s})),state.acct_size)}</div>
       <div class="field"><label>Drawdown type</label>${seg("dd_type",[{v:"eod",t:"End-of-day trailing"},{v:"intraday",t:"Intraday trailing"},{v:"static",t:"Static"}],state.dd_type)}
        <div class="pn">${state.dd_type==="eod"?"LucidFlex and most EOD plans: the floor trails your end-of-day balance and locks at start + $100.":state.dd_type==="intraday"?"The floor trails your highest open profit during the day. Harder: size is reduced to match.":"The floor never moves. Easiest drawdown: size is increased to match."}</div></div>`}
    ${cash?`<div class="field"><label>Plan your drawdown against</label>${seg("preset",[{v:"cons",t:"Full history (2008–26)"},{v:"norm",t:"Last 10 years"}],state.preset==="cons"?"cons":"norm")}
      <div class="pn">${state.preset==="cons"?"Sized so the worst drawdown since 2008 (about "+Math.round(DATA.cash.full.ddR)+" times the risk per trade, 2009–2011) fits inside your max drawdown. Much smaller size; survives a repeat of the worst years.":"Sized so the worst drawdown of the last 10 years (about "+Math.round(DATA.cash.ten.ddR)+" times the risk per trade) fits inside your max drawdown. A repeat of 2009–2011 would take you about "+Math.round(DATA.cash.full.ddR/DATA.cash.ten.ddR*state.cash_dd)+"% down."}</div></div>`:
    state.mode==="funded"?`<div class="field"><label>Funded mode</label>${seg("fmode",[{v:"std",t:"Standard"},{v:"safe",t:"Safe (recommended)"}],isSafe()?"safe":"std")}
      <div class="pn" style="margin-top:6px">${isSafe()?`<b>Safe</b> is built to get you paid and keep the account: the same 10:30 trade, but each winner is closed at <b>2&times; the stop</b> instead of held to the close, and you take a <b>small first payout early</b>.`:`<b>Standard</b> risks about 12% of the max loss per trade and holds every trade to the close. Bigger potential payouts, but about half of funded accounts blow before their first payout over the long run.`}</div></div>
      ${isSafe()?`<div class="field"><label>First payout</label>${seg("preset",[{v:"safe",t:"Not yet taken"},{v:"safe2",t:"Taken"}],state.preset)}
      <div class="pn">${safePlan()}</div></div>`:""}`:
      `<div class="field"><label>Risk preset</label>${seg("preset",[{v:"cons",t:"Conservative"},{v:"norm",t:"Normal"},{v:"aggr",t:"Aggressive"}],state.preset)}
      <div class="pn">${{cons:"Best odds. Passes in about 3–5 weeks.",norm:"Balanced. Passes in about 2–3 weeks.",aggr:"Fastest, about 2 weeks, but fails more often."}[state.preset]}</div></div>`}
   </div></div>`;
}

function projCard(){
  const r=riskUsd(),q=micros(),sp=stopPts();
  let hero,cap,meter=null,tiles=[];
  if(state.mode==="eval"){const p=DATA.presets[state.acct_size][state.dd_type][state.preset];
    const full=(p.pass_full!=null?p.pass_full:p.pass_),fd=DATA.funded[state.acct_size][state.dd_type];
    const epp=x=>Math.max(1,Math.round(100*100/Math.max(1,full*x)));
    const pf=(fd.p1_full!=null?fd.p1_full:fd.p1),pn=(fd.p1_now||pf);
    const lo=epp(Math.max(pf,pn)),hi=epp(Math.min(pf,pn));
    hero=full+"%";cap=`<b>chance to pass</b> the ${state.acct_size} evaluation (${DDN[state.dd_type].toLowerCase()}): long-run average, 2008–26, including the weak years. About ${Math.max(1,Math.round(100/Math.max(1,full)))} tries per pass.`;meter=full;
    const dd_=(p.days_full!=null?p.days_full:p.days),p75_=(p.p75_full!=null?p.p75_full:p.p75);
    tiles=[["Time to pass",dd_.toFixed(0)+" trading days","median when it passes; 3 in 4 within "+p75_.toFixed(0)],
           ["Good markets",p.pass_+"%","2020–26 starts; not a promise",""],
           ["Current market",p.pass_now+"%","starts since Jul 2025","warn"],
           ["Evaluations per payout",(lo===hi?lo:lo+"–"+hi),"from first evaluation to first cash","warn"]];
  }else if(isSafe()&&DATA.funded[state.acct_size][state.dd_type].safe){const f=DATA.funded[state.acct_size][state.dd_type].safe;
    hero=f.paid_full+"%";cap="<b>reach at least one payout</b> within a year of being funded, Safe mode: long-run average, 2008–25 starts, including the weak years";meter=f.paid_full;
    tiles=[["First payout",(f.med_full||0).toFixed(0)+" trading days","median, when paid; "+f.p120_full+"% are paid within 6 months"],
           ["Blown before payout",f.bb_full+"%","long run (Standard: "+DATA.funded[state.acct_size][state.dd_type].bb_full+"%)","warn"],
           ["Year-1 payouts",money(f.cash_full),"average per funded account, after the 90/10 split"],
           ["Good / current market",f.paid_good+"% / "+f.paid_now+"%","reach a payout: 2020–25 starts / 2025 starts",""]];
  }else if(state.mode==="funded"){const f=DATA.funded[state.acct_size][state.dd_type];
    const full=f.p1_full!=null;
    hero=(full?f.p1_full:f.p1)+"%";cap="<b>reach at least one payout</b> within a year of being funded: long-run average, 2008–25 starts, including the weak years";meter=full?f.p1_full:f.p1;
    tiles=[["Blown before payout",(full?f.bb_full:f.bb)+"%","within the first year, long run","warn"],
           ["First payout",((full?f.first_full:f.first)||0).toFixed(0)+" trading days","median, when paid"],
           ["Year-1 payouts",money(full?f.cash_full:f.cash),"average per funded account, after the 90/10 split"],
           ["Good / current market",f.p1+"% / "+f.p1_now+"%","reach a payout: 2020–25 starts / since Oct 2025",""]];
  }else{const c=DATA.cash[cashBasis()],cf=DATA.cash.full,sp_=(stopPts()||61)*2,ar=q>0?q*sp_:0;
    const need=Math.ceil((sp_/1.5)*c.ddR/(state.cash_dd/100)/1000)*1000;
    if(q<=0){hero="Too small";cap=`<b>one MNQ micro risks about ${money(sp_)}</b>, more than this drawdown plan allows. The engine will not trade. You need about <b>${money(need)}</b> at ${state.cash_dd}% max drawdown, or a larger max drawdown.`;
      tiles=[["Drawdown budget",money(state.cash_bal*state.cash_dd/100),state.cash_dd+"% of "+money(state.cash_bal)],["Minimum balance",money(need),"for 1 micro on this plan","warn"]];}
    else{hero=money(c.perMonthR*ar);cap=`<b>average per month</b> at ${q} micro (about ${money(ar)} risk per trade), based on ${cashBasis()==="full"?"2008–2026, including the flat decade":"the last 10 years"}. It is an average, not a monthly income: about ${c.loseM}% of months lose money.`;
      tiles=[["Typical month",money(c.p50*ar),"range "+money(c.p10*ar)+" to "+money(c.p90*ar)+" (8 in 10 months)"],
             ["Worst drawdown",money(-c.ddR*ar),Math.round(c.ddR*ar/state.cash_bal*100)+"% of the account, "+(cashBasis()==="full"?"since 2008":"last 10 years"),"warn"],
             ["Worst 12 months",money(c.worst12R*ar),"last 12 months: "+money(c.last12R*ar),"warn"],
             ["If 2009–2011 repeats",money(-cf.ddR*ar),Math.round(cf.ddR*ar/state.cash_bal*100)+"% of the account; almost 10 years with no new high","warn"]];}}
  return `<div class="card"><div class="hd"><h2>${state.mode==="eval"?"Evaluation odds":state.mode==="funded"?"Funded outcome":"Live cash: what to expect"}</h2><p>Real 1-minute NQ data with Lucid commissions and slippage.</p></div>
   <div class="bd"><div class="hero"><div class="big">${hero}</div><div class="cap">${cap}</div></div>
   ${meter!=null?`<div class="meter"><i style="width:${meter}%"></i></div>`:""}
   <div class="sz"><div class="t">What the engine will trade</div><div class="row">
     <div class="leg"><div class="m">Risk per trade</div><div class="n">${money(r)}</div></div>
     <div class="leg"><div class="m">MNQ contracts ${sp?"today":"(typical day)"}</div><div class="n">${q}<small> micro</small></div></div>
     <div class="leg"><div class="m">Stop distance</div><div class="n">${sp?sp.toFixed(0):"~60"}<small> pts</small></div></div></div></div>
   <div class="tiles">${tiles.map(t=>`<div class="t ${t[3]||""}"><div class="k">${t[0]}</div><div class="v">${t[1]}</div><div class="s">${t[2]}</div></div>`).join("")}</div>
   </div></div>`;
}

function todayLine(){
  if(!CTX.ready)return "Today's market context (20-day trend and volatility) loads from the data feed before 10:30 ET.";
  const dir=CTX.trend>0?"up":"down",side=CTX.trend>0?"long":"short",cmp=CTX.trend>0?"above":"below";
  return `<b>Today:</b> the 20-day trend is <b>${dir}</b>. At 10:30 ET the engine takes a <b>${side}</b> only if NQ is ${cmp} its 9:30 open; otherwise it sits out. Stop ≈ <b>${CTX.stop_pts} pts</b>.`;
}
function runCard(){
  const on=state.enabled;
  return `<div class="run"><div class="runrow">
     <button type="button" class="startbtn ${on?'on':''}" id="startBtn">${on?'Stop automation':'Start automation'}</button>
     <div class="rstat ${on?'live':''}"><span class="dot"></span>${on?'<b>Armed</b>: the engine will trade this account at 10:30 ET on qualifying days and flatten by 15:59 ET.':'Not armed. Press Start and the engine trades this account by the rules above from the next 10:30 ET decision.'}</div>
   </div><div class="today" id="today">${todayLine()}</div>${lastErr?`<div class="err">${lastErr}</div>`:""}<div class="acts" id="acts" hidden></div></div>
   <div class="note"><div><b>Start on a demo or a single evaluation first.</b> The strategy runs server-side inside KhomaAPI and never leaves it. Copy Trading boxes use this account's settings for every account in the box.</div></div>`;
}

function render(){
  if(!ACCOUNTS.length){$("#apickWrap").style.display="none";
    $("#app").innerHTML='<div class="empty">No connected Tradovate account yet. Connect one under <a href="/broker">Broker</a>, then come back here.</div>';renderEquity();return;}
  $("#app").innerHTML=`<div class="grid">${configCard()}${projCard()}</div>${runCard()}`;
  const acc=ACCOUNTS.find(a=>a.id===state.acct);
  $("#envBadge").textContent=(acc.env||"live").toUpperCase();
  $("#envBadge").style.color=acc.env==="demo"?"#0f8f45":"#dc2626";$("#envBadge").style.borderColor=acc.env==="demo"?"#0f8f45":"#dc2626";
  document.querySelectorAll("[data-seg]").forEach(sg=>sg.querySelectorAll("button").forEach(b=>b.onclick=()=>{const k=sg.dataset.seg,v=b.dataset.v;
    if(k==="fmode")state.preset=v==="safe"?"safe":"norm";else state[k]=v;
    if(k==="mode"&&v==="funded"&&!isSafe())state.preset="safe";   // Safe is the recommended funded default
    if(state.mode!=="funded"&&(state.preset==="safe"||state.preset==="safe2"))state.preset="norm";
    lastErr="";render();}));
  const bi=$("#balIn");if(bi)bi.onchange=e=>{state.cash_bal=Math.max(1000,+e.target.value.replace(/[^0-9]/g,"")||25000);render();};
  const di=$("#ddIn");if(di)di.oninput=e=>{state.cash_dd=+e.target.value;$("#ddOut").textContent=state.cash_dd+"%";};
  if(di)di.onchange=()=>render();
  $("#startBtn").onclick=toggleRun;
  renderEquity();
}

async function save(enabled){
  const b=new URLSearchParams({account_id:state.acct,enabled:enabled?"1":"0",mode:state.mode,acct_size:state.acct_size,
    preset:state.preset,asset:"nq",dd_type:state.dd_type,cash_bal:String(Math.round(state.cash_bal)),cash_dd:String(state.cash_dd)});
  try{const r=await fetch("/prop-engine/save",{method:"POST",body:b});const j=await r.json().catch(()=>null);return j||{ok:false,error:"HTTP "+r.status};}
  catch(e){return {ok:false,error:"network error"};}
}
async function toggleRun(){
  const btn=$("#startBtn");btn.disabled=true;btn.textContent=state.enabled?"Stopping…":"Starting…";
  const want=!state.enabled;const j=await save(want);
  if(j&&j.ok){state.enabled=want;lastErr="";CONFIGS[state.acct]=Object.assign(CONFIGS[state.acct]||{},{enabled:want,mode:state.mode,acct_size:state.acct_size,preset:state.preset,dd_type:state.dd_type,cash_bal:state.cash_bal,cash_dd:state.cash_dd});}
  else lastErr="Could not update automation: "+((j&&j.error)||"unknown error")+". Check the account is still connected under Broker.";
  render();
}
function loadAcct(id){
  const c=CONFIGS[id]||{};
  state={acct:id,mode:c.mode||"eval",acct_size:c.acct_size||"50K",preset:c.preset||"norm",dd_type:c.dd_type||"eod",
    cash_bal:+c.cash_bal||25000,cash_dd:+c.cash_dd||15,enabled:!!c.enabled};
  render();
}
function poll(){
  fetch("/prop-engine/status").then(r=>r.json()).then(s=>{
    const before=JSON.stringify(CTX);CTX=s.context||CTX;
    if(JSON.stringify(CTX)!==before&&state){render();return;}
    const acts=$("#acts");if(!acts)return;
    const list=((s.engine&&s.engine.actions)||[]).filter(a=>a.action&&a.action!=="tick");
    if(state&&state.enabled&&list.length){acts.hidden=false;
      acts.innerHTML="<div style='color:var(--fa);font-weight:800'>Recent engine activity</div>"+list.slice(-6).reverse().map(a=>`<div class="row"><b>${a.root||""}</b> ${a.action}${a.side?" "+a.side:""}${a.qty?" ×"+a.qty:""}${a.reason?" ("+a.reason+")":""}</div>`).join("");}
    else acts.hidden=true;
  }).catch(()=>{});
}
function renderEquity(){
  const eq=DATA.equity,r=state?riskUsd():500,c=eq.cumR.map(x=>x*r),W=780,H=210,L=52,R=12,T=14,B=22,n=c.length;
  const mx=Math.max(...c,0),mn=Math.min(...c,0),x=i=>L+i/(n-1)*(W-L-R),y=v=>T+(1-(v-mn)/((mx-mn)||1))*(H-T-B);
  let ln="M"+x(0).toFixed(1)+" "+y(c[0]).toFixed(1);c.forEach((v,i)=>{if(i)ln+=" L"+x(i).toFixed(1)+" "+y(v).toFixed(1);});
  const ar=ln+" L"+x(n-1).toFixed(1)+" "+y(0).toFixed(1)+" L"+x(0).toFixed(1)+" "+y(0).toFixed(1)+" Z";
  const yt=eq.months.map((m,i)=>[i,m]).filter(([i,m])=>m.endsWith("-01")).map(([i,m])=>`<text x="${x(i).toFixed(1)}" y="${H-6}" text-anchor="middle" font-size="10" font-weight="700" fill="#9ca3af">${m.slice(0,4)}</text>`).join("");
  const grid=`<line x1="${L}" x2="${W-R}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" stroke="#e5e7eb"/><text x="${L-6}" y="${(y(0)+3).toFixed(1)}" text-anchor="end" font-size="10" font-weight="700" fill="#9ca3af">$0</text><text x="${L-6}" y="${(y(mx)+9).toFixed(1)}" text-anchor="end" font-size="10" font-weight="700" fill="#9ca3af">${money(mx/1000)}k</text>`;
  const tiles=[["Net, 18 years",money(eq.totalR*r)],["Profit factor",eq.pf],["Win rate",eq.win+"%"],["Worst drawdown",money(eq.maxddR*r)],["Trades",eq.n.toLocaleString("en-US")],["Risk per trade",money(r)]];
  const yrs=Object.entries(eq.years).map(([y2,v])=>`<div class="yr"><div class="y">${y2}${y2==="2026"?" YTD":""}</div><div class="r ${v.r>=0?"pos":"neg"}">${money(v.r*r)}</div></div>`).join("");
  $("#eqSection").innerHTML=`<div class="eqhead"><h2>Full-history equity curve</h2><p>2008 to Oct 2026 · the exact rule the engine trades today · real 1-minute NQ data · Lucid commissions and slippage · scaled to your risk of ${money(r)} per trade</p></div>
    <div class="eqchart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Equity curve">
      <defs><linearGradient id="eqg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#16a34a" stop-opacity=".16"/><stop offset="1" stop-color="#16a34a" stop-opacity="0"/></linearGradient></defs>
      ${grid}<path d="${ar}" fill="url(#eqg)"/><path d="${ln}" fill="none" stroke="#0f8f45" stroke-width="2.2" stroke-linejoin="round"/>
      <circle cx="${x(n-1).toFixed(1)}" cy="${y(c[n-1]).toFixed(1)}" r="4.5" fill="#0f8f45" stroke="#fff" stroke-width="2"/>${yt}</svg></div>
    <div class="eqtiles">${tiles.map(t=>`<div class="eqt"><div class="k">${t[0]}</div><div class="v">${t[1]}</div></div>`).join("")}</div>
    <div class="yrs">${yrs}</div>
    <div class="eqnote">This is the raw strategy at a constant ${money(r)} risk per trade since 2008, not an account balance. <b>Look at 2009 to 2019: the strategy made no new high for almost 10 years</b>, and its worst drawdown was about ${Math.round(-eq.maxddR)} times the risk per trade. Most of the profit came in strong trend years (2008, 2018, 2022 to 2025). Flat stretches that long can happen again. A prop account stops at its target, its drawdown limit or its payout cap long before this, so use the odds above for what an account can do. Past results do not guarantee future results; 2026 so far is close to breakeven.</div>`;
}
const sel=$("#acctSel");
ACCOUNTS.forEach(a=>{const o=document.createElement("option");o.value=a.id;o.textContent=a.name+"  ("+(a.env||"live")+")";sel.appendChild(o);});
sel.onchange=()=>{lastErr="";loadAcct(sel.value);};
if(ACCOUNTS.length){loadAcct(ACCOUNTS[0].id);setInterval(poll,5000);poll();}else render();
</script>
"""
