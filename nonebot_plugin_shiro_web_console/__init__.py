import asyncio
import json
import httpx
import os
import random
import time
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote, unquote
from typing import Dict, List, Set, Optional, Any
from pathlib import Path
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from nonebot import get_app, get_bot, get_driver, logger, on_message, on_command, require
require("nonebot_plugin_localstore")
import nonebot_plugin_localstore
from .config import Config, config
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="Shiro Web Console",
    description="通过浏览器查看日志、管理机器人并发送消息",
    usage="访问 /web_console 查看，在机器人聊天框发送“web控制台”获取登录码",
    type="application",
    homepage="https://github.com/luojisama/nonebot-plugin-shiro-web-console",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={
        "author": "luojisama",
        "version": "0.1.6",
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
        active_connections.remove(ws)

# 日志缓冲区，保留最近 200 条日志
log_buffer = deque(maxlen=200)

async def log_sink(message):
    log_entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": message.record["level"].name,
        "message": message.record["message"],
        "module": message.record["module"]
    }
    log_buffer.append(log_entry)
    # 推送日志
    await broadcast_message({
        "type": "new_log",
        "data": log_entry
    })

# 注册 loguru sink
logger.add(log_sink, format="{time} {level} {message}", level="INFO")

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
        self.admin_password = self._load_password()

    def _load_password(self) -> str:
        if self.password_file.exists():
            try:
                data = json.loads(self.password_file.read_text(encoding="utf-8"))
                return data.get("password", "admin123")
            except:
                pass
        return config.web_console_password

    def save_password(self, new_password: str):
        self.admin_password = new_password
        self.password_file.write_text(json.dumps({"password": new_password}), encoding="utf-8")

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
        if password == self.admin_password:
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
    await password_cmd.finish(f"Web控制台密码已修改为: {new_password}\n请妥善保存。")

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

    # 消息内容解析
    elements = []
    # 消息唯一 ID：使用适配器提供的原始 ID，以便前端去重
    msg_id = str(event.message_id)
    
    # 如果 message 是列表（get_msg 返回格式），直接遍历；如果是 Message 对象，也可以遍历
    for seg in message:
        # 处理 get_msg 返回的字典格式或 MessageSegment 对象
        seg_type = seg["type"] if isinstance(seg, dict) else seg.type
        seg_data = seg["data"] if isinstance(seg, dict) else seg.data
        
        if seg_type == "text":
            elements.append({"type": "text", "data": seg_data.get("text", "")})
        elif seg_type == "image":
            # 记录图片数据以便排查
            logger.debug(f"解析到图片数据: {seg_data}")
            # 优先从 get_msg 的数据中获取 url，NapCat 在 Linux 下可能返回 path 或 file 字段
            raw_url = seg_data.get("url") or seg_data.get("file") or seg_data.get("path") or ""
            
            # 如果是本地文件路径或非 http 开头的，构造代理 URL
            if raw_url and not raw_url.startswith("http"):
                # 这种情况下可能是 base64 或者本地路径，或者是 CQ 码中的 file 字段
                # 我们先尝试直接传给前端，由前端处理或后续通过代理获取
                pass
            
            # 为了确保显示，所有图片都尝试通过代理中转，除非是 base64
            if raw_url.startswith("data:image"):
                final_url = raw_url
            else:
                # 代理链接不带 token，由前端动态注入或 check_auth 处理
                final_url = f"/web_console/proxy/image?url={quote(raw_url)}" if raw_url else ""
            
            elements.append({"type": "image", "data": final_url, "raw": raw_url})
        elif seg_type == "face":
            face_id = seg_data.get("id")
            face_url = f"https://s.p.qq.com/pub/get_face?img_type=3&face_id={face_id}"
            elements.append({"type": "face", "data": face_url, "id": face_id})
        elif seg_type == "mface":
            # 商城表情（Stickers）
            url = seg_data.get("url")
            elements.append({"type": "image", "data": url})
        elif seg_type == "at":
            elements.append({"type": "at", "data": seg_data.get("qq")})
        elif seg_type == "reply":
            elements.append({"type": "reply", "data": seg_data.get("id")})
    
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
        bot = get_bot()
        
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
        disk = psutil.disk_usage('/')
        
        # 网络流量
        net_io = psutil.net_io_counters()
        
        # 运行时间
        boot_time = psutil.boot_time()
        uptime = time.time() - boot_time
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
        return list(log_buffer)

    @app.get("/web_console/api/plugins", dependencies=[Depends(check_auth)])
    async def get_plugins():
        from nonebot import get_loaded_plugins
        import os
        plugins = []
        for p in get_loaded_plugins():
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
                
            plugins.append({
                "id": p.name,
                "name": metadata.name if metadata else p.name,
                "description": metadata.description if metadata else "暂无描述",
                "version": metadata.extra.get("version", "1.0.0") if metadata and metadata.extra else "1.0.0",
                "type": plugin_type,
                "module": module_name,
                "homepage": metadata.homepage if metadata else None
            })
        return plugins

    @app.post("/web_console/api/system/action", dependencies=[Depends(check_auth)])
    async def system_action(request: Request):
        data = await request.json()
        action = data.get("action")
        
        if action not in ["reboot", "shutdown"]:
            return {"error": "无效操作"}
            
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
        data = await request.json()
        action = data.get("action")  # install, update, uninstall
        plugin_name = data.get("plugin")
        
        if not action or not plugin_name:
            return {"error": "参数错误"}
            
        # 执行命令
        import asyncio
        import sys
        
        # 构建命令
        cmd = []
        # 尝试定位 nb 命令
        import shutil
        nb_path = shutil.which("nb")
        
        if not nb_path:
            # 如果系统 PATH 中找不到，再尝试在 Python 脚本目录下找
            script_dir = os.path.dirname(sys.executable)
            possible_nb = os.path.join(script_dir, "nb.exe" if sys.platform == "win32" else "nb")
            if os.path.exists(possible_nb):
                nb_path = possible_nb
            else:
                nb_path = "nb" # 最后的保底，尝试直接运行 nb

        # 获取项目根目录 (通常是当前工作目录)
        root_dir = Path.cwd()

        if action == "install":
            cmd = [nb_path, "plugin", "install", plugin_name]
        elif action == "update":
            cmd = [nb_path, "plugin", "update", plugin_name]
        elif action == "uninstall":
            cmd = [nb_path, "plugin", "uninstall", plugin_name]
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
        # 这里仅做模拟保存逻辑，实际应用中通常需要修改 .env 文件或数据库
        # 由于直接修改运行中的 config 风险较大，此处返回成功，并提示需要重启
        logger.info(f"收到插件 {plugin_id} 的新配置: {new_config}")
        return {"success": True}

    # API 路由
    @app.get("/web_console/api/chats", dependencies=[Depends(check_auth)])
    async def get_chats():
        try:
            bot = get_bot()
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
            path = Path(url)
            if path.exists() and path.is_file():
                return FileResponse(str(path))
        except Exception as e:
            logger.error(f"本地图片读取失败: {e}")
            
        return Response(status_code=404)

    @app.post("/web_console/api/send", dependencies=[Depends(check_auth)])
    async def send_message(data: dict):
        try:
            bot = get_bot()
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
                
            # 发送成功后手动添加一条自己的消息到缓存并推送
            my_msg = {
                "id": 0,
                "time": int(time.time()),
                "type": "group" if chat_id.startswith("group_") else "private",
                "sender_id": bot.self_id,
                "sender_name": "我",
                "sender_avatar": f"https://q1.qlogo.cn/g?b=qq&nk={bot.self_id}&s=640",
                "elements": [{"type": "text", "data": content}],
                "content": content,
                "is_self": True
            }
            
            if chat_id not in message_cache:
                message_cache[chat_id] = []
            message_cache[chat_id].append(my_msg)
            
            await broadcast_message({
                "type": "new_message",
                "chat_id": chat_id,
                "data": my_msg
            })
            
            return {"status": "ok"}
        except Exception as e:
            return {"error": str(e)}

    # WebSocket 端点
    @app.websocket("/web_console/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        active_connections.add(websocket)
        try:
            while True:
                # 保持连接，接收心跳或其他
                await websocket.receive_text()
        except WebSocketDisconnect:
            active_connections.remove(websocket)
        except Exception:
            if websocket in active_connections:
                active_connections.remove(websocket)
