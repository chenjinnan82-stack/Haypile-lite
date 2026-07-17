from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings

BASE_URL = os.environ.get("HAYPILE_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "0.2.0"
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SESSION_HEARTBEAT_SECONDS = 5.0
SESSION_ONLINE_SECONDS = 12.0
SESSION_CLEANUP_SECONDS = 60.0


class McpSessionHeartbeat:
    def __init__(self, index_dir: Path) -> None:
        self.directory = Path(index_dir) / "mcp_sessions"
        self.path = self.directory / f"{os.getpid()}.json"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "McpSessionHeartbeat":
        if self.directory.is_symlink():
            raise OSError("MCP session directory cannot be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise OSError("MCP session directory cannot be a symlink")
        if os.name != "nt":
            self.directory.chmod(0o700)
        payload = {
            "pid": os.getpid(),
            "server_version": SERVER_VERSION,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(self.path), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        if os.name != "nt":
            self.path.chmod(0o600)
        self._thread = threading.Thread(target=self._run, name="haypile-mcp-heartbeat", daemon=True)
        self._thread.start()
        return self

    def touch(self) -> None:
        try:
            os.utime(self.path, None, follow_symlinks=False)
        except OSError:
            pass

    def _run(self) -> None:
        while not self._stop.wait(SESSION_HEARTBEAT_SECONDS):
            self.touch()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        try:
            self.path.unlink()
        except OSError:
            pass


def active_mcp_sessions(
    index_dir: Path,
    *,
    now: float | None = None,
    online_seconds: float = SESSION_ONLINE_SECONDS,
    cleanup_seconds: float = SESSION_CLEANUP_SECONDS,
) -> list[dict[str, Any]]:
    directory = Path(index_dir) / "mcp_sessions"
    if directory.is_symlink() or not directory.is_dir():
        return []
    current = time.time() if now is None else float(now)
    sessions: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            age = max(0.0, current - path.stat().st_mtime)
            if age > cleanup_seconds:
                path.unlink()
                continue
            if age > online_seconds:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload["heartbeat_age_seconds"] = round(age, 3)
        sessions.append(payload)
    return sorted(sessions, key=lambda item: int(item.get("pid") or 0))


def get_json(path: str) -> Any:
    with LOCAL_OPENER.open(BASE_URL + path, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def get_status_json(path: str) -> dict[str, Any]:
    try:
        with LOCAL_OPENER.open(BASE_URL + path, timeout=5) as response:
            return {"status_code": response.status, "body": json.loads(response.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        return {"status_code": exc.code, "body": json.loads(exc.read().decode("utf-8") or "{}")}


def list_bundles(
    *,
    status: str | None = "ready",
    asset_type: str | None = None,
    role: str | None = None,
    theme_id: str | None = None,
    audio_usage: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Any:
    query = {
        "status": status,
        "type": asset_type,
        "role": role,
        "theme_id": theme_id,
        "audio_usage": audio_usage,
        "limit": limit,
        "cursor": cursor,
    }
    encoded = urllib.parse.urlencode({key: value for key, value in query.items() if value})
    return get_json("/api/v1/bundles" + (f"?{encoded}" if encoded else ""))


def build_handoff(bundles: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "handoff_version": "haypile.asset-handoff.v1",
        "source": "haypile",
        "base_url": BASE_URL,
        "assets": [_handoff_asset(bundle) for bundle in bundles],
    }


def _handoff_asset(bundle: dict[str, Any]) -> dict[str, Any]:
    resolved_url = BASE_URL + bundle["url"]
    return {
        "id": bundle["id"],
        "theme_id": bundle["theme_id"],
        "type": bundle["type"],
        "role": bundle["role"],
        "status": bundle["status"],
        "sha256": bundle["sha256"],
        "source_key": bundle["source_key"],
        "url": bundle["url"],
        "access": bundle["access"],
        "resolved_url": resolved_url,
        "ai_suggestions": bundle.get("ai_suggestions", {}),
        "duration_seconds": bundle.get("duration_seconds"),
        "audio_metadata": bundle.get("audio_metadata", {}),
        "audio_tags": bundle.get("audio_tags", {}),
        "audio_usage": bundle.get("audio_usage", "unknown"),
        "provenance": {
            "source": "haypile",
            "id": bundle["id"],
            "sha256": bundle["sha256"],
            "source_key": bundle["source_key"],
            "url": bundle["url"],
            "resolved_url": resolved_url,
            "access": bundle["access"],
        },
    }


def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    if name == "haypile_health":
        return {"health": get_status_json("/healthz"), "ready": get_status_json("/readyz")}
    if name == "haypile_list_bundles":
        return list_bundles(
            status=arguments.get("status", "ready"),
            asset_type=arguments.get("type"),
            role=arguments.get("role"),
            theme_id=arguments.get("theme_id"),
            audio_usage=arguments.get("audio_usage"),
            limit=arguments.get("limit"),
            cursor=arguments.get("cursor"),
        )
    if name == "haypile_copy_handoff":
        bundles = list_bundles(
            status=arguments.get("status", "ready"),
            asset_type=arguments.get("type"),
            role=arguments.get("role"),
            theme_id=arguments.get("theme_id"),
            audio_usage=arguments.get("audio_usage"),
            limit=arguments.get("limit"),
            cursor=arguments.get("cursor"),
        )
        return build_handoff(bundles)
    if name == "haypile_get_bundle":
        bundle_id = str(arguments.get("bundle_id") or "").strip()
        if not bundle_id:
            raise ValueError("bundle_id is required")
        return get_json(f"/api/v1/bundles/{urllib.parse.quote(bundle_id)}")
    if name == "haypile_list_themes":
        return get_json("/api/v1/vault")
    if name == "haypile_get_theme":
        theme_id = str(arguments.get("theme_id") or "").strip()
        if not theme_id:
            raise ValueError("theme_id is required")
        return get_json(f"/api/v1/vault/{urllib.parse.quote(theme_id)}")
    raise ValueError(f"Unknown tool: {name}")


TOOLS = [
    {
        "name": "haypile_health",
        "description": "Check the local Haypile backend and manifest readiness.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "haypile_list_bundles",
        "description": "List registered Haypile bundles. Audio bundles include duration_seconds, audio_metadata, audio_tags, and audio_usage when available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "type": {"type": "string"},
                "role": {"type": "string"},
                "theme_id": {"type": "string"},
                "audio_usage": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "string"},
            },
        },
    },
    {
        "name": "haypile_copy_handoff",
        "description": "Return asset-handoff JSON. Preserve identity/provenance fields and audio duration, metadata, and usage when present.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "type": {"type": "string"},
                "role": {"type": "string"},
                "theme_id": {"type": "string"},
                "audio_usage": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "string"},
            },
        },
    },
    {
        "name": "haypile_get_bundle",
        "description": "Get one Haypile bundle by id, including provenance fields id, sha256, source_key, and url.",
        "inputSchema": {
            "type": "object",
            "properties": {"bundle_id": {"type": "string"}},
            "required": ["bundle_id"],
        },
    },
    {
        "name": "haypile_list_themes",
        "description": "List Haypile theme ids.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "haypile_get_theme",
        "description": "Get one Haypile theme contract.",
        "inputSchema": {
            "type": "object",
            "properties": {"theme_id": {"type": "string"}},
            "required": ["theme_id"],
        },
    },
]


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "haypile", "version": SERVER_VERSION},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            payload = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
        elif method == "ping":
            result = {}
        elif method and message_id is None:
            return None
        else:
            raise ValueError(f"Unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": message_id, "result": result}
    except (ValueError, urllib.error.URLError, TimeoutError) as exc:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32000, "message": str(exc)}}


def main() -> None:
    heartbeat: McpSessionHeartbeat | None = None
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            if message.get("method") == "initialize" and heartbeat is None:
                heartbeat = McpSessionHeartbeat(get_settings().INDEX_DIR).start()
            elif heartbeat is not None:
                heartbeat.touch()
            response = handle(message)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        if heartbeat is not None:
            heartbeat.stop()


if __name__ == "__main__":
    main()
