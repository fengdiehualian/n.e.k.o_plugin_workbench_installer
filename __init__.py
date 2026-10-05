"""猫娘计划(N.E.K.O.)插件:Agent 工作台安装器 —— 入口垫片

实现代码位于 plugin/plugins/workbench_installer/(与 entry 的包路径同形)。
entry = "plugin.plugins.workbench_installer:WorkbenchInstallerPlugin" 里的
plugin.plugins.<id> 是**安装/挂载时**由 SDK 把插件包根注册成的运行时 Python 包;
本文件是包根入口,负责把嵌套实现里的类再导出给宿主。

注意:本包内的 plugin/ 目录会遮蔽 SDK 的顶层 plugin 包(从插件根以 python -m
运行测试/工具时必现),因此下面加载实现前会临时把插件根移出 sys.path,
确保实现里的 `from plugin.sdk...` 命中 SDK 而不是本包的 plugin/。
"""
import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _P

_local_root = _P(__file__).resolve().parent
_impl_path = _local_root / "plugin" / "plugins" / "workbench_installer" / "__init__.py"
_spec = _ilu.spec_from_file_location("workbench_installer_impl", _impl_path)
if _spec is None or _spec.loader is None:  # pragma: no cover - 文件恒存在
    raise ImportError(f"无法加载实现模块: {_impl_path}")


def _load_impl():
    """防遮蔽加载实现:插件根暂不参与解析,SDK 的 plugin 包才有机会被命中。"""
    poisoned = _sys.modules.get("plugin")
    if poisoned is not None:
        p = getattr(poisoned, "__file__", None) or next(iter(getattr(poisoned, "__path__", [])), "")
        if p and _P(str(p)).resolve().is_relative_to(_local_root):
            del _sys.modules["plugin"]
    saved = _sys.path[:]
    _sys.path[:] = [
        e for e in _sys.path
        if _P(e or ".").resolve() != _local_root
    ]
    try:
        _spec.loader.exec_module(_impl_mod)
    finally:
        _sys.path[:] = saved


_impl_mod = _ilu.module_from_spec(_spec)
_load_impl()

WorkbenchInstallerPlugin = _impl_mod.WorkbenchInstallerPlugin
InstallParams = _impl_mod.InstallParams
# SDK 结果类型一并转发(入口测试与调用方按 mod.Ok / mod.Err 判定返回)
Ok = _impl_mod.Ok
Err = _impl_mod.Err
SdkError = _impl_mod.SdkError

__all__ = ["WorkbenchInstallerPlugin", "InstallParams", "Ok", "Err", "SdkError"]
