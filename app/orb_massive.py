"""KhomaVolume market-data feed via Massive (formerly Polygon.io) — ONE feed for the whole
business. It streams real-time 1-minute futures bars over Massive's WebSocket and hands them to
the engine as app.orb_selective.Bar objects. The engine runs this feed ONCE on the server; the
resulting signals are executed to every armed account. Clients never need their own data.

Massive protocol (docs verified 2026-09):
  real-time : wss://socket.massive.com/futures    (delayed: wss://delayed.massive.com/futures)
  auth      : {"action":"auth","params":"<API_KEY>"}    -> [{"ev":"status","status":"auth_success"}]
  subscribe : {"action":"subscribe","params":"AM.NQZ6,AM.RTYZ6"}   (AM = aggregate-per-minute)
  bar (AM)  : {"ev":"AM","sym":"NQZ6","o","h","l","c","v","s":<start_ms>,"e":<end_ms>, ...}

We subscribe to the FULL NQ + RTY contracts (deep, clean volume — matching the backtest) and
map each to its execution root (NQ data -> trade MNQ micro; RTY data -> M2K); prices are
identical between the full and micro contracts, so the opening-range levels transfer directly.
Requires a Massive "Futures Advanced" plan (real-time) API key in MASSIVE_API_KEY.

Pure parsing (am_to_bar / parse_frames) is unit-tested; the async client is a thin shell.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover
    _ET = timezone.utc

from app.orb_selective import Bar

WS_REALTIME = "wss://socket.massive.com/futures"
WS_DELAYED = "wss://delayed.massive.com/futures"

# Signal is computed on the FULL contract's data (deep volume), executed on the micro.
_DATA_ROOT = {"MNQ": "NQ", "M2K": "RTY"}


def front_ticker(data_root: str, today=None) -> str:
    """Front-month Globex ticker for a full-contract root (e.g. NQ -> 'NQZ6'), using the SAME
    quarterly roll as the executor's Tradovate contract so data and execution stay on one month."""
    from app.tradovate_oauth import _front_quarter, _MONTH_CODE
    if today is None:
        today = datetime.now(_ET).date()
    y, m = _front_quarter(today)
    return f"{data_root}{_MONTH_CODE[m]}{y % 10}"


def subscriptions_for(exec_roots, today=None) -> Dict[str, str]:
    """{massive_data_ticker: exec_root} for a set of execution roots (MNQ/M2K)."""
    out: Dict[str, str] = {}
    for er in exec_roots:
        out[front_ticker(_DATA_ROOT.get(er, er), today)] = er
    return out


REST_BASE = "https://api.massive.com"


def fetch_today_bars(api_key: str, exec_root: str, now=None) -> List[Bar]:
    """Backfill: today's 1-minute bars (from ~09:00 ET) for the exec_root's data contract, via
    Massive REST. Lets the engine reconstruct the opening range after a mid-session restart so a
    deploy/restart never blinds it. Returns ET Bars ascending; [] on any failure (never raises)."""
    import requests
    now = now or datetime.now(_ET)
    ticker = front_ticker(_DATA_ROOT.get(exec_root, exec_root), now.date())
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)   # 09:00 ET today
    try:
        r = requests.get(f"{REST_BASE}/futures/v1/aggs/{ticker}",
                         params={"resolution": "1min", "window_start.gte": int(start.timestamp() * 1e9),
                                 "limit": 600, "apiKey": api_key}, timeout=20)
        results = (r.json() or {}).get("results") or []
    except Exception as e:
        print("ORB MASSIVE backfill error:", e)
        return []
    out: List[Bar] = []
    for a in results:
        ws = a.get("window_start")
        if ws is None:
            continue
        dt = datetime.fromtimestamp(int(ws) / 1e9, tz=timezone.utc).astimezone(_ET)
        out.append(Bar(dt, float(a["open"]), float(a["high"]), float(a["low"]),
                       float(a["close"]), float(a.get("volume", 0) or 0)))
    out.sort(key=lambda b: b.dt)
    return out


def am_to_bar(msg: dict) -> Optional[Bar]:
    """Convert one Massive AM (aggregate-per-minute) message into a completed ET Bar. Uses the
    window START timestamp `s` (Unix ms) as the bar's open time, converted to America/New_York
    so Bar.minute_of_day lines up with the 09:30 cash open. None if not an AM message."""
    if (msg or {}).get("ev") != "AM":
        return None
    s = msg.get("s")
    if s is None:
        return None
    dt = datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc).astimezone(_ET)
    return Bar(dt, float(msg["o"]), float(msg["h"]), float(msg["l"]), float(msg["c"]),
               float(msg.get("v", 0) or 0))


def parse_frames(raw) -> List[dict]:
    """Massive sends a JSON array of event objects per frame. Returns the list (or [])."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return [data] if isinstance(data, dict) else []


def status_of(frames: List[dict]) -> Optional[str]:
    """The 'status' value of a status frame in this batch, if any (connected/auth_success/...)."""
    for f in frames:
        if f.get("ev") == "status":
            return f.get("status")
    return None


class MassiveFeed:
    """Async Massive futures websocket client. Subscribes to per-minute aggregates for the
    configured tickers and calls on_bar(root, Bar) for each completed bar. `subscriptions` maps
    a Massive ticker to the engine's execution root, e.g. {"NQZ6": "MNQ", "RTYZ6": "M2K"}.
    One instance is shared by the whole engine."""

    _HEARTBEAT_SEC = 15.0   # keepalive ping (Massive tolerates idle but we stay warm)

    def __init__(self, get_key: Callable[[], Optional[str]], subscriptions: Dict[str, str],
                 on_bar: Callable[[str, Bar], None], real_time: bool = True):
        self._get_key = get_key
        self._subs = dict(subscriptions)                 # ticker -> root
        self._on_bar = on_bar
        self._url = WS_REALTIME if real_time else WS_DELAYED
        self._running = False

    def _dispatch(self, frames: List[dict]) -> None:
        for msg in frames:
            if msg.get("ev") != "AM":
                continue
            root = self._subs.get(msg.get("sym"))
            if root is None:
                continue
            bar = am_to_bar(msg)
            if bar is None:
                continue
            try:
                self._on_bar(root, bar)
            except Exception as e:                        # a bad callback must not kill the feed
                print("ORB MASSIVE on_bar error:", e)

    async def run(self) -> None:
        """Connect, auth, subscribe, stream — reconnecting with backoff on any drop so the one
        shared feed never silently dies."""
        import asyncio
        import websockets

        self._running = True
        backoff = 1.0
        while self._running:
            try:
                key = self._get_key()
                if not key:
                    raise RuntimeError("no MASSIVE_API_KEY set")
                async with websockets.connect(self._url, open_timeout=15, ping_interval=20,
                                              max_size=2 ** 22) as ws:
                    await self._session(ws, key)
                    backoff = 1.0
            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception as e:                        # pragma: no cover - network
                print("ORB MASSIVE: reconnect after error:", e)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

    async def _session(self, ws, key: str) -> None:
        # 1) wait for the "connected" status, 2) auth, 3) wait for auth_success, 4) subscribe
        await self._await_status(ws, "connected")
        await ws.send(json.dumps({"action": "auth", "params": key}))
        await self._await_status(ws, "auth_success")
        params = ",".join(f"AM.{t}" for t in self._subs)
        await ws.send(json.dumps({"action": "subscribe", "params": params}))
        print(f"ORB MASSIVE: subscribed {params}")
        async for raw in ws:
            self._dispatch(parse_frames(raw))

    async def _await_status(self, ws, want: str, tries: int = 6) -> None:
        for _ in range(tries):
            frames = parse_frames(await ws.recv())
            st = status_of(frames)
            if st == want:
                return
            # data frames may interleave before the status we want — dispatch and keep reading
            self._dispatch(frames)
        raise RuntimeError(f"did not receive status '{want}'")

    def stop(self) -> None:
        self._running = False
