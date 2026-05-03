import requests
from typing import Dict, Any
from app.core.config import settings


class TradovateError(Exception):
    pass


class TradovateClient:
    def __init__(self):
        self.base_url = self._base_url()
        self.access_token = None

    def _base_url(self) -> str:
        if settings.tradovate_env.lower() == "live":
            return "https://live.tradovateapi.com/v1"
        return "https://demo.tradovateapi.com/v1"

    def login(self) -> str:
        if not settings.tradovate_enabled:
            raise TradovateError("Tradovate is disabled. Set TRADOVATE_ENABLED=true only after demo testing.")

        required = {
            "TRADOVATE_USERNAME": settings.tradovate_username,
            "TRADOVATE_PASSWORD": settings.tradovate_password,
            "TRADOVATE_APP_ID": settings.tradovate_app_id,
            "TRADOVATE_CID": settings.tradovate_cid,
            "TRADOVATE_SEC": settings.tradovate_sec,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise TradovateError(f"Missing Tradovate settings: {', '.join(missing)}")

        payload = {
            "name": settings.tradovate_username,
            "password": settings.tradovate_password,
            "appId": settings.tradovate_app_id,
            "appVersion": settings.tradovate_app_version,
            "cid": settings.tradovate_cid,
            "sec": settings.tradovate_sec,
            "deviceId": settings.tradovate_device_id,
        }

        response = requests.post(
            f"{self.base_url}/auth/accesstokenrequest",
            json=payload,
            timeout=10,
        )

        if response.status_code >= 400:
            raise TradovateError(f"Tradovate login failed: {response.status_code} {response.text}")

        data = response.json()
        token = data.get("accessToken")
        if not token:
            raise TradovateError(f"No accessToken returned: {data}")

        self.access_token = token
        return token

    def _headers(self) -> Dict[str, str]:
        if not self.access_token:
            self.login()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def place_market_order(self, symbol: str, side: str, qty: int) -> Dict[str, Any]:
        # Sends a market order to Tradovate. Test in DEMO first.
        if not settings.tradovate_account_spec or not settings.tradovate_account_id:
            raise TradovateError("Missing TRADOVATE_ACCOUNT_SPEC or TRADOVATE_ACCOUNT_ID.")

        action = "Buy" if side.lower() == "buy" else "Sell"

        payload = {
            "accountSpec": settings.tradovate_account_spec,
            "accountId": int(settings.tradovate_account_id),
            "action": action,
            "symbol": symbol.upper(),
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
            "deviceId": settings.tradovate_device_id,
        }

        response = requests.post(
            f"{self.base_url}/order/placeorder",
            json=payload,
            headers=self._headers(),
            timeout=10,
        )

        if response.status_code >= 400:
            raise TradovateError(f"Order failed: {response.status_code} {response.text}")

        return response.json()

    def flatten_symbol(self, symbol: str) -> Dict[str, Any]:
        # Placeholder. Production version should first read open position, then send opposite qty.
        return {
            "ok": True,
            "message": f"Flatten placeholder received for {symbol}. Production flatten needs position lookup first."
        }
