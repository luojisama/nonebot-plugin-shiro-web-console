from nonebot import get_plugin_config
from pydantic import BaseModel
from typing import Optional

class Config(BaseModel):
    web_console_password: str = "admin123"
    web_console_log_retention_minutes: int = 60
    web_console_log_cleanup_interval_seconds: int = 60
    # 登录令牌有效期：普通会话与“记住此设备”免密登录
    web_console_session_days: int = 7
    web_console_remember_days: int = 30
    # 登录失败限流：窗口内失败次数达到上限后临时锁定
    web_console_login_max_fails: int = 5
    web_console_login_fail_window_minutes: int = 5

config = get_plugin_config(Config)
