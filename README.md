# Agent工作台安装器(workbench_installer)

猫娘计划(N.E.K.O.)插件:在 N.E.K.O. 里一键安装「N.E.K.O. 插件工坊」(Agent 工作台)。
相当于一个「通过猫娘计划插件分发的安装器」——对猫娘说一句「安装工作台」就能把工作台装到这台电脑上。

## 项目地址

- 本插件仓库:https://github.com/fengdiehualian/n.e.k.o_plugin_workbench_installer
- 「N.E.K.O. 插件工坊」(本插件安装的 Agent 工作台)源码:https://github.com/fengdiehualian/neko-plugin-workshop

## 怎么用

**先装进 N.E.K.O.(一次性)**:到 [Releases](https://github.com/fengdiehualian/n.e.k.o_plugin_workbench_installer/releases/latest) 下载 `workbench_installer.neko-plugin`,在 N.E.K.O. 插件管理里安装。

> ⚠️ **安装后请手动启动本插件**(插件管理里点「启动」):按代码评审要求已移除 `auto_start`,因此每次猫娘启动后都需要手动启动一次,下面的口令才会生效。

**然后对猫娘说话即可**(入口已注册为 LLM 工具,说人话就能触发):

| 你说 | 它做什么 |
|---|---|
| 「**安装工作台**」 | 下载官方便携包,解压安装到 `%LOCALAPPDATA%\Programs\NEKOWorkshop`,建桌面快捷方式并启动 |
| 「**启动工作台**」 | 启动工作台;已在运行则返回地址 `http://127.0.0.1:5099` |
| 「**工作台状态**」 | 报告安装/运行状态与引擎、模型健康情况 |

**常用变体**(说的时候带出来,或让 Agent 传参):

| 你说 / 传参 | 作用 |
|---|---|
| 「覆盖重装工作台」(`overwrite=true`) | 覆盖重装;**不带此参数时拒绝覆盖**,防误删 |
| 「用这个包安装」(`zip=本地 zip 路径`) | 离线安装,不联网 |
| `url=下载地址` | 自定义便携包下载源(默认官方 Release 最新便携包) |
| `target=安装目录` | 自定义安装目录(默认 `%LOCALAPPDATA%\Programs\NEKOWorkshop`;系统目录会被拒绝) |

## 入口(全部双注册为 llm_tool,对话/面板/Agent 都能调)

| 入口 | 作用 | 参数 |
|---|---|---|
| `install` | 下载/解压安装工作台,可建桌面快捷方式并启动 | `target` 安装目录(默认 `%LOCALAPPDATA%\Programs\NEKOWorkshop`)、`zip` 本地便携包路径(填了不联网)、`url` 自定义下载地址(默认官方 Release 最新便携包)、`launch` 装完是否启动(默认 true)、`shortcut` 是否建桌面快捷方式(默认 true)、`overwrite` 覆盖重装(默认 false,**不传不覆盖**) |
| `status` | 查看是否安装、是否在运行 | `target`(同上) |
| `launch` | 启动工作台;已在运行则返回地址 | `target`(同上) |

工作台访问地址:`http://127.0.0.1:5099`

## 目录布局与 entry

实现代码在 `plugin/plugins/workbench_installer/`,与 `entry = "plugin.plugins.workbench_installer:WorkbenchInstallerPlugin"` 的包路径同形;仓库根的 `__init__.py` 是入口垫片,把实现类再导出给宿主。

说明:`plugin.plugins.<id>` 是**安装/挂载时**由 SDK 把插件包根注册成的运行时 Python 包(官方 CI 的 `Mount plugin into N.E.K.O tree` 与引擎安装器都会生成该路径),它不是仓库内的相对路径 —— 这也是全部市场插件的公共约定。加载链:宿主按 entry 导入包根(垫片)→ 垫片载入嵌套实现 → 拿到 `WorkbenchInstallerPlugin`。

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
