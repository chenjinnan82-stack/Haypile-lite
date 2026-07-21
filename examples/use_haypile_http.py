from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from typing import Any

BASE_URL = os.environ.get("HAYPILE_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(path: str) -> Any:
    with LOCAL_OPENER.open(BASE_URL + path, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_batch_id() -> str:
    try:
        payload = get_json("/api/v1/batches/latest")
    except HTTPError as exc:
        if exc.code == 404:
            return ""
        raise
    return str(payload.get("id") or "") if isinstance(payload, dict) else ""


def ready_images(role: str | None = None, *, batch_id: str = "latest") -> list[dict[str, Any]]:
    query = {"status": "ready", "type": "image", "batch_id": batch_id}
    if role:
        query["role"] = role
    return get_json("/api/v1/bundles?" + urllib.parse.urlencode(query))


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


def main() -> int:
    try:
        get_json("/healthz")
        get_json("/readyz")
        batch_id = latest_batch_id()
        handoff = build_handoff(
            ready_images(role=os.environ.get("HAYPILE_ROLE"), batch_id=batch_id) if batch_id else [],
            batch_id=batch_id or None,
        )
    except HTTPError as exc:
        print(f"Haypile request failed: HTTP {exc.code} {exc.reason}. Check readiness and try again.", file=sys.stderr)
        return 2
    except (OSError, URLError) as exc:
        print(f"Cannot reach Haypile at {BASE_URL}. Start Haypile or set HAYPILE_BASE_URL. ({exc})", file=sys.stderr)
        return 2
    print(json.dumps(handoff, ensure_ascii=False, indent=2))
    if not handoff["assets"]:
        print("Haypile is reachable, but the latest batch has no ready images. Review the batch first.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
