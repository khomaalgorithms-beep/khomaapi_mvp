import time
import uuid
from fastapi import APIRouter
from app.models.schemas import TradeWebhook, FlattenWebhook, TradeResponse
from app.core.config import settings
from app.core.risk import run_trade_risk_checks, run_flatten_risk_checks, RiskError
from app.core.logger import log_trade
from app.brokers.tradovate import TradovateClient, TradovateError

router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.post("/trade", response_model=TradeResponse)
def trade_webhook(payload: TradeWebhook):
    start = time.perf_counter()
    trade_id = str(uuid.uuid4())[:8]
    broker_response = None

    try:
        symbol = run_trade_risk_checks(payload.auth, payload.symbol, payload.qty)

        if settings.khoma_execution_mode.lower() == "live":
            client = TradovateClient()
            broker_response = client.place_market_order(symbol, payload.side, payload.qty)
            status = "SENT_TO_BROKER"
            message = "Order sent to Tradovate."
        else:
            status = "SIMULATED"
            message = "Simulation mode: order logged but NOT sent to broker."

        latency_ms = round((time.perf_counter() - start) * 1000, 3)

        log_trade({
            "trade_id": trade_id,
            "client_id": payload.client_id,
            "symbol": symbol,
            "side": payload.side,
            "qty": payload.qty,
            "mode": settings.khoma_execution_mode,
            "status": status,
            "latency_ms": latency_ms,
            "message": message,
        })

        return TradeResponse(
            ok=True,
            mode=settings.khoma_execution_mode,
            message=message,
            trade_id=trade_id,
            latency_ms=latency_ms,
            broker_response=broker_response,
        )

    except (RiskError, TradovateError, Exception) as e:
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        log_trade({
            "trade_id": trade_id,
            "client_id": payload.client_id,
            "symbol": payload.symbol,
            "side": payload.side,
            "qty": payload.qty,
            "mode": settings.khoma_execution_mode,
            "status": "REJECTED",
            "latency_ms": latency_ms,
            "message": str(e),
        })
        return TradeResponse(
            ok=False,
            mode=settings.khoma_execution_mode,
            message=str(e),
            trade_id=trade_id,
            latency_ms=latency_ms,
            broker_response=None,
        )


@router.post("/flatten", response_model=TradeResponse)
def flatten_webhook(payload: FlattenWebhook):
    start = time.perf_counter()
    trade_id = str(uuid.uuid4())[:8]
    broker_response = None

    try:
        symbol = run_flatten_risk_checks(payload.auth, payload.symbol)

        if settings.khoma_execution_mode.lower() == "live":
            client = TradovateClient()
            broker_response = client.flatten_symbol(symbol)
            status = "FLATTEN_SENT"
            message = "Flatten request sent."
        else:
            status = "FLATTEN_SIMULATED"
            message = "Simulation mode: flatten logged but NOT sent to broker."

        latency_ms = round((time.perf_counter() - start) * 1000, 3)

        log_trade({
            "trade_id": trade_id,
            "client_id": payload.client_id,
            "symbol": symbol,
            "side": "flatten",
            "qty": "",
            "mode": settings.khoma_execution_mode,
            "status": status,
            "latency_ms": latency_ms,
            "message": message,
        })

        return TradeResponse(
            ok=True,
            mode=settings.khoma_execution_mode,
            message=message,
            trade_id=trade_id,
            latency_ms=latency_ms,
            broker_response=broker_response,
        )

    except (RiskError, TradovateError, Exception) as e:
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        return TradeResponse(
            ok=False,
            mode=settings.khoma_execution_mode,
            message=str(e),
            trade_id=trade_id,
            latency_ms=latency_ms,
            broker_response=None,
        )
