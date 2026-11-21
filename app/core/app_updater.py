"""Discover newer application releases without downloading or installing them."""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from .paths import UPDATE_DIR
from .release_utils import (
    ReleaseError,
    compare_versions,
    download_file,
    github_releases,
)
from ..version import __version__

APP_REPOSITORY = "bntool/CrowKit"


class AppUpdater:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {
            "status": "idle",
            "current_version": __version__,
            "latest_version": "",
            "available": False,
            "release_url": "",
            "download_url": "",
            "installer_name": "",
            "installer_size": 0,
            "expected_sha256": "",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "percent": 0,
            "speed_bps": 0,
            "eta_seconds": None,
            "message": "",
            "checked_at": "",
        }
        self._downloading = False

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def check(self) -> dict:
        with self._lock:
            self._state.update(status="checking", message="正在检查主程序更新")
        try:
            releases = github_releases(APP_REPOSITORY)
            latest = next(
                (
                    release for release in releases
                    if not release.prerelease and any(
                        re.fullmatch(
                            r"CrowKit-Setup-\d+\.\d+\.\d+\.exe",
                            asset.name,
                        )
                        for asset in release.assets
                    )
                ),
                None,
            )
            if latest is None:
                raise ReleaseError("没有找到可用的安装包")
            installer = next(
                asset for asset in latest.assets
                if re.fullmatch(
                    r"CrowKit-Setup-\d+\.\d+\.\d+\.exe",
                    asset.name,
                )
            )
            available = compare_versions(latest.version, __version__) > 0
            download_url = (
                f"https://github.com/{APP_REPOSITORY}/releases/download/"
                f"{urllib.parse.quote(latest.tag, safe='')}/"
                f"{urllib.parse.quote(installer.name, safe='')}"
            )
            with self._lock:
                self._state.update(
                    status="available" if available else "up_to_date",
                    latest_version=latest.version,
                    available=available,
                    release_url=latest.html_url,
                    download_url=download_url,
                    installer_name=installer.name,
                    installer_size=installer.size,
                    expected_sha256=installer.digest.removeprefix("sha256:"),
                    downloaded_bytes=0,
                    total_bytes=installer.size,
                    percent=0,
                    speed_bps=0,
                    eta_seconds=None,
                    message=(
                        f"发现新版本 {latest.version}，请下载安装包手动更新"
                        if available else "当前已是最新版本"
                    ),
                    checked_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                )
        except ReleaseError as exc:
            with self._lock:
                self._state.update(
                    status="error",
                    available=False,
                    download_url="",
                    message=str(exc),
                    checked_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                )
        return self.snapshot()

    def download_for_manual_install(
        self,
        on_progress: Callable[[dict], None],
        on_downloaded: Callable[[Path], None],
    ) -> None:
        with self._lock:
            state = dict(self._state)
            if not state.get("available") or not state.get("download_url"):
                raise ReleaseError("当前没有可用更新")
            if self._downloading and state.get("status") != "downloading":
                raise ReleaseError("安装包正在下载")
            if not self._downloading:
                self._downloading = True
                self._state.update(
                    status="downloading",
                    message="正在下载新版本安装包",
                    downloaded_bytes=0,
                    total_bytes=int(state.get("installer_size") or 0),
                    percent=0,
                    speed_bps=0,
                    eta_seconds=None,
                )
        on_progress(self.snapshot())
        destination = UPDATE_DIR / "manual" / str(state["installer_name"])
        started = time.monotonic()
        last_emit = 0.0

        def progress(downloaded: int, total: int) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if now - last_emit < 0.2 and (not total or downloaded < total):
                return
            last_emit = now
            elapsed = max(0.001, now - started)
            speed = downloaded / elapsed
            display_total = total or int(state.get("installer_size") or 0)
            eta = (
                round(max(0, display_total - downloaded) / speed)
                if display_total > 0 and speed > 0 else None
            )
            percent = (
                min(100, round(downloaded / display_total * 100, 1))
                if display_total else 0
            )
            with self._lock:
                self._state.update(
                    downloaded_bytes=downloaded,
                    total_bytes=display_total,
                    percent=percent,
                    speed_bps=round(speed),
                    eta_seconds=eta,
                )
            on_progress(self.snapshot())

        try:
            installed = download_file(
                str(state["download_url"]),
                destination,
                expected_sha256=str(state.get("expected_sha256") or ""),
                max_bytes=250 * 1024 * 1024,
                timeout=60,
                progress_callback=progress,
                accept="application/octet-stream",
            )
        except Exception as exc:
            with self._lock:
                self._downloading = False
                self._state.update(
                    status="error",
                    message=f"下载安装包失败: {exc}",
                )
            on_progress(self.snapshot())
            raise
        with self._lock:
            self._downloading = False
            self._state.update(
                status="downloaded",
                percent=100,
                downloaded_bytes=int(self._state.get("total_bytes") or 0),
                speed_bps=0,
                eta_seconds=0,
                message="下载完成，正在启动安装程序",
            )
        on_progress(self.snapshot())
        on_downloaded(installed)

    def prepare_manual_download(self) -> dict:
        with self._lock:
            state = dict(self._state)
            if not state.get("available") or not state.get("download_url"):
                raise ReleaseError("当前没有可用更新")
            if self._downloading:
                raise ReleaseError("安装包正在下载")
            self._downloading = True
            self._state.update(
                status="downloading",
                message="正在下载新版本安装包",
                downloaded_bytes=0,
                total_bytes=int(state.get("installer_size") or 0),
                percent=0,
                speed_bps=0,
                eta_seconds=None,
            )
            return dict(self._state)

    def mark_installer_launched(self) -> dict:
        with self._lock:
            self._state.update(
                status="installing",
                message="安装程序已启动，CrowKit 即将退出",
            )
        return self.snapshot()

    def fail_installer_launch(self, message: str) -> dict:
        with self._lock:
            self._state.update(
                status="error",
                message=f"无法启动安装程序: {message}",
            )
        return self.snapshot()


app_updater = AppUpdater()
