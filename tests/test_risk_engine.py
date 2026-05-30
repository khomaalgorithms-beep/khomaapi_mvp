"""Unit tests for the pure risk decision logic. Run: pytest -q

These cover the money-critical paths: daily loss, trailing drawdown (both bases),
profit target, size/position/open-position/trade-count caps, trading hours,
news lockout, lock state, and the always-allow-closing rule.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.risk_engine import (
    Decision,
    evaluate_order,
    evaluate_breach,
    session_anchor,
    next_session_anchor,
    update_high_water_mark,
)

UTC = ZoneInfo("UTC")


def _now(y=2026, mo=6, d=1, h=15, mi=0):  # default: midday ET-ish
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def buy(qty=1, root="MNQ", resulting=1):
    return {"side": "buy", "qty": qty, "symbol_root": root, "resulting_net": resulting}


# ---- baseline allow -------------------------------------------------------

def test_allows_clean_order():
    cfg = {}
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000}
    assert evaluate_order(cfg, state, buy(), _now()).action == Decision.ALLOW


# ---- daily loss limit -----------------------------------------------------

def test_daily_loss_breach():
    cfg = {"daily_loss_limit": 1000}
    state = {"equity": 9000, "day_pnl": -1000, "high_water_mark": 10000}
    d = evaluate_order(cfg, state, buy(), _now())
    assert d.action == Decision.BREACH
    assert "loss limit" in d.reason.lower()


def test_daily_loss_not_breached_just_under():
    cfg = {"daily_loss_limit": 1000}
    state = {"equity": 9001, "day_pnl": -999.99, "high_water_mark": 10000}
    assert evaluate_order(cfg, state, buy(), _now()).action == Decision.ALLOW


# ---- trailing drawdown ----------------------------------------------------

def test_trailing_dd_intraday_breach():
    cfg = {"trailing_dd": 2000, "trailing_basis": "intraday"}
    state = {"equity": 8000, "day_pnl": 0, "high_water_mark": 10000}
    d = evaluate_order(cfg, state, buy(), _now())
    assert d.action == Decision.BREACH
    assert "drawdown" in d.reason.lower()


def test_trailing_dd_headroom_ok():
    cfg = {"trailing_dd": 2000}
    state = {"equity": 8500, "day_pnl": 0, "high_water_mark": 10000}
    assert evaluate_order(cfg, state, buy(), _now()).action == Decision.ALLOW


def test_hwm_intraday_tracks_equity():
    assert update_high_water_mark(10000, 10500, "intraday", flat=False) == 10500
    assert update_high_water_mark(10500, 10200, "intraday", flat=False) == 10500


def test_hwm_closed_only_updates_when_flat():
    # open position: HWM does not move on closed-basis
    assert update_high_water_mark(10000, 11000, "closed", flat=False) == 10000
    # flat: HWM updates
    assert update_high_water_mark(10000, 11000, "closed", flat=True) == 11000


# ---- profit target --------------------------------------------------------

def test_profit_target_stops():
    cfg = {"profit_target": 1500}
    state = {"equity": 11500, "day_pnl": 1500, "high_water_mark": 11500}
    d = evaluate_order(cfg, state, buy(), _now())
    assert d.action == Decision.BREACH
    assert "profit target" in d.reason.lower()


# ---- size / position caps -------------------------------------------------

def test_max_contracts_per_order():
    cfg = {"max_contracts_per_order": 3}
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000}
    d = evaluate_order(cfg, state, buy(qty=5, resulting=5), _now())
    assert d.action == Decision.REJECT
    assert "per order" in d.reason.lower()


def test_max_position_size():
    cfg = {"max_position": 4}
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000}
    d = evaluate_order(cfg, state, buy(qty=2, resulting=6), _now())
    assert d.action == Decision.REJECT
    assert "max position" in d.reason.lower()


def test_max_open_positions_blocks_new_symbol():
    cfg = {"max_open_positions": 2}
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000,
             "open_symbols": ["MNQ", "MES"]}
    d = evaluate_order(cfg, state, buy(root="MYM", resulting=1), _now())
    assert d.action == Decision.REJECT


def test_max_open_positions_allows_existing_symbol():
    cfg = {"max_open_positions": 2}
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000,
             "open_symbols": ["MNQ", "MES"]}
    d = evaluate_order(cfg, state, buy(root="MNQ", resulting=2), _now())
    assert d.action == Decision.ALLOW


def test_daily_trade_cap():
    cfg = {"daily_trade_cap": 10}
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000, "day_trade_count": 10}
    d = evaluate_order(cfg, state, buy(), _now())
    assert d.action == Decision.REJECT
    assert "trade cap" in d.reason.lower()


# ---- trading hours --------------------------------------------------------

def test_outside_trading_hours_rejected():
    cfg = {"hours_start": "09:30", "hours_end": "16:00", "tz": "America/New_York"}
    # 06:00 ET = 10:00 UTC -> before window
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000}
    d = evaluate_order(cfg, state, buy(), datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    assert d.action == Decision.REJECT
    assert "hours" in d.reason.lower()


def test_inside_trading_hours_allowed():
    cfg = {"hours_start": "09:30", "hours_end": "16:00", "tz": "America/New_York"}
    # 14:00 UTC = 10:00 ET -> inside
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000}
    d = evaluate_order(cfg, state, buy(), datetime(2026, 6, 1, 14, 0, tzinfo=UTC))
    assert d.action == Decision.ALLOW


# ---- news lockout ---------------------------------------------------------

def test_news_window_blocks():
    cfg = {}
    start = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)
    end = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000, "news_windows": [(start, end)]}
    d = evaluate_order(cfg, state, buy(), datetime(2026, 6, 1, 14, 45, tzinfo=UTC))
    assert d.action == Decision.REJECT
    assert "news" in d.reason.lower()


# ---- lock state -----------------------------------------------------------

def test_locked_account_rejects_entries():
    cfg = {"locked": 1, "locked_reason": "Daily loss limit reached"}
    state = {"equity": 9000, "day_pnl": -1000, "high_water_mark": 10000}
    d = evaluate_order(cfg, state, buy(), _now())
    assert d.action == Decision.REJECT
    assert "loss limit" in d.reason.lower()


def test_locked_account_still_allows_flatten():
    cfg = {"locked": 1, "locked_reason": "Daily loss limit reached"}
    state = {"equity": 9000, "day_pnl": -1000, "high_water_mark": 10000}
    intent = {"side": "flatten", "qty": 0, "symbol_root": "MNQ", "resulting_net": 0}
    assert evaluate_order(cfg, state, intent, _now()).action == Decision.ALLOW


# ---- session boundary -----------------------------------------------------

def test_session_anchor_before_reset():
    # 14:00 UTC June 1 = 10:00 ET -> session started 17:00 ET previous day (May 31)
    a = session_anchor(datetime(2026, 6, 1, 14, 0, tzinfo=UTC))
    local = a.astimezone(ZoneInfo("America/New_York"))
    assert local.hour == 17
    assert local.day == 31 and local.month == 5


def test_session_anchor_after_reset():
    # 23:00 UTC June 1 = 19:00 ET -> session started 17:00 ET same day (June 1)
    a = session_anchor(datetime(2026, 6, 1, 23, 0, tzinfo=UTC))
    local = a.astimezone(ZoneInfo("America/New_York"))
    assert local.hour == 17 and local.day == 1


def test_next_session_anchor_is_a_day_later():
    now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    assert (next_session_anchor(now) - session_anchor(now)).days == 1


# ---- per-account isolation (config-driven, no global state) ---------------

def test_evaluation_is_pure_per_config():
    locked = {"locked": 1, "locked_reason": "x"}
    active = {}
    state = {"equity": 10000, "day_pnl": 0, "high_water_mark": 10000}
    assert evaluate_order(locked, state, buy(), _now()).action == Decision.REJECT
    assert evaluate_order(active, state, buy(), _now()).action == Decision.ALLOW
