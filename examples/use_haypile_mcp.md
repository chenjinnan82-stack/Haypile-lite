# Use Haypile Through MCP

Start Haypile:

```bash
python3 app_gui.py
```

For a manual backend smoke test:

```bash
HAYPILE_BACKEND_HOST_ALLOW_START=1 python3 backend_host.py
```

Add this MCP server to the agent host:

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

Minimal agent flow:

1. Call `haypile_health`.
2. Call `haypile_list_bundles` with `{"status":"ready","type":"image"}`.
3. Use `base_url + bundle.url`.
4. Preserve `id`, `role`, `status`, `sha256`, `source_key`, `url`, and `provenance` in `asset-handoff.json`.

Do not read `storage/assets` directly.

Handoff flow:

1. Call `haypile_copy_handoff` with `{"status":"ready"}`.
2. Use `resolved_url` to fetch files.
3. Preserve `id`, `role`, `status`, `sha256`, `source_key`, `url`, `resolved_url`, and `provenance` in downstream agent output.

Available tools:

- `haypile_health`
- `haypile_list_bundles`
- `haypile_get_bundle`
- `haypile_copy_handoff`
- `haypile_list_themes`
- `haypile_get_theme`
