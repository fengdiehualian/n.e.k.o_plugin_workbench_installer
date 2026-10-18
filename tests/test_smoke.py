"""workbench_installer 冒烟测试:静态结构 + install/status/launch 三入口真实逻辑。

安装只走官方安装版 exe(exe 来源),因此运行时用例用本地假 exe + 打桩的安装器,
全程离线、不触网、不写真实安装目录。需要 N.E.K.O SDK 在路径上
(python -m pytest tests/ 于 N.E.K.O 环境,或设 WB_NEKO_REPO 指向 N.E.K.O 源码根),
SDK 不在时自动跳过运行时用例,静态用例照常执行。
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------- 静态结构 ----------


def test_plugin_manifest_exists() -> None:
    manifest = ROOT / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "workbench_installer"' in text
    assert 'entry = "plugin.plugins.workbench_installer:WorkbenchInstallerPlugin"' in text
    # 只支持安装版:manifest 不应再把便携 zip 写进默认配置
    assert "Portable.zip" not in text


def test_entry_class_declared() -> None:
    # 实现位于 plugin/plugins/workbench_installer/(与 entry 包路径同形)
    impl = ROOT / "plugin" / "plugins" / "workbench_installer" / "__init__.py"
    source = impl.read_text(encoding="utf-8")
    assert "@neko_plugin" in source
    assert "class WorkbenchInstallerPlugin(NekoPluginBase)" in source
    # 根 __init__.py 是入口垫片:必须把实现类再导出(挂载后 entry 从包根取类)
    shim = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "WorkbenchInstallerPlugin" in shim


def test_no_zip_extraction_anymore() -> None:
    # 明确回归:不再做本地解压安装
    impl = ROOT / "plugin" / "plugins" / "workbench_installer" / "__init__.py"
    source = impl.read_text(encoding="utf-8")
    assert "zipfile" not in source
    assert "extractall" not in source


# ---------- 运行时(入口真实逻辑)----------


def _ensure_sdk_on_path() -> bool:
    try:
        # 可用性探测:importlib 动态导入,不产生未用导入绑定(官方 CI ruff --ignore-noqa 会查 F401)
        importlib.import_module("plugin.sdk")

        return True
    except Exception:
        pass
    for cand in (os.environ.get("WB_NEKO_REPO") or "", os.environ.get("NEKO_REPO_ROOT") or ""):
        if cand and (Path(cand) / "plugin" / "sdk").is_dir():
            sys.path.insert(0, cand)
            return True
    for parent in Path(__file__).resolve().parents:
        if (parent / "plugin" / "sdk").is_dir():
            sys.path.insert(0, str(parent))
            return True
    return False


def _load_module():
    if not _ensure_sdk_on_path():
        pytest.skip("N.E.K.O SDK 不在路径上(设置 WB_NEKO_REPO 后重跑)")
    spec = importlib.util.spec_from_file_location(
        "workbench_installer_under_test", ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_plugin(mod, tmp_path):
    p = mod.WorkbenchInstallerPlugin(mock.MagicMock())
    cache, data = tmp_path / "cache", tmp_path / "data"
    p.cache_path = lambda *parts: cache.joinpath(*parts)  # 离线隔离,不碰真实缓存目录
    p.data_path = lambda *parts: data.joinpath(*parts)
    # 全程不触网:健康探测打桩固定 None(否则会真请求 127.0.0.1:5099,
    # CI 上端口被占时 status 会误报 running,测试不稳定)
    p._health = mock.AsyncMock(return_value=None)
    return p


def _fake_setup(target: Path):
    """假安装器:像官方 --silent 一样把程序文件落到目标目录,并保留用户数据目录。"""

    def _run(exe_path: str) -> dict:
        target.mkdir(parents=True, exist_ok=True)
        (target / "start.cmd").write_text("@echo off\r\necho wb\r\n", encoding="utf-8")
        (target / "wb-studio.mjs").write_text("// stub", encoding="utf-8")
        (target / "runtime").mkdir(exist_ok=True)
        (target / "workspace").mkdir(exist_ok=True)
        return {"ok": True, "returncode": 0}

    return _run


def _val(res):
    return res.value if hasattr(res, "value") else res


def _bind_default_target(mod, target: Path, monkeypatch) -> None:
    monkeypatch.setattr(mod._impl_mod, "_default_target", lambda: str(target))


def test_status_not_installed(tmp_path) -> None:
    mod = _load_module()
    p = _make_plugin(mod, tmp_path)
    res = asyncio.run(p.status(target=str(tmp_path / "nope")))
    v = _val(res)
    assert not v.get("installed"), v


def test_install_exe_then_refuses_overwrite_then_overwrites(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    target = tmp_path / "wb"
    _bind_default_target(mod, target, monkeypatch)
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ-stub")
    p = _make_plugin(mod, tmp_path)
    p._run_setup_exe = _fake_setup(target)
    params = dict(exe=str(exe), launch=False, shortcut=False)

    res = asyncio.run(p.install(mod.InstallParams(**params)))
    v = _val(res)
    assert v.get("already_installed") is False, v
    assert (target / "start.cmd").is_file()

    # 已安装:拒绝覆盖(必须显式 overwrite=True)
    res2 = asyncio.run(p.install(mod.InstallParams(**params)))
    v2 = _val(res2)
    assert v2.get("already_installed") is True, v2
    assert "overwrite" in str(v2.get("message", "")), v2

    # 显式覆盖:允许重装
    res3 = asyncio.run(p.install(mod.InstallParams(**{**params, "overwrite": True})))
    v3 = _val(res3)
    assert v3.get("already_installed") is False, v3
    assert (target / "start.cmd").is_file()


def test_install_rejects_non_exe(tmp_path) -> None:
    # 只认官方安装版:传 zip/其它格式必须明确拒绝,不做本地解压
    mod = _load_module()
    pkg = tmp_path / "pkg.zip"
    pkg.write_bytes(b"PK-stub")
    p = _make_plugin(mod, tmp_path)
    res = asyncio.run(p.install(mod.InstallParams(exe=str(pkg), launch=False, shortcut=False)))
    assert isinstance(res, mod.Err), res
    assert "exe" in str(res.error).lower()


def test_install_missing_local_exe(tmp_path) -> None:
    mod = _load_module()
    p = _make_plugin(mod, tmp_path)
    res = asyncio.run(
        p.install(mod.InstallParams(exe=str(tmp_path / "nope.exe"), launch=False, shortcut=False))
    )
    assert isinstance(res, mod.Err), res
    assert "不存在" in str(res.error)


def test_install_reports_setup_failure(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    target = tmp_path / "wb"
    _bind_default_target(mod, target, monkeypatch)
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ-stub")
    p = _make_plugin(mod, tmp_path)
    p._run_setup_exe = lambda _path: {"ok": False, "error": "exit=1 权限不足"}
    res = asyncio.run(p.install(mod.InstallParams(exe=str(exe), launch=False, shortcut=False)))
    assert isinstance(res, mod.Err), res
    assert "安装程序" in str(res.error)


def test_install_detects_missing_start_cmd_after_setup(tmp_path, monkeypatch) -> None:
    # 安装器"成功退出"但没落盘(被杀软拦截等):必须报错而不是假装成功
    mod = _load_module()
    target = tmp_path / "wb"
    _bind_default_target(mod, target, monkeypatch)
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ-stub")
    p = _make_plugin(mod, tmp_path)
    p._run_setup_exe = lambda _path: {"ok": True, "returncode": 0}
    res = asyncio.run(p.install(mod.InstallParams(exe=str(exe), launch=False, shortcut=False)))
    assert isinstance(res, mod.Err), res
    assert "start.cmd" in str(res.error)


def test_launch_without_start_cmd_fails(tmp_path) -> None:
    mod = _load_module()
    empty = tmp_path / "empty"
    empty.mkdir()
    p = _make_plugin(mod, tmp_path)
    res = asyncio.run(p.launch(target=str(empty)))
    assert isinstance(res, mod.Err), res


@pytest.mark.skipif(os.name != "nt", reason="creationflags 仅 Windows")
def test_spawn_uses_compatible_creation_flags(tmp_path, monkeypatch) -> None:
    # DETACHED_PROCESS 与 CREATE_NEW_CONSOLE 互斥,同用会 WinError 87 启动静默失败
    import subprocess as sp

    mod = _load_module()
    p = _make_plugin(mod, tmp_path)
    target = tmp_path / "wbdir"
    target.mkdir()
    (target / "start.cmd").write_text("@echo off", encoding="utf-8")

    captured = {}

    class _FakeProc:
        pid = 12345

    def fake_popen(cmd, **kw):
        captured.update(kw)
        return _FakeProc()

    monkeypatch.setattr(sp, "Popen", fake_popen)
    res = p._spawn(str(target))
    assert res.get("ok") is True, res
    flags = captured.get("creationflags", 0)
    assert flags == sp.CREATE_NEW_CONSOLE
    assert not (flags & sp.DETACHED_PROCESS)
