import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
TRADE_LOG = DATA_DIR / "trades.csv"


def _ensure_file() -> None:
    if not TRADE_LOG.exists():
        with TRADE_LOG.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp_utc",
                "trade_id",
                "client_id",
                "symbol",
                "side",
                "qty",
                "mode",
                "status",
                "latency_ms",
                "message"
            ])


def log_trade(row: Dict[str, Any]) -> None:
    _ensure_file()
    with TRADE_LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            row.get("trade_id", ""),
            row.get("client_id", ""),
            row.get("symbol", ""),
            row.get("side", ""),
            row.get("qty", ""),
            row.get("mode", ""),
            row.get("status", ""),
            row.get("latency_ms", ""),
            row.get("message", "")
        ])


def read_trades(limit: int = 50) -> List[Dict[str, Any]]:
    _ensure_file()
    with TRADE_LOG.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:][::-1]
