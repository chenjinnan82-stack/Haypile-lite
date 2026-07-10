from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = os.environ.get("HAYPILE_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "0.2.0"


def get_json(path: str) -> Any:
    with urllib.request.urlopen(BASE_URL + path, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def get_status_json(path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(BASE_URL + path, timeout=5) as response:
            return {"status_code": response.status, "body": json.loads(response.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        return {"status_code": exc.code, "body": json.loads(exc.read().decode("utf-8") or "{}")}


def list_bundles(
    *,
    status: str | None = "ready",
    asset_type: str | None = None,
    role: str | None = None,
    theme_id: str | None = None,
) -> Any:
    query = {
        "status": status,
        "type": asset_type,
        "role": role,
        "theme_id": theme_id,
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
        )
    if name == "haypile_copy_handoff":
        bundles = list_bundles(
            status=arguments.get("status", "ready"),
            asset_type=arguments.get("type"),
            role=arguments.get("role"),
            theme_id=arguments.get("theme_id"),
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
        "description": "List registered Haypile bundles. Defaults to status=ready and returns provenance fields id, sha256, source_key, and url.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "type": {"type": "string"},
                "role": {"type": "string"},
                "theme_id": {"type": "string"},
            },
        },
    },
    {
        "name": "haypile_copy_handoff",
            "description": "Return an asset-handoff JSON payload for bundles. Agents should use resolved_url and preserve id, role, status, sha256, source_key, url, and provenance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "type": {"type": "string"},
                "role": {"type": "string"},
                "theme_id": {"type": "string"},
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
    for line in sys.stdin:
        if not line.strip():
            continue
        response = handle(json.loads(line))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
