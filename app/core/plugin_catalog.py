"""Local catalog for plugins bundled with the CrowKit installer."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .plugin_manager import (
    BUNDLED_PLUGINS_DIR,
    PluginError,
    PluginManager,
    _validate_manifest,
)
from .release_utils import compare_versions, minimum_host_satisfied
from ..version import __version__


class PluginCatalog:
    def __init__(self, manager: PluginManager):
        self.manager = manager
        self._lock = threading.Lock()

    def _bundled_plugins(self) -> list[dict[str, Any]]:
        plugins: list[dict[str, Any]] = []
        if not BUNDLED_PLUGINS_DIR.is_dir():
            return plugins
        for directory in sorted(BUNDLED_PLUGINS_DIR.iterdir()):
            if not directory.is_dir() or directory.name.startswith(("_", ".")):
                continue
            manifest_path = directory / "plugin.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = _validate_manifest(manifest, directory.name)
            except (OSError, json.JSONDecodeError, PluginError):
                continue
            plugins.append({
                "id": str(manifest["id"]),
                "name": str(manifest["name"]),
                "description": str(manifest.get("description") or ""),
                "repository": str(manifest.get("repository") or ""),
                "bundled": True,
                "source_dir": str(directory),
                "latest": {
                    "version": str(manifest["version"]),
                    "host_version": {
                        "min": str(
                            (manifest.get("host") or {}).get("min_version")
                            or ""),
                        "max": None,
                    },
                },
                "versions": [{"version": str(manifest["version"])}],
                "error": "",
                "_mtime": manifest_path.stat().st_mtime,
            })
        return plugins

    def list_catalog(self, *, force: bool = False) -> dict[str, Any]:
        del force
        bundled = self._bundled_plugins()
        installed = {
            plugin["id"]: plugin for plugin in self.manager.list_plugins()
        }
        catalog_ids = {item["id"] for item in bundled}
        plugins: list[dict[str, Any]] = []
        updated_at = ""
        for item in bundled:
            plugin = dict(item)
            source_dir = plugin.pop("source_dir")
            updated_at = max(
                updated_at,
                time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(plugin.pop("_mtime", 0)),
                ),
            )
            plugin["source_dir"] = source_dir
            installed_plugin = installed.get(plugin["id"])
            installed_version = (
                str(installed_plugin.get("version") or "")
                if installed_plugin else "")
            latest_version = str(
                (plugin.get("latest") or {}).get("version") or "")
            minimum = str(
                ((plugin.get("latest") or {}).get("host_version") or {})
                .get("min") or "")
            compatible = minimum_host_satisfied(minimum, __version__)
            plugins.append({
                **plugin,
                "installed": bool(installed_plugin),
                "installed_version": installed_version,
                "running": bool(
                    installed_plugin and installed_plugin.get("running")),
                "status": (
                    installed_plugin.get("status")
                    if installed_plugin else "not-installed"),
                "update_available": bool(
                    installed_plugin
                    and compatible
                    and compare_versions(
                        latest_version, installed_version) > 0
                ),
                "compatible": compatible,
                "dependency": minimum,
            })
        for plugin_id, plugin in installed.items():
            if plugin_id in catalog_ids:
                continue
            plugins.append({
                "id": plugin_id,
                "name": plugin["name"],
                "description": plugin.get("description", ""),
                "repository": plugin.get("repository", ""),
                "latest": None,
                "versions": [],
                "installed": True,
                "installed_version": plugin["version"],
                "running": plugin.get("running", False),
                "status": plugin.get("status", "stopped"),
                "update_available": False,
                "compatible": True,
                "dependency": str(
                    (plugin.get("host") or {}).get("min_version") or ""),
                "error": "",
                "local_only": True,
                "bundled": False,
            })
        plugins.sort(key=lambda item: item["name"].casefold())
        return {
            "plugins": plugins,
            "stale": False,
            "error": "",
            "updated_at": updated_at,
            "host_version": __version__,
        }

    def install(
        self,
        plugin_id: str,
        *,
        update: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict:
        items = self.list_catalog()["plugins"]
        item = next((entry for entry in items if entry["id"] == plugin_id), None)
        if not item or not item.get("bundled"):
            raise PluginError(f"插件不存在于随包目录: {plugin_id}")
        if not item.get("compatible"):
            raise PluginError(f"该插件需要更高版本的 CrowKit ({item['dependency']}+)")
        if item.get("installed") and not update:
            raise PluginError("插件已安装")
        if update and not item.get("update_available"):
            raise PluginError("插件已是最新版本")
        if item.get("running"):
            raise PluginError("插件正在运行，请先停止")
        if progress:
            progress(0, 1)
        source = Path(str(item.get("source_dir") or ""))
        result = self.manager.install_from_directory(source, replace=update)
        if progress:
            progress(1, 1)
        return result

    def auto_update_installed(self) -> list[dict]:
        updated: list[dict] = []
        for item in self.list_catalog()["plugins"]:
            if not item.get("update_available") or item.get("running"):
                continue
            try:
                updated.append(self.install(item["id"], update=True))
            except PluginError:
                continue
        return updated
