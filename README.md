# Agent工作台安装器(workbench_installer)

猫娘计划(N.E.K.O.)插件:在 N.E.K.O. 里一键安装「N.E.K.O. 插件工坊」(Agent 工作台)。
相当于一个「通过猫娘计划插件分发的安装器」——对猫娘说一句「安装工作台」就能把工作台装到这台电脑上。

## 入口(全部双注册为 llm_tool,对话/面板/Agent 都能调)

| 入口 | 作用 | 参数 |
|---|---|---|
| `install` | 下载/解压安装工作台,可建桌面快捷方式并启动 | `target` 安装目录(默认 `%LOCALAPPDATA%\Programs\NEKOWorkshop`)、`zip` 本地便携包路径(填了不联网)、`url` 自定义下载地址(默认官方 Release 最新便携包)、`launch` 装完是否启动(默认 true)、`shortcut` 是否建桌面快捷方式(默认 true)、`overwrite` 覆盖重装(默认 false,**不传不覆盖**) |
| `status` | 查看是否安装、是否在运行 | `target`(同上) |
| `launch` | 启动工作台;已在运行则返回地址 | `target`(同上) |

工作台访问地址:`http://127.0.0.1:5099`

## 安全设计

- **拒绝覆盖**:目标已安装时不覆盖重装,必须显式传 `overwrite=true`(任何替换操作都需用户明确确认)。
- **拒绝系统目录**:`C:\`、`C:\Windows`、`Program Files` 等不允许作为安装/覆盖目标。
- **不碰陌生目录**:目标已存在且不是工作台目录时直接拒绝。
- **压缩包路径校验**:拒绝绝对路径/`..` 穿越的压缩包条目。
- 联网失败自动降级:直连 → 环境变量代理 → 常见本地代理(7892/7890);`httpx` 缺失时自动用标准库实现。

## 开发

```bash
# 在 N.E.K.O 仓库根执行(便携/离线环境禁用 uv run)
python plugin\neko_plugin_cli\cli.py check workbench_installer
python plugin\neko_plugin_cli\cli.py build workbench_installer --out workbench_installer.neko-plugin
```

详情见 `AGENTS.md`;插件开发规范全文在 `.opencode/skill/neko-plugin-dev/SKILL.md`(工坊生成项目时自动放置,不随插件打包)。
