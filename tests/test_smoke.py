from pathlib import Path


def test_plugin_manifest_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "workbench_installer"' in text
    assert 'entry = "plugin.plugins.workbench_installer:WorkbenchInstallerPlugin"' in text


def test_entry_class_declared() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "__init__.py").read_text(encoding="utf-8")
    assert "@neko_plugin" in source
    assert "class WorkbenchInstallerPlugin(NekoPluginBase)" in source
