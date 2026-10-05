"""猫娘计划(N.E.K.O.)插件:Agent 工作台安装器

在猫娘计划里一键安装「N.E.K.O. 插件工坊」(Agent 工作台):
在线下载官方便携包(或使用本地 zip)→ 解压安装 → 可选建桌面快捷方式 → 一键启动;
随时查看安装与运行状态。所有入口都双注册为 llm_tool,对猫娘说「安装工作台」即可触发。

入口:
  - install : 安装/覆盖安装 Agent 工作台
  - status  : 查看安装与运行状态
  - launch  : 启动工作台(已在运行则直接返回地址)
"""

from __future__ import annotations

# isort: off
# 说明:官方两条门禁(CI 的 plugin-repo 形状与 publish 的 cwd=. 形状)对 plugin.sdk 的
# first/third-party 归类相反,同一排序无法同时满足 —— 冻结本块,双方一致放行

import asyncio
import os
import shutil
import subprocess
import zipfile

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)
from pydantic import BaseModel, Field

# isort: on

try:
    import httpx
except ImportError:  # 仅缺包时降级到标准库实现;httpx 自身的其他异常必须冒出来,不能静默吞
    httpx = None

_PLUGIN_ID = "workbench_installer"
_DEFAULT_URL = (
    "https://github.com/fengdiehualian/neko-plugin-workshop/releases/"
    "latest/download/NEKO.PluginWorkshop-Portable.zip"
)
_HEALTH_URL = "http://127.0.0.1:5099/api/health"
_WORKBENCH_URL = "http://127.0.0.1:5099"
_SHORTCUT_NAME = "N.E.K.O.插件工坊"
# None = 直连/走环境变量代理;失败后依次尝试常见本地代理
_PROXY_CANDIDATES = [None, "http://127.0.0.1:7892", "http://127.0.0.1:7890"]


def _default_target() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    return os.path.join(base, "Programs", "NEKOWorkshop")


def _is_system_target(target: str) -> bool:
    """拒绝把系统目录当安装/覆盖目标(安全底线;跨平台)。"""
    raw = str(target).strip()
    # Windows 盘根:任何平台都拒绝(Linux 上 "C:\\..." 只会是笔误,不是合法安装点)
    if len(raw) in (2, 3) and raw[1] == ":" and raw[0].isalpha() and (
        len(raw) == 2 or raw[2] in "\\/"
    ):
        return True
    t = os.path.abspath(raw).rstrip("\\/").lower()
    drive = os.environ.get("SystemDrive", "C:").lower()
    if t in (drive, drive + "\\"):
        return True
    if t == drive + "\\users":
        return True
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)"):
        p = (os.environ.get(var) or "").rstrip("\\/").lower()
        if p and (t == p or t.startswith(p + "\\")):
            return True
    # Unix 系统目录(含裸根):同样拒绝
    normalized = os.path.abspath(raw).replace("\\", "/").rstrip("/") or "/"
    if normalized in {
        "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64",
        "/proc", "/root", "/sbin", "/sys", "/usr", "/var",
    }:
        return True
    return False


def _safe_extract(zip_path: str, target: str) -> int:
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            parts = info.filename.replace("\\", "/").split("/")
            if info.filename.startswith(("/", "\\")) or ".." in parts:
                raise ValueError(f"压缩包包含不安全路径: {info.filename}")
        zf.extractall(target)
        return len(zf.namelist())


def _download_urllib(url: str, dest: str, proxy: str | None) -> None:
    import urllib.request

    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener()  # 默认走环境变量代理
    with opener.open(url, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _make_httpx_client(timeout, proxy):
    kwargs = {"timeout": timeout, "follow_redirects": True}
    if proxy is None:
        kwargs["trust_env"] = True
    else:
        # 按签名选参数名:新版 httpx 用 proxy=,旧版用 proxies=。
        # 不用 try/except TypeError 兜底——timeout 等无关 TypeError 会被误吞进错误分支
        import inspect

        params = inspect.signature(httpx.AsyncClient.__init__).parameters
        if "proxy" in params:
            kwargs["proxy"] = proxy
        else:
            kwargs["proxies"] = proxy
    return httpx.AsyncClient(**kwargs)


class InstallParams(BaseModel):
    target: str = Field(
        "", description="安装目录;留空=默认 %LOCALAPPDATA%\\Programs\\NEKOWorkshop"
    )
    zip: str = Field("", description="本地便携包(.zip)路径;填了就不联网下载")
    url: str = Field("", description="下载地址;留空=官方 Release 最新便携包")
    launch: bool = Field(True, description="安装完成后是否立刻启动")
    shortcut: bool = Field(True, description="是否创建桌面快捷方式")
    overwrite: bool = Field(False, description="目标已安装时是否覆盖重装(默认拒绝覆盖)")


@neko_plugin
class WorkbenchInstallerPlugin(NekoPluginBase):
    """Agent 工作台安装器 - N.E.K.O 插件入口"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._cfg_target = ""
        self._cfg_url = _DEFAULT_URL

    # ── lifecycle ──
    async def _load_cfg(self):
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        section = cfg.get(_PLUGIN_ID)
        section = section if isinstance(section, dict) else {}
        self._cfg_target = str(section.get("target", "") or "").strip()
        self._cfg_url = str(section.get("url", "") or "").strip() or _DEFAULT_URL

    @lifecycle(id="startup")
    async def startup(self, **_):
        await self._load_cfg()
        self.logger.info("Agent 工作台安装器已启动")
        return Ok({"status": "ready"})

    @lifecycle(id="config_change")
    async def config_change(self, **_):
        await self._load_cfg()
        return Ok({"status": "reloaded"})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        return Ok({"status": "bye"})

    # ── 辅助 ──
    def _resolve_target(self, target: str) -> str:
        t = (target or "").strip() or self._cfg_target or _default_target()
        return os.path.abspath(os.path.expandvars(os.path.expanduser(t)))

    def _probe_install(self, target: str) -> dict:
        start_cmd = os.path.join(target, "start.cmd")
        installed = os.path.isfile(start_cmd)
        files = 0
        size = 0
        if os.path.isdir(target):
            for root, _dirs, names in os.walk(target):
                for n in names:
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(root, n))
                    except OSError:
                        pass
        return {
            "installed": installed,
            "target": target,
            "start_cmd": start_cmd if installed else "",
            "files": files,
            "size_mb": round(size / 1024 / 1024, 1),
        }

    async def _download(self, url: str, dest: str) -> dict:
        dest_dir = os.path.dirname(dest)
        if dest_dir:  # 裸文件名时 dirname 为空,makedirs("") 会直接炸
            os.makedirs(dest_dir, exist_ok=True)
        errors = []
        for proxy in _PROXY_CANDIDATES:
            label = proxy or "direct/env"
            try:
                if httpx is not None:
                    timeout = httpx.Timeout(600.0, connect=15.0)
                    async with _make_httpx_client(timeout, proxy) as client:
                        async with client.stream("GET", url) as resp:
                            resp.raise_for_status()
                            with open(dest, "wb") as f:
                                async for chunk in resp.aiter_bytes(1 << 16):
                                    f.write(chunk)
                else:
                    await asyncio.to_thread(_download_urllib, url, dest, proxy)
                size = os.path.getsize(dest) if os.path.isfile(dest) else 0
                if size > 0:
                    return {"ok": True, "path": dest, "bytes": size, "via": label}
                errors.append(f"{label}: 文件为空")
            except Exception as e:
                errors.append(f"{label}: {e}")
        return {"ok": False, "error": " | ".join(errors)[:800]}

    async def _health(self):
        try:
            if httpx is not None:
                async with httpx.AsyncClient(timeout=3.0, trust_env=True) as client:
                    r = await client.get(_HEALTH_URL)
                    return r.json()
            import json
            import urllib.request

            with urllib.request.urlopen(_HEALTH_URL, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    async def _wait_health(self, seconds: float = 25.0):
        # get_event_loop() 在运行中的协程里已废弃(3.12 起报错),用 get_running_loop()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            h = await self._health()
            if h is not None:
                return h
            await asyncio.sleep(1.5)
        return None

    def _spawn(self, target: str) -> dict:
        start_cmd = os.path.join(target, "start.cmd")
        if not os.path.isfile(start_cmd):
            return {"ok": False, "error": "未找到 start.cmd,该目录不是工作台安装目录"}
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE
            )
        try:
            proc = subprocess.Popen(["cmd", "/c", str(start_cmd)], cwd=target, **kwargs)
            return {"ok": True, "pid": proc.pid}
        except Exception as e:
            return {"ok": False, "error": f"启动失败: {e}"}

    def _make_shortcut(self, target: str) -> dict:
        if os.name != "nt":
            return {"ok": False, "skipped": True, "error": "仅 Windows 支持快捷方式"}
        start_cmd = os.path.join(target, "start.cmd")
        # 路径经环境变量传给 PowerShell,不内联进脚本:含单引号/特殊字符的路径
        # (如 C:\Users\O'Brien\...)不会破坏语法
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "$desktop = [Environment]::GetFolderPath('Desktop'); "
            "$lnk = $ws.CreateShortcut((Join-Path $desktop ($env:WB_SC_NAME + '.lnk'))); "
            "$lnk.TargetPath = $env:WB_SC_TARGET; "
            "$lnk.WorkingDirectory = $env:WB_SC_DIR; "
            "$lnk.Description = 'N.E.K.O. Plugin Workbench'; "
            "$lnk.Save()"
        )
        env = {
            **os.environ,
            "WB_SC_TARGET": start_cmd,
            "WB_SC_DIR": target,
            "WB_SC_NAME": _SHORTCUT_NAME,
        }
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                timeout=30,
                env=env,
            )
            if r.returncode == 0:
                return {"ok": True, "name": f"{_SHORTCUT_NAME}.lnk"}
            return {
                "ok": False,
                "error": (r.stderr or b"").decode("utf-8", "replace")[:200],
            }
        except Exception as e:
            return {"ok": False, "error": f"创建快捷方式失败: {e}"}

    # ── 入口:安装 ──
    @llm_tool(
        name="workbench_installer_install",
        description="安装「N.E.K.O. 插件工坊」(Agent 工作台)到这台电脑:下载便携包(或用本地 zip)、"
        "解压安装、可选创建桌面快捷方式并启动。当用户想安装/重装 Agent 工作台、插件工坊时使用。",
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "安装目录;留空=默认 %LOCALAPPDATA%\\Programs\\NEKOWorkshop",
                },
                "zip": {
                    "type": "string",
                    "description": "本地便携包(.zip)路径;填了就不联网下载",
                },
                "url": {
                    "type": "string",
                    "description": "下载地址;留空=官方 Release 最新便携包",
                },
                "launch": {"type": "boolean", "description": "装完是否立刻启动,默认 true"},
                "shortcut": {"type": "boolean", "description": "是否创建桌面快捷方式,默认 true"},
                "overwrite": {
                    "type": "boolean",
                    "description": "目标已安装时是否覆盖重装,默认 false(拒绝覆盖)",
                },
            },
        },
        timeout=900.0,
    )
    @plugin_entry(
        id="install",
        name="安装 Agent 工作台",
        description="下载/解压安装 N.E.K.O. 插件工坊,可创建桌面快捷方式并启动",
        timeout=900.0,
        llm_result_fields=[
            "ok",
            "message",
            "target",
            "already_installed",
            "launched",
            "url",
        ],
    )
    async def install(self, params: InstallParams, **_):
        target = self._resolve_target(params.target)
        # 原始输入与解析后路径都要查:相对的系统目标(如 Linux 上的 "C:\\")
        # 会被 _resolve_target 先拼成绝对路径,只查解析值会漏
        if _is_system_target(params.target or "") or _is_system_target(target):
            return Err(SdkError(f"拒绝安装到系统目录: {target}"))
        info = await asyncio.to_thread(self._probe_install, target)
        if info["installed"] and not params.overwrite:
            return Ok({
                "ok": True,
                "already_installed": True,
                "target": target,
                "url": _WORKBENCH_URL,
                "message": "工作台已安装,未执行覆盖;如需覆盖重装请传 overwrite=true",
            })

        # 获取安装包:本地 zip 优先,否则联网下载
        zip_path = (params.zip or "").strip()
        source = zip_path or (params.url or "").strip() or self._cfg_url
        if zip_path:
            if not os.path.isfile(zip_path):
                return Err(SdkError(f"本地便携包不存在: {zip_path}"))
        else:
            dest = os.path.join(
                self.cache_path("download"), "NEKO.PluginWorkshop-Portable.zip"
            )
            self.logger.info("下载工作台便携包: {}", source)
            got = await self._download(source, dest)
            if not got.get("ok"):
                return Err(
                    SdkError(f"下载失败(直连与常见代理均不可用): {got.get('error', '')}")
                )
            zip_path = got["path"]
            source = f"{source} ({got['bytes']} bytes, via {got['via']})"

        # 覆盖安装:仅当目标确实是工作台目录时才清空重装
        if info["installed"] and params.overwrite:
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception as e:
                return Err(SdkError(f"清理旧安装失败: {e}"))
        elif os.path.isdir(target) and not info["installed"] and os.listdir(target):
            return Err(SdkError(
                f"目标目录已存在且不是工作台安装目录: {target};请换一个 target 或手动清空"
            ))

        try:
            os.makedirs(target, exist_ok=True)
            files = await asyncio.to_thread(_safe_extract, zip_path, target)
        except Exception as e:
            return Err(SdkError(f"解压安装失败: {e}"))
        self.logger.info("已解压 {} 个文件到 {}", files, target)

        shortcut = {"ok": False, "skipped": True}
        if params.shortcut:
            shortcut = await asyncio.to_thread(self._make_shortcut, target)

        launched = False
        launch_msg = "未启动"
        if params.launch:
            health = await self._health()
            if health is not None:
                launch_msg = "工作台已在运行,无需重复启动"
            else:
                spawned = await asyncio.to_thread(self._spawn, target)
                if spawned.get("ok"):
                    health = await self._wait_health(25.0)
                    launched = health is not None
                    launch_msg = (
                        "已启动"
                        if launched
                        else "已发出启动命令,但 25 秒内未就绪(可稍后用 status 查看)"
                    )
                else:
                    launch_msg = f"启动失败: {spawned.get('error', '')}"

        return Ok({
            "ok": True,
            "already_installed": False,
            "target": target,
            "source": source,
            "files": files,
            "shortcut": bool(shortcut.get("ok")),
            "launched": launched,
            "url": _WORKBENCH_URL,
            "message": f"安装完成({files} 个文件);{launch_msg}",
        })

    # ── 入口:状态 ──
    @llm_tool(
        name="workbench_installer_status",
        description="查看「N.E.K.O. 插件工坊」(Agent 工作台)的安装与运行状态。"
        "当用户问工作台装没装、有没有在运行、安装在哪个目录时使用。",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "安装目录;留空=默认目录"},
            },
        },
        timeout=30.0,
    )
    @plugin_entry(
        id="status",
        name="工作台状态",
        description="查看工作台是否已安装、是否在运行",
        timeout=30.0,
        llm_result_fields=["ok", "installed", "running", "target", "message", "url"],
    )
    async def status(self, target: str = "", **_):
        t = self._resolve_target(target)
        info = await asyncio.to_thread(self._probe_install, t)
        health = await self._health()
        running = health is not None
        if not info["installed"]:
            msg = "未安装;调用 install 入口即可安装"
        elif running:
            msg = "已安装,且工作台正在运行"
        else:
            msg = "已安装,但未运行;调用 launch 入口启动"
        return Ok({
            "ok": True,
            "installed": info["installed"],
            "running": running,
            "target": t,
            "files": info["files"],
            "size_mb": info["size_mb"],
            "url": _WORKBENCH_URL,
            "health": health,
            "message": msg,
        })

    # ── 入口:启动 ──
    @llm_tool(
        name="workbench_installer_launch",
        description="启动已安装的「N.E.K.O. 插件工坊」(Agent 工作台);已经在运行就直接返回访问地址。"
        "当用户想打开/启动工作台时使用。",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "安装目录;留空=默认目录"},
            },
        },
        timeout=60.0,
    )
    @plugin_entry(
        id="launch",
        name="启动工作台",
        description="启动已安装的 Agent 工作台;已在运行则返回地址",
        timeout=60.0,
        llm_result_fields=["ok", "started", "already_running", "url", "message"],
    )
    async def launch(self, target: str = "", **_):
        t = self._resolve_target(target)
        info = await asyncio.to_thread(self._probe_install, t)
        if not info["installed"]:
            return Err(SdkError(f"未检测到已安装的工作台({t});请先调用 install 入口安装"))
        health = await self._health()
        if health is not None:
            return Ok({
                "ok": True,
                "started": False,
                "already_running": True,
                "url": _WORKBENCH_URL,
                "message": "工作台已在运行,直接打开地址即可",
            })
        spawned = await asyncio.to_thread(self._spawn, t)
        if not spawned.get("ok"):
            return Err(SdkError(str(spawned.get("error", "启动失败"))))
        health = await self._wait_health(25.0)
        if health is None:
            return Ok({
                "ok": True,
                "started": False,
                "already_running": False,
                "url": _WORKBENCH_URL,
                "message": "已发出启动命令,但 25 秒内未就绪,请稍后用 status 查看",
            })
        return Ok({
            "ok": True,
            "started": True,
            "already_running": False,
            "url": _WORKBENCH_URL,
            "message": "工作台已启动,浏览器打开地址即可使用",
        })
