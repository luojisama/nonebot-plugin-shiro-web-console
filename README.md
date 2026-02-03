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
```

## 更新日志

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
