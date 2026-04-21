import asyncio
import json
import httpx
import os
import random
import time
import secrets
import hashlib
import re
import importlib
from datetime import datetime, timedelta
from urllib.parse import quote, unquote
from typing import Dict, List, Set, Optional, Any
from pathlib import Path
from collections import deque
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from nonebot import get_app, get_bot, get_bots, get_driver, get_plugin_config as nb_get_plugin_config, logger, on_message, on_command, require
require("nonebot_plugin_localstore")
import nonebot_plugin_localstore
from .config import Config, config
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent, PrivateMessageEvent, MessageSegment, Message
from nonebot.plugin import PluginMetadata

@dataclass
class PersonificationProvider:
    variant: str
    module_name: str
    display_name: str
    config_class: Any
    plugin_config: Any
    get_data_store: Any
    chat_histories: Any
    load_group_configs: Any
    load_whitelist: Any
    get_group_config: Any
    is_group_whitelisted: Any
    get_recent_group_msgs: Any
    set_group_enabled: Any
    set_group_sticker_enabled: Any
    set_group_prompt: Any
    set_group_schedule_enabled: Any
    set_group_tts_enabled: Any
    add_group_to_whitelist: Any
    remove_group_from_whitelist: Any
    load_runtime_config: Any
    save_runtime_config: Any
    get_runtime_config_path: Any
    get_config_entries: Any
    read_config_value: Any
    describe_choices: Any
    get_entry_label: Any


_PERSONIFICATION_CANDIDATES = (
    {
        "variant": "shiro",
        "module_name": "nonebot_plugin_personification",
        "display_name": "nonebot-plugin-shiro-personification",
    },
    {
        "variant": "local",
        "module_name": "personification",
        "display_name": "personification",
    },
)
_p13n_provider_cache: Optional[PersonificationProvider] = None
_p13n_provider_cache_key: Optional[str] = None

_P13N_BOOL_TRUE = {"1", "true", "yes", "on", "开", "开启", "启用"}
_P13N_BOOL_FALSE = {"0", "false", "no", "off", "关", "关闭", "禁用"}

_P13N_RUNTIME_GLOBAL_ENTRIES = (
    {
        "key": "global_enabled",
        "field_name": "personification_global_enabled",
        "display_name": "全局拟人回复",
        "value_type": "bool",
        "default": True,
        "description": "控制插件整体回复能力的总开关。",
    },
    {
        "key": "tts_global_enabled",
        "field_name": "personification_tts_global_enabled",
        "display_name": "全局语音回复",
        "value_type": "bool",
        "default": True,
        "description": "控制语音回复能力是否允许在任意群生效。",
    },
    {
        "key": "web_search",
        "field_name": "personification_web_search",
        "display_name": "兼容联网总开关",
        "value_type": "bool",
        "default": True,
        "description": "兼容旧配置的联网总开关。",
    },
    {
        "key": "schedule_global",
        "field_name": "personification_schedule_global",
        "display_name": "全局作息模拟",
        "value_type": "bool",
        "default": False,
        "description": "是否允许作息模拟在全局运行。",
    },
    {
        "key": "proactive_enabled",
        "field_name": "personification_proactive_enabled",
        "display_name": "主动私聊",
        "value_type": "bool",
        "default": False,
        "description": "是否允许主动私聊发起话题。",
    },
    {
        "key": "group_idle_enabled",
        "field_name": "personification_group_idle_enabled",
        "display_name": "群空闲主动发话",
        "value_type": "bool",
        "default": False,
        "description": "是否允许群聊长时间安静时主动发话。",
    },
    {
        "key": "skill_remote_enabled",
        "field_name": "personification_skill_remote_enabled",
        "display_name": "远程技能加载",
        "value_type": "bool",
        "default": False,
        "description": "允许使用远程 skill 源。",
    },
    {
        "key": "skill_require_admin_review",
        "field_name": "personification_skill_require_admin_review",
        "display_name": "远程技能管理员审核",
        "value_type": "bool",
        "default": True,
        "description": "远程技能是否必须管理员审核后才能启用。",
    },
    {
        "key": "skill_allow_unsafe_external",
        "field_name": "personification_skill_allow_unsafe_external",
        "display_name": "允许不安全外部技能",
        "value_type": "bool",
        "default": False,
        "description": "是否放宽对不安全外部 skill 的限制。",
    },
)

_P13N_GROUP_EXTRA_ENTRIES = (
    {
        "key": "whitelisted",
        "field_name": "whitelisted",
        "display_name": "群白名单",
        "value_type": "bool",
        "default": False,
        "description": "控制当前群是否在拟人化白名单内。",
    },
    {
        "key": "custom_prompt",
        "field_name": "custom_prompt",
        "display_name": "自定义 Prompt",
        "value_type": "str",
        "default": "",
        "description": "为单个群追加额外人设提示词。",
    },
)


def _parse_p13n_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in _P13N_BOOL_TRUE:
        return True
    if text in _P13N_BOOL_FALSE:
        return False
    raise ValueError("布尔值仅支持 true/false、on/off、开/关、1/0")


def _normalize_p13n_value(value: Any, value_type: str, *, choices: Optional[List[str]] = None) -> Any:
    if value_type == "bool":
        return _parse_p13n_bool(value)
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "list":
        if isinstance(value, list):
            return value
        text = str(value or "").strip()
        if not text:
            return []
        return [item.strip() for item in text.split(",") if item.strip()]

    normalized = "" if value is None else str(value)
    if choices:
        lowered = normalized.strip().lower()
        matched = next((choice for choice in choices if str(choice).lower() == lowered), None)
        if matched is None:
            raise ValueError(f"可选值: {', '.join(choices)}")
        return matched
    return normalized


def _detect_personification_module_name() -> Optional[str]:
    try:
        from nonebot import get_loaded_plugins

        loaded_modules = {
            str(getattr(plugin, "module_name", getattr(plugin, "name", "")) or "")
            for plugin in get_loaded_plugins()
        }
    except Exception as e:
        logger.debug(f"读取已加载插件列表失败: {e}")
        return None

    for candidate in _PERSONIFICATION_CANDIDATES:
        if candidate["module_name"] in loaded_modules:
            return candidate["module_name"]
    return None


def _build_personification_provider(module_name: str) -> PersonificationProvider:
    candidate = next(
        item for item in _PERSONIFICATION_CANDIDATES if item["module_name"] == module_name
    )
    config_module = importlib.import_module(f"{module_name}.config")
    data_store_module = importlib.import_module(f"{module_name}.core.data_store")
    session_store_module = importlib.import_module(f"{module_name}.core.session_store")
    utils_module = importlib.import_module(f"{module_name}.utils")
    config_registry_module = importlib.import_module(f"{module_name}.core.config_registry")
    runtime_module = importlib.import_module(f"{module_name}.core.runtime_config")

    return PersonificationProvider(
        variant=str(candidate["variant"]),
        module_name=str(candidate["module_name"]),
        display_name=str(candidate["display_name"]),
        config_class=getattr(config_module, "Config"),
        plugin_config=nb_get_plugin_config(getattr(config_module, "Config")),
        get_data_store=getattr(data_store_module, "get_data_store"),
        chat_histories=getattr(session_store_module, "chat_histories"),
        load_group_configs=getattr(utils_module, "load_group_configs"),
        load_whitelist=getattr(utils_module, "load_whitelist", lambda: []),
        get_group_config=getattr(utils_module, "get_group_config"),
        is_group_whitelisted=getattr(utils_module, "is_group_whitelisted"),
        get_recent_group_msgs=getattr(utils_module, "get_recent_group_msgs"),
        set_group_enabled=getattr(utils_module, "set_group_enabled", None),
        set_group_sticker_enabled=getattr(utils_module, "set_group_sticker_enabled", None),
        set_group_prompt=getattr(utils_module, "set_group_prompt", None),
        set_group_schedule_enabled=getattr(utils_module, "set_group_schedule_enabled", None),
        set_group_tts_enabled=getattr(utils_module, "set_group_tts_enabled", None),
        add_group_to_whitelist=getattr(utils_module, "add_group_to_whitelist", None),
        remove_group_from_whitelist=getattr(utils_module, "remove_group_from_whitelist", None),
        load_runtime_config=getattr(runtime_module, "load_plugin_runtime_config", None),
        save_runtime_config=getattr(runtime_module, "save_plugin_runtime_config", None),
        get_runtime_config_path=getattr(runtime_module, "get_runtime_config_path", None),
        get_config_entries=getattr(config_registry_module, "get_config_entries"),
        read_config_value=getattr(config_registry_module, "read_config_value"),
        describe_choices=getattr(config_registry_module, "describe_choices"),
        get_entry_label=getattr(config_registry_module, "get_entry_label"),
    )


def _get_personification_provider(force_refresh: bool = False) -> Optional[PersonificationProvider]:
    global _p13n_provider_cache
    global _p13n_provider_cache_key

    module_name = _detect_personification_module_name()
    if not module_name:
        _p13n_provider_cache = None
        _p13n_provider_cache_key = None
        return None

    if (
        not force_refresh
        and _p13n_provider_cache is not None
        and _p13n_provider_cache_key == module_name
    ):
        return _p13n_provider_cache

    try:
        provider = _build_personification_provider(module_name)
    except Exception as e:
        logger.error(f"拟人插件集成初始化失败，将降级为未启用状态: {e}")
        _p13n_provider_cache = None
        _p13n_provider_cache_key = None
        return None

    _p13n_provider_cache = provider
    _p13n_provider_cache_key = module_name
    logger.info(
        f"拟人插件已检测到，Web 控制台将启用拟人集成功能: {provider.display_name} ({provider.module_name})"
    )
    return provider

START_TIME = time.time()

__plugin_meta__ = PluginMetadata(
    name="Shiro Web Console",
    description="通过浏览器查看日志、管理机器人、管理插件并发送消息",
    usage="访问 /web_console 查看，在机器人聊天框发送“web控制台”获取登录码",
    type="application",
    homepage="https://github.com/luojisama/nonebot-plugin-shiro-web-console",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={
        "author": "luojisama",
        "version": "0.2.1",
        "pypi_test": "nonebot-plugin-shiro-web-console",
    },
)

# WebSocket 连接池
active_connections: Set[WebSocket] = set()

async def broadcast_message(data: dict):
    if not active_connections:
        return
    
    dead_connections = set()
    for ws in active_connections:
        try:
            await ws.send_json(data)
        except Exception:
            dead_connections.add(ws)
    
    for ws in dead_connections:
        active_connections.discard(ws)

# 日志缓冲区，保留最近 200 条日志
log_buffer = deque(maxlen=200)
LOG_RETENTION = timedelta(minutes=max(config.web_console_log_retention_minutes, 1))
LOG_CLEANUP_INTERVAL_SECONDS = max(config.web_console_log_cleanup_interval_seconds, 10)


def _cleanup_expired_log_entries(now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    while log_buffer and log_buffer[0]["expires_at"] <= now:
        log_buffer.popleft()


def _serialize_log_entry(log_entry: Dict[str, Any]) -> Dict[str, str]:
    return {
        "time": log_entry["time"],
        "level": log_entry["level"],
        "message": log_entry["message"],
        "module": log_entry["module"],
    }


async def _log_buffer_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(LOG_CLEANUP_INTERVAL_SECONDS)
        _cleanup_expired_log_entries()

async def log_sink(message):
    now = datetime.now()
    _cleanup_expired_log_entries(now)
    log_entry = {
        "time": now.strftime("%H:%M:%S"),
        "level": message.record["level"].name,
        "message": message.record["message"],
        "module": message.record["module"],
        "expires_at": now + LOG_RETENTION,
    }
    log_buffer.append(log_entry)
    # 推送日志
    await broadcast_message({
        "type": "new_log",
        "data": _serialize_log_entry(log_entry)
    })

# 注册 loguru sink
logger.add(log_sink, format="{time} {level} {message}", level="INFO")

# Async lock for plugin actions
store_lock = asyncio.Lock()

# Persistent log configuration
log_dir = nonebot_plugin_localstore.get_plugin_data_dir() / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
full_log_path = log_dir / "web_console_full.log"
logger.add(full_log_path, rotation="10 MB", encoding="utf-8", level="INFO", format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{line} | {message}")

# 群聊调用统计（内存，重启清零；如需持久化可写入 localstore）
# 结构：{ "group_<group_id>": { "total": int, "hourly": {"%Y-%m-%dT%H": int}, "daily": {"%Y-%m-%d": int} } }
_group_call_stats: Dict[str, Dict[str, Any]] = {}


def _record_p13n_call(group_id: str):
    """记录一次拟人插件在指定群的调用（由 Bot API Hook 检测到拟人回复时调用）。"""
    key = f"group_{group_id}"
    now = datetime.now()
    hour_key = now.strftime("%Y-%m-%dT%H")
    day_key = now.strftime("%Y-%m-%d")
    if key not in _group_call_stats:
        _group_call_stats[key] = {"total": 0, "hourly": {}, "daily": {}}
    stat = _group_call_stats[key]
    stat["total"] += 1
    stat["hourly"][hour_key] = stat["hourly"].get(hour_key, 0) + 1
    stat["daily"][day_key] = stat["daily"].get(day_key, 0) + 1
    if len(stat["hourly"]) > 48:
        oldest = sorted(stat["hourly"].keys())[0]
        del stat["hourly"][oldest]
    if len(stat["daily"]) > 30:
        oldest = sorted(stat["daily"].keys())[0]
        del stat["daily"][oldest]


def report_personification_call(group_id: str):
    """供拟人插件主动调用，上报一次调用记录。也可通过 Bot Hook 自动检测。"""
    _record_p13n_call(str(group_id))


def _is_personification_stack() -> bool:
    provider = _get_personification_provider()
    if provider is None:
        return False

    import sys

    frame = None
    try:
        frame = sys._getframe(1)
        while frame:
            module_name = str(frame.f_globals.get("__name__", "") or "")
            if module_name.startswith(provider.module_name):
                return True
            frame = frame.f_back
    except Exception as e:
        logger.error(f"检测拟人插件调用栈失败: {e}")
    finally:
        del frame
    return False


def _clone_p13n_plugin_config(provider: Optional[PersonificationProvider]):
    if provider is None or provider.plugin_config is None:
        return None
    if hasattr(provider.plugin_config, "model_copy"):
        return provider.plugin_config.model_copy(deep=True)
    if hasattr(provider.plugin_config, "copy"):
        return provider.plugin_config.copy(deep=True)
    return provider.plugin_config


def _get_p13n_runtime_path(provider: PersonificationProvider, plugin_config: Any) -> Optional[Path]:
    runtime_path_getter = getattr(provider, "get_runtime_config_path", None)
    if runtime_path_getter is None:
        return None
    try:
        runtime_path = runtime_path_getter(plugin_config)
        return runtime_path if isinstance(runtime_path, Path) else Path(runtime_path)
    except Exception as e:
        logger.error(f"读取拟人插件运行时配置路径失败: {e}")
        return None


def _load_p13n_group_configs_safe(provider: Optional[PersonificationProvider]) -> Dict[str, dict]:
    if provider is None:
        return {}

    try:
        configs = provider.load_group_configs()
        if isinstance(configs, dict):
            return configs
    except Exception as e:
        logger.error(f"读取拟人插件群配置失败，将尝试数据存储兜底: {e}")

    try:
        data = provider.get_data_store().load_sync("group_config")
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"读取拟人插件数据存储失败: {e}")
        return {}


def _load_p13n_effective_plugin_config(provider: Optional[PersonificationProvider]) -> Any:
    runtime_config = _clone_p13n_plugin_config(provider)
    if runtime_config is None or provider is None:
        return runtime_config

    if provider.load_runtime_config is None:
        return runtime_config

    runtime_path = _get_p13n_runtime_path(provider, runtime_config)
    try:
        if runtime_path is not None:
            provider.load_runtime_config(runtime_config, logger, path=runtime_path)
        else:
            provider.load_runtime_config(runtime_config, logger)
    except TypeError:
        try:
            provider.load_runtime_config(runtime_config, logger)
        except Exception as e:
            logger.error(f"加载拟人插件运行时配置失败: {e}")
    except Exception as e:
        logger.error(f"加载拟人插件运行时配置失败: {e}")
    return runtime_config


def _load_p13n_global_state(provider: Optional[PersonificationProvider]) -> Dict[str, Any]:
    if provider is None:
        return {}

    effective_config = _load_p13n_effective_plugin_config(provider)
    if effective_config is None:
        return {}

    state: Dict[str, Any] = {}
    for entry in _P13N_RUNTIME_GLOBAL_ENTRIES:
        state[entry["key"]] = getattr(
            effective_config,
            entry["field_name"],
            entry["default"],
        )
    for entry in provider.get_config_entries("global"):
        state[entry.key] = provider.read_config_value(entry, plugin_config=effective_config)
    return state


def _serialize_p13n_entry(
    provider: Optional[PersonificationProvider],
    entry: Any,
    value: Any,
) -> Dict[str, Any]:
    if isinstance(entry, dict):
        choices = list(entry.get("choices", []) or [])
        return {
            "key": str(entry["key"]),
            "field_name": str(entry.get("field_name") or entry["key"]),
            "label": str(entry.get("display_name") or entry["key"]),
            "description": str(entry.get("description") or ""),
            "value_type": str(entry.get("value_type") or "str"),
            "default": entry.get("default"),
            "value": value,
            "choices": choices,
            "choices_text": " / ".join(choices) if choices else "",
        }

    choices = list(getattr(entry, "choices", ()) or ())
    return {
        "key": str(entry.key),
        "field_name": str(entry.field_name),
        "label": str(provider.get_entry_label(entry) if provider is not None else entry.key),
        "description": str(getattr(entry, "description", "") or ""),
        "value_type": str(getattr(entry, "value_type", "str") or "str"),
        "default": getattr(entry, "default", None),
        "value": value,
        "choices": choices,
        "choices_text": str(provider.describe_choices(entry) if provider is not None else ""),
    }


def _build_p13n_global_entries(provider: Optional[PersonificationProvider]) -> List[Dict[str, Any]]:
    if provider is None:
        return []

    effective_config = _load_p13n_effective_plugin_config(provider)
    if effective_config is None:
        return []

    entries: List[Dict[str, Any]] = []
    for entry in _P13N_RUNTIME_GLOBAL_ENTRIES:
        entries.append(
            _serialize_p13n_entry(
                provider,
                entry,
                getattr(effective_config, entry["field_name"], entry["default"]),
            )
        )
    for entry in provider.get_config_entries("global"):
        entries.append(
            _serialize_p13n_entry(
                provider,
                entry,
                provider.read_config_value(entry, plugin_config=effective_config),
            )
        )
    return entries


def _build_p13n_group_entries(
    provider: Optional[PersonificationProvider],
    group_id: str,
) -> List[Dict[str, Any]]:
    if provider is None:
        return []

    normalized_group_id = str(group_id).replace("group_", "", 1)
    group_config = provider.get_group_config(normalized_group_id)
    if not isinstance(group_config, dict):
        group_config = {}
    config_whitelist = list(getattr(provider.plugin_config, "personification_whitelist", []) or [])
    runtime_whitelist = provider.load_whitelist()
    if not isinstance(runtime_whitelist, list):
        runtime_whitelist = []

    entries: List[Dict[str, Any]] = []
    for entry in provider.get_config_entries("group"):
        entries.append(
            _serialize_p13n_entry(
                provider,
                entry,
                provider.read_config_value(
                    entry,
                    plugin_config=provider.plugin_config,
                    group_config=group_config,
                ),
            )
        )
    for entry in _P13N_GROUP_EXTRA_ENTRIES:
        if entry["key"] == "whitelisted":
            value = normalized_group_id in config_whitelist or normalized_group_id in runtime_whitelist
        else:
            value = group_config.get(entry["field_name"], entry["default"])
        entries.append(_serialize_p13n_entry(provider, entry, value))
    return entries


def _write_p13n_runtime_config(provider: PersonificationProvider, runtime_config: Any) -> None:
    runtime_path = _get_p13n_runtime_path(provider, runtime_config)
    if provider.save_runtime_config is None:
        raise RuntimeError("拟人插件未导出运行时配置保存函数")
    if runtime_path is not None:
        provider.save_runtime_config(runtime_config, logger, path=runtime_path)
    else:
        provider.save_runtime_config(runtime_config, logger)
    for entry in _P13N_RUNTIME_GLOBAL_ENTRIES:
        setattr(
            provider.plugin_config,
            entry["field_name"],
            getattr(runtime_config, entry["field_name"], entry["default"]),
        )
    for entry in provider.get_config_entries("global"):
        setattr(
            provider.plugin_config,
            entry.field_name,
            getattr(runtime_config, entry.field_name, entry.default),
        )


def _collect_p13n_group_ids(
    provider: Optional[PersonificationProvider],
    group_configs: Dict[str, dict],
) -> List[str]:
    group_ids: Set[str] = set()
    for raw_group_id in group_configs.keys():
        group_ids.add(str(raw_group_id).replace("group_", "", 1))
    if provider is not None:
        for session_id in provider.chat_histories.keys():
            if isinstance(session_id, str) and session_id.startswith("group_"):
                group_ids.add(session_id.replace("group_", "", 1))
    for stat_key in _group_call_stats.keys():
        if stat_key.startswith("group_"):
            group_ids.add(stat_key.replace("group_", "", 1))
    return sorted(group_ids, key=lambda item: int(item) if str(item).isdigit() else str(item))

# 验证码管理
class AuthManager:
    def __init__(self):
        self.code: Optional[str] = None
        self.expire_time: Optional[datetime] = None
        self.token: Optional[str] = None
        self.token_expire: Optional[datetime] = None
        
        # 密码持久化文件路径
        self.data_dir = nonebot_plugin_localstore.get_plugin_data_dir()
        self.password_file = self.data_dir / "password.json"
        
        # 初始加载密码
        self.admin_password_hash = self._load_password_hash()

    def _load_password_hash(self) -> str:
        pwd = "admin123"
        if self.password_file.exists():
            try:
                data = json.loads(self.password_file.read_text(encoding="utf-8"))
                if "password_hash" in data:
                    return data["password_hash"]
                pwd = data.get("password", "admin123")
            except:
                pass
        else:
            pwd = config.web_console_password
        
        # 迁移或初始化：将明文转换为哈希
        return hashlib.sha256(pwd.encode()).hexdigest()

    def save_password(self, new_password: str):
        pwd_hash = hashlib.sha256(new_password.encode()).hexdigest()
        self.admin_password_hash = pwd_hash
        self.password_file.write_text(json.dumps({"password_hash": pwd_hash}), encoding="utf-8")
        # 修改密码后使旧 token 失效
        self.token = None

    def generate_code(self) -> str:
        self.code = "".join([str(random.randint(0, 9)) for _ in range(6)])
        self.expire_time = datetime.now() + timedelta(minutes=5)
        return self.code

    def verify_code(self, code: str) -> bool:
        if not self.code or not self.expire_time:
            return False
        if datetime.now() > self.expire_time:
            self.code = None
            return False
        if self.code == code:
            self.code = None  # 验证码一次性
            self.generate_token()
            return True
        return False

    def verify_password(self, password: str) -> bool:
        input_hash = hashlib.sha256(password.encode()).hexdigest()
        if input_hash == self.admin_password_hash:
            self.generate_token()
            return True
        return False

    def generate_token(self):
        self.token = secrets.token_hex(16)
        self.token_expire = datetime.now() + timedelta(days=7)

    def verify_token(self, token: str) -> bool:
        if not self.token or not self.token_expire:
            return False
        if datetime.now() > self.token_expire:
            return False
        return self.token == token

auth_manager = AuthManager()

# 获取管理员列表
driver = get_driver()
superusers = driver.config.superusers


@driver.on_startup
async def _startup_log_cleanup() -> None:
    task = getattr(driver, "_web_console_log_cleanup_task", None)
    if task is None or task.done():
        setattr(driver, "_web_console_log_cleanup_task", asyncio.create_task(_log_buffer_cleanup_loop()))


@driver.on_shutdown
async def _shutdown_log_cleanup() -> None:
    task = getattr(driver, "_web_console_log_cleanup_task", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

async def check_auth(request: Request):
    # 优先从 Header 获取，其次从 Query Params 获取（用于 <img> 标签）
    token = request.headers.get("Authorization") or request.query_params.get("token")
    if not token or not auth_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

try:
    _app = get_app()
except (ValueError, AssertionError):
    _app = None

# 只有在驱动器支持 FastAPI 时才挂载
if isinstance(_app, FastAPI):
    app = _app
else:
    app = None
    logger.warning("驱动器不支持 FastAPI，Web 控制台路由将无法访问。")

if app:
    static_path = Path(__file__).parent / "static"
    index_html = static_path / "index.html"

    # 挂载静态文件
    if static_path.exists():
        app.mount("/web_console/static", StaticFiles(directory=str(static_path)), name="web_console_static")

    # Web 控制台入口路由
    @app.get("/web_console", response_class=HTMLResponse)
    async def serve_console():
        if not index_html.exists():
            return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
        return HTMLResponse(content=index_html.read_text(encoding="utf-8"), status_code=200)

    # 兼容 /web_console/ 路径
    @app.get("/web_console/", response_class=HTMLResponse)
    async def serve_console_slash():
        return await serve_console()

# 消息缓存 {chat_id: [messages]}
message_cache: Dict[str, List[dict]] = {}
# 图片缓存 {url: {"content": bytes, "type": str}}
image_cache: Dict[str, dict] = {}
CACHE_SIZE = 100

# WebSocket 连接池
# active_connections defined at top


# 基础人设
def get_chat_id(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}"
    return f"private_{event.user_id}"

# 命令：获取控制台登录码
login_cmd = on_command("web控制台", aliases={"console", "控制台"}, permission=SUPERUSER, priority=1, block=True)
password_cmd = on_command("web密码", aliases={"修改web密码"}, permission=SUPERUSER, priority=1, block=True)

@password_cmd.handle()
async def handle_password_cmd(bot: Bot, event: MessageEvent):
    new_password = event.get_plaintext().strip().replace("web密码", "").replace("修改web密码", "").strip()
    if not new_password:
        await password_cmd.finish("请在命令后输入新密码，例如：web密码 mynewpassword")
    
    auth_manager.save_password(new_password)
    await password_cmd.finish(f"Web控制台密码已修改。\n请妥善保存。")

@login_cmd.handle()
async def handle_login_cmd(bot: Bot, event: MessageEvent):
    # 搜集所有可能的 IP
    ips = []
    
    # 1. 获取公网 IP
    try:
        async with httpx.AsyncClient() as client:
            # 尝试多个服务以提高可靠性
            for service in ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]:
                try:
                    resp = await client.get(service, timeout=3.0)
                    if resp.status_code == 200:
                        ip = resp.text.strip()
                        if ip and ip not in ips:
                            ips.append(ip)
                            break
                except:
                    continue
    except:
        pass

    # 2. 获取内网 IP (通用方法)
    import socket
    try:
        # 获取所有网卡信息
        interfaces = socket.getaddrinfo(socket.gethostname(), None)
        for iface in interfaces:
            if iface[0] == socket.AF_INET: # IPv4
                ip = iface[4][0]
                if ip not in ips and not ip.startswith("127."):
                    ips.append(ip)
    except:
        pass

    # 3. 备选内网 IP 获取 (UDP 技巧)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        main_ip = s.getsockname()[0]
        if main_ip not in ips:
            ips.append(main_ip)
        s.close()
    except:
        pass

    # 4. 获取所有网卡 IP (备选方法)
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            if info[0] == socket.AF_INET: # IPv4
                ip = info[4][0]
                if ip not in ips and not ip.startswith("127."):
                    ips.append(ip)
    except:
        pass

    # 5. 始终添加 127.0.0.1 (本地回环)
    if "127.0.0.1" not in ips:
        ips.append("127.0.0.1")
    
    port = get_driver().config.port
    
    # 按照分类构造消息
    public_ips = [ip for ip in ips if ip != "127.0.0.1"]
    
    msg_parts = ["【Web控制台】"]
    
    if public_ips:
        msg_parts.append("访问地址：")
        for i, ip in enumerate(public_ips):
            msg_parts.append(f" - http://{ip}:{port}/web_console")
            
    msg_parts.append(f"本地地址：http://127.0.0.1:{port}/web_console")
    
    code = auth_manager.generate_code()
    msg_parts.append(f"您的登录验证码为：{code}")
    msg_parts.append("5分钟内有效。")
    
    msg = "\n".join(msg_parts)
    
    if isinstance(event, PrivateMessageEvent):
        await login_cmd.finish(msg)
    else:
        try:
            await bot.send_private_msg(user_id=event.user_id, message=msg)
            await login_cmd.finish("访问地址与验证码已通过私聊发送给您，请查收。")
        except Exception as e:
            logger.error(f"发送私聊验证码失败: {e}")
            first_url = f"http://{public_ips[0]}:{port}/web_console" if public_ips else f"http://127.0.0.1:{port}/web_console"
            await login_cmd.finish(f"私聊发送失败，请确保您已添加机器人为好友。\n(当前环境访问地址提示：{first_url})")

# 辅助函数：解析消息段
def parse_message_elements(message_segments) -> List[dict]:
    elements = []
    
    # 鲁棒性处理：如果是字符串，尝试转为 Message 对象
    if isinstance(message_segments, str):
        try:
            # Message(str) 会自动解析 CQ 码（如果适配器支持）或作为纯文本
            message_segments = Message(message_segments)
        except Exception:
            # 降级处理
            return [{"type": "text", "data": {"text": message_segments}}]

    # 如果是 Message 对象，转为 list
    if hasattr(message_segments, "__iter__") and not isinstance(message_segments, (list, tuple)):
        # Message 对象迭代出来是 MessageSegment
        segments = list(message_segments)
    else:
        segments = message_segments

    for seg in segments:
        # 兼容 dict 和 MessageSegment
        if isinstance(seg, dict):
            seg_type = seg.get("type")
            seg_data = seg.get("data", {})
        else:
            seg_type = seg.type
            seg_data = seg.data
        
        if seg_type == "text":
            elements.append({"type": "text", "data": seg_data.get("text", "")})
        elif seg_type == "image":
            # 记录图片数据以便排查
            logger.debug(f"解析到图片数据: {seg_data}")
            # 优先从 get_msg 的数据中获取 url，NapCat 在 Linux 下可能返回 path 或 file 字段
            raw_url = seg_data.get("url") or seg_data.get("file") or seg_data.get("path") or ""
            
            # 代理链接不带 token，由前端动态注入或 check_auth 处理
            final_url = f"/web_console/proxy/image?url={quote(raw_url)}" if raw_url else ""
            if raw_url.startswith("data:image"):
                final_url = raw_url
                
            elements.append({"type": "image", "data": final_url, "raw": raw_url})
        elif seg_type == "face":
            face_id = seg_data.get("id")
            face_url = f"https://s.p.qq.com/pub/get_face?img_type=3&face_id={face_id}"
            elements.append({"type": "face", "data": face_url, "id": face_id})
        elif seg_type == "mface":
            url = seg_data.get("url")
            elements.append({"type": "image", "data": url})
        elif seg_type == "at":
            elements.append({"type": "at", "data": seg_data.get("qq")})
        elif seg_type == "reply":
            elements.append({"type": "reply", "data": seg_data.get("id")})
            
    return elements

# Hook: 监听 Bot API 调用，捕获发送的消息
async def on_api_called(bot: Bot, exception: Optional[Exception], api: str, data: Dict[str, Any], result: Any):
    if exception:
        return
        
    if api in ["send_group_msg", "send_private_msg", "send_msg"]:
        try:
            # Parse data
            message = data.get("message")
            if isinstance(message, str):
                msg_obj = Message(message)
            elif isinstance(message, list):
                # 假设是 list of dicts
                msg_obj = message 
            else:
                msg_obj = message
                
            elements = parse_message_elements(msg_obj)
            
            # Determine chat_id
            chat_id = ""
            if api == "send_group_msg":
                chat_id = f"group_{data.get('group_id')}"
            elif api == "send_private_msg":
                chat_id = f"private_{data.get('user_id')}"
            elif api == "send_msg":
                if data.get("message_type") == "group":
                    chat_id = f"group_{data.get('group_id')}"
                else:
                    chat_id = f"private_{data.get('user_id')}"
                    
            if not chat_id:
                return

            # Construct msg_data
            msg_id = 0
            if isinstance(result, dict):
                msg_id = result.get("message_id", 0)
            elif isinstance(result, int):
                msg_id = result
                
            # 获取 content 字符串表示
            content_str = str(message) if not isinstance(message, list) else "[Message]"
            if (
                _get_personification_provider() is not None
                and chat_id.startswith("group_")
                and result is not None
                and str(content_str).strip()
                and _is_personification_stack()
            ):
                _record_p13n_call(chat_id.replace("group_", "", 1))
            
            msg_data = {
                "id": msg_id,
                "chat_id": chat_id,
                "time": int(time.time()),
                "type": "group" if "group" in chat_id else "private",
                "sender_id": bot.self_id,
                "sender_name": "我",
                "sender_avatar": f"https://q1.qlogo.cn/g?b=qq&nk={bot.self_id}&s=640",
                "elements": elements,
                "content": content_str,
                "self_id": bot.self_id,
                "is_self": True
            }
            
            # Add to cache and broadcast
            if chat_id not in message_cache:
                message_cache[chat_id] = []
            
            message_cache[chat_id].append(msg_data)
            if len(message_cache[chat_id]) > CACHE_SIZE:
                message_cache[chat_id].pop(0)
                
            await broadcast_message({
                "type": "new_message",
                "chat_id": chat_id,
                "data": msg_data
            })
        except Exception as e:
            logger.error(f"处理 Bot 发送消息 Hook 失败: {e}")

@driver.on_bot_connect
async def _(bot: Bot):
    if hasattr(bot, "on_called_api"):
        bot.on_called_api(on_api_called)

# 监听所有消息
msg_matcher = on_message(priority=1, block=False)

@msg_matcher.handle()
async def handle_all_messages(bot: Bot, event: MessageEvent):
    chat_id = get_chat_id(event)
    
    # 尝试通过 get_msg 获取更详细的消息内容（尤其是 NapCat 等框架提供的 URL）
    sender_name = event.sender.nickname or str(event.user_id)
    try:
        msg_details = await bot.get_msg(message_id=event.message_id)
        message = msg_details["message"]
        # 如果 get_msg 返回了 sender 信息，则优先使用
        if "sender" in msg_details:
            sender_name = msg_details["sender"].get("nickname") or msg_details["sender"].get("card") or sender_name
    except Exception as e:
        logger.warning(f"获取消息详情失败: {e}，将使用事件自带消息内容")
        message = event.get_message()

    # 使用辅助函数解析消息内容
    elements = parse_message_elements(message)
    
    msg_data = {
        "id": event.message_id,
        "chat_id": chat_id,
        "time": event.time,
        "type": "group" if isinstance(event, GroupMessageEvent) else "private",
        "sender_id": event.user_id,
        "sender_name": sender_name,
        "sender_avatar": f"https://q1.qlogo.cn/g?b=qq&nk={event.user_id}&s=640",
        "elements": elements,
        "content": event.get_plaintext(),
        "self_id": bot.self_id,
        "is_self": False
    }
    
    # 存入缓存
    if chat_id not in message_cache:
        message_cache[chat_id] = []
    message_cache[chat_id].append(msg_data)
    if len(message_cache[chat_id]) > CACHE_SIZE:
        message_cache[chat_id].pop(0)
        
    # 通过 WebSocket 推送
    await broadcast_message({
        "type": "new_message",
        "chat_id": chat_id,
        "data": msg_data
    })


if app:
    # 认证 API
    @app.post("/web_console/api/send_code")
    async def send_code():
        if not superusers:
            return {"error": "未设置 SUPERUSERS 管理员列表"}
        
        code = auth_manager.generate_code()
        
        # 兼容多 Bot 场景
        from nonebot import get_bots
        bots = get_bots()
        if not bots:
            return {"error": "未连接任何 Bot"}
        bot = list(bots.values())[0]
        
        success_count = 0
        for user_id in superusers:
            try:
                await bot.send_private_msg(user_id=int(user_id), message=f"【Web控制台】您的登录验证码为：{code}，5分钟内有效。")
                success_count += 1
            except Exception as e:
                logger.error(f"发送验证码给管理员 {user_id} 失败: {e}")
                
        if success_count > 0:
            return {"msg": "验证码已发送至管理员 QQ"}
        return {"error": "验证码发送失败，请检查机器人是否在线或管理员账号是否正确"}

    @app.post("/web_console/api/login")
    async def login(data: dict):
        code = data.get("code")
        password = data.get("password")
        
        if code:
            if auth_manager.verify_code(code):
                return {"token": auth_manager.token}
            return {"error": "验证码错误或已过期", "code": 401}
        elif password:
            if auth_manager.verify_password(password):
                return {"token": auth_manager.token}
            return {"error": "密码错误", "code": 401}
            
        return {"error": "请输入验证码或密码", "code": 400}

    @app.get("/web_console/api/status", dependencies=[Depends(check_auth)])
    async def get_system_status():
        from nonebot import get_bots
        import psutil
        import platform
        import time
        import datetime
        
        # 系统性能
        cpu_percent = psutil.cpu_percent()
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('.')
        
        # 网络流量
        net_io = psutil.net_io_counters()
        
        # 运行时间
        uptime = time.time() - START_TIME
        uptime_str = str(datetime.timedelta(seconds=int(uptime)))
        
        # 机器人信息
        bots_info = []
        for bot_id, bot in get_bots().items():
            try:
                profile = await bot.get_login_info()
                bots_info.append({
                    "id": bot_id,
                    "nickname": profile.get("nickname", "未知"),
                    "avatar": f"https://q.qlogo.cn/headimg_dl?dst_uin={bot_id}&spec=640",
                    "status": "在线"
                })
            except:
                bots_info.append({
                    "id": bot_id,
                    "nickname": "机器人",
                    "avatar": f"https://q.qlogo.cn/headimg_dl?dst_uin={bot_id}&spec=640",
                    "status": "离线"
                })
                
        return {
            "system": {
                "os": platform.system(),
                "cpu": f"{cpu_percent}%",
                "memory": f"{memory.percent}%",
                "memory_used": f"{round(memory.used / 1024 / 1024 / 1024, 2)} GB",
                "memory_total": f"{round(memory.total / 1024 / 1024 / 1024, 2)} GB",
                "disk": f"{disk.percent}%",
                "disk_used": f"{round(disk.used / 1024 / 1024 / 1024, 2)} GB",
                "disk_total": f"{round(disk.total / 1024 / 1024 / 1024, 2)} GB",
                "net_sent": f"{round(net_io.bytes_sent / 1024 / 1024, 2)} MB",
                "net_recv": f"{round(net_io.bytes_recv / 1024 / 1024, 2)} MB",
                "uptime": uptime_str,
                "python": platform.python_version()
            },
            "bots": bots_info
        }

    @app.get("/web_console/api/logs", dependencies=[Depends(check_auth)])
    async def get_logs():
        _cleanup_expired_log_entries()
        return [_serialize_log_entry(log_entry) for log_entry in log_buffer]

    @app.get("/web_console/api/logs/download", dependencies=[Depends(check_auth)])
    async def download_logs():
        if not log_dir.exists():
             return Response("暂无日志目录", status_code=404)
        
        # 查找所有日志文件
        log_files = list(log_dir.glob("*.log*"))
        if not log_files:
            return Response("暂无日志文件", status_code=404)
            
        # 如果只有一个文件且是当前日志，直接返回
        if len(log_files) == 1 and log_files[0].name == "web_console_full.log":
            return FileResponse(log_files[0], filename="nonebot_full.log", media_type="text/plain")

        # 否则打包下载
        import zipfile
        import io
        
        # 创建内存中的 zip 文件
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for log_file in log_files:
                # 排除空文件或临时文件
                if log_file.is_file():
                    zip_file.write(log_file, arcname=log_file.name)
        
        zip_buffer.seek(0)
        
        # 返回流式响应
        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            zip_buffer, 
            media_type="application/zip", 
            headers={"Content-Disposition": f"attachment; filename=nonebot_logs_{int(time.time())}.zip"}
        )

    @app.get("/web_console/api/plugins", dependencies=[Depends(check_auth)])
    async def get_plugins():
        from nonebot import get_loaded_plugins
        from importlib.metadata import version, PackageNotFoundError, distributions
        import os
        plugins = []
        loaded_modules = set()
        
        # 1. 获取已加载的插件
        for p in get_loaded_plugins():
            loaded_modules.add(p.module_name)
            metadata = p.metadata
            
            # 识别插件来源
            plugin_type = "local"
            module_name = p.module_name
            
            if module_name.startswith("nonebot.plugins"):
                plugin_type = "builtin"
            elif metadata and metadata.homepage and ("github.com/nonebot" in metadata.homepage or "nonebot.dev" in metadata.homepage):
                plugin_type = "official"
            elif module_name.startswith("nonebot_plugin_"):
                plugin_type = "store"
                
            # 获取版本号
            ver = "0.0.0"
            pkg_names_to_try = [
                module_name,
                module_name.replace("_", "-"),
                f"nonebot-plugin-{module_name.split('.')[-1]}"
            ]
            
            for pkg_name in pkg_names_to_try:
                try:
                    ver = version(pkg_name)
                    break
                except PackageNotFoundError:
                    continue
            
            if ver == "0.0.0" and metadata:
                 if metadata.extra and "version" in metadata.extra:
                     ver = metadata.extra.get("version", "0.0.0")
                 elif hasattr(metadata, "version") and metadata.version:
                     ver = metadata.version

            plugins.append({
                "id": p.name,
                "name": metadata.name if metadata else p.name,
                "description": metadata.description if metadata else "暂无描述",
                "version": ver,
                "type": plugin_type,
                "module": module_name,
                "homepage": metadata.homepage if metadata else None,
                "enabled": True
            })

        # 2. 查找已安装但未加载的插件 (Disabled)
        # 仅扫描以 nonebot-plugin- 开头的包
        for dist in distributions():
            pkg_name = dist.metadata["Name"]
            if not pkg_name.startswith("nonebot-plugin-"):
                continue
                
            # 推测模块名：将连字符替换为下划线
            module_name = pkg_name.replace("-", "_")
            
            # 如果该模块已加载，则跳过
            if module_name in loaded_modules:
                continue
                
            # 某些插件模块名可能与包名差异较大，这里尝试简单的 heuristic
            # 也可以尝试读取 dist.files 查找 top_level.txt，但比较耗时
            # 这里简单处理，如果不匹配则视为未加载
            
            # 排除 web console 自身 (虽然它通常是加载的)
            if pkg_name == "nonebot-plugin-shiro-web-console":
                continue

            plugins.append({
                "id": pkg_name,
                "name": pkg_name, # 未加载插件可能无法获取详细元数据，使用包名
                "description": dist.metadata.get("Summary", "未加载的插件"),
                "version": dist.version,
                "type": "store", # 假设都是 store 插件
                "module": module_name,
                "homepage": dist.metadata.get("Home-page"),
                "enabled": False
            })
            
        return plugins

    @app.post("/web_console/api/system/action", dependencies=[Depends(check_auth)])
    async def system_action(request: Request):
        data = await request.json()
        action = data.get("action")
        confirm = data.get("confirm")
        
        if action not in ["reboot", "shutdown"]:
            return {"error": "无效操作"}
            
        if not confirm:
            return {"error": "请确认操作", "need_confirm": True}
            
        import os
        import sys
        import subprocess
        import asyncio
        
        logger.warning(f"收到系统指令: {action}")
        
        if action == "shutdown":
            # 延迟执行关闭，确保响应能发出去
            loop = asyncio.get_event_loop()
            loop.call_later(1.0, lambda: os._exit(0))
            return {"msg": "Bot 正在关闭..."}
            
        elif action == "reboot":
            # 获取项目根目录 (通常是当前工作目录)
            root_dir = Path.cwd()
            bot_py = root_dir / "bot.py"
            
            if bot_py.exists():
                cmd = [sys.executable, str(bot_py)]
            else:
                cmd = [sys.executable] + sys.argv
                
            def do_reboot():
                try:
                    if sys.platform == "win32":
                        subprocess.Popen(cmd, cwd=str(root_dir))
                        os._exit(0)
                    else:
                        os.chdir(root_dir)
                        os.execv(sys.executable, cmd)
                except Exception as e:
                    logger.error(f"重启执行失败: {e}")
                    os._exit(1)

            # 延迟执行重启
            loop = asyncio.get_event_loop()
            loop.call_later(1.0, do_reboot)
            return {"msg": "Bot 正在重启..."}

    @app.get("/web_console/api/plugins/{plugin_id}/config", dependencies=[Depends(check_auth)])
    async def get_plugin_config(plugin_id: str):
        from nonebot import get_loaded_plugins, get_driver
        
        # 查找插件
        target_plugin = None
        for p in get_loaded_plugins():
            if p.name == plugin_id:
                target_plugin = p
                break
                
        if not target_plugin:
            raise HTTPException(status_code=404, detail="Plugin not found")
            
        # 获取配置元数据 (NoneBot 插件通常通过 metadata.config 导出 Config 类)
        config_schema = {}
        current_config = {}
        
        if target_plugin.metadata and target_plugin.metadata.config:
            try:
                config_class = target_plugin.metadata.config
                if hasattr(config_class, "schema"):
                    schema = config_class.schema()
                    config_schema = schema.get("properties", {})
                    # 注入当前值
                    driver_config = get_driver().config
                    for key in config_schema:
                        current_config[key] = getattr(driver_config, key, None)
            except Exception as e:
                logger.error(f"解析插件 {plugin_id} 配置失败: {e}")
                
        return {"config": current_config, "schema": config_schema}

    # --- 插件商店相关 API ---

    STORE_URL = "https://registry.nonebot.dev/plugins.json"
    store_cache = {"data": [], "time": 0}

    @app.get("/web_console/api/store", dependencies=[Depends(check_auth)])
    async def get_store():
        # 缓存 1 小时
        if not store_cache["data"] or time.time() - store_cache["time"] > 3600:
            try:
                async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
                    resp = await client.get(STORE_URL, timeout=15.0)
                    if resp.status_code == 200:
                        store_cache["data"] = resp.json()
                        store_cache["time"] = time.time()
                    else:
                        logger.error(f"获取 NoneBot 商店数据失败: HTTP {resp.status_code}")
            except Exception as e:
                logger.error(f"获取 NoneBot 商店数据失败: {e}")
                # 如果之前有缓存，即使失败也返回旧缓存，避免页面空白
                if store_cache["data"]:
                    return store_cache["data"]
                return {"error": "无法连接到 NoneBot 商店，请检查服务器网络或稍后再试"}
                
        return store_cache["data"]

    @app.post("/web_console/api/store/action", dependencies=[Depends(check_auth)])
    async def store_action(request: Request):
        # 尝试获取锁，如果已被占用则立即返回错误，或等待
        # 这里选择等待，确保操作按顺序执行
        if store_lock.locked():
             logger.warning("插件操作正在进行中，请求已排队...")
        
        async with store_lock:
            data = await request.json()
            action = data.get("action")  # install, update, uninstall, enable, disable
            plugin_name = data.get("plugin")
            
            if not action or not plugin_name:
                return {"error": "参数错误"}
                
            if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_name):
                return {"error": "非法插件名称"}
            
            # 获取项目根目录 (通常是当前工作目录)
            root_dir = Path.cwd()

            # 处理启用/禁用操作
            if action in ["enable", "disable"]:
                try:
                    import tomlkit
                    pyproject_path = root_dir / "pyproject.toml"
                    if not pyproject_path.exists():
                        return {"error": "未找到 pyproject.toml，无法管理插件状态"}
                    
                    content = pyproject_path.read_text(encoding="utf-8")
                    doc = tomlkit.parse(content)
                    
                    if "tool" not in doc or "nonebot" not in doc["tool"]:
                        return {"error": "pyproject.toml 中缺少 [tool.nonebot] 配置"}
                        
                    plugins_config = doc["tool"]["nonebot"].get("plugins", [])
                    
                    # 确保是列表
                    if not isinstance(plugins_config, list):
                         plugins_config = list(plugins_config)
                    
                    # 确保使用模块名 (下划线)
                    target_module = plugin_name.replace("-", "_")
                    
                    changed = False
                    if action == "disable":
                        if target_module in plugins_config:
                            plugins_config.remove(target_module)
                            changed = True
                        else:
                             return {"msg": f"插件 {target_module} 已禁用或未在配置中"}
                    elif action == "enable":
                        if target_module not in plugins_config:
                            plugins_config.append(target_module)
                            changed = True
                        else:
                             return {"msg": f"插件 {target_module} 已启用"}
                             
                    if changed:
                        doc["tool"]["nonebot"]["plugins"] = plugins_config
                        pyproject_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
                        return {"msg": f"插件 {target_module} 已{action == 'enable' and '启用' or '禁用'}，请重启机器人生效。"}
                    
                    return {"msg": "配置未变更"}
                    
                except ImportError:
                    return {"error": "缺少 tomlkit 库，无法修改配置"}
                except Exception as e:
                    logger.error(f"修改 pyproject.toml 失败: {e}")
                    return {"error": f"修改配置文件失败: {e}"}

            # 规范化插件名称：将下划线替换为连字符，nb cli 通常使用连字符格式的包名
            plugin_name = plugin_name.replace("_", "-")

            # 执行命令
            import asyncio
            import sys
            
            # 构建命令
            cmd = []
            # 尝试定位 nb 命令
            import shutil
            nb_cmd = []
            nb_path = shutil.which("nb")
            
            if nb_path:
                nb_cmd = [nb_path]
            else:
                # 如果系统 PATH 中找不到，再尝试在 Python 脚本目录下找
                script_dir = os.path.dirname(sys.executable)
                possible_nb = os.path.join(script_dir, "nb.exe" if sys.platform == "win32" else "nb")
                if os.path.exists(possible_nb):
                    nb_cmd = [possible_nb]
                else:
                    # 尝试使用 python -m nb_cli
                    try:
                        import nb_cli
                        nb_cmd = [sys.executable, "-m", "nb_cli"]
                    except ImportError:
                        nb_cmd = ["nb"] # 最后的保底，尝试直接运行 nb

            # 获取项目根目录 (通常是当前工作目录)
            root_dir = Path.cwd()

            if action == "install":
                cmd = nb_cmd + ["plugin", "install", plugin_name]
            elif action == "update":
                cmd = nb_cmd + ["plugin", "update", plugin_name]
            elif action == "uninstall":
                cmd = nb_cmd + ["plugin", "uninstall", plugin_name]
            else:
                return {"error": "无效操作"}
                
            logger.info(f"开始执行插件操作: {' '.join(cmd)} (工作目录: {root_dir})")
            
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(root_dir)
                )
                
                stdout_bytes, stderr_bytes = await process.communicate()
                
                def safe_decode(data: bytes) -> str:
                    if not data:
                        return ""
                    for encoding in ["utf-8", "gbk", "cp936"]:
                        try:
                            return data.decode(encoding).strip()
                        except UnicodeDecodeError:
                            continue
                    return data.decode("utf-8", errors="replace").strip()

                stdout = safe_decode(stdout_bytes)
                stderr = safe_decode(stderr_bytes)
                
                if process.returncode == 0:
                    msg = f"插件 {plugin_name} {action} 成功"
                    logger.info(msg)
                    return {"msg": msg, "output": stdout}
                else:
                    error_msg = stderr or stdout
                    logger.error(f"插件操作失败: {error_msg}")
                    return {"error": error_msg}
                    
            except Exception as e:
                logger.error(f"执行插件命令时发生异常: {e}")
                return {"error": str(e)}

    @app.post("/web_console/api/plugins/{plugin_id}/config", dependencies=[Depends(check_auth)])
    async def update_plugin_config(plugin_id: str, new_config: dict):
        # 尝试更新 .env 文件
        env_path = Path.cwd() / ".env"
        # 简单查找逻辑
        if not env_path.exists():
            for name in [".env.prod", ".env.dev"]:
                p = Path.cwd() / name
                if p.exists():
                    env_path = p
                    break
        
        try:
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
                lines = content.splitlines()
                new_lines = []
                keys_updated = set()
                
                for line in lines:
                    line_strip = line.strip()
                    if not line_strip or line_strip.startswith("#"):
                        new_lines.append(line)
                        continue
                        
                    if "=" in line:
                        key = line.split("=", 1)[0].strip()
                        if key in new_config:
                            val = new_config[key]
                            if isinstance(val, bool):
                                val_str = str(val).lower()
                            else:
                                val_str = str(val)
                            new_lines.append(f"{key}={val_str}")
                            keys_updated.add(key)
                        else:
                            new_lines.append(line)
                    else:
                        new_lines.append(line)
                
                # 追加新配置
                for key, val in new_config.items():
                    if key not in keys_updated:
                        if isinstance(val, bool):
                            val_str = str(val).lower()
                        else:
                            val_str = str(val)
                        new_lines.append(f"{key}={val_str}")
                
                env_path.write_text("\n".join(new_lines), encoding="utf-8")
                logger.info(f"已更新配置文件 {env_path}")
            else:
                logger.warning("未找到 .env 文件，无法持久化配置")
                
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return {"error": str(e)}

        logger.info(f"收到插件 {plugin_id} 的新配置: {new_config}")
        return {"success": True, "msg": "配置已保存至 .env (需重启生效)"}

    # API 路由
    @app.get("/web_console/api/chats", dependencies=[Depends(check_auth)])
    async def get_chats():
        try:
            from nonebot import get_bots
            bots = get_bots()
            if not bots:
                return {"error": "No bot connected"}
            bot = list(bots.values())[0]

            if not isinstance(bot, Bot):
                return {"error": "Only OneBot v11 is supported"}
            
            groups = await bot.get_group_list()
            friends = await bot.get_friend_list()
            
            return {
                "groups": [
                    {
                        "id": f"group_{g['group_id']}",
                        "name": g['group_name'],
                        "avatar": f"https://p.qlogo.cn/gh/{g['group_id']}/{g['group_id']}/640"
                    } for g in groups
                ],
                "private": [
                    {
                        "id": f"private_{f['user_id']}",
                        "name": f['nickname'] or f['remark'] or str(f['user_id']),
                        "avatar": f"https://q1.qlogo.cn/g?b=qq&nk={f['user_id']}&s=640"
                    } for f in friends
                ]
            }
        except Exception as e:
            return {"error": str(e)}

    @app.get("/web_console/api/history/{chat_id}", dependencies=[Depends(check_auth)])
    async def get_history(chat_id: str):
        # 优先返回缓存
        if chat_id in message_cache and len(message_cache[chat_id]) > 0:
            return message_cache[chat_id]
            
        # 尝试从 Bot 获取历史消息 (OneBot v11 get_group_msg_history)
        try:
            from nonebot import get_bots
            bots = get_bots()
            if bots:
                bot = list(bots.values())[0]
                if chat_id.startswith("group_"):
                    group_id = int(chat_id.replace("group_", ""))
                    # 尝试调用 NapCat/Go-CQHTTP 的 get_group_msg_history
                    res = await bot.call_api("get_group_msg_history", group_id=group_id)
                    messages = res.get("messages", [])
                    
                    parsed_msgs = []
                    for raw in messages:
                        # raw: {message_id, time, sender: {...}, message: [...], raw_message: ...}
                        sender = raw.get("sender", {})
                        sender_id = sender.get("user_id") or 0
                        is_self = str(sender_id) == str(bot.self_id)
                        
                        parsed_msgs.append({
                            "id": raw.get("message_id"),
                            "chat_id": chat_id,
                            "time": raw.get("time"),
                            "type": "group",
                            "sender_id": sender_id,
                            "sender_name": sender.get("nickname") or sender.get("card") or str(sender_id),
                            "sender_avatar": f"https://q1.qlogo.cn/g?b=qq&nk={sender_id}&s=640",
                            "elements": parse_message_elements(raw.get("message", [])),
                            "content": raw.get("raw_message", ""),
                            "self_id": bot.self_id,
                            "is_self": is_self
                        })
                    
                    if parsed_msgs:
                        message_cache[chat_id] = parsed_msgs[-CACHE_SIZE:]
                        return message_cache[chat_id]
        except Exception as e:
            logger.warning(f"获取历史消息失败: {e}")
            
        return message_cache.get(chat_id, [])

    @app.get("/web_console/proxy/image", dependencies=[Depends(check_auth)])
    async def proxy_image(url: str):
        url = unquote(url)
        
        # 处理 file:// 协议头 (Linux 下常见)
        if url.startswith("file://"):
            url = url.replace("file:///", "/").replace("file://", "")
            # 在 Windows 下剥离开头的斜杠，例如 /C:/Users -> C:/Users
            if os.name == "nt" and url.startswith("/") and ":" in url:
                url = url.lstrip("/")
                
        if url.startswith("http"):
            # 尝试从缓存获取
            if url in image_cache:
                return Response(content=image_cache[url]["content"], media_type=image_cache[url]["type"])
            
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, timeout=10.0, follow_redirects=True)
                    if resp.status_code == 200:
                        content = resp.content
                        media_type = resp.headers.get("content-type", "image/jpeg")
                        # 写入缓存
                        if len(image_cache) >= CACHE_SIZE:
                            image_cache.pop(next(iter(image_cache)))
                        image_cache[url] = {"content": content, "type": media_type}
                        return Response(content=content, media_type=media_type)
            except Exception as e:
                logger.error(f"代理图片下载失败: {e}")
                
        # 尝试作为本地路径处理
        try:
            path = Path(url).resolve()
            # 安全检查：只允许访问当前工作目录下的文件
            if not str(path).startswith(str(Path.cwd())):
                 return Response(status_code=403)
            
            if path.exists() and path.is_file():
                return FileResponse(str(path))
        except Exception as e:
            logger.error(f"本地图片读取失败: {e}")
            
        return Response(status_code=404)

    @app.post("/web_console/api/send", dependencies=[Depends(check_auth)])
    async def send_message(data: dict):
        try:
            from nonebot import get_bots
            bots = get_bots()
            if not bots:
                return {"error": "No bot connected"}
            bot = list(bots.values())[0]

            chat_id = data.get("chat_id")
            content = data.get("content")
            
            if not chat_id or not content:
                return {"error": "Invalid data"}
            
            if chat_id.startswith("group_"):
                group_id = int(chat_id.replace("group_", ""))
                await bot.send_group_msg(group_id=group_id, message=content)
            else:
                user_id = int(chat_id.replace("private_", ""))
                await bot.send_private_msg(user_id=user_id, message=content)
                
            return {"status": "ok"}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/web_console/api/personification/status", dependencies=[Depends(check_auth)])
    async def get_personification_status():
        provider = _get_personification_provider()
        if provider is None:
            return {
                "available": False,
                "error": "未检测到已加载的 nonebot-plugin-shiro-personification 或 personification",
            }

        try:
            global_state = _load_p13n_global_state(provider)
            group_configs = _load_p13n_group_configs_safe(provider)
            config_whitelist = list(getattr(provider.plugin_config, "personification_whitelist", []) or [])
            runtime_whitelist = provider.load_whitelist()
            if not isinstance(runtime_whitelist, list):
                runtime_whitelist = []
            groups: Dict[str, Dict[str, Any]] = {}

            for group_id in _collect_p13n_group_ids(provider, group_configs):
                group_key = f"group_{group_id}"
                try:
                    group_config = provider.get_group_config(group_id)
                    if not isinstance(group_config, dict):
                        group_config = {}
                    whitelisted = group_id in config_whitelist or group_id in runtime_whitelist
                    enabled = bool(group_config.get("enabled")) if "enabled" in group_config else whitelisted
                    session_history = provider.chat_histories.get(group_key, [])
                    recent_messages = provider.get_recent_group_msgs(group_id)
                    groups[group_key] = {
                        "enabled": enabled,
                        "whitelisted": whitelisted,
                        "sticker_enabled": bool(group_config.get("sticker_enabled", True)),
                        "schedule_enabled": bool(group_config.get("schedule_enabled", False)),
                        "tts_enabled": bool(group_config.get("tts_enabled", True)),
                        "proactive_enabled": bool(global_state.get("proactive_enabled", False)),
                        "custom_prompt": group_config.get("custom_prompt") or "",
                        "session_history_len": len(session_history) if isinstance(session_history, list) else 0,
                        "recent_messages_count": len(recent_messages) if isinstance(recent_messages, list) else 0,
                    }
                except Exception as e:
                    logger.error(f"读取拟人插件群 {group_id} 状态失败: {e}")
                    groups[group_key] = {
                        "enabled": False,
                        "whitelisted": False,
                        "sticker_enabled": False,
                        "schedule_enabled": False,
                        "tts_enabled": False,
                        "proactive_enabled": bool(global_state.get("proactive_enabled", False)),
                        "custom_prompt": "",
                        "session_history_len": 0,
                        "recent_messages_count": 0,
                        "error": str(e),
                    }

            return {
                "available": True,
                "provider": {
                    "variant": provider.variant,
                    "module_name": provider.module_name,
                    "display_name": provider.display_name,
                },
                "groups": groups,
                "global": global_state,
            }
        except Exception as e:
            logger.error(f"获取拟人插件状态失败: {e}")
            return {"available": False, "error": str(e)}

    @app.get("/web_console/api/personification/config", dependencies=[Depends(check_auth)])
    async def get_personification_config():
        provider = _get_personification_provider()
        if provider is None:
            return {
                "available": False,
                "error": "未检测到已加载的 nonebot-plugin-shiro-personification 或 personification",
            }

        try:
            return {
                "available": True,
                "provider": {
                    "variant": provider.variant,
                    "module_name": provider.module_name,
                    "display_name": provider.display_name,
                },
                "entries": _build_p13n_global_entries(provider),
            }
        except Exception as e:
            logger.error(f"读取拟人插件全局配置失败: {e}")
            return {"available": False, "error": str(e)}

    @app.post("/web_console/api/personification/config", dependencies=[Depends(check_auth)])
    async def update_personification_config(data: dict):
        provider = _get_personification_provider()
        if provider is None:
            return {
                "available": False,
                "error": "未检测到已加载的 nonebot-plugin-shiro-personification 或 personification",
            }

        runtime_config = _load_p13n_effective_plugin_config(provider)
        if runtime_config is None:
            return {"available": False, "error": "无法读取拟人插件运行时配置"}

        runtime_entry_map = {entry["key"]: entry for entry in _P13N_RUNTIME_GLOBAL_ENTRIES}
        registry_entry_map = {entry.key: entry for entry in provider.get_config_entries("global")}

        try:
            for key, raw_value in (data or {}).items():
                if key in runtime_entry_map:
                    entry = runtime_entry_map[key]
                    normalized_value = _normalize_p13n_value(
                        raw_value,
                        str(entry.get("value_type") or "str"),
                        choices=list(entry.get("choices", []) or []),
                    )
                    setattr(runtime_config, entry["field_name"], normalized_value)
                    continue
                if key in registry_entry_map:
                    entry = registry_entry_map[key]
                    normalized_value = (
                        entry.normalize_value(raw_value)
                        if hasattr(entry, "normalize_value")
                        else _normalize_p13n_value(
                            raw_value,
                            str(getattr(entry, "value_type", "str") or "str"),
                            choices=list(getattr(entry, "choices", ()) or ()),
                        )
                    )
                    setattr(runtime_config, entry.field_name, normalized_value)
                    continue
                raise ValueError(f"不支持的拟人全局配置项: {key}")

            _write_p13n_runtime_config(provider, runtime_config)
            return {
                "available": True,
                "provider": {
                    "variant": provider.variant,
                    "module_name": provider.module_name,
                    "display_name": provider.display_name,
                },
                "entries": _build_p13n_global_entries(provider),
            }
        except Exception as e:
            logger.error(f"更新拟人插件全局配置失败: {e}")
            return {"available": False, "error": str(e)}

    @app.get("/web_console/api/personification/stats", dependencies=[Depends(check_auth)])
    async def get_personification_stats():
        if _get_personification_provider() is None:
            return {"available": False, "groups": {}}

        try:
            groups = {
                key: {
                    "total": int(value.get("total", 0)),
                    "daily": dict(sorted((value.get("daily") or {}).items())),
                    "hourly": dict(sorted((value.get("hourly") or {}).items())),
                }
                for key, value in sorted(_group_call_stats.items())
                if key.startswith("group_") and isinstance(value, dict)
            }
            return {"available": True, "groups": groups}
        except Exception as e:
            logger.error(f"获取拟人插件调用统计失败: {e}")
            return {"available": False, "groups": {}, "error": str(e)}

    @app.get("/web_console/api/personification/group/{group_id}/config", dependencies=[Depends(check_auth)])
    async def get_personification_group_config(group_id: str):
        provider = _get_personification_provider()
        if provider is None:
            return {
                "available": False,
                "error": "未检测到已加载的 nonebot-plugin-shiro-personification 或 personification",
            }

        normalized_group_id = str(group_id).replace("group_", "", 1)
        try:
            return {
                "available": True,
                "provider": {
                    "variant": provider.variant,
                    "module_name": provider.module_name,
                    "display_name": provider.display_name,
                },
                "group_id": f"group_{normalized_group_id}",
                "entries": _build_p13n_group_entries(provider, normalized_group_id),
            }
        except Exception as e:
            logger.error(f"读取拟人插件群配置失败: {e}")
            return {"available": False, "error": str(e)}

    @app.post("/web_console/api/personification/group/{group_id}/config", dependencies=[Depends(check_auth)])
    async def update_personification_group_config(group_id: str, data: dict):
        provider = _get_personification_provider()
        if provider is None:
            return {
                "available": False,
                "error": "未检测到已加载的 nonebot-plugin-shiro-personification 或 personification",
            }

        normalized_group_id = str(group_id).replace("group_", "", 1)

        try:
            for key, raw_value in (data or {}).items():
                if key == "enabled":
                    if provider.set_group_enabled is None:
                        raise ValueError("当前拟人插件未提供群开关设置接口")
                    provider.set_group_enabled(normalized_group_id, _parse_p13n_bool(raw_value))
                    continue
                if key == "sticker_enabled":
                    if provider.set_group_sticker_enabled is None:
                        raise ValueError("当前拟人插件未提供表情包设置接口")
                    provider.set_group_sticker_enabled(normalized_group_id, _parse_p13n_bool(raw_value))
                    continue
                if key == "schedule_enabled":
                    if provider.set_group_schedule_enabled is None:
                        raise ValueError("当前拟人插件未提供作息模拟设置接口")
                    provider.set_group_schedule_enabled(normalized_group_id, _parse_p13n_bool(raw_value))
                    continue
                if key == "tts_enabled":
                    if provider.set_group_tts_enabled is None:
                        raise ValueError("当前拟人插件未提供语音设置接口")
                    provider.set_group_tts_enabled(normalized_group_id, _parse_p13n_bool(raw_value))
                    continue
                if key == "custom_prompt":
                    if provider.set_group_prompt is None:
                        raise ValueError("当前拟人插件未提供自定义 Prompt 设置接口")
                    custom_prompt = None if raw_value in [None, ""] else str(raw_value)
                    provider.set_group_prompt(normalized_group_id, custom_prompt)
                    continue
                if key == "whitelisted":
                    whitelist_enabled = _parse_p13n_bool(raw_value)
                    if whitelist_enabled:
                        if provider.add_group_to_whitelist is None:
                            raise ValueError("当前拟人插件未提供白名单写入接口")
                        provider.add_group_to_whitelist(normalized_group_id)
                    else:
                        if provider.remove_group_from_whitelist is None:
                            raise ValueError("当前拟人插件未提供白名单写入接口")
                        provider.remove_group_from_whitelist(normalized_group_id)
                    continue
                raise ValueError(f"不支持的拟人群配置项: {key}")

            return {
                "available": True,
                "group_id": f"group_{normalized_group_id}",
                "config": provider.get_group_config(normalized_group_id),
                "entries": _build_p13n_group_entries(provider, normalized_group_id),
            }
        except Exception as e:
            logger.error(f"更新拟人插件群配置失败: {e}")
            return {"available": False, "error": str(e)}

    # WebSocket 端点
    @app.websocket("/web_console/ws")
    async def websocket_endpoint(websocket: WebSocket):
        token = websocket.query_params.get("token")
        if not token or not auth_manager.verify_token(token):
            await websocket.close(code=1008)
            return
            
        await websocket.accept()
        active_connections.add(websocket)
        try:
            while True:
                # 保持连接，接收心跳或其他
                await websocket.receive_text()
        except WebSocketDisconnect:
            active_connections.discard(websocket)
        except Exception:
            active_connections.discard(websocket)
