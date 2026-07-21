# Haypile Release Manifest

Package only source, tests, and empty runtime directories.

## Include

```text
README.md
README.zh-CN.md
.gitignore
CHANGELOG.md
CONTRIBUTING.md
LICENSE
NOTICE
RELEASE_MANIFEST.md
SECURITY.md
requirements.txt
requirements-core.txt
requirements-desktop.txt
requirements-dev.txt
pyproject.toml
pytest.ini
assets/logo.png
assets/haypile-app-icon.png
assets/haypile-social-preview.png
app/
app_gui.py
backend_host.py
mcp_server.py
one-click-start-haypile.bat
.github/
scripts/
pysidedeploy.spec
pysidedeploy.windows.spec
docs/AGENT_HTTP_CONTRACT.md
docs/AGENT_RECIPES.md
docs/AI_EVALUATION.md
docs/LOCAL_AI.md
docs/MACOS_INTERNAL_BUILD.md
docs/OPEN_SOURCE_RELEASE.md
docs/haypile-demo.gif
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
.build-venv/
build/
dist/
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
