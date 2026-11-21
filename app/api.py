"""REST API 与 WebSocket 路由。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from typing import Annotated
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, WebSocket, WebSocketDisconnect, HTTPException

from .core.config_store import (
    AccountSelectionStore,
    GlobalConfig,
    PluginConfigStore,
)
from .core import course_cache
from .core.edge_driver import edge_status, ensure_edge_driver
from .core.app_updater import app_updater
from .core.error_reports import (
    compact_object,
    create_error_bundle,
    read_error_bundle,
    reveal_path,
)
from .core.error_policy import classify_user_error, friendly_user_message
from .core.glitchtip import send_event
from .core.plugin_catalog import PluginCatalog
from .core.plugin_manager import PluginManager, PluginError
from .core.resources import resource_profile
from .core.runner import PLUGIN_SDK_DIR, Runner, get_bus
from .core.release_utils import compare_versions
from .version import __version__

api_router = APIRouter(prefix="/api")

global_config = GlobalConfig()
account_selections = AccountSelectionStore()
runner = Runner()
pm = PluginManager(runner)
plugin_catalog = PluginCatalog(pm)
_BDATA_ID = "bdata-autoplay"
_CAMPUS_AUTH_ID = "campus-network-auth"

# ---------------- 窗口关闭自动退出 ----------------
# 页面通过 WebSocket 保持连接;所有客户端断开且宽限期内无人重连(如页面刷新)时,
# 视为 GUI 窗口已关闭,自动停止服务与插件子进程。

GRACE_SECONDS = 8.0
_shutdown_callback = None
_shutdown_timer: threading.Timer | None = None
_ws_count = 0
_ws_lock = threading.Lock()


def set_shutdown_callback(cb) -> None:
    global _shutdown_callback
    _shutdown_callback = cb


def _fire_shutdown() -> None:
    global _shutdown_timer
    with _ws_lock:
        if _ws_count > 0:
            return
        _shutdown_timer = None
        cb = _shutdown_callback
    if cb:
        print("检测到窗口关闭且无客户端连接,自动退出服务", flush=True)
        try:
            cb()
        except Exception:
            pass


def _client_connected() -> None:
    global _ws_count, _shutdown_timer
    with _ws_lock:
        _ws_count += 1
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
            _shutdown_timer = None


def _client_disconnected() -> None:
    global _ws_count, _shutdown_timer
    with _ws_lock:
        _ws_count = max(0, _ws_count - 1)
        if _ws_count == 0 and _shutdown_timer is None and _shutdown_callback is not None:
            _shutdown_timer = threading.Timer(GRACE_SECONDS, _fire_shutdown)
            _shutdown_timer.daemon = True
            _shutdown_timer.start()


def _effective_plugin_config(
    plugin: dict,
    cfg: dict,
    account_id: str = "",
) -> dict:
    """Inject the host-selected account into an isolated plugin config."""
    g = global_config.get()
    effective = dict(cfg)
    selected_id = account_id or account_selections.get(plugin["id"])
    account = global_config.resolve_account(selected_id)
    username = str(account.get("username") or "")
    password = (
        str(account.get("campus_password") or "")
        if plugin.get("id") == _CAMPUS_AUTH_ID
        else str(account.get("password") or "")
    )
    merged = {
        "id": str(account.get("id") or ""),
        "nickname": str(account.get("nickname") or username),
        "username": username,
        "password": password,
        "education_level": str(account.get("education_level") or ""),
        "grade": str(account.get("grade") or ""),
        "department": str(account.get("department") or ""),
        "major": str(account.get("major") or ""),
    }
    effective["account"] = merged
    effective["username"] = merged["username"]
    effective["password"] = merged["password"]
    effective["account_nickname"] = merged["nickname"]
    general = effective.get("general")
    if not isinstance(general, dict):
        general = {}
        effective["general"] = general
    global_general = g.get("general")
    if not isinstance(global_general, dict):
        global_general = {}
    for key, value in global_general.items():
        general.setdefault(key, value)
    return effective


def _restore_masked_secrets(patch: object, current: object) -> object:
    """Recursively replace ``******`` placeholders without mutating request data."""
    if isinstance(patch, dict):
        base = current if isinstance(current, dict) else {}
        return {
            key: _restore_masked_secrets(value, base.get(key))
            for key, value in patch.items()
        }
    if isinstance(patch, list):
        base = current if isinstance(current, list) else []
        return [
            _restore_masked_secrets(value, base[i] if i < len(base) else None)
            for i, value in enumerate(patch)
        ]
    if patch == "******" and isinstance(current, str) and current:
        return current
    return patch


def _restore_masked_accounts(patch: list, current: object) -> list:
    """Merge account secrets by stable id so add/delete cannot shift passwords."""
    current_by_id = {
        str(item.get("id") or ""): item
        for item in current
        if isinstance(item, dict)
    } if isinstance(current, list) else {}
    restored = []
    for item in patch:
        if not isinstance(item, dict):
            continue
        account = dict(item)
        old = current_by_id.get(str(account.get("id") or ""), {})
        for key in ("password", "campus_password"):
            if account.get(key) == "******" and old.get(key):
                account[key] = old[key]
        restored.append(account)
    return restored


def _config_patch(body: dict) -> dict:
    patch = body.get("config")
    if not isinstance(patch, dict):
        raise HTTPException(422, "config 必须是 JSON 对象")
    return patch


def _strip_plugin_styles(html: str) -> str:
    """Remove plugin-provided CSS, including both quote styles for attributes."""
    import re as _re
    html = _re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", html, flags=_re.I)
    return _re.sub(
        r"""\sstyle\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
        "", html, flags=_re.I,
    )


# ---------------- 插件 ----------------

@api_router.get("/plugins")
def list_plugins():
    plugins = pm.list_plugins()
    for plugin in plugins:
        plugin["selected_account_id"] = account_selections.get(plugin["id"])
    return {"plugins": plugins}


@api_router.put("/plugins/{plugin_id}/account")
def select_plugin_account(plugin_id: str, body: dict):
    try:
        pm.get_plugin(plugin_id)
    except PluginError as exc:
        raise HTTPException(404, str(exc)) from exc
    account_id = str(body.get("account_id") or "").strip()
    if account_id and not global_config.resolve_account(account_id).get("id"):
        raise HTTPException(404, "账号不存在")
    account_selections.set(plugin_id, account_id)
    return {"ok": True, "account_id": account_id}


@api_router.get("/plugins/{plugin_id}/selected-account")
def get_selected_plugin_account(plugin_id: str):
    try:
        pm.get_plugin(plugin_id)
    except PluginError as exc:
        raise HTTPException(404, str(exc)) from exc
    account = global_config.resolve_account(account_selections.get(plugin_id))
    if not account.get("id"):
        return {"account": None}
    return {
        "account": {
            "id": str(account.get("id") or ""),
            "username": str(account.get("username") or ""),
            "nickname": str(account.get("nickname") or ""),
            "education_level": str(account.get("education_level") or ""),
            "grade": str(account.get("grade") or ""),
            "department": str(account.get("department") or ""),
            "major": str(account.get("major") or ""),
        },
    }


@api_router.get("/plugins/catalog")
def list_plugin_catalog(refresh: bool = False):
    try:
        return plugin_catalog.list_catalog(force=refresh)
    except Exception as exc:
        raise HTTPException(502, f"无法获取插件目录: {exc}") from exc


@api_router.post("/plugins/catalog/{plugin_id}/install")
def install_catalog_plugin(plugin_id: str, body: dict | None = None):
    update = bool((body or {}).get("update", False))
    try:
        bus = get_bus()
        last_percent = -1

        def report(downloaded: int, total: int) -> None:
            nonlocal last_percent
            percent = (
                min(100, int(downloaded * 100 / total))
                if total > 0 else None
            )
            if percent == last_percent:
                return
            last_percent = percent
            bus.publish({
                "type": "plugin_download",
                "plugin": plugin_id,
                "downloaded": downloaded,
                "total": total,
                "percent": percent,
            })

        result = plugin_catalog.install(
            plugin_id, update=update, progress=report)
        get_bus().publish({
            "type": "plugin_installed",
            "plugin": plugin_id,
            "updated": update,
        })
        return result
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@api_router.get("/system/edge")
def get_edge_status():
    return edge_status()


@api_router.get("/system/info")
def get_system_info():
    return {
        "name": "CrowKit",
        "version": __version__,
        "platform": "Windows 11 x64",
        "browser": "Microsoft Edge",
    }


@api_router.get("/system/specialty-catalog")
def get_specialty_catalog():
    root = Path(__file__).resolve().parents[1]
    candidates = []
    try:
        plugin = pm.get_plugin(_GRAB_ID)
        candidates.append(Path(plugin["dir"]) / "catalog.json")
    except PluginError:
        pass
    candidates.extend([
        Path(PLUGIN_SDK_DIR).parent / "plugins" / _GRAB_ID / "catalog.json",
        root / "plugins" / _GRAB_ID / "catalog.json",
    ])
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("levels"), list):
            return data
    raise HTTPException(404, "抢课插件内置专业目录不可用")


@api_router.get("/system/app-update")
def get_app_update():
    return app_updater.snapshot()


@api_router.post("/system/app-update/check")
def check_app_update():
    return app_updater.check()


_app_update_download_lock = threading.Lock()
_app_update_downloading = False


@api_router.post("/system/app-update/download")
def download_app_update():
    global _app_update_downloading
    with _app_update_download_lock:
        if _app_update_downloading:
            raise HTTPException(409, "安装包正在下载")
        if not app_updater.snapshot().get("available"):
            raise HTTPException(409, "当前没有可用更新")
        _app_update_downloading = True
    try:
        prepared = app_updater.prepare_manual_download()
    except Exception as exc:
        with _app_update_download_lock:
            _app_update_downloading = False
        raise HTTPException(409, str(exc)) from exc

    def publish(state: dict) -> None:
        get_bus().publish({"type": "app_update", **state})

    def downloaded(path: Path) -> None:
        try:
            os.startfile(str(path))
        except OSError as exc:
            publish(app_updater.fail_installer_launch(str(exc)))
            return
        publish(app_updater.mark_installer_launched())
        callback = _shutdown_callback
        if callback:
            threading.Timer(1.5, callback).start()

    def worker() -> None:
        global _app_update_downloading
        try:
            app_updater.download_for_manual_install(publish, downloaded)
        except Exception:
            pass
        finally:
            with _app_update_download_lock:
                _app_update_downloading = False

    threading.Thread(
        target=worker, daemon=True, name="app-update-download",
    ).start()
    return {"ok": True, **prepared}


@api_router.get("/system/resources")
def get_system_resources():
    return resource_profile().to_dict()


@api_router.post("/system/edge/driver")
def update_edge_driver():
    try:
        path = ensure_edge_driver(log=print, force=True)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, "status": edge_status(), "path": str(path)}


@api_router.post("/system/edge/check")
def check_edge_driver():
    before = edge_status()
    try:
        path = ensure_edge_driver(log=print)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    after = edge_status()
    return {
        "ok": True,
        "updated": (
            before.get("driver_version") != after.get("driver_version")
        ),
        "status": after,
        "path": str(path),
    }


@api_router.post("/plugins/install")
async def install_plugin(file: Annotated[UploadFile, File()]):
    # 文件名完全由服务端生成, 不拼接客户端可控路径。
    tmp = Path(tempfile.gettempdir()) / f"crowkit-plugin-{uuid.uuid4().hex}.zip"
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f)
        result = pm.install(str(tmp))
        get_bus().publish({"type": "plugin_installed", "plugin": result["id"]})
        return result
    except PluginError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        tmp.unlink(missing_ok=True)


@api_router.post("/system/open-logs")
def open_logs_folder(body: dict | None = None):
    from .core.paths import LOG_DIR, ensure_data_dirs

    ensure_data_dirs()
    target = LOG_DIR
    raw_path = str((body or {}).get("path") or "").strip()
    if raw_path:
        candidate = Path(raw_path).resolve()
        reports_dir = (LOG_DIR / "reports").resolve()
        if candidate.parent != reports_dir or candidate.suffix.lower() != ".zip":
            raise HTTPException(400, "无效的错误报告路径")
        if not candidate.is_file():
            raise HTTPException(404, "错误报告文件不存在")
        target = candidate
    try:
        reveal_path(target)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(500, f"无法打开日志文件夹: {exc}") from exc
    return {"ok": True, "path": str(target), "selected": target.is_file()}


def _report_path(raw_path: object) -> Path | None:
    from .core.paths import LOG_DIR

    value = str(raw_path or "").strip()
    if not value:
        return None
    candidate = Path(value).resolve()
    reports_dir = (LOG_DIR / "reports").resolve()
    if candidate.parent != reports_dir or candidate.suffix.lower() != ".zip":
        raise HTTPException(400, "无效的错误报告路径")
    return candidate if candidate.is_file() else None


def _report_plugin_info(plugin_id: str) -> tuple[dict, dict]:
    if not plugin_id:
        return {}, {}
    try:
        manifest = pm.get_plugin(plugin_id)
        info = {
            "id": manifest.get("id", ""),
            "name": manifest.get("name", ""),
            "version": manifest.get("version", ""),
            "description": manifest.get("description", ""),
            "repository": manifest.get("repository", ""),
            "host": manifest.get("host", {}),
        }
    except PluginError:
        info = {}
    try:
        config = PluginConfigStore(plugin_id).get_masked()
    except (OSError, ValueError):
        config = {}
    return info, config


def _feedback_eligibility(plugin_id: str) -> dict:
    app_state = app_updater.check()
    if app_state.get("status") == "error":
        return {
            "status": "verification_failed",
            "message": "无法确认主程序是否为最新版本，请检查网络后重试。",
        }
    if app_state.get("available"):
        return {
            "status": "host_outdated",
            "message": (
                f"主程序不是最新版本。请先更新到 {app_state.get('latest_version')}，"
                "本次错误不会上传。"
            ),
            "download_url": app_state.get("download_url", ""),
        }
    if not plugin_id:
        return {"status": "eligible"}
    try:
        catalog = plugin_catalog.list_catalog(force=True)
    except Exception:
        return {
            "status": "verification_failed",
            "message": "无法确认插件是否为最新官方版本，请检查网络后重试。",
        }
    if catalog.get("stale"):
        return {
            "status": "verification_failed",
            "message": "插件版本目录暂时不可用，无法确认官方版本，本次错误不会上传。",
        }
    item = next(
        (entry for entry in catalog.get("plugins", [])
         if entry.get("id") == plugin_id),
        None,
    )
    if not item or item.get("local_only") or not item.get("bundled"):
        return {
            "status": "non_official",
            "message": f"出错插件“{item.get('name') if item else plugin_id}”不是官方插件，本次错误不会上传。",
        }
    latest = item.get("latest")
    if not isinstance(latest, dict) or not latest.get("version"):
        return {
            "status": "verification_failed",
            "message": "无法确认官方插件的最新版本，本次错误不会上传。",
        }
    installed_version = str(item.get("installed_version") or "")
    if (
        not installed_version
        or compare_versions(
            str(latest.get("version") or ""), installed_version) > 0
    ):
        return {
            "status": "plugin_outdated",
            "message": (
                f"插件“{item.get('name') or plugin_id}”不是最新版本。"
                "请先在插件管理中更新，本次错误不会上传。"
            ),
        }
    return {"status": "eligible"}


def _create_error_report_from_payload(
    payload: dict, *, action_description: str = "",
) -> Path:
    plugin_id = str(payload.get("plugin") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    plugin_info, plugin_config = _report_plugin_info(plugin_id)
    edge = edge_status()
    edge_info = {
        "edge_version": edge.get("edge_version", ""),
        "driver_version": edge.get("driver_version", ""),
        "compatible": edge.get("compatible", False),
    }
    try:
        return create_error_bundle(
            message=str(payload.get("message") or ""),
            context=str(payload.get("context") or ""),
            plugin=plugin_id,
            run_id=run_id,
            action_description=action_description,
            log_path=runner.run_log_path(plugin_id, run_id),
            plugin_info=plugin_info,
            plugin_config=plugin_config,
            edge_info=edge_info,
        )
    except (OSError, zipfile.BadZipFile) as exc:
        raise HTTPException(500, f"无法生成错误报告: {exc}") from exc


@api_router.post("/system/error-report")
def create_error_report(body: dict | None = None):
    payload = body or {}
    path = _create_error_report_from_payload(payload)
    diagnostics, log_text = read_error_bundle(path)
    traceback_info = diagnostics.get("python_traceback") or {}
    user_error = classify_user_error(
        payload.get("message"),
        payload.get("context"),
        traceback_info.get("type"),
        traceback_info.get("value"),
        log_text,
    )
    return {
        "ok": True,
        "path": str(path),
        "submitted": False,
        "expected": bool(user_error),
        "user_error_category": (
            user_error.get("category", "") if user_error else ""),
        "detail": (
            friendly_user_message(traceback_info.get("value"))
            if user_error and traceback_info
            else (
                f"{traceback_info.get('type')}: {traceback_info.get('value')}"
                if traceback_info else ""
            )
        ),
    }


@api_router.post("/system/error-report/submit")
def submit_error_report(body: dict | None = None):
    payload = body or {}
    action_description = str(payload.get("action_description") or "").strip()
    if len(action_description) < 2:
        raise HTTPException(400, "请填写触发错误前的操作")
    existing = _report_path(payload.get("bundle_path"))
    existing_diagnostics: dict = {}
    existing_log = ""
    if existing is not None:
        existing_diagnostics, existing_log = read_error_bundle(existing)
    traceback_info = existing_diagnostics.get("python_traceback") or {}
    user_error = classify_user_error(
        payload.get("message"),
        payload.get("context"),
        traceback_info.get("type"),
        traceback_info.get("value"),
        existing_log,
    )
    if user_error:
        return {
            "ok": True,
            "uploaded": False,
            "status": "user_error",
            "user_error_category": user_error["category"],
            "message": "这是操作提示，不会提交到 GlitchTip。",
        }
    eligibility = _feedback_eligibility(
        str(payload.get("plugin") or "").strip())
    if eligibility["status"] != "eligible":
        return {
            "ok": True,
            "uploaded": False,
            **eligibility,
        }
    path = _create_error_report_from_payload(
        payload, action_description=action_description)
    diagnostics, log_text = read_error_bundle(path)
    uploaded = send_event(
        str(payload.get("message") or ""),
        context=str(payload.get("context") or ""),
        plugin=str(payload.get("plugin") or ""),
        source=str(payload.get("source") or "ui"),
        action_description=action_description,
        plugin_info=compact_object(diagnostics.get("plugin_info") or {}),
        plugin_config=compact_object(diagnostics.get("plugin_config") or {}),
        edge_info=compact_object(diagnostics.get("edge") or {}),
        python_traceback=diagnostics.get("python_traceback"),
        log_text=log_text,
    )
    return {
        "ok": True,
        "uploaded": uploaded,
        "status": "submitted" if uploaded else "upload_failed",
        "message": (
            "错误报告已上传到 GlitchTip。"
            if uploaded else "日志已保存在本地，但上传 GlitchTip 失败。"
        ),
        "path": str(path),
    }


@api_router.delete("/plugins/{plugin_id}")
def uninstall_plugin(plugin_id: str):
    if plugin_id == _CAMPUS_AUTH_ID:
        try:
            script = _campus_autostart_script(plugin_id)
            _run_campus_autostart(script, "--uninstall")
        except HTTPException:
            # Deleting the plugin should still work if its autostart helper is
            # already missing or the registry entry cannot be read.
            pass
    try:
        pm.uninstall(plugin_id)
        get_bus().publish({"type": "plugin_uninstalled", "plugin": plugin_id})
        return {"ok": True}
    except PluginError as e:
        raise HTTPException(400, str(e)) from e


@api_router.get("/plugins/{plugin_id}/config")
def get_plugin_config(plugin_id: str):
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e)) from e
    cfg = PluginConfigStore(plugin_id).get_masked()
    if plugin_id == _CAMPUS_AUTH_ID:
        for key in ("username", "password", "campus_password", "account"):
            cfg.pop(key, None)
    if plugin_id == _BDATA_ID and not cfg.get("workers"):
        cfg["workers"] = resource_profile().recommended_workers
    # 未配置过的字段用清单里的默认值填充,前端表单才有初始显示
    for f in plugin.get("config_schema", []):
        key = f.get("key")
        if key and key not in cfg and "default" in f:
            cfg[key] = f["default"]
    return {"config": cfg, "schema": plugin.get("config_schema", [])}


@api_router.get("/plugins/{plugin_id}/ui")
def get_plugin_ui(plugin_id: str):
    """返回插件自定义配置界面 HTML(config_ui)。

    安全与风格约定: 只允许插件目录内的单个 .html 文件; 强制剥离 <style>
    标签与 style 属性(插件界面不允许自带 CSS, 风格由宿主 crowkit-* 类统一提供),
    防止插件破坏主界面外观或注入钓鱼样式。脚本保留(经用户主动安装插件)。
    """
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e)) from e
    ui_name = plugin.get("config_ui") or ""
    if not ui_name:
        raise HTTPException(404, "该插件未提供自定义配置界面")
    plugin_dir = Path(plugin["dir"]).resolve()
    ui_path = (plugin_dir / ui_name).resolve()
    if not ui_path.is_relative_to(plugin_dir):
        raise HTTPException(400, "配置界面文件不在插件目录内")
    if not ui_path.is_file():
        raise HTTPException(404, f"配置界面文件不存在: {ui_name}")
    html = ui_path.read_text(encoding="utf-8")
    return {"html": _strip_plugin_styles(html)}


@api_router.put("/plugins/{plugin_id}/config")
async def save_plugin_config(plugin_id: str, body: dict):
    try:
        pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e)) from e
    store = PluginConfigStore(plugin_id)
    old = store.get()
    new = _restore_masked_secrets(_config_patch(body), old)
    if plugin_id == _CAMPUS_AUTH_ID:
        for key in ("username", "password", "campus_password", "account"):
            new.pop(key, None)
    store.save(new)
    return {"ok": True}


@api_router.post("/plugins/{plugin_id}/start")
def start_plugin(plugin_id: str, body: dict | None = None):
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e)) from e
    if runner.is_running(plugin_id):
        raise HTTPException(409, "插件已在运行")
    if plugin_id == _BDATA_ID:
        for path in _review_paths(plugin_id):
            path.unlink(missing_ok=True)
    cfg = PluginConfigStore(plugin_id).get()
    requested_account = str((body or {}).get("account_id") or "").strip()
    if requested_account:
        if not global_config.resolve_account(requested_account).get("id"):
            raise HTTPException(404, "所选账号不存在")
        account_selections.set(plugin_id, requested_account)
    effective = _effective_plugin_config(
        plugin, cfg, requested_account or account_selections.get(plugin_id))
    if not effective.get("username"):
        raise HTTPException(409, "请先在账号管理中添加并选择账号")
    if plugin_id == _CAMPUS_AUTH_ID and not effective.get("password"):
        raise HTTPException(409, "所选账号未配置校园网密码")
    if plugin_id in {_BDATA_ID, _GRAB_ID} and not effective.get("password"):
        raise HTTPException(409, "所选账号未配置统一身份认证密码")
    if plugin_id in {_BDATA_ID, _GRAB_ID}:
        try:
            ensure_edge_driver(log=print)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
    data_dir = pm.plugin_data_dir(plugin_id)
    plugin_dir = Path(plugin["dir"]).resolve()
    entry = (plugin_dir / plugin["entry"]).resolve()
    if not entry.is_relative_to(plugin_dir):
        raise HTTPException(400, "插件入口文件不在插件目录内")
    env_extra = {"PYTHONPATH": str(PLUGIN_SDK_DIR)}
    try:
        runner.start(plugin_id, entry, effective, data_dir, env_extra=env_extra)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@api_router.post("/plugins/{plugin_id}/stop")
def stop_plugin(plugin_id: str):
    runner.request_stop(plugin_id)
    if plugin_id == _BDATA_ID:
        for path in _review_paths(plugin_id):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return {"ok": True}


def _review_paths(plugin_id: str) -> tuple[Path, Path]:
    if plugin_id != _BDATA_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    data_dir = pm.plugin_data_dir(plugin_id)
    return data_dir / "review_request.json", data_dir / "review_response.json"


def _bdata_task_cache_path(plugin_id: str) -> Path:
    if plugin_id != _BDATA_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    return pm.plugin_data_dir(plugin_id) / "course_tasks.json"


def _read_json_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@api_router.get("/plugins/{plugin_id}/tasks")
def get_bdata_tasks(plugin_id: str):
    if plugin_id != _BDATA_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e)) from e
    cfg = PluginConfigStore(plugin_id).get()
    effective = _effective_plugin_config(plugin, cfg)
    account = str(effective.get("username") or "").strip()
    data = _read_json_object(_bdata_task_cache_path(plugin_id))
    if not data or isinstance(data.get("version"), int):
        return {
            "account": account,
            "updated_at": "",
            "bdata": {},
            "spoc": [],
        }
    entry = data.get(account)
    if not isinstance(entry, dict):
        return {
            "account": account,
            "updated_at": "",
            "bdata": {},
            "spoc": [],
        }
    return {
        "account": account,
        "updated_at": str(entry.get("updated_at") or ""),
        "bdata": entry.get("bdata")
        if isinstance(entry.get("bdata"), dict) else {},
        "spoc": entry.get("spoc")
        if isinstance(entry.get("spoc"), list) else [],
    }


@api_router.post("/plugins/{plugin_id}/tasks/refresh")
def refresh_bdata_tasks(plugin_id: str):
    if plugin_id != _BDATA_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e)) from e
    if runner.is_running(plugin_id):
        raise HTTPException(409, "插件已在运行")
    cfg = PluginConfigStore(plugin_id).get()
    effective = _effective_plugin_config(plugin, cfg)
    if not effective.get("username") or not effective.get("password"):
        raise HTTPException(400, "请先在账号管理中配置并选择统一身份认证账号")
    effective["task_mode"] = "crawl"
    for path in _review_paths(plugin_id):
        path.unlink(missing_ok=True)
    data_dir = pm.plugin_data_dir(plugin_id)
    plugin_dir = Path(plugin["dir"]).resolve()
    entry = (plugin_dir / plugin["entry"]).resolve()
    if not entry.is_relative_to(plugin_dir):
        raise HTTPException(400, "插件入口文件不在插件目录内")
    try:
        runner.start(
            plugin_id, entry, effective, data_dir,
            env_extra={"PYTHONPATH": str(PLUGIN_SDK_DIR)},
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


def _campus_autostart_script(plugin_id: str) -> Path:
    if plugin_id != _CAMPUS_AUTH_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e)) from e
    plugin_dir = Path(plugin["dir"]).resolve()
    script = (plugin_dir / "autostart.py").resolve()
    if not script.is_relative_to(plugin_dir) or not script.is_file():
        raise HTTPException(404, "校园网认证自启脚本不存在")
    return script


def _run_campus_autostart(script: Path, action: str) -> dict:
    host_root = Path(__file__).resolve().parents[1]
    plugin_data_dir = pm.plugin_data_dir(_CAMPUS_AUTH_ID)
    env = os.environ.copy()
    env["CROWKIT_HOME"] = str(host_root)
    env["CROWKIT_PLUGIN_DATA_DIR"] = str(plugin_data_dir)
    try:
        result = subprocess.run(
            [
                sys.executable, str(script), action,
                "--home", str(host_root),
                "--data-dir", str(plugin_data_dir),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(500, f"执行开机自启配置失败: {exc}") from exc
    output = (result.stdout or result.stderr or "").strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            500, f"开机自启脚本返回异常: {output[:300]}") from exc
    if result.returncode != 0 and not isinstance(payload.get("status"), dict):
        raise HTTPException(
            500, str(payload.get("error") or "开机自启配置失败"))
    return payload


@api_router.get("/plugins/{plugin_id}/autostart")
def get_campus_autostart(plugin_id: str):
    script = _campus_autostart_script(plugin_id)
    return _run_campus_autostart(script, "--status")


@api_router.post("/plugins/{plugin_id}/autostart")
def set_campus_autostart(plugin_id: str, body: dict):
    script = _campus_autostart_script(plugin_id)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(422, "enabled 必须是布尔值")
    store = PluginConfigStore(plugin_id)
    config = store.get()
    for key in ("username", "password", "campus_password", "account"):
        config.pop(key, None)
    config["autostart"] = enabled
    store.save(config)
    result = _run_campus_autostart(
        script, "--install" if enabled else "--uninstall")
    return {"ok": True, "autostart": bool(result.get("installed")), **result}


@api_router.get("/plugins/{plugin_id}/review")
def get_review(plugin_id: str):
    request_path, response_path = _review_paths(plugin_id)
    if not runner.is_running(plugin_id):
        return {"request": None, "response": None}
    request = _read_json_object(request_path)
    response = _read_json_object(response_path)
    if request and response and response.get("request_id") != request.get("id"):
        response = None
    return {"request": request, "response": response}


@api_router.put("/plugins/{plugin_id}/review")
async def save_review(plugin_id: str, body: dict):
    request_path, response_path = _review_paths(plugin_id)
    if not runner.is_running(plugin_id):
        raise HTTPException(409, "插件当前未运行")
    request = _read_json_object(request_path)
    if not request:
        raise HTTPException(409, "当前没有待确认内容")
    request_id = str(body.get("request_id") or "")
    if request_id != str(request.get("id") or ""):
        raise HTTPException(409, "确认内容已失效，请刷新后重试")
    action = str(body.get("action") or "approve")
    if action not in {"approve", "skip"}:
        raise HTTPException(422, "action 必须是 approve 或 skip")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(422, "payload 必须是 JSON 对象")
    response = {
        "request_id": request_id,
        "action": action,
        "payload": payload,
    }
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=response_path.parent,
                prefix=".review-response-", suffix=".tmp",
                delete=False) as handle:
            tmp_path = handle.name
            json.dump(response, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, response_path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return {"ok": True}


@api_router.get("/plugins/{plugin_id}/logs")
def plugin_logs(plugin_id: str, limit: int = 200):
    limit = max(1, min(limit, 2000))
    return {"events": get_bus().recent(plugin_id, limit)}


# ---------------- 专业目录 / 课程列表(course-grab) ----------------

_GRAB_ID = "course-grab"
_TASK_CATALOG = "catalog"     # 抓取全部专业目录
_TASK_COURSES = "courses"     # 抓取指定专业的课程列表


def _grab_task_running() -> bool:
    """同一时刻只允许一个抓取任务: 目录抓取与课程抓取、插件正常运行互斥。

    Runner 按 plugin_id 管理子进程, 复用 course-grab 的运行槽位天然保证互斥。
    """
    return runner.is_running(_GRAB_ID)


def _start_grab_task(task: str, extra: dict) -> None:
    try:
        plugin = pm.get_plugin(_GRAB_ID)
    except PluginError as e:
        raise HTTPException(404, str(e)) from e
    if _grab_task_running():
        raise HTTPException(409, "抓取任务已在运行")
    cfg = PluginConfigStore(_GRAB_ID).get()
    effective = _effective_plugin_config(plugin, cfg)
    if not effective.get("username") or not effective.get("password"):
        raise HTTPException(400, "请先在账号管理中配置并选择统一身份认证账号")
    effective["task"] = task
    effective.update(extra)
    data_dir = pm.plugin_data_dir(_GRAB_ID)
    plugin_dir = Path(plugin["dir"]).resolve()
    entry = (plugin_dir / plugin["entry"]).resolve()
    if not entry.is_relative_to(plugin_dir):
        raise HTTPException(400, "插件入口文件不在插件目录内")
    env_extra = {"PYTHONPATH": str(PLUGIN_SDK_DIR)}
    try:
        runner.start(_GRAB_ID, entry, effective, data_dir, env_extra=env_extra)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@api_router.get("/plugins/{plugin_id}/majors")
def get_majors(plugin_id: str):
    if plugin_id != _GRAB_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    catalog = course_cache.load_catalog()
    if catalog is None:
        raise HTTPException(404, "专业目录尚未抓取, 请先点击「抓取专业目录」")
    return catalog


@api_router.post("/plugins/{plugin_id}/majors/refresh")
def refresh_majors(plugin_id: str):
    if plugin_id != _GRAB_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    _start_grab_task(_TASK_CATALOG, {})
    return {"ok": True}


@api_router.get("/plugins/{plugin_id}/major-courses")
def get_major_courses(
    plugin_id: str,
    level: str = "",
    grade: str = "",
    dept: str = "",
    major: str = "",
):
    if plugin_id != _GRAB_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    if not (level and grade and dept and major):
        raise HTTPException(400, "缺少专业参数")
    data = course_cache.load_courses(level, grade, dept, major)
    if data is None:
        raise HTTPException(404, "该专业课程列表尚未缓存, 请先点击「获取该专业课程列表」")
    return data


@api_router.post("/plugins/{plugin_id}/major-courses/refresh")
def refresh_major_courses(plugin_id: str, body: dict):
    if plugin_id != _GRAB_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    # 新版配置界面发送 {"specialty": {...}}; 保留顶层旧格式兼容。
    sel = body.get("specialty", body) if isinstance(body, dict) else None
    if not isinstance(sel, dict):
        raise HTTPException(400, "专业参数格式错误")

    def _norm(v) -> str:
        # 兼容 {"v": 值, "t": 文本} 与纯字符串两种形态
        if isinstance(v, dict):
            v = v.get("v", "")
        return str(v or "").strip()

    keys = ("level", "grade", "dept", "major")
    sel = {k: _norm(sel.get(k)) for k in keys}
    missing = [k for k in keys if not sel[k]]
    if missing:
        raise HTTPException(400, "请先选择培养层次、年级、院(系)/部与专业")
    _start_grab_task(_TASK_COURSES, {"specialty": sel})
    return {"ok": True}


# ---------------- 全局配置 ----------------

@api_router.get("/global")
def get_global():
    return {"config": global_config.get_masked()}


@api_router.put("/global")
async def update_global(body: dict):
    patch = _config_patch(body)
    current = global_config.get()
    if isinstance(patch.get("accounts"), list):
        patch["accounts"] = _restore_masked_accounts(
            patch["accounts"], current.get("accounts", []))
    patch = _restore_masked_secrets(patch, current)
    global_config.update(patch)
    return {"ok": True}


_maintenance_started = False


def _startup_maintenance() -> None:
    global _maintenance_started
    if _maintenance_started:
        return
    _maintenance_started = True
    time.sleep(4)
    from .core.paths import start_log_maintenance

    start_log_maintenance()
    get_bus().publish({"type": "app_update", **app_updater.check()})
    settings = global_config.get().get("general", {})
    if settings.get("plugin_update_mode", "auto") != "auto":
        return
    try:
        updated = plugin_catalog.auto_update_installed()
        if updated:
            get_bus().publish({
                "type": "plugin_catalog_updated",
                "updated": updated,
            })
    except Exception as exc:
        get_bus().publish({
            "type": "plugin_catalog_error",
            "message": f"自动更新插件失败: {exc}",
        })


def start_startup_maintenance() -> None:
    threading.Thread(
        target=_startup_maintenance,
        daemon=True,
        name="startup-maintenance",
    ).start()


# ---------------- WebSocket ----------------

@api_router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _client_connected()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _on_event(event: dict):
        loop.call_soon_threadsafe(queue.put_nowait, event)

    unsubscribe = get_bus().subscribe(_on_event)

    async def sender():
        hello = json.dumps({"type": "hello", "history": get_bus().recent(None, 300)},
                           ensure_ascii=False)
        await ws.send_text(hello)
        while True:
            event = await queue.get()
            await ws.send_text(json.dumps(event, ensure_ascii=False))

    async def disconnect_watcher():
        # 前端不发送消息, receive 只会在客户端断开时返回/抛错
        while True:
            await ws.receive_text()

    send_task = asyncio.create_task(sender())
    watch_task = asyncio.create_task(disconnect_watcher())
    try:
        done, _pending = await asyncio.wait(
            {send_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        send_task.cancel()
        watch_task.cancel()
        unsubscribe()
        _client_disconnected()
