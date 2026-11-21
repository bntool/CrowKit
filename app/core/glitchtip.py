"""Minimal privacy-conscious GlitchTip reporting through the project DSN."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from configparser import ConfigParser
from datetime import datetime, timezone
from pathlib import Path

from .error_reports import redact_text
from .paths import ROOT
from ..version import __version__

_LOCK = threading.Lock()
_RECENT_SENDS: deque[float] = deque()
_MAX_EVENTS_PER_HOUR = 60


def _dsn() -> str:
    direct = str(os.environ.get("CROWKIT_GLITCHTIP_DSN") or "").strip()
    if direct:
        return direct
    dev_config = ROOT / ".sentryclirc"
    if dev_config.is_file():
        parser = ConfigParser()
        try:
            parser.read(dev_config, encoding="utf-8")
            configured = parser.get("auth", "dsn", fallback="").strip()
        except (OSError, ValueError):
            configured = ""
        if configured:
            return configured
    packaged_config = ROOT / "app" / "glitchtip.json"
    try:
        payload = json.loads(packaged_config.read_text(encoding="utf-8-sig"))
        return str(payload.get("dsn") or "").strip()
    except (OSError, ValueError, json.JSONDecodeError):
        return ""


def _store_endpoint(dsn: str) -> str:
    parsed = urllib.parse.urlsplit(dsn)
    key = parsed.username or ""
    project_id = parsed.path.strip("/").split("/")[-1]
    if parsed.scheme != "https" or not parsed.hostname or not key or not project_id:
        raise ValueError("GlitchTip DSN 格式不完整")
    authority = parsed.hostname
    if parsed.port:
        authority += f":{parsed.port}"
    return (
        f"https://{authority}/api/{urllib.parse.quote(project_id, safe='')}/"
        f"store/?sentry_key={urllib.parse.quote(key, safe='')}"
    )


def _normalized_fingerprint(message: str) -> str:
    text = redact_text(message).lower()
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    return text[:240] or "unknown-error"


def _allowed_by_rate_limit() -> bool:
    now = time.monotonic()
    with _LOCK:
        while _RECENT_SENDS and now - _RECENT_SENDS[0] > 3600:
            _RECENT_SENDS.popleft()
        if len(_RECENT_SENDS) >= _MAX_EVENTS_PER_HOUR:
            return False
        _RECENT_SENDS.append(now)
        return True


def send_event(
    message: str,
    *,
    context: str = "",
    plugin: str = "",
    source: str = "ui",
    action_description: str = "",
    plugin_info: dict | None = None,
    plugin_config: dict | None = None,
    edge_info: dict | None = None,
    python_traceback: dict | None = None,
    log_text: str = "",
) -> bool:
    """Send one error event synchronously. Return False when disabled or rejected."""
    dsn = _dsn()
    if not dsn:
        return False
    safe_message = redact_text(message)[:4000]
    safe_context = redact_text(context)[:4000]
    safe_plugin = redact_text(plugin)[:120]
    if not _allowed_by_rate_limit():
        return False
    event = {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": "python",
        "level": "error",
        "logger": "crowkit",
        "release": f"crowkit@{__version__}",
        "environment": (
            "production"
            if Path(sys.executable).parent.name.casefold() == "runtime"
            else "development"
        ),
        "message": safe_message,
        "fingerprint": ["crowkit", source, safe_plugin, _normalized_fingerprint(safe_message)],
        "tags": {
            "application": "crowkit",
            "source": source[:64] or "unknown",
            "plugin": safe_plugin or "host",
        },
        "extra": {
            "context": safe_context,
            "action_description": redact_text(action_description)[:4000],
            "plugin_info": plugin_info or {},
            "plugin_config": plugin_config or {},
            "edge": edge_info or {},
            "python_traceback": python_traceback or {},
            "run_log": redact_text(log_text)[:100_000],
        },
        "sdk": {"name": "crowkit", "version": __version__},
    }
    if python_traceback:
        event["exception"] = {"values": [python_traceback]}
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _store_endpoint(dsn),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"crowkit/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False
