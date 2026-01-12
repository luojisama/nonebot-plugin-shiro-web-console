from nonebot import get_plugin_config
from pydantic import BaseModel
from typing import Optional

class Config(BaseModel):
    web_console_password: str = "admin123"

config = get_plugin_config(Config)
