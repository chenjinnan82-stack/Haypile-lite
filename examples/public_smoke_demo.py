from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from app.services.bundle_service import BundleService
from app.services.scanner import AssetScanner
from app.services.storage_runtime import StorageRuntimeDB
from app.services.theme_registry import ThemeRegistry
from app.services.vfs_storage import VFSStorage
from examples.use_haypile_http import build_handoff


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a tiny headless Haypile demo.")
    parser.add_argument("--out", default="/tmp/haypile-demo", help="Demo workspace.")
    args = parser.parse_args()

    root = Path(args.out).expanduser().resolve()
    storage = root / "storage"
    assets = storage / "assets"
    themes = storage / "themes"
    index = storage / "index"
    manifest = index / "assets_manifest.json"
    runtime_db = index / "storage_runtime.db"
    sample_dir = root / "sample_assets"
    sample = sample_dir / "haypile-sample.svg"

    for path in (assets, themes, index, sample_dir):
        path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HAYPILE_IPC_AUTHKEY_FILE", str(storage / "ipc_authkey"))

    _write_sample_svg(sample)
    sha256 = _sha256(sample)
    destination = assets / "generic" / "images" / f"generic_img_hero_image_{sha256[:8]}{sample.suffix}"
    strategy = VFSStorage(copy_max_retries=1).materialize(sample, destination)
    StorageRuntimeDB(runtime_db).record_link(
        sha256_hex=sha256,
        src_path=sample,
        dst_path=destination,
        strategy=strategy,
    )
    source_key = destination.relative_to(assets).as_posix()
    ThemeRegistry(themes).upsert_image_asset(
        theme_id="generic",
        asset_key="hero_image",
        asset_url=f"/static/{source_key}",
        role="hero_image",
    )
    asyncio.run(AssetScanner(assets, manifest).scan_assets_directory())

    service = BundleService(
        assets_dir=assets,
        manifest_path=manifest,
        themes_dir=themes,
        runtime_db_path=runtime_db,
    )
    handoff = build_handoff(service.list_bundles(status="ready"))
    (root / "asset-handoff.json").write_text(
        json.dumps(handoff, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(handoff, ensure_ascii=False, indent=2))
    return 0 if handoff["assets"] else 1


def _write_sample_svg(path: Path) -> None:
    path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="96" height="64" viewBox="0 0 96 64">
<rect width="96" height="64" fill="#fff7df"/>
<rect y="46" width="96" height="18" fill="#40562c"/>
<path d="M18 48 C32 34 39 18 48 10 C57 18 64 34 78 48" fill="#e2a91f" opacity=".95"/>
<path d="M22 48 L48 12 M31 48 L48 14 M40 48 L48 16 M56 48 L48 16 M65 48 L48 14 M74 48 L48 12" stroke="#f1c34b" stroke-width="2" stroke-linecap="round"/>
</svg>
""",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
