"""KhomaVolume live engine runtime — wires feeds to autopilots and runs the poll loop.

This is the single object main.py starts (leader-gated, like orb_engine_loop): it owns one
Autopilot + one market-data feed per connected account, routes each completed bar from the feed
to the right autopilot, and reconciles every account against the broker on a fixed cadence.

Kept deliberately thin so the main.py wiring is a few lines and the routing/poll logic stays
unit-testable without a live socket. Nothing here decides trades — that's app.orb_live.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from app.orb_selective import Bar
from app.orb_live import Autopilot, AccountConfig, INSTRUMENTS, DailyATR
from app.orb_feed import TradovateMarketData


def symbol_to_root(symbol: str) -> Optional[str]:
    """Map a resolved Tradovate contract symbol (e.g. 'MNQZ5', 'M2KH6') back to its instrument
    root ('MNQ' / 'M2K'). Longest known root that the symbol starts with wins."""
    s = str(symbol).upper()
    for root in sorted(INSTRUMENTS, key=len, reverse=True):
        if s.startswith(root):
            return root
    return None


def make_bar_router(autopilot: Autopilot, symbols: Dict[str, str],
                    sink: Optional[Callable[[str, Bar, dict], None]] = None):
    """Build the on_bar callback the feed calls. The feed emits (contract_symbol, bar); we
    translate the symbol to its root and hand it to the autopilot. `sink`, if given, receives
    (root, bar, action) for journaling (e.g. logging observe-only RTY signals)."""
    sym_to_root = {sym: symbol_to_root(sym) for sym in symbols.values()}

    def on_bar(contract_symbol: str, bar: Bar) -> None:
        root = sym_to_root.get(contract_symbol) or symbol_to_root(contract_symbol)
        if root is None:
            return
        action = autopilot.on_bar(root, bar)
        if sink is not None and action is not None:
            try:
                sink(root, bar, action)
            except Exception:
                pass
    return on_bar


def build_account_engine(cfg: AccountConfig, get_token: Callable[[], Optional[str]],
                         broker=None, atr: Optional[Dict[str, DailyATR]] = None,
                         now_et: Callable = None,
                         sink: Optional[Callable[[str, Bar, dict], None]] = None):
    """Create the (Autopilot, TradovateMarketData) pair for one account, already wired so the
    feed's completed bars flow into the autopilot. The feed subscribes to the account's
    resolved contract symbols; the poll loop is driven by LiveEngine."""
    ap = Autopilot(cfg, get_token=get_token, broker=broker, atr=atr, now_et=now_et)
    on_bar = make_bar_router(ap, cfg.instruments, sink=sink)
    feed = TradovateMarketData(get_token=get_token, contracts=list(cfg.instruments.values()),
                               on_bar=on_bar, env=cfg.env)
    return ap, feed


class LiveEngine:
    """Owns the autopilots + feeds for every connected account and runs the reconcile loop.
    main.py does: `engine = LiveEngine(pairs); asyncio.create_task(engine.run())` behind the
    leader lock, and `engine.stop()` on shutdown."""

    def __init__(self, autopilots: List[Autopilot], feeds: List[TradovateMarketData],
                 poll_interval: float = 3.0):
        self.autopilots = list(autopilots)
        self.feeds = list(feeds)
        self.poll_interval = poll_interval
        self._running = False

    def poll_once(self) -> List[dict]:
        """One reconcile pass across every account (broker-truth fills/closes/EOD). Returns the
        flattened list of actions taken — safe to call on a timer; pure of any sleeping."""
        out: List[dict] = []
        for ap in self.autopilots:
            try:
                out.extend(ap.poll())
            except Exception as e:                 # one account's error must not stall the rest
                out.append({"error": str(e)})
        return out

    async def run(self) -> None:
        """Start every feed and reconcile on the poll cadence until stop()/cancel. Feeds
        self-reconnect; the poll loop is independent so reconciliation continues through a
        transient feed drop."""
        import asyncio

        self._running = True
        feed_tasks = [asyncio.create_task(f.run()) for f in self.feeds]
        try:
            while self._running:
                self.poll_once()
                await asyncio.sleep(self.poll_interval)
        finally:
            for f in self.feeds:
                f.stop()
            for t in feed_tasks:
                t.cancel()

    def stop(self) -> None:
        self._running = False
        for f in self.feeds:
            f.stop()
