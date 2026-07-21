# Haypile HTTP Agent Contract

This contract is the first supported way for agents to use Haypile.

Haypile is a local asset registry. Agents read registered bundles through HTTP.
Agents do not scan, open, or mutate `storage/assets` directly.

## Scope

Supported in this phase:

- Health and readiness checks.
- Read-only bundle listing and lookup.
- Read-only theme contract lookup.
- Static access to manifest-registered assets.
- A thin stdio MCP adapter over the same HTTP surface.

Not supported in this phase:

- Asset deletion, moving, renaming, or reclassification.
- Direct writes to Haypile storage.
- Agent-side filesystem scanning.
- MCP tools that mutate assets or bypass HTTP.

## Base URL

Default local backend:

```text
http://127.0.0.1:8010
```

Launchers should expose this service as `haypile`. The agent API name is
Haypile.

## Readiness

Agents should check both endpoints before reading assets:

```text
GET /healthz
GET /readyz
```

Expected success body:

```json
{"status":"ok"}
```

`/healthz` means the backend process is alive.
`/readyz` means the asset manifest exists. It can return `503` before the first
manifest has been generated.

## Bundle API

List bundles:

```text
GET /api/v1/bundles
```

Filter bundles:

```text
GET /api/v1/bundles?status=ready
GET /api/v1/bundles?status=ready&type=image
GET /api/v1/bundles?status=ready&type=image&role=hero_image
GET /api/v1/bundles?status=pending&type=audio
GET /api/v1/bundles?status=ready&type=audio&audio_usage=voice
GET /api/v1/bundles?theme_id=generic
GET /api/v1/bundles?status=ready&batch_id=latest
```

Resolve the latest completed, non-empty ingest batch:

```text
GET /api/v1/batches/latest
```

`batch_id` accepts `latest` or a concrete batch UUID. Omitting it preserves the
original all-assets behavior. A drop containing only invalid files never
becomes latest; an interrupted batch is never exposed to agents.

Page a large result without changing the response shape:

```text
GET /api/v1/bundles?status=ready&limit=50
GET /api/v1/bundles?status=ready&limit=50&cursor=generic/images/last-item.png
```

Results are sorted by `source_key`. Use the final returned item's `source_key`
as the next `cursor`; omit `cursor` for the first page. `limit` is optional and
is capped at 100. Existing requests without `limit` still return every match.

Get one bundle:

```text
GET /api/v1/bundles/{bundle_id}
```

Missing bundle response:

```text
404 Bundle not found.
```

Bundle payload:

```json
{
  "id": "generic_img_hero_image_abcd1234",
  "theme_id": "generic",
  "type": "image",
  "role": "hero_image",
  "status": "ready",
  "sha256": "abcd...",
  "url": "/static/generic/images/generic_img_hero_image_abcd1234.png",
  "access": "manifest_static",
  "source_key": "generic/images/generic_img_hero_image_abcd1234.png",
  "duration_seconds": null,
  "audio_metadata": {},
  "audio_tags": {},
  "audio_usage": "unknown"
}
```

Fields:

- `id`: stable bundle id derived from the registered asset filename.
- `theme_id`: theme bucket.
- `type`: currently `image` or `audio`.
- `role`: `main_background`, `hero_image`, `logo`, `icon`, `content_image`,
  `texture`, `audio`, or `unknown`.
- `status`: `ready`, `pending`, or `missing`.
- `sha256`: content hash when available.
- `url`: static URL path. Resolve it against the same backend base URL.
- `access`: currently `manifest_static`.
- `source_key`: manifest-relative key, not an absolute local path.
- `duration_seconds`: audio length when the bundle is audio; otherwise `null`.
- `audio_metadata`: available audio facts such as `sample_rate_hz`, `channels`, and `bitrate_bps`.
- `audio_tags`: available file tags: `title`, `artist`, and `album`.
- `audio_usage`: `music`, `voice`, `ambience`, `sound_effect`, `loop`, or `unknown`.

Status meanings:

- `ready`: registered and classified for use.
- `pending`: registered but still `unknown`; audio also stays pending until its `audio_usage` is confirmed.
- `missing`: referenced by a theme contract but absent from the manifest.

## Theme Vault API

List theme ids:

```text
GET /api/v1/vault
```

Get one theme contract:

```text
GET /api/v1/vault/{theme_id}
```

Theme contract payload:

```json
{
  "theme_name": "generic",
  "css_variables": {},
  "tailwind_extend": {},
  "fonts": [],
  "physical_assets": {
    "hero_image": {
      "url": "/static/generic/images/generic_img_hero_image_abcd1234.png",
      "type": "image",
      "resolution": "1024x768",
      "aspect_ratio": "1.3333",
      "css_advice": "object-cover",
      "placement_intent": "hero"
    }
  },
  "ui_dev_instruction": "Use these theme assets for consistent visual rendering. Do not fabricate image URLs."
}
```

Use the vault when the agent needs a full theme package. Use bundles when the
agent only needs individual assets.

## Static Asset Access

Bundle `url` values are relative paths. Agents must resolve them against the
same backend base URL:

```text
http://127.0.0.1:8010/static/generic/images/example.png
```

`/static` is manifest-gated. A file is served only when it is present in
`storage/index/assets_manifest.json`.

## MCP Adapter

`mcp_server.py` exposes the read-only HTTP contract as stdio MCP tools:

- `haypile_health`
- `haypile_list_bundles`
- `haypile_get_bundle`
- `haypile_copy_handoff`
- `haypile_list_themes`
- `haypile_get_theme`

Set `HAYPILE_BASE_URL` when the backend is not using
`http://127.0.0.1:8010`.

`haypile_copy_handoff` returns:

```json
{
  "source": "haypile",
  "batch_id": "resolved-batch-uuid",
  "base_url": "http://127.0.0.1:8010",
  "assets": [
    {
      "id": "generic_img_hero_image_abcd1234",
      "theme_id": "generic",
      "type": "image",
      "role": "hero_image",
      "status": "ready",
      "sha256": "abcd...",
      "source_key": "generic/images/generic_img_hero_image_abcd1234.png",
      "url": "/static/generic/images/generic_img_hero_image_abcd1234.png",
      "access": "manifest_static",
      "resolved_url": "http://127.0.0.1:8010/static/generic/images/generic_img_hero_image_abcd1234.png"
    }
  ]
}
```

`haypile_list_bundles` and `haypile_copy_handoff` accept the same optional
`batch_id` value as HTTP. Agent recipes should default to `"latest"`.

## Agent Rules

Agents should:

- Prefer `status=ready` assets for generation, UI assembly, or project output.
- Ask the user before using `pending` assets.
- Keep `bundle_id`, `role`, `status`, `sha256`, and `source_key` in any generated handoff or trace.
- Treat `url` as the only supported asset access path.
- Retry `/readyz` or `/api/v1/bundles` if the user just dropped new assets.

Agents must not:

- Read `storage/assets` directly.
- Infer local absolute paths from `source_key`.
- Modify files under `storage/`.
- Fabricate `/static` URLs.
- Treat `missing` bundles as usable assets.

## Minimal Agent Flow

```text
1. GET /healthz
2. GET /readyz
3. GET /api/v1/bundles?status=ready&type=image
4. Pick a bundle by role/theme/sha.
5. Fetch or reference base_url + bundle.url.
6. Record bundle.id and bundle.sha256 in the agent result.
```

## Minimal Python Example

```python
from __future__ import annotations

import json
import urllib.parse
import urllib.request

BASE_URL = "http://127.0.0.1:8010"


def get_json(path: str):
    with urllib.request.urlopen(BASE_URL + path, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def ready_images(role: str | None = None):
    query = {"status": "ready", "type": "image"}
    if role:
        query["role"] = role
    path = "/api/v1/bundles?" + urllib.parse.urlencode(query)
    return get_json(path)


get_json("/healthz")
get_json("/readyz")
images = ready_images(role="hero_image")
for bundle in images:
    asset_url = BASE_URL + bundle["url"]
    print(bundle["id"], bundle["sha256"], asset_url)
```

## Acceptance Check

An agent integration satisfies this contract when it can:

- Read health and readiness.
- List ready bundles over HTTP.
- Resolve a returned static URL.
- Preserve bundle provenance in its output.
- Avoid direct filesystem reads from Haypile storage.
