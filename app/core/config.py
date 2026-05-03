from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "KhomaAPI MVP"
    khoma_webhook_secret: str = "CHANGE_ME_SECRET"
    khoma_execution_mode: str = "simulation"

    max_contracts_per_order: int = 2
    max_orders_per_day: int = 10
    allow_symbols: str = "MNQ,NQ,MES,ES,MYM,YM"

    tradovate_enabled: bool = False
    tradovate_env: str = "demo"
    tradovate_username: str = ""
    tradovate_password: str = ""
    tradovate_app_id: str = ""
    tradovate_app_version: str = "1.0"
    tradovate_cid: str = ""
    tradovate_sec: str = ""
    tradovate_account_spec: str = ""
    tradovate_account_id: str = ""
    tradovate_device_id: str = "khomaapi-device-001"

    @property
    def allowed_symbols_list(self) -> List[str]:
        return [x.strip().upper() for x in self.allow_symbols.split(",") if x.strip()]


settings = Settings()
