# TradingView Alert Setup

Use this webhook URL when running locally:

```text
http://127.0.0.1:8000/webhook/trade
```

When using ngrok:

```text
https://YOUR-NGROK-DOMAIN.ngrok.app/webhook/trade
```

Alert body:

```json
{
  "auth": "CHANGE_ME_SECRET",
  "client_id": "demo_client",
  "symbol": "MNQ",
  "side": "buy",
  "qty": 1
}
```
