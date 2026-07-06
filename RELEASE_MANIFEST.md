# Haypile Release Manifest

Package only source, tests, and empty runtime directories.

## Include

```text
README.md
.gitignore
LICENSE
NOTICE
RELEASE_MANIFEST.md
requirements.txt
pytest.ini
app/
app_gui.py
backend_host.py
mcp_server.py
one-click-start-haypile.bat
docs/AGENT_HTTP_CONTRACT.md
docs/AGENT_RECIPES.md
examples/
ui_assets/
tests/
storage/.gitkeep
storage/assets/.gitkeep
storage/index/.gitkeep
storage/themes/.gitkeep
```

## Exclude

```text
.env
.pydeps/
.pydeps_user/
__pycache__/
.pytest_cache/
*.log
legacy one-click start launchers
storage/assets/**/*
storage/index/assets_manifest.json
storage/index/storage_runtime.db
storage/ipc_authkey
storage/real_project_binding.json
storage/themes/*.json
```

Current `storage/themes/*.json` files reference local assets, so they are not
release seeds. Add a sanitized theme JSON only when it has no private asset
URLs.
