from fastapi import APIRouter
from app.core.config import settings
from app.core.logger import read_trades

router = APIRouter(tags=["status"])


@router.get("/")
def home():
    return {
        "app": settings.app_name,
        "status": "online",
        "execution_mode": settings.khoma_execution_mode,
        "tradovate_enabled": settings.tradovate_enabled,
        "message": "KhomaAPI MVP is running."
    }


@router.get("/health")
def health():
    return {
        "ok": True,
        "execution_mode": settings.khoma_execution_mode,
        "allowed_symbols": settings.allowed_symbols_list,
    }


@router.get("/status")
def status():
    return {
        "app": settings.app_name,
        "execution_mode": settings.khoma_execution_mode,
        "tradovate_enabled": settings.tradovate_enabled,
        "tradovate_env": settings.tradovate_env,
        "max_contracts_per_order": settings.max_contracts_per_order,
        "max_orders_per_day": settings.max_orders_per_day,
        "allowed_symbols": settings.allowed_symbols_list,
    }


@router.get("/trades")
def trades(limit: int = 50):
    return {
        "count": min(limit, 50),
        "trades": read_trades(limit=min(limit, 50)),
    }
