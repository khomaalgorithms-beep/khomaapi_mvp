import requests

DEMO_URL = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"
LIVE_URL = "https://live.tradovateapi.com/v1/auth/accesstokenrequest"


class TradovateClient:

    def __init__(
        self,
        username,
        password,
        cid,
        sec,
        app_id="KhomaAPI",
        app_version="1.0",
        device_id="khomaapi"
    ):

        self.username = username
        self.password = password
        self.cid = cid
        self.sec = sec

        self.app_id = app_id
        self.app_version = app_version
        self.device_id = device_id

        self.access_token = None

    def login(self, environment="demo"):

        url = DEMO_URL if environment == "demo" else LIVE_URL

        payload = {
            "name": self.username,
            "password": self.password,
            "cid": self.cid,
            "sec": self.sec,
            "appId": self.app_id,
            "appVersion": self.app_version,
            "deviceId": self.device_id
        }

        try:

            response = requests.post(url, json=payload)
            data = response.json()

            if "accessToken" in data:

                self.access_token = data["accessToken"]

                return {
                    "ok": True,
                    "token": self.access_token
                }

            else:

                return {
                    "ok": False,
                    "error": data
                }

        except Exception as e:

            return {
                "ok": False,
                "error": str(e)
            }

    def get_accounts(self, environment="demo"):

        if not self.access_token:

            return {
                "ok": False,
                "error": "Not authenticated"
            }

        base_url = (
            "https://demo.tradovateapi.com/v1"
            if environment == "demo"
            else "https://live.tradovateapi.com/v1"
        )

        url = f"{base_url}/account/list"

        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }

        try:

            response = requests.get(url, headers=headers)
            data = response.json()

            if isinstance(data, list):

                return {
                    "ok": True,
                    "accounts": data
                }

            else:

                return {
                    "ok": False,
                    "error": data
                }

        except Exception as e:

            return {
                "ok": False,
                "error": str(e)
            }
    def place_order(
        self,
        account_id,
        symbol,
        side,
        qty,
        environment="live"
    ):

        if not self.access_token:

            return {
                "ok": False,
                "error": "Not authenticated"
            }

        base_url = (
            "https://demo.tradovateapi.com/v1"
            if environment == "demo"
            else "https://live.tradovateapi.com/v1"
        )

        url = f"{base_url}/order/placeorder"

        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }

        action = "Buy" if side.lower() == "buy" else "Sell"

        payload = {
            "accountSpec": "1047092",
            "accountId": account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
            "timeInForce": "Day"

        }

        try:

            response = requests.post(
                url,
                json=payload,
                headers=headers
            )

            data = response.json()

            return {
                "ok": True,
                "response": data
            }

        except Exception as e:

            return {
                "ok": False,
                "error": str(e)
            }