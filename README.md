# KhomaAPI MVP

This is the first safe MVP for KhomaAPI:

TradingView Webhook → KhomaAPI → Risk Engine → Simulated Trade Log or Tradovate API

## Important

Default mode is **SIMULATION**. It will not place real orders unless you set:

```env
KHOMA_EXECUTION_MODE=live
TRADOVATE_ENABLED=true
```

Start with demo/simulation first.

## Open in PyCharm

1. Unzip this folder.
2. Open the `khomaapi_mvp` folder in PyCharm.
3. Open PyCharm terminal.
4. Run:

```bash
python -m venv .venv
```

Mac:

```bash
source .venv/bin/activate
```

Windows:

```bash
.venv\Scripts\activate
```

5. Install packages:

```bash
pip install -r requirements.txt
```

6. Copy `.env.example` to `.env`.

Mac:

```bash
cp .env.example .env
```

Windows:

```bash
copy .env.example .env
```

7. Run the server:

```bash
uvicorn app.main:app --reload --port 8000
```

8. Open:

```text
http://127.0.0.1:8000
```

## Test webhook

Mac/Linux:

```bash
curl -X POST http://127.0.0.1:8000/webhook/trade \
  -H "Content-Type: application/json" \
  -d '{"auth":"CHANGE_ME_SECRET","client_id":"demo_client","symbol":"MNQ","side":"buy","qty":1}'
```

Windows CMD:

```cmd
curl -X POST http://127.0.0.1:8000/webhook/trade ^
  -H "Content-Type: application/json" ^
  -d "{\"auth\":\"CHANGE_ME_SECRET\",\"client_id\":\"demo_client\",\"symbol\":\"MNQ\",\"side\":\"buy\",\"qty\":1}"
```

## Main endpoints

```text
GET  /
GET  /health
POST /webhook/trade
POST /webhook/flatten
GET  /trades
GET  /status
```

## TradingView alert body

```json
{
  "auth": "CHANGE_ME_SECRET",
  "client_id": "demo_client",
  "symbol": "MNQ",
  "side": "buy",
  "qty": 1
}
```
