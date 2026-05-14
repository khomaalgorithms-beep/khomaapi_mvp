from pydantic import BaseModel


class TradePayload(BaseModel):

    auth: str
    symbol: str
    side: str
    qty: int
    request_id: str