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
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {PROTOCOL_VERSION, "2024-11-05"}
SERVER_VERSION = "0.3.0-alpha.2"
MAX_LINE_BYTES = 1024 * 1024
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
            os.utime(self.path, None)
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
    batch_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Any:
    query = {
        "status": status,
        "type": asset_type,
        "role": role,
        "theme_id": theme_id,
        "audio_usage": audio_usage,
        "batch_id": batch_id,
        "limit": limit,
        "cursor": cursor,
    }
    encoded = urllib.parse.urlencode({key: value for key, value in query.items() if value})
    return get_json("/api/v1/bundles" + (f"?{encoded}" if encoded else ""))


def build_handoff(bundles: list[dict[str, Any]], *, batch_id: str | None = None) -> dict[str, Any]:
    handoff = {
        "handoff_version": "haypile.asset-handoff.v1",
        "source": "haypile",
        "base_url": BASE_URL,
        "assets": [_handoff_asset(bundle) for bundle in bundles],
    }
    if batch_id:
        handoff["batch_id"] = batch_id
    return handoff


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
    arguments = _validate_tool_arguments(name, arguments)
    if name == "haypile_health":
        return {"health": get_status_json("/healthz"), "ready": get_status_json("/readyz")}
    if name == "haypile_list_bundles":
        return list_bundles(
            status=arguments.get("status", "ready"),
            asset_type=arguments.get("type"),
            role=arguments.get("role"),
            theme_id=arguments.get("theme_id"),
            audio_usage=arguments.get("audio_usage"),
            batch_id=arguments.get("batch_id"),
            limit=arguments.get("limit", 100),
            cursor=arguments.get("cursor"),
        )
    if name == "haypile_copy_handoff":
        requested_batch_id = str(arguments.get("batch_id") or "").strip() or None
        resolved_batch_id = requested_batch_id
        if requested_batch_id == "latest":
            latest = get_json("/api/v1/batches/latest")
            resolved_batch_id = str(latest.get("id") or "").strip() if isinstance(latest, dict) else None
        bundles = list_bundles(
            status=arguments.get("status", "ready"),
            asset_type=arguments.get("type"),
            role=arguments.get("role"),
            theme_id=arguments.get("theme_id"),
            audio_usage=arguments.get("audio_usage"),
            batch_id=resolved_batch_id,
            limit=arguments.get("limit", 100),
            cursor=arguments.get("cursor"),
        )
        return build_handoff(bundles, batch_id=resolved_batch_id)
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


def _validate_tool_arguments(name: str, arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    allowed_by_tool = {
        "haypile_health": set(),
        "haypile_list_bundles": {
            "status", "type", "role", "theme_id", "audio_usage", "batch_id", "limit", "cursor"
        },
        "haypile_copy_handoff": {
            "status", "type", "role", "theme_id", "audio_usage", "batch_id", "limit", "cursor"
        },
        "haypile_get_bundle": {"bundle_id"},
        "haypile_list_themes": set(),
        "haypile_get_theme": {"theme_id"},
    }
    allowed = allowed_by_tool.get(name)
    if allowed is None:
        raise ValueError(f"Unknown tool: {name}")
    unknown = set(arguments) - allowed
    if unknown:
        raise ValueError(f"unsupported arguments: {', '.join(sorted(unknown))}")

    result = dict(arguments)
    enums = {
        "status": {"ready", "pending", "missing"},
        "type": {"image", "audio", "asset"},
        "role": {
            "main_background", "hero_image", "logo", "icon", "content_image",
            "texture", "audio", "unknown",
        },
        "audio_usage": {"music", "voice", "ambience", "sound_effect", "loop", "unknown"},
    }
    for key, allowed_values in enums.items():
        if key in result and result[key] is not None and result[key] not in allowed_values:
            raise ValueError(f"unsupported {key}")
    for key, limit in {"theme_id": 128, "batch_id": 64, "cursor": 512, "bundle_id": 128}.items():
        if key not in result or result[key] is None:
            continue
        if not isinstance(result[key], str) or len(result[key]) > limit:
            raise ValueError(f"invalid {key}")
    if "limit" in result and result["limit"] is not None:
        if isinstance(result["limit"], bool) or not isinstance(result["limit"], int):
            raise ValueError("invalid limit")
        if result["limit"] < 1 or result["limit"] > 100:
            raise ValueError("invalid limit")
    return result


TOOLS = [
    {
        "name": "haypile_health",
        "description": "Check the local Haypile backend and manifest readiness.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "haypile_list_bundles",
        "description": "List registered Haypile bundles. Audio bundles include duration_seconds, audio_metadata, audio_tags, and audio_usage when available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ready", "pending", "missing"]},
                "type": {"type": "string", "enum": ["image", "audio", "asset"]},
                "role": {"type": "string", "enum": ["main_background", "hero_image", "logo", "icon", "content_image", "texture", "audio", "unknown"]},
                "theme_id": {"type": "string", "maxLength": 128},
                "audio_usage": {"type": "string", "enum": ["music", "voice", "ambience", "sound_effect", "loop", "unknown"]},
                "batch_id": {"type": "string", "maxLength": 64, "description": "Use latest or a completed ingest batch id."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "string", "maxLength": 512},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "haypile_copy_handoff",
        "description": "Return asset-handoff JSON. Preserve identity/provenance fields and audio duration, metadata, and usage when present.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ready", "pending", "missing"]},
                "type": {"type": "string", "enum": ["image", "audio", "asset"]},
                "role": {"type": "string", "enum": ["main_background", "hero_image", "logo", "icon", "content_image", "texture", "audio", "unknown"]},
                "theme_id": {"type": "string", "maxLength": 128},
                "audio_usage": {"type": "string", "enum": ["music", "voice", "ambience", "sound_effect", "loop", "unknown"]},
                "batch_id": {"type": "string", "maxLength": 64, "description": "Use latest or a completed ingest batch id."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "string", "maxLength": 512},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "haypile_get_bundle",
        "description": "Get one Haypile bundle by id, including provenance fields id, sha256, source_key, and url.",
        "inputSchema": {
            "type": "object",
            "properties": {"bundle_id": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["bundle_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "haypile_list_themes",
        "description": "List Haypile theme ids.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "haypile_get_theme",
        "description": "Get one Haypile theme contract.",
        "inputSchema": {
            "type": "object",
            "properties": {"theme_id": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["theme_id"],
            "additionalProperties": False,
        },
    },
]


class McpProtocolSession:
    def __init__(self, *, initialized: bool = False, initialize_received: bool = False) -> None:
        self.initialize_received = initialize_received
        self.initialized = initialized
        self.protocol_version = ""


def _error(message_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle(
    message: dict[str, Any],
    session: McpProtocolSession | None = None,
) -> dict[str, Any] | None:
    if session is None:
        session = McpProtocolSession(initialized=True, initialize_received=True)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    method = message.get("method")
    message_id = message.get("id")
    if not isinstance(method, str) or not method:
        return _error(message_id, -32600, "Invalid Request")
    if message_id is None and method != "notifications/initialized":
        return None

    try:
        if method == "initialize":
            if session.initialize_received:
                return _error(message_id, -32600, "Initialize may only be sent once")
            params = message.get("params") or {}
            if not isinstance(params, dict):
                return _error(message_id, -32602, "Invalid params")
            requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
            session.protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            session.initialize_received = True
            result = {
                "protocolVersion": session.protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "haypile", "version": SERVER_VERSION},
            }
        elif method == "notifications/initialized":
            if not session.initialize_received:
                return None
            session.initialized = True
            return None
        elif method == "ping":
            result = {}
        elif not session.initialized:
            return _error(message_id, -32002, "Server not initialized")
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            if not isinstance(params, dict):
                return _error(message_id, -32602, "Invalid params")
            arguments = params["arguments"] if "arguments" in params else {}
            payload = call_tool(str(params.get("name") or ""), arguments)
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
        elif message_id is None:
            return None
        else:
            return _error(message_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": message_id, "result": result}
    except ValueError as exc:
        return _error(message_id, -32602, str(exc))
    except (urllib.error.URLError, TimeoutError) as exc:
        return _error(message_id, -32000, str(exc))
    except Exception:
        return _error(message_id, -32603, "Internal error")


def main() -> None:
    heartbeat: McpSessionHeartbeat | None = None
    session = McpProtocolSession()
    try:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        while True:
            raw_line = stream.readline(MAX_LINE_BYTES + 1)
            if not raw_line:
                break
            if isinstance(raw_line, str):
                encoded_length = len(raw_line.encode("utf-8", errors="replace"))
                line = raw_line
            else:
                encoded_length = len(raw_line)
                line = ""
            if encoded_length > MAX_LINE_BYTES:
                while raw_line and not raw_line.endswith(b"\n" if isinstance(raw_line, bytes) else "\n"):
                    raw_line = stream.readline(MAX_LINE_BYTES + 1)
                print(json.dumps(_error(None, -32700, "Parse error: message exceeds 1MB")), flush=True)
                continue
            if isinstance(raw_line, bytes):
                try:
                    line = raw_line.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    print(json.dumps(_error(None, -32700, "Parse error")), flush=True)
                    continue
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                print(json.dumps(_error(None, -32700, "Parse error")), flush=True)
                continue
            if not isinstance(message, dict):
                response = _error(None, -32600, "Invalid Request")
            else:
                response = handle(message, session)
            if session.initialized and heartbeat is None:
                heartbeat = McpSessionHeartbeat(get_settings().INDEX_DIR).start()
            if heartbeat is not None:
                heartbeat.touch()
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        if heartbeat is not None:
            heartbeat.stop()


if __name__ == "__main__":
    main()
