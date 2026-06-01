# nonebot-plugin-shiro-web-console

一个用于 NoneBot2 的网页控制台插件，支持通过浏览器查看日志和发送消息。

## 安装

```bash
nb plugin install nonebot-plugin-shiro-web-console
```

或者使用 pip:

```bash
pip install nonebot-plugin-shiro-web-console
```

## 使用

1. 启动机器人后，在群聊或私聊中发送 `web控制台` 获取登录验证码。
2. 访问机器人运行所在的 `http://ip:port/web_console`。
3. 输入验证码即可登录。

## 配置

在 `.env` 文件中可以配置：

```env
web_console_password=your_password  # 设置固定登录密码
web_console_log_retention_minutes=60  # Web 控制台日志缓存保留时长（分钟）
web_console_log_cleanup_interval_seconds=60  # 过期日志自动清理间隔（秒）
web_console_session_days=7  # 普通登录令牌有效期（天）
web_console_remember_days=30  # 勾选“记住此设备”后免密登录令牌有效期（天）
web_console_login_max_fails=5  # 登录失败限流：窗口内最大失败次数
web_console_login_fail_window_minutes=5  # 登录失败限流窗口（分钟）
```

## 更新日志

### v0.3.0
- **新增**：登录令牌持久化到本地存储，Bot 重启后仍然有效；勾选「记住此设备」即可免密登录，页面加载时自动校验令牌。
- **新增**：支持多设备同时在线（多令牌），并提供「退出登录」吊销当前设备令牌；修改密码会吊销全部令牌。
- **新增**：登录失败限流，窗口内连续失败达到上限后临时锁定，防止暴力猜解。
- **新增**：日志页支持按级别（DEBUG / INFO / SUCCESS / WARNING / ERROR）筛选，可多选与「全部」一键切换，对历史与实时日志同时生效。
- **优化**：大幅减少界面 emoji，导航与操作按钮统一改为线性 SVG 图标，跟随主题配色；补全设置页样式，整体向生产级观感看齐。
- **优化**：密码校验改用 `secrets.compare_digest` 常量时间比较；令牌使用更长的随机长度。

### v0.2.2
- **新增**：改为通过拟人插件导出的 `web_console_api` 稳定接口读取在线版 `nonebot-plugin-shiro-personification` 与本地版 `personification`。
- **优化**：双版本同时存在时按“在线版优先，本地版回退”自动选择活动 provider，并在接口响应中返回 `detected_providers` 供前端展示。
- **修复**：拟人插件联动不再依赖内部模块结构，避免发布版与本地版架构差异导致控制台页面失效。

### v0.2.1
- **修复**：为 Web 控制台日志缓存增加过期日志自动清理，避免低日志量场景下旧缓存长期残留。
- **新增**：支持通过 `web_console_log_retention_minutes` 与 `web_console_log_cleanup_interval_seconds` 配置日志缓存保留时长和清理周期。

### v0.2.0
- **新增**：为 `nonebot-plugin-shiro-personification` 与本地 `personification` 提供统一的 Web Console 联动兼容层。
- **新增**：拟人插件页面支持图形化查看和修改运行时全局配置、群配置与自定义 Prompt。
- **优化**：拟人插件状态页增加白名单、作息模拟、语音回复等群级状态展示，并兼容双插件变体自动识别。

### v0.1.23
- **新增**：支持在 Web 控制台启用或禁用插件（需要 `tomlkit` 库支持）。
- **优化**：改进已安装插件的检测逻辑，支持识别已安装但未加载的插件。

### v0.1.22
- **优化**：改进日志下载功能，支持将多个轮转日志文件打包为 ZIP 格式下载，解决旧日志无法获取的问题。

### v0.1.21
- **修复**：修复因 `nonebot_plugin_localstore` 加载顺序导致的 `RuntimeError`，确保插件能够正常加载。

### v0.1.20
- **优化**：改进 `nb` 命令调用逻辑，支持自动规范化插件名称（处理下划线/连字符），增加 `python -m nb_cli` 调用方式作为回退，提高插件安装/更新的成功率。

### v0.1.19
- **修复**：优化插件版本检测逻辑，增加对连字符命名和 PyPI 包名的兼容性匹配，解决更新后版本号不刷新的问题。

### v0.1.18
- **修复**：尝试通过清理工作流配置来解决 GitHub Actions 触发失效问题。
- **修复**：修复 WebSocket 连接清理时的竞争条件（KeyError），解决偶发的 500 错误。

### v0.1.17
- **修复**：尝试修复 GitHub Actions 发布工作流触发问题。
- **修复**：修复 WebSocket 连接清理时的竞争条件（KeyError），解决偶发的 500 错误。

### v0.1.16

### v0.1.15
- **修复**：修复插件商店操作时的 500 错误（缺少异步锁定义）。
- **修复**：修复 WebSocket 连接断开时的异常报错。
- **新增**：正式实装完整日志记录与下载功能。

### v0.1.14
- **新增**：完整运行日志记录功能，支持在网页端直接下载。
- **优化**：添加插件商店操作锁，解决多插件同时更新导致的冲突问题。
- **优化**：改进插件版本获取逻辑，准确对比版本号并过滤非更新项。
- **优化**：优化移动端适配及部分 UI 交互。

## 许可证

MIT
