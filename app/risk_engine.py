"""KhomaAPI Risk Engine — PURE decision logic (no I/O, no DB, no network).

This module is deliberately side-effect free so it can be unit-tested exhaustively.
The caller (app.main) fetches live Tradovate state, calls evaluate_order() /
evaluate_breach() here, and then performs the actual enforcement (place/reject/
flatten/lock). Wrong risk logic can lose real money, so the decision is isolated
and tested independently of the broker integration.
"""

from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

# CME index futures session reset (daily loss / trade-count / HWM rollover).
DEFAULT_RESET_HOUR = 17  # 5:00 PM
DEFAULT_TZ = "America/New_York"


# ----------------------------------------------------------------------------
# Trading-day / session boundary
# ----------------------------------------------------------------------------

def session_anchor(now_utc: datetime, reset_hour: int = DEFAULT_RESET_HOUR, tz: str = DEFAULT_TZ) -> datetime:
    """UTC start of the trading session containing now_utc. The trading day rolls
    over at reset_hour local time (default 17:00 America/New_York)."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    z = ZoneInfo(tz)
    local = now_utc.astimezone(z)
    anchor = local.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
    if local < anchor:
        anchor -= timedelta(days=1)
    return anchor.astimezone(UTC)


def next_session_anchor(now_utc: datetime, reset_hour: int = DEFAULT_RESET_HOUR, tz: str = DEFAULT_TZ) -> datetime:
    """UTC time of the NEXT session reset — used as a lock expiry (auto-unlock at
    the start of the next trading day)."""
    return session_anchor(now_utc, reset_hour, tz) + timedelta(days=1)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _num(v):
    """Coerce to float, treating None/''/0/negative as 'not set' for limits."""
    try:
        if v in (None, ""):
            return None
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _within_hours(now_utc: datetime, start_hhmm: str, end_hhmm: str, tz: str) -> bool:
    """True if now (in tz) is within [start, end]. Handles overnight windows
    where end < start (e.g. 18:00–16:00 covers the overnight session)."""
    try:
        sh, sm = [int(x) for x in str(start_hhmm).split(":")]
        eh, em = [int(x) for x in str(end_hhmm).split(":")]
    except Exception:
        return True  # malformed window => don't block
    local = now_utc.astimezone(ZoneInfo(tz))
    t = local.time()
    start = dtime(sh, sm)
    end = dtime(eh, em)
    if start <= end:
        return start <= t <= end
    # Overnight window (e.g. 18:00 -> 16:00 next day)
    return t >= start or t <= end


class Decision:
    """Result of a risk evaluation."""

    ALLOW = "allow"
    REJECT = "reject"       # block this order, account stays active
    BREACH = "breach"       # hard limit hit -> flatten + lock the account

    def __init__(self, action: str, reason: str = ""):
        self.action = action
        self.reason = reason

    @property
    def allowed(self) -> bool:
        return self.action == self.ALLOW

    def __repr__(self):
        return f"Decision({self.action!r}, {self.reason!r})"


# ----------------------------------------------------------------------------
# Hard-limit breach detection (daily loss, trailing DD, profit target)
# ----------------------------------------------------------------------------

def evaluate_breach(config: dict, state: dict):
    """Return a reason string if a HARD limit is breached (=> flatten + lock),
    else None. Used both by the background poller and as a pre-order gate."""
    day_pnl = state.get("day_pnl")
    equity = state.get("equity")
    hwm = state.get("high_water_mark")

    dll = _num(config.get("daily_loss_limit"))
    if dll is not None and day_pnl is not None and day_pnl <= -dll:
        return f"Daily loss limit reached: today's PnL ${day_pnl:,.2f} ≤ -${dll:,.2f}"

    tdd = _num(config.get("trailing_dd"))
    if tdd is not None and hwm is not None and equity is not None and (hwm - equity) >= tdd:
        return f"Trailing drawdown breached: ${hwm - equity:,.2f} below peak (limit ${tdd:,.2f})"

    target = _num(config.get("profit_target"))
    if target is not None and day_pnl is not None and day_pnl >= target:
        return f"Profit target reached: today's PnL ${day_pnl:,.2f} ≥ ${target:,.2f}"

    return None


# ----------------------------------------------------------------------------
# Pre-order gate
# ----------------------------------------------------------------------------

def evaluate_order(config: dict, state: dict, intent: dict, now_utc: datetime = None) -> Decision:
    """Decide whether one order may be placed on an account.

    config: the account's risk_config row (dict).
    state:  live account state: equity, day_pnl, high_water_mark, day_trade_count,
            open_symbols (list of contract roots currently open), news_windows
            (list of (start_utc, end_utc) tuples).
    intent: {side, qty, symbol_root, resulting_net} for the prospective order.
    """
    if now_utc is None:
        now_utc = datetime.now(UTC)

    side = (intent.get("side") or "").lower()

    # Closing/reducing exposure is ALWAYS allowed, even when locked.
    if side in ("flatten", "close"):
        return Decision(Decision.ALLOW)

    # Already locked -> reject everything that opens/increases risk.
    if config.get("locked"):
        return Decision(Decision.REJECT, config.get("locked_reason") or "Account is locked")

    # Hard-limit already breached -> trigger flatten + lock.
    breach = evaluate_breach(config, state)
    if breach:
        return Decision(Decision.BREACH, breach)

    qty = int(intent.get("qty") or 0)

    # Trading hours window.
    hs, he = config.get("hours_start"), config.get("hours_end")
    if hs and he and not _within_hours(now_utc, hs, he, config.get("tz") or DEFAULT_TZ):
        return Decision(Decision.REJECT, f"Outside trading hours ({hs}–{he} {config.get('tz') or DEFAULT_TZ})")

    # News lockout windows.
    for win in (state.get("news_windows") or []):
        start, end = win
        if start <= now_utc <= end:
            return Decision(Decision.REJECT, "News lockout window is active")

    # Max contracts per single order.
    mcpo = _num(config.get("max_contracts_per_order"))
    if mcpo is not None and qty > mcpo:
        return Decision(Decision.REJECT, f"Order size {qty} exceeds max contracts per order ({int(mcpo)})")

    # Daily trade count cap.
    dtc = _num(config.get("daily_trade_cap"))
    if dtc is not None and state.get("day_trade_count", 0) >= dtc:
        return Decision(Decision.REJECT, f"Daily trade cap reached ({int(dtc)})")

    # Max distinct open positions/symbols.
    mop = _num(config.get("max_open_positions"))
    if mop is not None:
        open_syms = set(state.get("open_symbols") or [])
        root = intent.get("symbol_root")
        if root not in open_syms and len(open_syms) >= mop:
            return Decision(Decision.REJECT, f"Max open positions reached ({int(mop)})")

    # Max net position size on the account (after this order).
    mps = _num(config.get("max_position"))
    if mps is not None:
        resulting = abs(int(intent.get("resulting_net", 0) or 0))
        if resulting > mps:
            return Decision(Decision.REJECT, f"Resulting position {resulting} exceeds max position ({int(mps)})")

    return Decision(Decision.ALLOW)


def resolve_phase_limits(cfg: dict):
    """Return (daily_loss_limit, trailing_dd, profit_target) actually in force for
    the account's phase. In 'funded' the funded_* limits apply and buffer_zone is
    a cushion that locks BEFORE the firm's hard max loss."""
    if (cfg.get("account_phase") or "evaluation") == "funded":
        maxl = _num(cfg.get("funded_max_loss"))
        buf = _num(cfg.get("buffer_zone")) or 0
        return (cfg.get("funded_daily_loss"),
                (maxl - buf) if maxl is not None else None,
                None)
    return (cfg.get("daily_loss_limit"), cfg.get("trailing_dd"), cfg.get("profit_target"))


def update_high_water_mark(prev_hwm, equity, basis: str, flat: bool):
    """Compute the new high-water-mark.

    basis='intraday' -> HWM tracks live equity (includes open PnL) continuously.
    basis='closed'   -> HWM only updates when the account is flat (no open positions),
                        i.e. on realized/closed balance.
    """
    if equity is None:
        return prev_hwm
    if basis == "closed" and not flat:
        return prev_hwm
    if prev_hwm is None:
        return equity
    return max(prev_hwm, equity)
