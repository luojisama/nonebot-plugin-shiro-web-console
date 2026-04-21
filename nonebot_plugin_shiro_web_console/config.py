from nonebot import get_plugin_config
from pydantic import BaseModel
from typing import Optional

class Config(BaseModel):
    web_console_password: str = "admin123"
    web_console_log_retention_minutes: int = 60
    web_console_log_cleanup_interval_seconds: int = 60

config = get_plugin_config(Config)
