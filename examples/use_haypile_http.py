from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from typing import Any

from app.services.handoff import build_asset_handoff, build_handoff_asset

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


def build_handoff(
    bundles: list[dict[str, Any]],
    *,
    batch_id: str | None = None,
    manifest_generation: str = "",
) -> dict[str, Any]:
    return build_asset_handoff(
        bundles,
        base_url=BASE_URL,
        batch_id=batch_id,
        manifest_generation=manifest_generation,
    )


def _handoff_asset(bundle: dict[str, Any]) -> dict[str, Any]:
    return build_handoff_asset(bundle, base_url=BASE_URL)


def main() -> int:
    try:
        get_json("/healthz")
        readiness = get_json("/readyz")
        batch_id = latest_batch_id()
        handoff = build_handoff(
            ready_images(role=os.environ.get("HAYPILE_ROLE"), batch_id=batch_id) if batch_id else [],
            batch_id=batch_id or None,
            manifest_generation=(
                str(readiness.get("manifest_generation") or "")
                if isinstance(readiness, dict)
                else ""
            ),
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
