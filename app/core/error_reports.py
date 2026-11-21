"""Create sanitized local error bundles and reveal them in Explorer."""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from .paths import LOG_DIR
from ..version import __version__

_REPORTS_DIR = LOG_DIR / "reports"
_MAX_REPORTS = 10
_HOST_LOG_PATH = LOG_DIR / "crowkit.log"
try:
    _HOST_LOG_OFFSET = _HOST_LOG_PATH.stat().st_size
except OSError:
    _HOST_LOG_OFFSET = 0
_SENSITIVE_TEXT_RE = re.compile(
    r"((?:password|passwd|pwd|campus_password|api[_-]?key|apikey|token|secret|cookie)"
    r"\s*[:=]\s*)[^\s,;\"']+",
    re.IGNORECASE,
)
_SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|pwd|campus_password|api[_-]?key|apikey|token|secret|cookie)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|apikey|token|secret|password)=)[^&#\s]+",
    re.IGNORECASE,
)


def redact_text(value: object) -> str:
    text = str(value or "")
    text = _SENSITIVE_TEXT_RE.sub(r"\1******", text)
    return _SENSITIVE_QUERY_RE.sub(r"\1******", text)


def sanitize_object(value: object, *, depth: int = 0) -> object:
    """Mask sensitive keys before plugin configuration is written or uploaded."""
    if depth > 12:
        return "<depth-limit>"
    if isinstance(value, dict):
        return {
            str(key): (
                "******"
                if _SECRET_KEY_RE.search(str(key)) and item not in ("", None)
                else sanitize_object(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_object(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def compact_object(value: object, max_chars: int = 24_000) -> object:
    sanitized = sanitize_object(value)
    try:
        text = json.dumps(sanitized, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return redact_text(value)
    if len(text) <= max_chars:
        return sanitized
    return {
        "truncated": True,
        "preview": text[:max_chars],
    }


def _current_host_log() -> str:
    try:
        with open(_HOST_LOG_PATH, "rb") as handle:
            handle.seek(_HOST_LOG_OFFSET)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return redact_text(text)


def _validated_run_log(log_path: Path | None) -> Path | None:
    if log_path is None:
        return None
    try:
        resolved = log_path.resolve(strict=True)
        runs_root = (LOG_DIR / "runs").resolve()
    except OSError:
        return None
    if runs_root not in resolved.parents or resolved.suffix != ".log":
        return None
    return resolved


def extract_python_traceback(log_text: str) -> dict | None:
    """Convert the final Python traceback in a run log into a Sentry exception."""
    clean_lines = [
        re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", "", line)
        for line in str(log_text or "").splitlines()
    ]
    try:
        start = max(
            index for index, line in enumerate(clean_lines)
            if line.startswith("Traceback (most recent call last):")
        )
    except ValueError:
        return None
    traceback_lines = clean_lines[start:]
    frames: list[dict] = []
    index = 1
    while index < len(traceback_lines):
        line = traceback_lines[index]
        match = re.match(
            r'\s*File "([^"]+)", line (\d+), in (.+)$', line)
        if not match:
            index += 1
            continue
        filename, lineno, function = match.groups()
        filename = re.sub(
            r"(?i)^[A-Z]:\\Users\\[^\\]+", r"C:\\Users\\<user>", filename)
        context_line = ""
        if index + 1 < len(traceback_lines):
            candidate = traceback_lines[index + 1]
            if candidate.startswith("    "):
                context_line = candidate.strip()
                index += 1
        frames.append({
            "filename": filename,
            "lineno": int(lineno),
            "function": function,
            "context_line": context_line,
        })
        index += 1
    exception_line = next(
        (line for line in reversed(traceback_lines)
         if re.match(r"^[A-Za-z_][\w.]*(?:: .*)?$", line.strip())
         and not line.lstrip().startswith(("File ", "Traceback "))),
        "",
    ).strip()
    exc_type, _, exc_value = exception_line.partition(":")
    if not exc_type:
        return None
    return {
        "type": exc_type,
        "value": exc_value.strip(),
        "stacktrace": {"frames": frames},
    }


def _prune_reports() -> None:
    reports = sorted(
        _REPORTS_DIR.glob("CrowKit-error-*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in reports[_MAX_REPORTS:]:
        try:
            stale.unlink()
        except OSError:
            pass


def create_error_bundle(
    message: str = "",
    context: str = "",
    plugin: str = "",
    run_id: str = "",
    action_description: str = "",
    log_path: Path | None = None,
    plugin_info: dict | None = None,
    plugin_config: dict | None = None,
    edge_info: dict | None = None,
) -> Path:
    """Zip the current run log and structured diagnostics into a local report."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = _REPORTS_DIR / (
        f"CrowKit-error-{stamp}-{uuid.uuid4().hex[:6]}.zip")
    temporary = destination.with_suffix(".zip.part")
    selected_log = _validated_run_log(log_path)
    if selected_log is not None:
        try:
            log_text = redact_text(
                selected_log.read_text(encoding="utf-8", errors="replace"))
            log_name = selected_log.name
        except OSError:
            log_text = ""
            log_name = ""
    elif not plugin:
        log_text = _current_host_log()
        log_name = "crowkit.log"
    else:
        log_text = ""
        log_name = ""
    traceback = extract_python_traceback(log_text)
    diagnostics = {
        "app_version": __version__,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plugin": redact_text(plugin),
        "plugin_info": compact_object(plugin_info or {}),
        "plugin_config": compact_object(plugin_config or {}),
        "run_id": redact_text(run_id),
        "action_description": redact_text(action_description)[:4000],
        "edge": compact_object(edge_info or {}),
        "python_traceback": traceback,
        "message": redact_text(message),
        "context": redact_text(context),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "diagnostics.json",
                json.dumps(diagnostics, ensure_ascii=False, indent=2),
            )
            if log_text:
                archive.writestr(
                    str(Path("logs") / log_name),
                    log_text,
                )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    _prune_reports()
    return destination


def read_error_bundle(path: Path) -> tuple[dict, str]:
    """Read sanitized diagnostics and concatenated logs from a report ZIP."""
    with zipfile.ZipFile(path) as archive:
        try:
            diagnostics = json.loads(
                archive.read("diagnostics.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
            diagnostics = {}
        log_chunks: list[str] = []
        for name in archive.namelist():
            if not name.startswith("logs/") or not name.endswith(".log"):
                continue
            try:
                log_chunks.append(archive.read(name).decode("utf-8"))
            except (KeyError, UnicodeDecodeError):
                continue
    return diagnostics, redact_text("\n\n".join(log_chunks))


def reveal_path(path: Path) -> None:
    """Open Explorer with a concrete file selected so its window is visible."""
    if os.name != "nt":
        raise RuntimeError("当前平台不支持资源管理器定位")
    target = path.resolve()
    if not target.exists():
        raise FileNotFoundError(target)
    if target.is_file():
        focus = target
    else:
        candidates = [
            item for item in target.rglob("*.log")
            if item.is_file() and _REPORTS_DIR not in item.parents
        ]
        focus = max(
            candidates, key=lambda item: item.stat().st_mtime,
            default=target / "reports",
        )
        if not focus.exists():
            focus = target
    subprocess.Popen(
        ["explorer.exe", f"/select,{focus}"],
        cwd=str(focus.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
