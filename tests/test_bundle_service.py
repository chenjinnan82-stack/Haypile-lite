from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.asset_provenance import write_asset_provenance
from app.api.v1.batches import get_bundle_service as get_batch_bundle_service, router as batches_router
from app.api.v1.bundles import get_bundle_service, router
from app.api.v1.theme import get_vault_service, router as theme_router
from app.services.bundle_service import BundleService
from app.services.scanner import manifest_dirty_path
from app.services.storage_runtime import StorageRuntimeDB
from app.services.vault_service import VaultService


class BundleServiceTests(unittest.TestCase):
    def test_bundle_service_projects_manifest_and_theme_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))

            bundles = service.list_bundles()

            by_source = {bundle["source_key"]: bundle for bundle in bundles}
            hero = by_source["generic/images/generic_img_hero_image_abcd.png"]
            unknown = by_source["generic/images/generic_img_unknown_eeee.png"]
            self.assertEqual(hero["status"], "ready")
            self.assertEqual(hero["role"], "hero_image")
            self.assertEqual(hero["sha256"], hashlib.sha256(b"hero").hexdigest())
            self.assertEqual(hero["id"], hero["sha256"])
            self.assertEqual(hero["origin_url"], "https://cdn.example.com")
            self.assertEqual(hero["ai_suggestions"]["quality"], "high")
            self.assertEqual(unknown["status"], "pending")
            self.assertEqual(
                [bundle["id"] for bundle in service.list_bundles(status="ready")],
                [hero["id"]],
            )
            self.assertEqual(service.list_bundles(role="icon"), [])

    def test_bundle_service_reports_quarantined_theme_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service = _bundle_service(root)
            (root / "themes" / "generic.json").write_text("{broken", encoding="utf-8")

            bundles = service.list_bundles()

            self.assertTrue(bundles)
            self.assertEqual(len(service.theme_recoveries), 1)
            self.assertEqual(service.theme_recoveries[0]["theme_id"], "generic")
            self.assertTrue(any((root / "quarantine" / "themes").iterdir()))

    def test_bundle_service_can_set_bundle_role(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))

            updated = service.set_bundle_role("generic_img_unknown_eeee", "hero_image")

            self.assertIsNotNone(updated)
            self.assertEqual(updated["role"], "hero_image")
            self.assertEqual(updated["status"], "ready")
            by_id = {bundle["id"]: bundle for bundle in service.list_bundles(role="hero_image")}
            self.assertIn(updated["id"], by_id)
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

    def test_bundle_service_confirms_audio_usage_and_projects_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))

            before = service.get_bundle("generic_aud_unknown_ffff")
            updated = service.set_bundle_audio_usage("generic_aud_unknown_ffff", "voice")

            self.assertEqual(before["status"], "pending")
            self.assertEqual(before["audio_tags"]["title"], "Pika Call")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["role"], "audio")
            self.assertEqual(updated["audio_usage"], "voice")
            self.assertEqual(updated["status"], "ready")
            self.assertEqual(updated["duration_seconds"], 12.5)
            self.assertEqual(updated["audio_metadata"]["channels"], 2)
            saved = json.loads((Path(raw) / "assets/generic/audio/generic_aud_unknown_ffff.wav.provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["audio_usage"], "voice")

    def test_bundle_service_pages_by_stable_source_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))

            all_bundles = service.list_bundles()
            first_page = service.list_bundles(limit=1)
            second_page = service.list_bundles(limit=1, cursor=first_page[-1]["source_key"])

            self.assertEqual(first_page, [all_bundles[0]])
            self.assertEqual(second_page, [all_bundles[1]])
            self.assertLess(first_page[-1]["source_key"], second_page[-1]["source_key"])

    def test_bundle_service_rehashes_stale_runtime_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))
            hero = Path(raw) / "assets/generic/images/generic_img_hero_image_abcd.png"
            hero.write_bytes(b"changed hero")

            bundle = service.get_bundle("generic_img_hero_image_abcd")

            self.assertIsNotNone(bundle)
            self.assertEqual(bundle["sha256"], hashlib.sha256(b"changed hero").hexdigest())

    def test_bundle_service_filters_latest_batch_and_includes_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service = _bundle_service(root)
            runtime = StorageRuntimeDB(service.runtime_db_path)
            unknown_hash = hashlib.sha256(b"unknown").hexdigest()
            batch_id = runtime.begin_batch()
            runtime.record_batch_asset(batch_id, unknown_hash, 0)
            runtime.record_batch_asset(batch_id, hashlib.sha256(b"hero").hexdigest(), 1)
            runtime.complete_batch(batch_id, accepted_count=1, duplicate_count=1, rejected_count=0)

            latest = service.get_latest_batch()
            bundles = service.list_bundles(batch_id="latest")
            first_page = service.list_bundles(batch_id="latest", limit=1)
            second_page = service.list_bundles(
                batch_id="latest",
                limit=1,
                cursor=first_page[0]["source_key"],
            )

            self.assertEqual(latest["id"], batch_id)
            self.assertEqual(
                [bundle["id"] for bundle in bundles],
                [unknown_hash, hashlib.sha256(b"hero").hexdigest()],
            )
            self.assertEqual(first_page[0]["id"], unknown_hash)
            self.assertEqual(second_page[0]["id"], hashlib.sha256(b"hero").hexdigest())
            self.assertEqual(service.list_bundles(batch_id="missing"), [])

    def test_bundles_api_lists_and_gets_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))
            app = FastAPI()
            app.include_router(router, prefix="/api/v1")
            app.include_router(batches_router, prefix="/api/v1")
            app.dependency_overrides[get_bundle_service] = lambda: service
            app.dependency_overrides[get_batch_bundle_service] = lambda: service
            client = TestClient(app)
            self.addCleanup(client.close)

            listed = client.get("/api/v1/bundles")
            ready = client.get(
                "/api/v1/bundles",
                params={"status": "ready", "role": "hero_image"},
            )
            audio_pending = client.get(
                "/api/v1/bundles",
                params={"type": "audio", "audio_usage": "unknown"},
            )
            first_page = client.get("/api/v1/bundles", params={"limit": 1})
            second_page = client.get(
                "/api/v1/bundles",
                params={"limit": 1, "cursor": first_page.json()[-1]["source_key"]},
            )
            one = client.get("/api/v1/bundles/generic_img_hero_image_abcd")
            missing = client.get("/api/v1/bundles/nope")
            no_batch = client.get("/api/v1/batches/latest")

            self.assertEqual(listed.status_code, 200)
            self.assertEqual(len(listed.headers["X-Haypile-Manifest-Generation"]), 64)
            hero_hash = hashlib.sha256(b"hero").hexdigest()
            self.assertTrue(any(item["id"] == hero_hash for item in listed.json()))
            self.assertEqual(ready.status_code, 200)
            self.assertEqual([item["id"] for item in ready.json()], [hero_hash])
            self.assertEqual([item["id"] for item in audio_pending.json()], [hashlib.sha256(b"audio").hexdigest()])
            self.assertEqual(audio_pending.json()[0]["audio_tags"]["artist"], "Winter Ridge")
            self.assertEqual(len(first_page.json()), 1)
            self.assertEqual(len(second_page.json()), 1)
            self.assertLess(first_page.json()[0]["source_key"], second_page.json()[0]["source_key"])
            self.assertEqual(one.status_code, 200)
            self.assertEqual(one.json()["access"], "manifest_static")
            self.assertEqual(one.json()["origin_url"], "https://cdn.example.com")
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(no_batch.status_code, 404)

    def test_missing_physical_asset_is_never_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service = _bundle_service(root)
            hero = root / "assets/generic/images/generic_img_hero_image_abcd.png"
            unknown = root / "assets/generic/images/generic_img_unknown_eeee.png"
            hero.unlink()
            unknown.unlink()

            bundles = {item["source_key"]: item for item in service.list_bundles()}

            known = bundles["generic/images/generic_img_hero_image_abcd.png"]
            legacy = bundles["generic/images/generic_img_unknown_eeee.png"]
            self.assertEqual(known["status"], "missing")
            self.assertEqual(known["id"], hashlib.sha256(b"hero").hexdigest())
            self.assertEqual(legacy["status"], "missing")
            self.assertTrue(legacy["id"].startswith("missing-"))
            self.assertTrue(legacy["id"])
            self.assertNotIn(known["id"], {item["id"] for item in service.list_bundles(status="ready")})

    def test_bundles_api_fails_closed_for_untrusted_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service = _bundle_service(root)
            app = FastAPI()
            app.include_router(router, prefix="/api/v1")
            app.dependency_overrides[get_bundle_service] = lambda: service
            client = TestClient(app)
            self.addCleanup(client.close)
            manifest = root / "index/assets_manifest.json"

            manifest_dirty_path(manifest).write_text("{}", encoding="utf-8")
            dirty = client.get("/api/v1/bundles")
            manifest_dirty_path(manifest).unlink()
            manifest.write_text("{broken", encoding="utf-8")
            unreadable = client.get("/api/v1/bundles")
            manifest.unlink()
            missing = client.get("/api/v1/bundles")
            manifest.write_text("{}", encoding="utf-8")
            empty = client.get("/api/v1/bundles")

            self.assertEqual(dirty.status_code, 503)
            self.assertEqual(dirty.json()["detail"]["code"], "catalog_projection_dirty")
            self.assertEqual(unreadable.status_code, 503)
            self.assertEqual(unreadable.json()["detail"]["code"], "catalog_projection_unreadable")
            self.assertEqual(missing.status_code, 503)
            self.assertEqual(missing.json()["detail"]["code"], "catalog_projection_missing")
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.json(), [])

    def test_vault_api_fails_closed_with_the_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "index/assets_manifest.json"
            manifest.parent.mkdir(parents=True)
            themes = root / "themes"
            service = VaultService(themes_dir=themes)
            app = FastAPI()
            app.include_router(theme_router, prefix="/api/v1")
            app.dependency_overrides[get_vault_service] = lambda: service
            client = TestClient(app)
            self.addCleanup(client.close)

            with patch(
                "app.api.v1.theme.get_settings",
                return_value=SimpleNamespace(MANIFEST_PATH=manifest),
            ):
                missing = client.get("/api/v1/vault")
                manifest.write_text("{}", encoding="utf-8")
                healthy = client.get("/api/v1/vault")

            self.assertEqual(missing.status_code, 503)
            self.assertEqual(
                missing.json()["detail"]["code"],
                "catalog_projection_missing",
            )
            self.assertEqual(healthy.status_code, 200)
            self.assertEqual(healthy.json(), [])
            self.assertEqual(
                len(healthy.headers["X-Haypile-Manifest-Generation"]),
                64,
            )

    def test_bundles_api_supports_latest_and_concrete_batch_without_changing_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = _bundle_service(Path(raw))
            runtime = StorageRuntimeDB(service.runtime_db_path)
            batch_id = runtime.begin_batch()
            runtime.record_batch_asset(batch_id, hashlib.sha256(b"hero").hexdigest(), 0)
            runtime.complete_batch(batch_id, accepted_count=0, duplicate_count=1, rejected_count=0)
            app = FastAPI()
            app.include_router(router, prefix="/api/v1")
            app.include_router(batches_router, prefix="/api/v1")
            app.dependency_overrides[get_bundle_service] = lambda: service
            app.dependency_overrides[get_batch_bundle_service] = lambda: service
            client = TestClient(app)
            self.addCleanup(client.close)

            unfiltered = client.get("/api/v1/bundles").json()
            latest = client.get("/api/v1/bundles", params={"batch_id": "latest"}).json()
            concrete = client.get("/api/v1/bundles", params={"batch_id": batch_id}).json()
            batch = client.get("/api/v1/batches/latest")

            self.assertGreater(len(unfiltered), len(latest))
            self.assertEqual([item["id"] for item in latest], [hashlib.sha256(b"hero").hexdigest()])
            self.assertEqual(concrete, latest)
            self.assertEqual(batch.status_code, 200)
            self.assertEqual(batch.json()["id"], batch_id)
            self.assertEqual(batch.json()["asset_count"], 1)


def _bundle_service(tmp_path: Path) -> BundleService:
    assets_dir = tmp_path / "assets"
    themes_dir = tmp_path / "themes"
    manifest_path = tmp_path / "index" / "assets_manifest.json"
    runtime_db_path = tmp_path / "index" / "storage_runtime.db"
    hero = assets_dir / "generic/images/generic_img_hero_image_abcd.png"
    unknown = assets_dir / "generic/images/generic_img_unknown_eeee.png"
    audio = assets_dir / "generic/audio/generic_aud_unknown_ffff.wav"
    hero.parent.mkdir(parents=True)
    themes_dir.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True)
    hero.write_bytes(b"hero")
    unknown.write_bytes(b"unknown")
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    write_asset_provenance(
        hero,
        {
            "origin_url": "https://cdn.example.com/hero.png",
            "content_type": "image/png",
            "downloaded_at": "2026-07-06T00:00:00+00:00",
            "temp_file": "/tmp/hero.png",
            "source_key": "generic/images/generic_img_hero_image_abcd.png",
            "sha256": hashlib.sha256(b"hero").hexdigest(),
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
                "generic/audio/generic_aud_unknown_ffff.wav": {
                    "type": "audio",
                    "duration_seconds": 12.5,
                    "audio_metadata": {"sample_rate_hz": 48000, "channels": 2, "bitrate_bps": 192000},
                    "audio_tags": {"title": "Pika Call", "artist": "Winter Ridge", "album": "Haypile"},
                    "url_path": "/static/generic/audio/generic_aud_unknown_ffff.wav",
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
    StorageRuntimeDB(db_path=runtime_db_path).record_link(
        sha256_hex=hashlib.sha256(b"hero").hexdigest(),
        src_path=hero,
        dst_path=hero,
        strategy="copy",
    )
    return BundleService(
        assets_dir=assets_dir,
        manifest_path=manifest_path,
        themes_dir=themes_dir,
        runtime_db_path=runtime_db_path,
    )


if __name__ == "__main__":
    unittest.main()
