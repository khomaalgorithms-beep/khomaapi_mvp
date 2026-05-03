from datetime import date
from app.core.config import settings
from app.core.logger import read_trades


class RiskError(Exception):
    pass


def validate_auth(auth: str) -> None:
    if auth != settings.khoma_webhook_secret:
        raise RiskError("Invalid webhook secret.")


def validate_symbol(symbol: str) -> str:
    clean_symbol = symbol.upper().strip()
    if clean_symbol not in settings.allowed_symbols_list:
        raise RiskError(f"Symbol {clean_symbol} is not allowed.")
    return clean_symbol


def validate_qty(qty: int) -> None:
    if qty < 1:
        raise RiskError("Quantity must be at least 1.")
    if qty > settings.max_contracts_per_order:
        raise RiskError(f"Quantity {qty} exceeds max allowed {settings.max_contracts_per_order}.")


def validate_daily_order_limit() -> None:
    today = date.today().isoformat()
    trades = read_trades(limit=1000)
    todays_orders = [
        t for t in trades
        if t.get("timestamp_utc", "").startswith(today)
        and t.get("status") in ("SIMULATED", "SENT_TO_BROKER")
    ]
    if len(todays_orders) >= settings.max_orders_per_day:
        raise RiskError(f"Daily order limit reached: {settings.max_orders_per_day}.")


def run_trade_risk_checks(auth: str, symbol: str, qty: int) -> str:
    validate_auth(auth)
    clean_symbol = validate_symbol(symbol)
    validate_qty(qty)
    validate_daily_order_limit()
    return clean_symbol


def run_flatten_risk_checks(auth: str, symbol: str) -> str:
    validate_auth(auth)
    return validate_symbol(symbol)
