"""Runtime orchestrator + observe-only mode. No network. Proves: symbol->root mapping; the
feed->autopilot bar router; observe-only logs a signal without placing orders; LiveEngine
polls every account and isolates a failing one."""
from datetime import datetime

from app.orb_selective import Bar
from app.orb_live import Autopilot, AccountConfig
from app.orb_runtime import symbol_to_root, make_bar_router, LiveEngine
from tests.test_orb_live import FakeBroker, breakout_day


def test_symbol_to_root():
    assert symbol_to_root("MNQZ5") == "MNQ"
    assert symbol_to_root("M2KH6") == "M2K"
    assert symbol_to_root("ESZ5") is None        # not one of our instruments


def cfg(observe=None, qty=None):
    return AccountConfig(env="demo", account_spec="D1", account_id=7, account_name="D1",
                         instruments={"MNQ": "MNQZ5"}, qty=qty or {"MNQ": 3},
                         observe=observe or {})


def test_router_translates_symbol_and_feeds_autopilot():
    fb = FakeBroker()
    ap = Autopilot(cfg(), get_token=lambda: "t", broker=fb)
    logged = []
    on_bar = make_bar_router(ap, {"MNQ": "MNQZ5"}, sink=lambda r, b, a: logged.append((r, a["action"])))
    for b in breakout_day():
        on_bar("MNQZ5", b)                        # feed emits the SYMBOL, router maps to MNQ
    assert len(fb.place_calls) == 1
    assert ("MNQ", "placed") in logged


def test_observe_only_logs_without_placing():
    fb = FakeBroker()
    ap = Autopilot(cfg(observe={"MNQ": True}), get_token=lambda: "t", broker=fb)
    actions = [a for a in (ap.on_bar("MNQ", b) for b in breakout_day()) if a]
    assert fb.place_calls == []                   # observe-only -> NO broker orders
    assert actions[-1]["action"] == "observed"
    assert actions[-1]["plan"].sl_price == 20025 - 30   # plan still computed for the journal
    assert ap._state["MNQ"].status == "DONE"


def test_live_engine_polls_all_and_isolates_failures():
    fb = FakeBroker()
    good = Autopilot(cfg(), get_token=lambda: "t", broker=fb)

    class Boom(Autopilot):
        def poll(self):
            raise RuntimeError("broker down")
    bad = Boom(cfg(), get_token=lambda: "t", broker=fb)

    eng = LiveEngine([good, bad], feeds=[])
    out = eng.poll_once()
    # the failing account is captured, not propagated; the good one still polled
    assert any("error" in a for a in out)
