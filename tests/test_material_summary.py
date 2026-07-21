from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import material_summary as material_summary_module
from app.services.asset_provenance import write_asset_provenance
from app.services.bundle_service import BundleService
from app.services.material_summary import build_material_panel_summary


class MaterialSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.assets_dir = self.tmpdir / "assets"
        self.themes_dir = self.tmpdir / "themes"
        self.index_dir = self.tmpdir / "index"
        self.manifest_path = self.index_dir / "assets_manifest.json"
        self.binding_path = self.tmpdir / "real_project_binding.json"
        self.assets_dir.mkdir(parents=True)
        self.themes_dir.mkdir(parents=True)
        self.index_dir.mkdir(parents=True)
        self.previous_real_project_env = os.environ.get("HAYPILE_REAL_PROJECT_ROOT")
        self.previous_haypile_picker_preview_env = os.environ.get("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH")
        os.environ.pop("HAYPILE_REAL_PROJECT_ROOT", None)
        os.environ.pop("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH", None)

    def tearDown(self) -> None:
        if self.previous_real_project_env is None:
            os.environ.pop("HAYPILE_REAL_PROJECT_ROOT", None)
        else:
            os.environ["HAYPILE_REAL_PROJECT_ROOT"] = self.previous_real_project_env
        if self.previous_haypile_picker_preview_env is None:
            os.environ.pop("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH", None)
        else:
            os.environ["HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH"] = self.previous_haypile_picker_preview_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_summary_maps_theme_roles_to_plain_labels(self) -> None:
        self._write_json(
            self.manifest_path,
            {
                "generic/images/generic_img_hero_image_aaaa.png": {
                    "type": "image",
                    "resolution": "100x80",
                    "aspect_ratio": "1.25",
                    "url_path": "/static/generic/images/generic_img_hero_image_aaaa.png",
                },
                "generic/images/generic_img_unknown_bbbb.png": {
                    "type": "image",
                    "resolution": "40x40",
                    "aspect_ratio": "1.0",
                    "url_path": "/static/generic/images/generic_img_unknown_bbbb.png",
                },
                "generic/audio/generic_aud_unknown_cccc.mp3": {
                    "type": "audio",
                    "duration_seconds": 2.5,
                    "url_path": "/static/generic/audio/generic_aud_unknown_cccc.mp3",
                },
            },
        )
        self._write_json(
            self.themes_dir / "generic.json",
            {
                "theme_name": "generic",
                "physical_assets": {
                    "hero_image": {
                        "url": "/static/generic/images/generic_img_hero_image_aaaa.png",
                        "type": "image",
                        "css_advice": "object-cover",
                        "placement_intent": "hero",
                    },
                    "generic_img_unknown_bbbb": {
                        "url": "/static/generic/images/generic_img_unknown_bbbb.png",
                        "type": "image",
                        "css_advice": "object-contain",
                        "placement_intent": "general",
                    },
                },
            },
        )
        hero_path = self.assets_dir / "generic/images/generic_img_hero_image_aaaa.png"
        hero_path.parent.mkdir(parents=True, exist_ok=True)
        hero_path.write_bytes(b"hero")
        write_asset_provenance(hero_path, {"origin_url": "https://cdn.example.com/hero.png"})

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertEqual(summary.total_count, 3)
        self.assertEqual(summary.recognized_count, 2)
        self.assertEqual(summary.pending_count, 1)
        labels_by_title = {item.title: item.usage_label for item in summary.recent_items}
        origins_by_title = {item.title: item.origin_url for item in summary.recent_items}
        self.assertEqual(labels_by_title["generic_img_hero_image_aaaa.png"], "主视觉")
        self.assertEqual(origins_by_title["generic_img_hero_image_aaaa.png"], "https://cdn.example.com/hero.png")
        self.assertEqual(labels_by_title["generic_img_unknown_bbbb.png"], "未确定")
        self.assertEqual(labels_by_title["generic_aud_unknown_cccc.mp3"], "音频")
        self.assertIn("草堆里有 3 个 bundle", summary.summary_text())

    def test_summary_default_keeps_more_than_five_items_visible(self) -> None:
        manifest = {
            f"generic/images/generic_img_unknown_{idx}.png": {
                "type": "image",
                "url_path": f"/static/generic/images/generic_img_unknown_{idx}.png",
            }
            for idx in range(8)
        }
        self._write_json(self.manifest_path, manifest)

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertEqual(summary.total_count, 8)
        self.assertEqual(len(summary.recent_items), 8)
        self.assertEqual(summary.pending_count, 8)

    def test_summary_prefers_saved_role_over_unknown_asset_key(self) -> None:
        self._write_json(
            self.manifest_path,
            {
                "generic/images/generic_img_unknown_bbbb.png": {
                    "type": "image",
                    "url_path": "/static/generic/images/generic_img_unknown_bbbb.png",
                },
            },
        )
        self._write_json(
            self.themes_dir / "generic.json",
            {
                "theme_name": "generic",
                "physical_assets": {
                    "generic_img_unknown_bbbb": {
                        "url": "/static/generic/images/generic_img_unknown_bbbb.png",
                        "type": "background",
                        "role": "main_background",
                    },
                },
            },
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertEqual(summary.pending_count, 0)
        self.assertEqual(summary.recent_items[0].usage_label, "背景")

    def test_summary_reflects_bundle_role_confirmation(self) -> None:
        source_key = "generic/images/generic_img_unknown_bbbb.png"
        url = "/static/generic/images/generic_img_unknown_bbbb.png"
        asset_path = self.assets_dir / source_key
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(b"image")
        self._write_json(self.manifest_path, {source_key: {"type": "image", "url_path": url}})
        service = BundleService(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            runtime_db_path=self.index_dir / "storage_runtime.db",
        )

        updated = service.set_bundle_role("generic_img_unknown_bbbb", "main_background")
        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertEqual(updated["role"], "main_background")
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(summary.pending_count, 0)
        self.assertEqual(summary.recent_items[0].usage_label, "背景")

    def test_summary_handles_missing_manifest_as_empty_pocket(self) -> None:
        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertEqual(summary.total_count, 0)
        self.assertEqual(summary.recognized_count, 0)
        self.assertEqual(summary.pending_count, 0)
        self.assertEqual(summary.service_status, "Haypile：等待入库")
        self.assertEqual(summary.recent_items, [])
        self.assertEqual(summary.rehearsal_status, "")
        self.assertEqual(summary.rehearsal_status_label, "")
        self.assertEqual(summary.real_project_status, "")
        self.assertEqual(summary.real_project_status_label, "")
        self.assertEqual(summary.real_project_root, "")
        self.assertEqual(summary.project_display_label, "")
        self.assertEqual(summary.project_display_state, "")
        self.assertEqual(summary.panel_status_label, "")
        self.assertEqual(summary.panel_action_label, "")
        self.assertEqual(summary.panel_status_text, "")
        self.assertEqual(summary.panel_display_text, "")
        self.assertFalse(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "")
        self.assertEqual(summary.confirmation_primary_label, "")
        self.assertEqual(summary.confirmation_title, "")
        self.assertEqual(summary.confirmation_summary, "")

    def test_classifier_status_reports_missing_model_from_tags(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"models":[{"name":"other:latest"}]}'

        settings = SimpleNamespace(
            VISION_CLASSIFIER_ENABLED=True,
            VISION_CLASSIFIER_MODEL="qwen3-vl:8b",
            VISION_CLASSIFIER_BASE_URL="http://127.0.0.1:11434",
        )
        material_summary_module._classifier_status_cached.cache_clear()
        class FakeOpener:
            def open(self, *_args, **_kwargs):
                return FakeResponse()

        with patch.object(
            material_summary_module.urllib.request,
            "build_opener",
            return_value=FakeOpener(),
        ) as build_opener:
            status = material_summary_module._classifier_status(settings)

        self.assertEqual(status, "模型：未安装 qwen3-vl:8b")
        self.assertEqual(build_opener.call_count, 1)

    def test_summary_shows_ready_rehearsal_as_one_plain_status(self) -> None:
        rehearsal_root = self.tmpdir / "rehearsal"
        report_root = rehearsal_root / "haypile-rehearsal-reports"
        report_root.mkdir(parents=True)
        (rehearsal_root / "haypile-hydration.html").write_text(
            "<!doctype html><title>Haypile</title>",
            encoding="utf-8",
        )
        self._write_json(
            report_root / "static-compatible-verification-report.json",
            {
                "status": "verified",
                "remote_urls": [],
                "unregistered_assets": [],
            },
        )
        self._write_json(
            report_root / "static-compatible-dom-resource-check.json",
            {"status": "passed"},
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            rehearsal_root=rehearsal_root,
            real_project_binding_path=self.binding_path,
        )

        self.assertEqual(summary.rehearsal_status, "ready")
        self.assertEqual(summary.rehearsal_status_label, "演练：可预览")
        self.assertEqual(summary.panel_status_label, "演练：可预览")
        self.assertEqual(summary.panel_action_label, "")
        self.assertEqual(summary.panel_status_text, "演练：可预览")
        self.assertEqual(summary.panel_display_text, "可预览")

    def test_summary_shows_blocked_rehearsal_as_one_plain_status(self) -> None:
        rehearsal_root = self.tmpdir / "rehearsal"
        report_root = rehearsal_root / "haypile-rehearsal-reports"
        report_root.mkdir(parents=True)
        self._write_json(
            report_root / "static-compatible-verification-report.json",
            {
                "status": "verified",
                "remote_urls": ["https://example.test/asset.png"],
                "unregistered_assets": [],
            },
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            rehearsal_root=rehearsal_root,
            real_project_binding_path=self.binding_path,
        )

        self.assertEqual(summary.rehearsal_status, "blocked")
        self.assertEqual(summary.rehearsal_status_label, "演练：需处理")
        self.assertEqual(summary.panel_status_label, "演练：需处理")
        self.assertEqual(summary.panel_action_label, "")
        self.assertEqual(summary.panel_status_text, "演练：需处理")
        self.assertEqual(summary.panel_display_text, "需处理")

    def test_summary_shows_real_project_applied_status_as_plain_panel_status(self) -> None:
        project_root = self.tmpdir / "real-project"
        self._write_real_project_base(project_root)
        written_files = self._real_project_written_files()
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-apply-report.json",
            {"status": "applied", "passed": True, "written_files": written_files},
        )
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-post-apply-verification.json",
            {
                "status": "verified",
                "passed": True,
                "remote_urls": [],
                "unregistered_assets": [],
            },
        )
        for path in written_files:
            target = project_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("haypile", encoding="utf-8")

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_root=project_root,
        )

        self.assertEqual(summary.real_project_status, "applied_verified")
        self.assertEqual(summary.real_project_status_label, "真实项目：已投放")
        self.assertEqual(summary.real_project_root, project_root.as_posix())
        self.assertEqual(summary.project_display_label, "● real-project")
        self.assertEqual(summary.project_display_state, "applied_verified")
        self.assertEqual(summary.panel_status_label, "真实项目：已投放")
        self.assertEqual(summary.panel_action_label, "可确认撤回投放 5 项")
        self.assertEqual(summary.panel_status_text, "真实项目：已投放\n可确认撤回投放 5 项")
        self.assertEqual(summary.panel_display_text, "已投放 · 撤回 5")
        self.assertTrue(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "rollback")
        self.assertEqual(summary.confirmation_primary_label, "撤回投放")
        self.assertEqual(summary.confirmation_title, "撤回投放？")
        self.assertEqual(summary.confirmation_body, "real-project")
        self.assertEqual(summary.confirmation_summary, "5 项")
        self.assertEqual(summary.confirmation_warning, "再次确认后执行")
        for path in written_files:
            self.assertNotIn(path, summary.panel_status_text)
            self.assertNotIn(path, summary.confirmation_summary)

    def test_summary_shows_real_project_rollback_status_before_rehearsal_status(self) -> None:
        rehearsal_root = self.tmpdir / "rehearsal"
        report_root = rehearsal_root / "haypile-rehearsal-reports"
        report_root.mkdir(parents=True)
        (rehearsal_root / "haypile-hydration.html").write_text("<!doctype html>", encoding="utf-8")
        self._write_json(report_root / "static-compatible-verification-report.json", {"status": "verified"})
        self._write_json(report_root / "static-compatible-dom-resource-check.json", {"status": "passed"})
        project_root = self.tmpdir / "real-project"
        self._write_real_project_base(project_root)
        written_files = self._real_project_written_files()
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-apply-report.json",
            {"status": "applied", "passed": True, "written_files": written_files},
        )
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-rollback-report.json",
            {
                "status": "restored",
                "passed": True,
                "removed_files": written_files,
                "remaining_written_files": [],
            },
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            rehearsal_root=rehearsal_root,
            real_project_root=project_root,
        )

        self.assertEqual(summary.rehearsal_status_label, "演练：可预览")
        self.assertEqual(summary.real_project_status, "rolled_back")
        self.assertEqual(summary.real_project_status_label, "真实项目：已撤回")
        self.assertEqual(summary.real_project_root, project_root.as_posix())
        self.assertEqual(summary.project_display_label, "● real-project")
        self.assertEqual(summary.project_display_state, "rolled_back")
        self.assertEqual(summary.panel_status_label, "真实项目：已撤回")
        self.assertEqual(summary.panel_action_label, "可确认重新投放 5 项")
        self.assertEqual(summary.panel_status_text, "真实项目：已撤回\n可确认重新投放 5 项")
        self.assertEqual(summary.panel_display_text, "已撤回 · 投放 5")
        self.assertTrue(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "reapply")
        self.assertEqual(summary.confirmation_primary_label, "重新投放")
        self.assertEqual(summary.confirmation_title, "重新投放？")
        self.assertEqual(summary.confirmation_body, "real-project")
        self.assertEqual(summary.confirmation_summary, "5 项")
        self.assertEqual(summary.confirmation_warning, "再次确认后执行")

    def test_summary_reads_real_project_from_binding_file_when_no_explicit_root(self) -> None:
        project_root = self.tmpdir / "real-project"
        self._write_real_project_base(project_root)
        written_files = self._real_project_written_files()
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-apply-report.json",
            {"status": "applied", "passed": True, "written_files": written_files},
        )
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-rollback-report.json",
            {
                "status": "restored",
                "passed": True,
                "removed_files": written_files,
                "remaining_written_files": [],
            },
        )
        binding_path = self.tmpdir / "real_project_binding.json"
        self._write_json(
            binding_path,
            {
                "binding_type": "haypile_real_project_binding",
                "version": "haypile_real_project_binding.v1",
                "project_root": project_root.as_posix(),
            },
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=binding_path,
        )

        self.assertEqual(summary.real_project_status, "rolled_back")
        self.assertEqual(summary.panel_status_text, "真实项目：已撤回\n可确认重新投放 5 项")
        self.assertEqual(summary.panel_display_text, "已撤回 · 投放 5")
        self.assertEqual(summary.real_project_root, project_root.resolve(strict=False).as_posix())
        self.assertEqual(summary.project_display_label, "● real-project")
        self.assertEqual(summary.project_display_state, "rolled_back")

    def test_project_picker_preview_shows_rolled_back_reapply_display_only(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
                next_step="show_reapply_confirmation_ui",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(summary.project_picker_preview_available)
        self.assertEqual(summary.panel_status_label, "真实项目：已回滚\n可确认重新投放 5 项")
        self.assertIn("真实项目：已回滚", summary.panel_display_text)
        self.assertIn("重新投放", summary.panel_display_text)
        self.assertEqual(summary.project_picker_primary_label, "重新投放")
        self.assertTrue(summary.project_picker_primary_enabled)
        self.assertTrue(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "reapply")
        self.assertEqual(summary.confirmation_primary_label, "重新投放")
        self.assertEqual(summary.confirmation_title, "重新投放？")
        self.assertEqual(summary.confirmation_body, "real-project")
        self.assertEqual(summary.confirmation_summary, "5 项")
        self.assertEqual(summary.confirmation_warning, "再次确认后执行")
        self.assertEqual(summary.real_project_root, project_root.as_posix())
        self.assertFalse(summary.project_picker_contract["write_allowed"])
        self.assertFalse(summary.project_picker_contract["execute_allowed"])

    def test_project_picker_preview_shows_applied_withdrawal_display_only(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="applied_verified",
                status_label="真实项目：已投放",
                picker_status="selection_ready",
                primary_action="withdrawal",
                primary_label="撤回投放",
                next_step="show_withdrawal_confirmation_ui",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(summary.project_picker_preview_available)
        self.assertEqual(summary.panel_status_label, "真实项目：已投放\n可确认撤回投放 5 项")
        self.assertIn("真实项目：已投放", summary.panel_display_text)
        self.assertIn("撤回投放", summary.panel_display_text)
        self.assertEqual(summary.project_picker_primary_label, "撤回投放")
        self.assertTrue(summary.project_picker_primary_enabled)
        self.assertTrue(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "rollback")
        self.assertEqual(summary.confirmation_primary_label, "撤回投放")
        self.assertEqual(summary.confirmation_title, "撤回投放？")
        self.assertEqual(summary.confirmation_body, "real-project")
        self.assertEqual(summary.confirmation_summary, "5 项")
        self.assertEqual(summary.confirmation_warning, "再次确认后执行")
        self.assertEqual(summary.real_project_root, project_root.as_posix())

    def test_project_picker_preview_shows_blocked_reasons(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="needs_review",
                status_label="真实项目：需处理",
                picker_status="blocked",
                primary_action="review_only",
                primary_label="查看详情",
                blockers=["missing_rollback_report", "operation_paths_unavailable"],
                next_step="show_project_picker_details",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(summary.project_picker_preview_available)
        self.assertEqual(summary.panel_status_label, "真实项目：需处理")
        self.assertFalse(summary.project_picker_primary_enabled)
        self.assertEqual(summary.project_picker_blocked_reason_count, 2)
        self.assertIn("missing_rollback_report", summary.panel_display_text)
        self.assertIn("operation_paths_unavailable", summary.project_picker_tooltip)
        self.assertFalse(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "")

    def test_project_picker_preview_does_not_arm_mismatched_action(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="applied_verified",
                status_label="真实项目：已投放",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(summary.project_picker_preview_available)
        self.assertTrue(summary.project_picker_primary_enabled)
        self.assertFalse(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "")

    def test_project_picker_preview_can_display_confirmation_prompt_without_execution(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
                include_confirmation=True,
                next_step="show_reapply_confirmation_ui",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertEqual(summary.project_picker_confirmation_title, "确认重新投放")
        self.assertEqual(summary.project_picker_confirmation_body_line, "真实项目：已回滚")
        self.assertEqual(summary.project_picker_confirmation_summary_line, "real-project · 5 项")
        self.assertIn("确认提示：确认重新投放", summary.project_picker_tooltip)
        self.assertTrue(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "reapply")

    def test_project_picker_preview_displays_execution_readiness_dry_run(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
                include_execution_readiness=True,
                readiness_status="dry_run_ready",
                operation_paths_hash="abc123",
                next_step="show_disabled_execution_button",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(summary.project_picker_execution_readiness_available)
        self.assertEqual(summary.project_picker_execution_readiness_status, "dry_run_ready")
        self.assertFalse(summary.project_picker_execution_button_enabled)
        self.assertFalse(summary.project_picker_execution_would_execute)
        self.assertEqual(summary.project_picker_operation_paths_hash, "abc123")
        self.assertFalse(summary.project_picker_primary_enabled)
        self.assertIn("重新投放（不可用）", summary.panel_display_text)
        self.assertIn("干跑检查就绪", summary.panel_display_text)
        self.assertIn("执行检查：dry_run_ready", summary.project_picker_tooltip)
        self.assertIn("操作路径哈希：abc123", summary.project_picker_tooltip)
        self.assertIn("当前为干跑检查，不会执行写入。", summary.project_picker_tooltip)
        self.assertFalse(summary.confirmation_available)

    def test_project_picker_preview_displays_blocked_execution_readiness(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="applied_verified",
                status_label="真实项目：已投放",
                picker_status="selection_ready",
                primary_action="withdrawal",
                primary_label="撤回投放",
                include_execution_readiness=True,
                readiness_status="blocked",
                readiness_blockers=["operation_paths_hash_mismatch"],
                operation_paths_hash="newhash",
                next_step="resolve_execution_readiness_blocks",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(summary.project_picker_execution_readiness_available)
        self.assertEqual(summary.project_picker_execution_readiness_status, "blocked")
        self.assertFalse(summary.project_picker_primary_enabled)
        self.assertEqual(summary.project_picker_blocked_reason_count, 1)
        self.assertIn("operation_paths_hash_mismatch", summary.panel_display_text)
        self.assertIn("执行检查：blocked", summary.project_picker_tooltip)
        self.assertIn("operation_paths_hash_mismatch", summary.project_picker_tooltip)
        self.assertFalse(summary.project_picker_contract["execute_allowed"])

    def test_project_picker_preview_rejects_unsafe_execution_readiness(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        payload = self._picker_preview(
            project_root=self.tmpdir / "real-project",
            project_state="rolled_back",
            status_label="真实项目：已回滚",
            picker_status="selection_ready",
            primary_action="reapply",
            primary_label="重新投放",
            include_execution_readiness=True,
            readiness_status="dry_run_ready",
        )
        payload["execution_readiness"]["button_enabled"] = True
        self._write_json(preview_path, payload)

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertFalse(summary.project_picker_preview_available)
        self.assertEqual(summary.project_picker_preview_file_status, "invalid")
        self.assertEqual(summary.project_picker_preview_error, "preview_file_unsafe_execution_readiness")
        self.assertFalse(summary.project_picker_execution_readiness_available)
        self.assertIn("preview_file_unsafe_execution_readiness", summary.project_picker_tooltip)

    def test_project_picker_preview_displays_real_execution_applied_result(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
                execution_result=self._execution_result(
                    project_root=project_root,
                    status="applied",
                    action="reapply",
                    executed=True,
                    write_allowed=True,
                ),
                next_step="refresh_project_picker_preview",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(summary.project_picker_execution_result_available)
        self.assertEqual(summary.project_picker_execution_result_status, "applied")
        self.assertEqual(summary.project_picker_execution_result_action, "reapply")
        self.assertTrue(summary.project_picker_execution_result_executed)
        self.assertFalse(summary.project_picker_primary_enabled)
        self.assertIn("结果：已重新投放", summary.panel_display_text)
        self.assertIn("执行结果：applied", summary.project_picker_tooltip)
        self.assertIn("已执行：是", summary.project_picker_tooltip)
        self.assertFalse(summary.confirmation_available)

    def test_project_picker_preview_displays_real_execution_blocked_result(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="applied_verified",
                status_label="真实项目：已投放",
                picker_status="selection_ready",
                primary_action="withdrawal",
                primary_label="撤回投放",
                execution_result=self._execution_result(
                    project_root=project_root,
                    status="blocked",
                    action="rollback",
                    executed=False,
                    blocked_by=["operation_paths_hash_mismatch"],
                    gui_error_code="operation_paths_hash_mismatch",
                    gui_error_message="Preview is stale; refresh before execution.",
                ),
                next_step="refresh_project_picker_preview",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(summary.project_picker_execution_result_available)
        self.assertEqual(summary.project_picker_execution_result_status, "blocked")
        self.assertEqual(summary.project_picker_execution_result_error_code, "operation_paths_hash_mismatch")
        self.assertFalse(summary.project_picker_execution_result_executed)
        self.assertFalse(summary.project_picker_primary_enabled)
        self.assertIn("结果：撤回被阻止", summary.panel_display_text)
        self.assertIn("operation_paths_hash_mismatch", summary.panel_display_text)
        self.assertIn("执行结果：blocked", summary.project_picker_tooltip)
        self.assertIn("错误信息：Preview is stale; refresh before execution.", summary.project_picker_tooltip)

    def test_project_picker_preview_refresh_reads_updated_execution_result(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        project_root = self.tmpdir / "real-project"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
            ),
        )

        before = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(before.confirmation_available)
        self.assertEqual(before.confirmation_action, "reapply")
        self.assertFalse(before.project_picker_execution_result_available)

        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="applied_verified",
                status_label="真实项目：已投放",
                picker_status="selection_ready",
                primary_action="withdrawal",
                primary_label="撤回投放",
                execution_result=self._execution_result(
                    project_root=project_root,
                    status="applied",
                    action="reapply",
                    executed=True,
                    write_allowed=True,
                ),
                next_step="refresh_project_picker_preview",
            ),
        )

        after = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertTrue(after.project_picker_execution_result_available)
        self.assertEqual(after.project_picker_execution_result_status, "applied")
        self.assertEqual(after.project_picker_execution_result_action, "reapply")
        self.assertFalse(after.confirmation_available)
        self.assertEqual(after.confirmation_action, "")
        self.assertIn("结果：已重新投放", after.panel_display_text)
        self.assertIn("执行结果：applied", after.project_picker_tooltip)

    def test_project_picker_preview_rejects_unsafe_real_execution_result(self) -> None:
        for unsafe_field in ("worker_allowed", "apply_allowed", "rollback_allowed"):
            preview_path = self.tmpdir / f"picker-preview-{unsafe_field}.json"
            payload = self._picker_preview(
                project_root=self.tmpdir / "real-project",
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
                execution_result=self._execution_result(
                    project_root=self.tmpdir / "real-project",
                    status="applied",
                    action="reapply",
                    executed=True,
                    write_allowed=True,
                ),
            )
            payload["execution_result"][unsafe_field] = True
            self._write_json(preview_path, payload)

            summary = build_material_panel_summary(
                assets_dir=self.assets_dir,
                manifest_path=self.manifest_path,
                themes_dir=self.themes_dir,
                real_project_binding_path=self.binding_path,
                project_picker_preview_path=preview_path,
            )

            self.assertFalse(summary.project_picker_preview_available)
            self.assertEqual(summary.project_picker_preview_file_status, "invalid")
            self.assertEqual(summary.project_picker_preview_error, "preview_file_unsafe_execution_result")
            self.assertFalse(summary.project_picker_execution_result_available)
            self.assertIn("preview_file_unsafe_execution_result", summary.project_picker_tooltip)

    def test_project_picker_preview_preserves_no_execution_boundary(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=self.tmpdir / "real-project",
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
            project_picker_preview_path=preview_path,
        )

        self.assertFalse(summary.project_picker_contract["write_allowed"])
        self.assertFalse(summary.project_picker_contract["execute_allowed"])
        self.assertFalse(summary.project_picker_contract["worker_allowed"])
        self.assertFalse(summary.project_picker_contract["saga_mutation_allowed"])
        self.assertFalse(summary.project_picker_contract["task_qa_publish_allowed"])
        self.assertIn("再次确认前不会执行写入", summary.project_picker_tooltip)
        self.assertTrue(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "reapply")

    def test_project_picker_preview_env_path_present_is_displayed(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        os.environ["HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH"] = preview_path.as_posix()
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=self.tmpdir / "real-project",
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertTrue(summary.project_picker_preview_available)
        self.assertEqual(summary.project_picker_preview_path, preview_path.as_posix())
        self.assertEqual(summary.project_picker_preview_error, "")
        self.assertEqual(summary.project_picker_preview_source, "env")
        self.assertTrue(summary.project_picker_preview_file_exists)
        self.assertEqual(summary.project_picker_preview_file_status, "loaded")
        self.assertTrue(summary.project_picker_preview_loaded_at)
        self.assertIn("Project Picker：已读取", summary.project_picker_status_line)
        self.assertEqual(summary.project_picker_primary_label, "重新投放")
        self.assertIn("真实项目：已回滚", summary.panel_display_text)
        self.assertIn("预览文件：", summary.project_picker_tooltip)
        self.assertIn("刷新只会重新读取本地 preview file", summary.project_picker_tooltip)
        self.assertTrue(summary.confirmation_available)
        self.assertEqual(summary.confirmation_action, "reapply")

    def test_project_picker_preview_env_path_unset_shows_readback_state(self) -> None:
        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertFalse(summary.project_picker_preview_available)
        self.assertEqual(summary.project_picker_preview_path, "")
        self.assertEqual(summary.project_picker_preview_error, "preview_path_unset")
        self.assertEqual(summary.project_picker_preview_source, "unset")
        self.assertFalse(summary.project_picker_preview_file_exists)
        self.assertEqual(summary.project_picker_preview_file_status, "unset")
        self.assertEqual(summary.project_picker_preview_loaded_at, "")
        self.assertEqual(summary.panel_status_label, "")
        self.assertEqual(summary.panel_display_text, "")
        self.assertIn("预览未设置", summary.project_picker_status_line)
        self.assertIn("刷新只会重新读取本地 preview file", summary.project_picker_tooltip)
        self.assertFalse(summary.confirmation_available)

    def test_project_picker_preview_env_path_missing_shows_handoff_state(self) -> None:
        preview_path = self.tmpdir / "missing-picker-preview.json"
        os.environ["HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH"] = preview_path.as_posix()

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertFalse(summary.project_picker_preview_available)
        self.assertEqual(summary.project_picker_preview_path, preview_path.as_posix())
        self.assertEqual(summary.project_picker_preview_error, "preview_file_missing")
        self.assertEqual(summary.project_picker_preview_source, "env")
        self.assertFalse(summary.project_picker_preview_file_exists)
        self.assertEqual(summary.project_picker_preview_file_status, "missing")
        self.assertEqual(summary.project_picker_preview_loaded_at, "")
        self.assertEqual(summary.panel_status_label, "Project Picker：预览文件不可用")
        self.assertIn("preview_file_missing", summary.panel_display_text)
        self.assertIn("Project Picker：文件缺失", summary.project_picker_status_line)
        self.assertIn("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH", summary.project_picker_tooltip)
        self.assertFalse(summary.confirmation_available)

    def test_project_picker_preview_env_path_invalid_shows_handoff_state(self) -> None:
        preview_path = self.tmpdir / "invalid-picker-preview.json"
        preview_path.write_text("not json", encoding="utf-8")
        os.environ["HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH"] = preview_path.as_posix()

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertFalse(summary.project_picker_preview_available)
        self.assertEqual(summary.project_picker_preview_path, preview_path.as_posix())
        self.assertEqual(summary.project_picker_preview_error, "preview_file_unreadable_or_invalid")
        self.assertTrue(summary.project_picker_preview_file_exists)
        self.assertEqual(summary.project_picker_preview_file_status, "invalid")
        self.assertEqual(summary.project_picker_preview_loaded_at, "")
        self.assertIn("Project Picker：预览文件不可用", summary.panel_display_text)
        self.assertIn("Project Picker：文件无效", summary.project_picker_status_line)
        self.assertIn("preview_file_unreadable_or_invalid", summary.project_picker_tooltip)
        self.assertFalse(summary.confirmation_available)

    def test_project_picker_preview_env_path_applied_shows_withdrawal_readback(self) -> None:
        preview_path = self.tmpdir / "picker-preview.json"
        os.environ["HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH"] = preview_path.as_posix()
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=self.tmpdir / "real-project",
                project_state="applied_verified",
                status_label="真实项目：已投放",
                picker_status="selection_ready",
                primary_action="withdrawal",
                primary_label="撤回投放",
            ),
        )

        summary = build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )

        self.assertTrue(summary.project_picker_preview_available)
        self.assertEqual(summary.project_picker_preview_file_status, "loaded")
        self.assertEqual(summary.project_picker_primary_label, "撤回投放")
        self.assertIn("真实项目：已投放", summary.panel_display_text)
        self.assertIn("下一步：show_project_picker_details", summary.project_picker_tooltip)
        self.assertFalse(summary.project_picker_contract["write_allowed"])
        self.assertFalse(summary.project_picker_contract["execute_allowed"])

    def test_project_picker_preview_readback_does_not_change_real_project_files(self) -> None:
        project_root = self.tmpdir / "real-project"
        self._write_real_project_base(project_root)
        before = {
            path.name: path.read_text(encoding="utf-8")
            for path in (project_root / "index.html", project_root / "styles.css", project_root / "app.js")
        }
        preview_path = self.tmpdir / "picker-preview.json"
        os.environ["HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH"] = preview_path.as_posix()
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
            ),
        )

        build_material_panel_summary(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            real_project_binding_path=self.binding_path,
        )
        after = {
            path.name: path.read_text(encoding="utf-8")
            for path in (project_root / "index.html", project_root / "styles.css", project_root / "app.js")
        }

        self.assertEqual(after, before)

    def test_one_click_launcher_preserves_project_picker_preview_handoff(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "one-click-start-haypile.bat"
        text = launcher.read_text(encoding="utf-8")

        self.assertIn("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH", text)
        self.assertIn("Project picker preview handoff", text)
        self.assertIn("Haypile GUI will start and show the missing preview state", text)
        self.assertNotIn("/ui-preview-file", text)
        self.assertNotIn("/ui-preview", text)

    @staticmethod
    def _write_real_project_base(project_root: Path) -> None:
        project_root.mkdir(parents=True)
        (project_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
        (project_root / "styles.css").write_text("body{}", encoding="utf-8")
        (project_root / "app.js").write_text("console.log('demo')", encoding="utf-8")

    @staticmethod
    def _real_project_written_files() -> list[str]:
        return [
            "haypile-hydration.html",
            "assets/images/water-drop.svg",
            "assets/css/hydration-theme.css",
            "public/assets/images/water-drop.svg",
            "public/assets/css/hydration-theme.css",
        ]

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _picker_preview(
        *,
        project_root: Path,
        project_state: str,
        status_label: str,
        picker_status: str,
        primary_action: str,
        primary_label: str,
        blockers: list[str] | None = None,
        include_confirmation: bool = False,
        include_execution_readiness: bool = False,
        readiness_status: str = "dry_run_ready",
        readiness_blockers: list[str] | None = None,
        operation_paths_hash: str = "hash",
        execution_result: dict | None = None,
        next_step: str = "show_project_picker_details",
    ) -> dict:
        blockers = blockers or []
        readiness_blockers = readiness_blockers or []
        operation_count = 5
        ready = picker_status == "selection_ready" and not blockers
        if primary_action == "reapply":
            panel_status_label = f"{status_label}\n可确认重新投放 {operation_count} 项"
        elif primary_action == "withdrawal":
            panel_status_label = f"{status_label}\n可确认撤回投放 {operation_count} 项"
        else:
            panel_status_label = status_label
        contract = {
            "mode": "display_only_project_picker_panel_summary",
            "write_allowed": False,
            "execution_allowed": False,
            "apply_allowed": False,
            "rollback_allowed": False,
            "auto_apply_allowed": False,
            "auto_rollback_allowed": False,
            "full_argus_inspect_allowed": False,
            "worker_allowed": False,
            "saga_mutation_allowed": False,
            "task_qa_publish_allowed": False,
        }
        confirmation_intent = {}
        if include_confirmation:
            confirmation_intent = {
                "intent_type": "haypile_real_project_confirmation_intent",
                "intent_status": "confirmation_ready",
                "ui_prompt": {
                    "display_mode": "compact_confirmation",
                    "title": "确认重新投放",
                    "body_line": status_label,
                    "summary_line": f"{project_root.name} · {operation_count} 项",
                    "primary_label": primary_label,
                    "secondary_label": "取消",
                    "detail_label": "查看详情",
                    "warning_line": "执行前会再次确认，不会自动写入。",
                    "primary_enabled": True,
                    "blocked_reason_count": 0,
                    "max_visible_operation_paths": 0,
                    "show_operation_count_only": True,
                },
                "write_allowed": False,
                "execute_allowed": False,
                "auto_apply_allowed": False,
                "auto_rollback_allowed": False,
                "non_executable": True,
            }
        execution_readiness = {}
        if include_execution_readiness:
            execution_readiness = {
                "readiness_type": "haypile_real_project_picker_execution_readiness",
                "version": "haypile_real_project_picker_execution_readiness.v1",
                "readiness_status": readiness_status,
                "requested_action": "rollback" if primary_action == "withdrawal" else primary_action,
                "selected_project_root": project_root.as_posix(),
                "selected_project_name": project_root.name,
                "project_state": project_state,
                "project_status_label": status_label,
                "operation_count": operation_count,
                "operation_paths_hash": operation_paths_hash,
                "blocked_by": readiness_blockers,
                "dry_run_available": readiness_status == "dry_run_ready",
                "dry_run_only": True,
                "button_enabled": False,
                "primary_enabled": False,
                "primary_label": primary_label,
                "would_execute": False,
                "write_allowed": False,
                "apply_allowed": False,
                "rollback_allowed": False,
                "execute_allowed": False,
                "auto_apply_allowed": False,
                "auto_rollback_allowed": False,
                "full_argus_inspect_allowed": False,
                "worker_allowed": False,
                "saga_mutation_allowed": False,
                "task_qa_publish_allowed": False,
                "non_executable": True,
                "next_step": next_step,
            }
        return {
            "preview_type": "haypile_real_project_picker_ui_preview",
            "version": "haypile_real_project_picker_ui_preview.v1",
            "picker_intent": {
                "intent_type": "haypile_real_project_picker_intent",
                "selected_project_name": project_root.name,
                "selected_project_root": project_root.as_posix(),
                "project_state": project_state,
                "project_status_label": status_label,
                "write_allowed": False,
                "execute_allowed": False,
                "auto_apply_allowed": False,
                "auto_rollback_allowed": False,
            },
            "panel_summary": {
                "summary_type": "haypile_real_project_picker_panel_summary",
                "version": "haypile_real_project_picker_panel_summary.v1",
                "project_name": project_root.name,
                "selected_project_root": project_root.as_posix(),
                "picker_status": picker_status,
                "ready": ready,
                "project_state": project_state,
                "panel_status_label": panel_status_label,
                "primary_action": primary_action,
                "primary_label": primary_label,
                "operation_count": operation_count,
                "blockers": blockers,
                "actions": ["view_project_picker_details"],
                "compact_prompt": {},
                "contract": contract,
                "next_step": next_step,
            },
            "confirmation_intent": confirmation_intent,
            "confirmation_included": bool(confirmation_intent),
            "execution_readiness": execution_readiness,
            "execution_readiness_included": bool(execution_readiness),
            "execution_result": execution_result or {},
            "execution_result_included": bool(execution_result),
            "write_allowed": False,
            "execute_allowed": False,
            "auto_apply_allowed": False,
            "auto_rollback_allowed": False,
            "worker_allowed": False,
            "saga_mutation_allowed": False,
            "task_qa_publish_allowed": False,
            "non_executable": True,
            "next_step": next_step,
        }

    @staticmethod
    def _execution_result(
        *,
        project_root: Path,
        status: str,
        action: str,
        executed: bool,
        write_allowed: bool = False,
        blocked_by: list[str] | None = None,
        gui_error_code: str = "",
        gui_error_message: str = "",
    ) -> dict:
        return {
            "result_type": "haypile_real_project_picker_real_execution_adapter_result",
            "version": "haypile_real_project_picker_real_execution_adapter_result.v1",
            "adapter_status": status,
            "status": status,
            "executed": executed,
            "action": action,
            "requested_action": action,
            "selected_project_root": project_root.as_posix(),
            "operation_paths": ["haypile-hydration.html"],
            "operation_count": 1,
            "operation_paths_hash": "hash",
            "current_operation_paths_hash": "hash",
            "blocked_by": blocked_by or [],
            "gui_error_code": gui_error_code,
            "gui_error_message": gui_error_message,
            "pre_operation_summary": {},
            "post_operation_summary": {},
            "written_files": ["haypile-hydration.html"] if status == "applied" else [],
            "removed_files": ["haypile-hydration.html"] if status == "rolled_back" else [],
            "write_allowed": write_allowed,
            "real_project_write_allowed": write_allowed,
            "accepted_for_real_project_execution": False,
            "apply_allowed": False,
            "rollback_allowed": False,
            "execute_allowed": False,
            "auto_apply_allowed": False,
            "auto_rollback_allowed": False,
            "worker_allowed": False,
            "saga_mutation_allowed": False,
            "task_qa_publish_allowed": False,
            "full_argus_inspect_allowed": False,
            "next_step": "refresh_project_picker_preview",
        }


if __name__ == "__main__":
    unittest.main()
