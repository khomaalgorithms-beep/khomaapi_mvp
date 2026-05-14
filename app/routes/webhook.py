from fastapi import APIRouter, Request
from app.brokers.tradovate import TradovateClient
import time

router = APIRouter()

# duplicate protection
recent_requests = {}

WEBHOOK_SECRET = "abd54ccbb9f82904eaec006b90e380480e1fea2f"


@router.post("/webhook/trade")
async def webhook_trade(request: Request):

    start_time = time.time()

    try:

        payload = await request.json()

        client_id = payload.get("client_id")
        auth = payload.get("auth")
        symbol = payload.get("symbol")
        side = payload.get("side")
        qty = payload.get("qty")
        request_id = payload.get("request_id")

        # -----------------------------
        # AUTH CHECK
        # -----------------------------

        if auth != WEBHOOK_SECRET:

            return {
                "ok": False,
                "error": "Invalid webhook secret."
            }

        # -----------------------------
        # DUPLICATE LOCK
        # -----------------------------

        now = time.time()

        if request_id in recent_requests:

            if now - recent_requests[request_id] < 8:

                return {
                    "ok": False,
                    "error": "Duplicate request blocked."
                }

        recent_requests[request_id] = now

        # -----------------------------
        # LOGIN TO TRADOVATE
        # -----------------------------

        tradovate = TradovateClient(
            username="DmytriiKhoma",
            password="Dimaoffkh25112008@",
            cid="13281",
            sec="3c7f3c53-0377-45f2-b3f2-04eda8b5a588"
        )

        login_result = tradovate.login("live")

        if not login_result.get("ok"):

            return {
                "ok": False,
                "error": "Tradovate login failed",
                "details": login_result
            }

        # -----------------------------
        # FETCH ACCOUNT
        # -----------------------------

        accounts_result = tradovate.get_accounts("live")

        if not accounts_result.get("ok"):

            return {
                "ok": False,
                "error": "Failed to fetch accounts",
                "details": accounts_result
            }

        account_id = accounts_result["accounts"][0]["id"]

        # -----------------------------
        # PLACE ORDER
        # -----------------------------

        order_result = tradovate.place_order(
            account_id=account_id,
            symbol=symbol,
            side=side,
            qty=qty,
            environment="live"
        )

        latency_ms = round((time.time() - start_time) * 1000, 2)

        return {
            "ok": True,
            "action": "LIVE_ORDER_EXECUTED",
            "latency_ms": latency_ms,
            "order_result": order_result
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }