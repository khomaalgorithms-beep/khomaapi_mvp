"""KhomaVolume live market-data feed — real-time 1-minute OHLCV bars for the autopilot.

This is the "data feed updating every millisecond and not delayed" the engine runs on. It
talks to Tradovate's market-data WebSocket (wss://md.tradovateapi.com/v1/websocket) and turns
the broker's real-time chart stream into completed 1-minute `Bar`s (app.orb_selective.Bar),
in America/New_York time, which is exactly what find_signal() consumes.

REQUIRES a Tradovate real-time CME market-data subscription on the account whose token is used
(see the subscription note the operator was given). Without it Tradovate returns delayed or
empty data and the autopilot correctly sees no signals.

Design: ALL bar semantics live in small PURE helpers (MinuteAggregator, BarStreamTracker,
frame/packet parsing) that are unit-tested with no network. `TradovateMarketData` is a thin
async shell over them — connect, authorize, subscribe, heartbeat, parse, emit. Nothing here
places orders; the feed only produces bars. Execution lives in app.orb_live.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

try:                                   # ET conversion; stdlib on 3.9+
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                      # pragma: no cover - tzdata missing
    _ET = timezone.utc

from app.orb_selective import Bar

MD_WS_URL = "wss://md.tradovateapi.com/v1/websocket"          # live real-time feed
MD_WS_URL_DEMO = "wss://md-demo.tradovateapi.com/v1/websocket"  # demo / sim feed


def _minute_floor(dt: datetime) -> datetime:
    """Start-of-minute (seconds/micros zeroed), timezone preserved."""
    return dt.replace(second=0, microsecond=0)


def _to_et(dt: datetime) -> datetime:
    """Any aware (or naive-UTC) datetime -> America/New_York, so Bar.minute_of_day is ET."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET)


def parse_iso(ts: str) -> datetime:
    """Tradovate bar timestamps are UTC ISO-8601 ('...Z'). Parse to an aware UTC datetime."""
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# PURE: local trade->bar aggregation (fallback / tick feeds that carry no bars)
# ---------------------------------------------------------------------------
@dataclass
class MinuteAggregator:
    """Fold a stream of trade prints into completed 1-minute OHLCV bars (ET). `add()` returns
    the just-COMPLETED prior bar when a print crosses a minute boundary, else None. Call
    `flush()` to force-close the bar in progress (e.g. at EOD). Deterministic, no clock reads —
    every decision comes from the print's own timestamp, so it is fully unit-testable."""
    _cur: Optional[Bar] = None
    _minute: Optional[datetime] = None

    def add(self, ts: datetime, price: float, size: float = 0.0) -> Optional[Bar]:
        et = _to_et(ts)
        m = _minute_floor(et)
        completed = None
        if self._minute is None:
            self._start(m, price, size)
        elif m > self._minute:
            completed = self._cur
            self._start(m, price, size)
        elif m == self._minute:
            b = self._cur
            b.h = max(b.h, price)
            b.l = min(b.l, price)
            b.c = price
            b.v += size
        # a print stamped BEFORE the current minute is out-of-order -> ignore (don't rewrite history)
        return completed

    def _start(self, minute: datetime, price: float, size: float) -> None:
        self._minute = minute
        self._cur = Bar(minute, price, price, price, price, size)

    def flush(self) -> Optional[Bar]:
        b, self._cur, self._minute = self._cur, None, None
        return b


# ---------------------------------------------------------------------------
# PURE: completion-by-supersession for a broker chart stream
# ---------------------------------------------------------------------------
@dataclass
class BarStreamTracker:
    """Tradovate streams the CURRENT minute's chart bar repeatedly as it forms, then begins a
    new one. A bar is only final once the next minute's bar appears. This tracks the latest
    (still-forming) bar per contract and, on receiving a bar for a newer minute, returns the
    previous bar as COMPLETED. Historical backfill (many bars at once) is handled by update_many.
    Pure: completion is decided purely by the timestamps seen, never by wall-clock."""
    _last: Dict[str, Bar] = field(default_factory=dict)

    def update(self, contract: str, bar: Bar) -> Optional[Bar]:
        prev = self._last.get(contract)
        if prev is not None and bar.dt > prev.dt:
            self._last[contract] = bar
            return prev                       # the prior minute just closed
        if prev is None or bar.dt == prev.dt:
            self._last[contract] = bar        # first, or an in-place update of the forming bar
        # bar.dt < prev.dt -> stale/out-of-order, ignore
        return None

    def update_many(self, contract: str, bars: List[Bar]) -> List[Bar]:
        """Feed a batch (e.g. historical backfill sorted ascending). Returns every bar that
        became complete (all but the last, which remains the forming bar)."""
        out: List[Bar] = []
        for b in sorted(bars, key=lambda x: x.dt):
            c = self.update(contract, b)
            if c is not None:
                out.append(c)
        return out

    def forming(self, contract: str) -> Optional[Bar]:
        return self._last.get(contract)


# ---------------------------------------------------------------------------
# PURE: Tradovate WebSocket frame + chart-packet parsing
# ---------------------------------------------------------------------------
def parse_frames(raw: str) -> List[Tuple[str, object]]:
    """Tradovate's WS uses single-letter frames: 'o' open, 'h' heartbeat, 'c' close, and
    'a[...]' carrying a JSON array of messages. Returns [(kind, payload)] where kind is
    'open'|'heartbeat'|'close'|'data'; data payload is the decoded list of messages."""
    if not raw:
        return []
    kind = raw[0]
    if kind == "o":
        return [("open", None)]
    if kind == "h":
        return [("heartbeat", None)]
    if kind == "c":
        return [("close", None)]
    if kind == "a":
        try:
            return [("data", json.loads(raw[1:]))]
        except json.JSONDecodeError:
            return []
    return []


def chart_bars(msg: dict, tz_bars: bool = True) -> Dict[int, List[Bar]]:
    """Extract OHLCV bars from a Tradovate 'chart' data message, keyed by the chart's
    subscription id. Tradovate chart bar fields: timestamp (UTC ISO), open/high/low/close,
    and up/down volume. Total volume = upVolume + downVolume (tick counts are ignored).
    Returns {chartId: [Bar,...]} (empty for non-chart / end-of-history packets)."""
    if (msg or {}).get("e") != "chart":
        return {}
    out: Dict[int, List[Bar]] = {}
    for chart in (msg.get("d", {}) or {}).get("charts", []) or []:
        cid = chart.get("id")
        bars: List[Bar] = []
        for b in chart.get("bars", []) or []:
            ts = b.get("timestamp")
            if ts is None:
                continue
            dt = parse_iso(ts)
            if tz_bars:
                dt = _to_et(dt)
            vol = float(b.get("upVolume", 0) or 0) + float(b.get("downVolume", 0) or 0)
            if not vol and b.get("volume") is not None:
                vol = float(b.get("volume") or 0)
            bars.append(Bar(dt, float(b["open"]), float(b["high"]),
                            float(b["low"]), float(b["close"]), vol))
        if cid is not None:
            out[cid] = bars
    return out


# ---------------------------------------------------------------------------
# LIVE: the async WebSocket client (thin shell over the pure helpers above)
# ---------------------------------------------------------------------------
class TradovateMarketData:
    """Minimal async Tradovate market-data client. Subscribes to 1-minute chart bars for a set
    of contracts and invokes on_bar(contract, Bar) for every COMPLETED bar (ET). Requires the
    real-time CME market-data subscription on the token's account.

    Usage (inside the app's event loop):
        md = TradovateMarketData(get_token=lambda: ensure_fresh_token(acct),
                                 contracts=["NQZ5", "RTYZ5"], on_bar=autopilot.on_bar, env="demo")
        await md.run()     # reconnects on drop; cancel the task to stop
    """
    _HEARTBEAT_SEC = 2.5   # Tradovate drops the socket if no frame is sent within ~5s

    def __init__(self, get_token: Callable[[], Optional[str]], contracts: List[str],
                 on_bar: Callable[[str, Bar], None], env: str = "live",
                 history_bars: int = 60):
        self._get_token = get_token
        self._contracts = list(contracts)
        self._on_bar = on_bar
        self._url = MD_WS_URL_DEMO if env == "demo" else MD_WS_URL
        self._history_bars = history_bars
        self._tracker = BarStreamTracker()
        self._chart_to_contract: Dict[int, str] = {}
        self._req = 0
        self._running = False

    def _next_id(self) -> int:
        self._req += 1
        return self._req

    def _frame(self, endpoint: str, body: Optional[dict] = None) -> str:
        """Tradovate request frame: '<endpoint>\\n<id>\\n\\n<json-body>'."""
        rid = self._next_id()
        payload = json.dumps(body) if body is not None else ""
        return f"{endpoint}\n{rid}\n\n{payload}"

    async def run(self) -> None:
        """Connect + maintain the feed until cancelled. Reconnects with backoff on any drop so
        a transient network blip never silently stops the autopilot's data."""
        import asyncio
        import websockets

        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(self._url, open_timeout=15,
                                              ping_interval=None, max_size=2 ** 22) as ws:
                    await self._session(ws)
                    backoff = 1.0
            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception as e:   # pragma: no cover - network
                print("ORB FEED: reconnect after error:", e)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

    async def _session(self, ws) -> None:
        import asyncio

        token = self._get_token()
        if not token:
            raise RuntimeError("no market-data token (reconnect account)")
        # 1) wait for the open frame, then authorize
        await self._pump_until_open(ws)
        await ws.send(self._frame("authorize", None).rsplit("\n\n", 1)[0] + "\n\n" + token)
        # 2) subscribe to a 1-minute chart for each contract (historical backfill + live)
        for sym in self._contracts:
            body = {
                "symbol": sym,
                "chartDescription": {
                    "underlyingType": "MinuteBar",
                    "elementSize": 1,
                    "elementSizeUnit": "UnderlyingUnits",
                },
                "timeRange": {"asMuchAsElements": self._history_bars},
            }
            frame = self._frame("md/getChart", body)
            # remember which chart id maps to which contract once the server echoes it
            await ws.send(frame)

        hb = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                self._handle_raw(raw)
        finally:
            hb.cancel()

    async def _pump_until_open(self, ws) -> None:
        raw = await ws.recv()
        for kind, _ in parse_frames(raw):
            if kind == "open":
                return
        # some stacks deliver the open frame slightly later; tolerate one extra read
        raw = await ws.recv()

    async def _heartbeat(self, ws) -> None:
        import asyncio
        try:
            while True:
                await asyncio.sleep(self._HEARTBEAT_SEC)
                await ws.send("[]")
        except asyncio.CancelledError:
            pass

    def _handle_raw(self, raw: str) -> None:
        for kind, payload in parse_frames(raw):
            if kind != "data" or not isinstance(payload, list):
                continue
            for msg in payload:
                self._handle_msg(msg)

    def _handle_msg(self, msg: dict) -> None:
        # learn chartId -> contract from the subscribe response ({"s":200,"i":<reqId>,"d":{...}})
        # (the chart 'e' packets only carry the chart id, so we map via the per-contract charts)
        for cid, bars in chart_bars(msg).items():
            contract = self._chart_to_contract.get(cid)
            if contract is None:
                # first chart packet for this id: bind it to the next unbound contract in order
                bound = set(self._chart_to_contract.values())
                for sym in self._contracts:
                    if sym not in bound:
                        self._chart_to_contract[cid] = contract = sym
                        break
            if contract is None:
                continue
            for done in self._tracker.update_many(contract, bars):
                try:
                    self._on_bar(contract, done)
                except Exception as e:   # a bad callback must not kill the feed
                    print("ORB FEED on_bar error:", e)

    def stop(self) -> None:
        self._running = False
