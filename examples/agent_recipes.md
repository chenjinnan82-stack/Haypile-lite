# Haypile Agent Recipes

These recipes prove that agents can use Haypile without reading the local asset directory.

## Design Agent

Goal: turn ready images into page visual direction.

1. Read ready images with `GET /api/v1/bundles?status=ready&type=image` or MCP `haypile_list_bundles`.
2. For each bundle, use `resolved_url` from `asset-handoff.json`, or build it from `base_url + url`.
3. Produce layout/color/hero/image-use suggestions.
4. Keep `id`, `role`, `status`, `sha256`, `source_key`, `url`, `ai_suggestions`, `duration_seconds`, `audio_metadata`, `audio_tags`, `audio_usage`, and `provenance` beside every suggestion.
5. Treat `ai_suggestions` as hints only; asset truth still comes from `sha256`, `source_key`, `url`, and `provenance`.

For a large pile, request `limit=50`, then use the final returned `source_key`
as `cursor` for the next page.

Output rule: every visual suggestion must cite the bundle id and sha256.

## Review Agent

Goal: reject unsafe or incomplete handoffs.

Check that every asset has:

- `id`
- `role`
- `status`
- `sha256`
- `source_key`
- `url`
- `resolved_url`
- `provenance`

Reject the handoff if it contains `storage/assets`, absolute local paths, missing sha256, or an asset URL outside the Haypile base URL.

## Generation Agent

Goal: generate UI/code using Haypile assets.

1. Accept `asset-handoff.json`.
2. Fetch assets only through `resolved_url`.
3. Preserve the asset `provenance` object in generated comments, metadata, or handoff notes.
4. Never inspect Haypile's local storage directory.

Minimal rule: generated output may use remote/static URLs, but not local filesystem paths.

## Codex Agent

Goal: let Codex consume Haypile like any other agent.

1. Run `python3 examples/use_haypile_http.py`.
2. Use only `resolved_url` for fetching assets.
3. Keep `id`, `role`, `status`, `sha256`, `source_key`, `url`, and `provenance` in the generated handoff or notes.
4. Do not read `storage/assets` directly.
