# Haypile Agent Recipes

Haypile is the local source of registered assets. Agents should use HTTP or the
MCP adapter, then keep bundle provenance in their output.

## MCP Setup

Start Haypile first:

```bash
python3 app_gui.py
```

For a manual backend smoke test:

```bash
HAYPILE_BACKEND_HOST_ALLOW_START=1 python3 backend_host.py
```

Register the stdio MCP adapter in the agent host:

```json
{
  "mcpServers": {
    "haypile": {
      "command": "python3",
      "args": ["/absolute/path/to/haypile/mcp_server.py"],
      "env": {
        "HAYPILE_BASE_URL": "http://127.0.0.1:8010"
      }
    }
  }
}
```

Tools:

- `haypile_health`
- `haypile_list_bundles`
- `haypile_get_bundle`
- `haypile_copy_handoff`
- `haypile_list_themes`
- `haypile_get_theme`

## Recipe 1: UI Builder Agent

Goal: build a page or app using local approved assets.

Flow:

1. Call `haypile_health`.
2. Call `haypile_list_bundles` with
   `{"status":"ready","type":"image","batch_id":"latest"}`.
3. Prefer `hero_image`, then `main_background`, then `texture`.
4. Use `HAYPILE_BASE_URL + bundle.url` as the image source.
5. Record `bundle.id`, `bundle.role`, `bundle.status`, `bundle.sha256`, and `source_key` in the handoff.

Rule: if only `pending` assets exist, ask before using them.

## Recipe 2: Project Writer Agent

Goal: copy selected assets into a generated project with traceability.

Flow:

1. Call `haypile_list_bundles` with `batch_id: "latest"` plus the needed
   `type` and `role`.
2. Download through the returned `url`, not from `storage/assets`.
3. Write a small `asset-handoff.json` in the target project.

Handoff shape:

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

## Recipe 3: Review Agent

Goal: verify that generated work used Haypile assets correctly.

Flow:

1. Read the generated `asset-handoff.json`.
2. For each entry, call `haypile_get_bundle`.
3. Compare `id`, `sha256`, and `url`.
4. Flag assets with missing handoff, mismatched hash, direct `storage/` paths, or fabricated `/static` URLs.

## Direct HTTP Fallback

If MCP is unavailable, use the HTTP contract in `docs/AGENT_HTTP_CONTRACT.md`
or run `examples/use_haypile_http.py`.
