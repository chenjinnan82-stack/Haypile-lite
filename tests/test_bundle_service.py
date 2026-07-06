from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.asset_provenance import write_asset_provenance
from app.api.v1.bundles import get_bundle_service, router
from app.services.bundle_service import BundleService


class BundleServiceTests(unittest.TestCase):
    def test_bundle_service_projects_manifest_and_theme_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))

            bundles = service.list_bundles()

            by_id = {bundle["id"]: bundle for bundle in bundles}
            self.assertEqual(by_id["generic_img_hero_image_abcd"]["status"], "ready")
            self.assertEqual(by_id["generic_img_hero_image_abcd"]["role"], "hero_image")
            self.assertEqual(by_id["generic_img_hero_image_abcd"]["sha256"], "db-sha")
            self.assertEqual(by_id["generic_img_hero_image_abcd"]["origin_url"], "https://cdn.example.com/hero.png")
            self.assertEqual(by_id["generic_img_hero_image_abcd"]["ai_suggestions"]["quality"], "high")
            self.assertEqual(by_id["generic_img_unknown_eeee"]["status"], "pending")
            self.assertEqual(by_id["missing_icon"]["status"], "missing")
            self.assertEqual(by_id["missing_icon"]["sha256"], "")
            self.assertEqual(
                [bundle["id"] for bundle in service.list_bundles(status="ready")],
                ["generic_img_hero_image_abcd"],
            )
            self.assertEqual(service.list_bundles(role="icon")[0]["id"], "missing_icon")

    def test_bundle_service_can_set_bundle_role(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))

            updated = service.set_bundle_role("generic_img_unknown_eeee", "hero_image")

            self.assertIsNotNone(updated)
            self.assertEqual(updated["role"], "hero_image")
            self.assertEqual(updated["status"], "ready")
            by_id = {bundle["id"]: bundle for bundle in service.list_bundles(role="hero_image")}
            self.assertIn("generic_img_unknown_eeee", by_id)
            payload = json.loads((Path(raw) / "themes" / "generic.json").read_text(encoding="utf-8"))
            saved_assets = payload["physical_assets"]
            saved = next(value for value in saved_assets.values() if value["url"].endswith("generic_img_unknown_eeee.png"))
            self.assertEqual(saved["role"], "hero_image")
            self.assertEqual(saved["css_advice"], "object-cover object-center")

    def test_bundle_service_rejects_unknown_role(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))

            with self.assertRaises(ValueError):
                service.set_bundle_role("generic_img_unknown_eeee", "sidebar")

    def test_bundles_api_lists_and_gets_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))
            app = FastAPI()
            app.include_router(router, prefix="/api/v1")
            app.dependency_overrides[get_bundle_service] = lambda: service
            client = TestClient(app)

            listed = client.get("/api/v1/bundles")
            ready = client.get(
                "/api/v1/bundles",
                params={"status": "ready", "role": "hero_image"},
            )
            one = client.get("/api/v1/bundles/generic_img_hero_image_abcd")
            missing = client.get("/api/v1/bundles/nope")

            self.assertEqual(listed.status_code, 200)
            self.assertTrue(any(item["id"] == "generic_img_hero_image_abcd" for item in listed.json()))
            self.assertEqual(ready.status_code, 200)
            self.assertEqual([item["id"] for item in ready.json()], ["generic_img_hero_image_abcd"])
            self.assertEqual(one.status_code, 200)
            self.assertEqual(one.json()["access"], "manifest_static")
            self.assertEqual(one.json()["origin_url"], "https://cdn.example.com/hero.png")
            self.assertEqual(missing.status_code, 404)


def _bundle_service(tmp_path: Path) -> BundleService:
    assets_dir = tmp_path / "assets"
    themes_dir = tmp_path / "themes"
    manifest_path = tmp_path / "index" / "assets_manifest.json"
    runtime_db_path = tmp_path / "index" / "storage_runtime.db"
    hero = assets_dir / "generic/images/generic_img_hero_image_abcd.png"
    unknown = assets_dir / "generic/images/generic_img_unknown_eeee.png"
    hero.parent.mkdir(parents=True)
    themes_dir.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True)
    hero.write_bytes(b"hero")
    unknown.write_bytes(b"unknown")
    write_asset_provenance(
        hero,
        {
            "origin_url": "https://cdn.example.com/hero.png",
            "content_type": "image/png",
            "downloaded_at": "2026-07-06T00:00:00+00:00",
            "temp_file": "/tmp/hero.png",
            "source_key": "generic/images/generic_img_hero_image_abcd.png",
            "sha256": "db-sha",
            "ai_suggestions": {
                "tags": ["主视觉"],
                "usage": "hero_image",
                "quality": "high",
                "agent_summary": "适合作为主视觉。",
            },
        },
    )
    manifest_path.write_text(
        json.dumps(
            {
                "generic/images/generic_img_hero_image_abcd.png": {
                    "type": "image",
                    "url_path": "/static/generic/images/generic_img_hero_image_abcd.png",
                },
                "generic/images/generic_img_unknown_eeee.png": {
                    "type": "image",
                    "url_path": "/static/generic/images/generic_img_unknown_eeee.png",
                },
            }
        ),
        encoding="utf-8",
    )
    (themes_dir / "generic.json").write_text(
        json.dumps(
            {
                "theme_name": "generic",
                "physical_assets": {
                    "hero_image": {
                        "url": "/static/generic/images/generic_img_hero_image_abcd.png",
                        "type": "image",
                        "css_advice": "object-cover",
                        "placement_intent": "hero",
                    },
                    "missing_icon": {
                        "url": "/static/generic/images/missing_icon.png",
                        "type": "image",
                        "css_advice": "object-contain",
                        "placement_intent": "icon",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with closing(sqlite3.connect(runtime_db_path)) as conn:
        conn.execute("CREATE TABLE vfs_asset_links (sha256 TEXT, dst_path TEXT)")
        conn.execute("INSERT INTO vfs_asset_links VALUES (?, ?)", ("db-sha", str(hero.resolve(strict=False))))
        conn.commit()
    return BundleService(
        assets_dir=assets_dir,
        manifest_path=manifest_path,
        themes_dir=themes_dir,
        runtime_db_path=runtime_db_path,
    )


if __name__ == "__main__":
    unittest.main()
