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
# 官方从 v1.2.5 起只发安装版 exe,不再更新便携 zip;
# 旧的 NEKO.PluginWorkshop-Portable.zip 已停留在 v1.2.3,因此默认取 Setup.exe
_SETUP_ASSET = "N.E.K.O.PluginWorkshop-Setup.exe"
_PORTABLE_ASSET = "NEKO.PluginWorkshop-Portable.zip"
_RELEASE_BASE = "https://github.com/fengdiehualian/neko-plugin-workshop/releases/latest/download/"
_DEFAULT_URL = _RELEASE_BASE + _SETUP_ASSET
_HEALTH_URL = "http://127.0.0.1:5099/api/health"
_WORKBENCH_URL = "http://127.0.0.1:5099"
_SHORTCUT_NAME = "N.E.K.O.插件工坊"
# 下载尝试顺序:显式配置的 proxy → 环境变量代理 → 直连。
# 不再硬编码 7890/7892 之类本地端口:那些端口可能是别的软件在用,
# 且每个候选都会把 133MB 完整重下一遍(实测最坏 30 分钟)。
_PROXY_ATTEMPTS = ("config", "env", "direct")
# zip 解压安全上限:防压缩炸弹(正常包约 3600 个文件 / 340MB)
_MAX_ZIP_FILES = 20000
_MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024


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
        infos = zf.infolist()
        if len(infos) > _MAX_ZIP_FILES:
            raise ValueError(f"压缩包文件数异常({len(infos)}),拒绝解压")
        total = sum(max(0, int(i.file_size)) for i in infos)
        if total > _MAX_ZIP_BYTES:
            raise ValueError(f"压缩包解压后体积异常({total} 字节),拒绝解压")
        for info in infos:
            parts = info.filename.replace("\\", "/").split("/")
            if info.filename.startswith(("/", "\\")) or ".." in parts:
                raise ValueError(f"压缩包包含不安全路径: {info.filename}")
        zf.extractall(target)
        return len(infos)


def _download_urllib(url: str, dest: str, proxy: str | None, trust_env: bool) -> None:
    import urllib.request

    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    elif trust_env:
        handler = urllib.request.ProxyHandler()  # 空参 = 读环境变量
    else:
        handler = urllib.request.ProxyHandler({})  # 显式直连
    opener = urllib.request.build_opener(handler)
    with opener.open(url, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _make_httpx_client(timeout, proxy, trust_env: bool):
    kwargs = {"timeout": timeout, "follow_redirects": True, "trust_env": trust_env}
    if proxy:
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
    overwrite: bool = Field(
        False,
        description="目标已安装时是否覆盖重装(默认拒绝覆盖;覆盖只替换工作台程序文件,保留 runtime 配置与 workspace 用户项目)",
    )


@neko_plugin
class WorkbenchInstallerPlugin(NekoPluginBase):
    """Agent 工作台安装器 - N.E.K.O 插件入口"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._cfg_target = ""
        self._cfg_url = _DEFAULT_URL
        self._cfg_proxy = ""
        self._cfg_download_timeout = 900.0

    # ── lifecycle ──
    async def _load_cfg(self):
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        section = cfg.get(_PLUGIN_ID)
        section = section if isinstance(section, dict) else {}
        self._cfg_target = str(section.get("target", "") or "").strip()
        self._cfg_url = str(section.get("url", "") or "").strip() or _DEFAULT_URL
        self._cfg_proxy = str(section.get("proxy", "") or "").strip()
        try:
            t = float(section.get("download_timeout", 900) or 900)
        except (TypeError, ValueError):
            t = 900.0
        self._cfg_download_timeout = max(60.0, min(t, 1800.0))

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

    def _proxy_for(self, attempt: str):
        """按尝试计划返回 (proxy, trust_env);config 未配置时返回 (None, None) 表示跳过。"""
        if attempt == "config":
            return (self._cfg_proxy or None, False) if self._cfg_proxy else (None, None)
        if attempt == "env":
            return None, True
        return None, False

    async def _download_once(self, url: str, dest: str, proxy, trust_env: bool) -> None:
        if httpx is not None:
            timeout = httpx.Timeout(self._cfg_download_timeout, connect=10.0)
            async with _make_httpx_client(timeout, proxy, trust_env) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes(1 << 16):
                            f.write(chunk)
        else:
            await asyncio.to_thread(_download_urllib, url, dest, proxy, trust_env)

    async def _download(self, url: str, dest: str) -> dict:
        dest_dir = os.path.dirname(dest)
        if dest_dir:  # 裸文件名时 dirname 为空,makedirs("") 会直接炸
            os.makedirs(dest_dir, exist_ok=True)
        errors = []
        for attempt in _PROXY_ATTEMPTS:
            proxy, trust_env = self._proxy_for(attempt)
            if trust_env is None:  # 该通道未配置,跳过
                continue
            label = proxy or ("env" if trust_env else "direct")
            try:
                await self._download_once(url, dest, proxy, trust_env)
                size = os.path.getsize(dest) if os.path.isfile(dest) else 0
                if size > 0:
                    return {"ok": True, "path": dest, "bytes": size, "via": label}
                errors.append(f"{label}: 文件为空")
            except Exception as e:
                # 服务端已明确回应(404/403 等):换代理重试没有意义,直接报错
                if httpx is not None and isinstance(e, httpx.HTTPStatusError):
                    code = e.response.status_code if e.response is not None else "?"
                    return {
                        "ok": False,
                        "error": f"{label}: HTTP {code}(地址不存在或无权限): {url}",
                    }
                errors.append(f"{label}: {e}")
        return {"ok": False, "error": " | ".join(errors)[:800]}

    async def _health(self):
        # 本机回环探测绝不能走代理:环境里一旦有 HTTP_PROXY,127.0.0.1 会被拦掉,
        # 会把"已在运行"误判成"未运行"(实测踩过)。
        try:
            if httpx is not None:
                async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                    r = await client.get(_HEALTH_URL)
                    return r.json()
            import json
            import urllib.request

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(_HEALTH_URL, timeout=3) as resp:
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
            # DETACHED_PROCESS 与 CREATE_NEW_CONSOLE 互斥:同时使用 CreateProcess 报
            # WinError 87(参数错误),启动会静默失败 —— 只用 CREATE_NEW_CONSOLE(等效双击)
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        try:
            proc = subprocess.Popen(["cmd", "/c", str(start_cmd)], cwd=target, **kwargs)
            return {"ok": True, "pid": proc.pid}
        except Exception as e:
            return {"ok": False, "error": f"启动失败: {e}"}

    def _run_setup_exe(self, exe_path: str) -> dict:
        """静默运行官方安装器(--silent);它固定装到默认目录,并在升级时自行保留 runtime/workspace。"""
        try:
            r = subprocess.run(
                [str(exe_path), "--silent"],
                capture_output=True,
                timeout=300,
            )
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", "replace")[:200]
                return {"ok": False, "error": f"exit={r.returncode} {err}".strip()}
            return {"ok": True, "returncode": r.returncode}
        except Exception as e:
            return {"ok": False, "error": f"运行安装器超时或失败: {e}"}

    async def _wait_installed(self, target: str, seconds: float) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        info = await asyncio.to_thread(self._probe_install, target)
        while loop.time() < deadline and not info["installed"]:
            await asyncio.sleep(1.5)
            info = await asyncio.to_thread(self._probe_install, target)
        return info

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
        # 官方文档硬上限:@llm_tool timeout ≤ 300 秒(服务端会截断)。下载慢时可能不够,
        # 面板/手动触发走下面的 @plugin_entry(900 秒),不受此限制。
        timeout=300.0,
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

        # —— 安装版 exe:交给官方安装器(它自己固定装到默认目录,并保证升级保留用户数据)——
        if str(zip_path).lower().endswith(".exe"):
            default_dir = _default_target()
            if os.path.normcase(os.path.abspath(target)) != os.path.normcase(
                os.path.abspath(os.path.expanduser(default_dir))
            ):
                return Err(SdkError(
                    f"安装版只能装到默认目录 {default_dir};要装到自定义目录请改用便携包 zip"
                ))
            ran = await asyncio.to_thread(self._run_setup_exe, zip_path)
            if not ran.get("ok"):
                return Err(SdkError(f"运行安装程序失败: {ran.get('error', '')}"))
            info = await self._wait_installed(target, 120.0)
            if not info["installed"]:
                return Err(SdkError(
                    "安装程序已结束,但 120 秒内未检测到 start.cmd;请手动运行安装包确认"
                ))
            files = info["files"]
        else:
            # 覆盖安装:仅当目标确实是工作台目录时清理**工作台自有文件**后重装。
            # runtime(API 配置/会话)与 workspace(用户生成的插件项目)是用户数据,
            # 必须保留 —— 早期实现直接 rmtree(target) 会把用户项目一并删掉(实测事故)
            if info["installed"] and params.overwrite:
                try:
                    for entry in os.listdir(target):
                        if entry in ("runtime", "workspace"):
                            continue
                        p = os.path.join(target, entry)
                        if os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            try:
                                os.unlink(p)
                            except OSError:
                                pass
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
