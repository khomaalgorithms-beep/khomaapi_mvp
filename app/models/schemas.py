from pydantic import BaseModel, Field
from typing import Optional, Literal


class TradeWebhook(BaseModel):
    auth: str
    client_id: str = "demo_client"
    symbol: str = Field(..., examples=["MNQ"])
    side: Literal["buy", "sell"]
    qty: int = Field(..., ge=1, le=100)
    order_type: Literal["market"] = "market"
    note: Optional[str] = None


class FlattenWebhook(BaseModel):
    auth: str
    client_id: str = "demo_client"
    symbol: str = Field(..., examples=["MNQ"])
    note: Optional[str] = None


class TradeResponse(BaseModel):
    ok: bool
    mode: str
    message: str
    trade_id: Optional[str] = None
    latency_ms: Optional[float] = None
    broker_response: Optional[dict] = None
