from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, QRect, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import app_gui
except ImportError as exc:  # pragma: no cover - optional desktop runtime
    QApplication = None
    app_gui = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI runtime unavailable: {_IMPORT_ERROR}")
class AttachedHubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        app_gui.set_ui_language("auto")
        self.temp = tempfile.TemporaryDirectory()
        self.settings = app_gui.Settings(_env_file=None, STORAGE_DIR=Path(self.temp.name) / "storage", LOG_DIR=Path(self.temp.name) / "logs", VISION_CLASSIFIER_ENABLED=False)
        self.ball = app_gui.HaypileFloatingBall(settings=self.settings)
        self.ball._available_geometry = lambda: QRect(0, 0, 1000, 760)
        self.ball.move(180, 260)
        self.app.processEvents()

    def tearDown(self) -> None:
        self.ball.close()
        self.app.processEvents()
        self.temp.cleanup()
        app_gui.set_ui_language("auto")

    def test_card_and_management_keep_grass_origin(self) -> None:
        origin = self.ball.pos()
        self.ball._toggle_quick_menu()
        self.assertTrue(self.ball.intake_card.isVisible())
        self.assertEqual(self.ball.intake_card.width(), 280)
        for page in ("assets", "agent", "settings"):
            self.ball._handle_quick_menu_action(page)
            self.app.processEvents()
            menu = self.ball.quick_menu
            self.assertEqual(menu.current_page(), page)
            self.assertTrue(menu.isVisible())
            self.assertFalse(self.ball.intake_card.isVisible())
            self.assertEqual(menu.size(), app_gui.QSize(620, 520))
            self.assertEqual(self.ball.pos(), origin)
        self.assertTrue(self.ball.material_panel._embedded)
        self.assertIsNone(self.ball.material_panel.confirmation_preview)

    def test_opening_card_does_not_probe_local_ai(self) -> None:
        self.ball.ai_enabled = True
        self.ball._ai_model_state = lambda: self.fail("opening the card must not probe Ollama")
        self.ball._toggle_quick_menu()
        self.assertTrue(self.ball.intake_card.isVisible())

    def test_action_sounds_follow_ring_navigation_and_persist_the_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.ball._gui_state_path = Path(tmp) / "gui_state.json"
            with (
                patch.object(self.ball._sound_feedback, "play") as play,
                patch.object(self.ball._sound_feedback, "prepare", return_value=4),
            ):
                self.ball._toggle_quick_menu()
                self.ball._handle_quick_menu_action("assets")
                self.ball._handle_quick_menu_action("assets")
                self.ball._handle_quick_menu_action("settings")

                self.assertEqual(
                    [call.args for call in play.call_args_list],
                    [("nav",), ("nav",), ("nav",)],
                )

                self.ball._set_sound_enabled(False)
                self.assertFalse(self.ball.sound_enabled)
                self.assertFalse(self.ball.quick_menu.sound_button.isChecked())
                self.assertFalse(self.ball._sound_feedback.enabled)
                payload = json.loads(
                    self.ball._gui_state_path.read_text(encoding="utf-8")
                )
                self.assertFalse(payload["sound_enabled"])

                self.ball._set_sound_enabled(True)
                self.assertTrue(self.ball.quick_menu.sound_button.isChecked())
                self.assertTrue(self.ball._sound_feedback.enabled)
                self.assertEqual(play.call_args_list[-1].args, ("nav",))

    def test_action_sound_preference_is_applied_before_first_frame(self) -> None:
        for stored_value in (False, "false"):
            restored = app_gui.HaypileFloatingBall(
                settings=self.settings, initial_state={"sound_enabled": stored_value},
            )
            try:
                self.assertFalse(restored.sound_enabled)
                self.assertFalse(restored._sound_feedback.enabled)
                self.assertFalse(restored.quick_menu.sound_button.isChecked())
            finally:
                restored.close()
                self.app.processEvents()

    def test_management_refreshes_assets_once_when_opened(self) -> None:
        calls = []
        self.ball.material_panel.refresh = lambda: calls.append(True)
        self.ball._handle_quick_menu_action("assets")
        self.assertTrue(self.ball.quick_menu.isVisible())
        self.assertEqual(calls, [True])

    def test_management_fits_screen_without_moving_grass(self) -> None:
        self.ball.move(918, 280)
        origin = self.ball.pos()
        self.ball._handle_quick_menu_action("settings")
        self.app.processEvents()
        self.assertEqual(self.ball.pos(), origin)
        self.assertTrue(self.ball._available_geometry().contains(self.ball.quick_menu.frameGeometry()))
        self.assertEqual(self.ball.quick_menu.drawer_shell.geometry(), self.ball.quick_menu.rect())



    def test_language_and_low_power_persist_without_losing_ai_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.ball._gui_state_path = Path(tmp) / "gui_state.json"
            self.ball.ai_enabled = True
            self.ball._ai_preference = True

            self.ball._set_language_mode("en")
            self.assertEqual(self.ball.language_mode, "en")
            self.assertEqual(self.ball.quick_menu.language_buttons["en"].text(), "English")

            self.ball._set_low_power_enabled(True)
            self.assertTrue(self.ball.low_power_enabled)
            self.assertFalse(self.ball.ai_enabled)
            self.assertFalse(self.ball._drag_awareness_timer.isActive())

            self.ball._set_low_power_enabled(False)
            self.assertFalse(self.ball.low_power_enabled)
            self.assertTrue(self.ball.ai_enabled)
            payload = json.loads(self.ball._gui_state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["language"], "en")
            self.assertFalse(payload["low_power_enabled"])
            self.assertTrue(payload["ai_enabled"])
            self.assertTrue(self.ball.sound_enabled)

    def test_settings_and_selection_controls_expose_native_state(self) -> None:
        app_gui.set_ui_language("en")
        menu = self.ball.quick_menu
        menu.retranslate()
        menu.update_settings_state(
            ai_enabled=False,
            ai_status="API authorization needed",
            low_power=True,
            sound_enabled=False,
            language="en",
            service_status="Ready",
            ai_provider="api",
            api_base_url="https://vision.example/v1",
            api_model="vision-model",
            api_key_present=False,
        )

        self.assertTrue(menu.low_power_button.isCheckable())
        self.assertTrue(menu.low_power_button.isChecked())
        self.assertIn("Low power: on", menu.low_power_button.text())
        self.assertTrue(menu.sound_button.isCheckable())
        self.assertFalse(menu.sound_button.isChecked())
        self.assertIn("Action sounds: off", menu.sound_button.text())
        self.assertFalse(menu.ai_settings_button.isCheckable())
        self.assertEqual(menu.ai_state_badge.text(), "Off")
        self.assertIn("currently Off", menu.ai_settings_button.accessibleDescription())
        self.assertTrue(menu.language_buttons["en"].isChecked())
        self.assertTrue(menu.ai_provider_buttons["api"].isChecked())
        self.assertEqual(
            sum(button.isChecked() for button in menu.language_buttons.values()),
            1,
        )
        self.assertEqual(
            sum(button.isChecked() for button in menu.ai_provider_buttons.values()),
            1,
        )
        self.assertIn("QPushButton:focus", menu.low_power_button.styleSheet())
        self.assertIn("QPushButton:focus", menu.language_buttons["en"].styleSheet())
        self.assertEqual(
            menu.diagnostics_button.focusPolicy(),
            Qt.FocusPolicy.StrongFocus,
        )
        self.assertIn(
            "without filenames, paths, URLs, or keys",
            menu.diagnostics_button.accessibleDescription(),
        )

    def test_card_buttons_support_keyboard_close(self) -> None:
        self.ball._toggle_quick_menu()
        card = self.ball.intake_card
        self.app.processEvents()
        self.assertEqual(card.close_button.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertTrue(card.close_button.hasFocus())
        QTest.keyClick(card.close_button, Qt.Key.Key_Space)
        self.assertFalse(card.isVisible())

    def test_card_reopen_restores_keyboard_focus(self) -> None:
        self.ball._toggle_quick_menu()
        card = self.ball.intake_card
        QTest.keyClick(card, Qt.Key.Key_Escape)
        self.assertFalse(card.isVisible())
        self.ball._toggle_quick_menu()
        self.app.processEvents()
        self.assertTrue(card.close_button.hasFocus())

    def test_embedded_assets_tab_order_matches_visual_order(self) -> None:
        panel = self.ball.material_panel

        self.assertEqual(panel.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertIs(
            panel.paste_ingest_button.nextInFocusChain(),
            panel.scope_buttons["latest"],
        )
        self.assertIs(
            panel.scope_buttons["latest"].nextInFocusChain(),
            panel.scope_buttons["all"],
        )
        self.assertIs(
            panel.scope_buttons["all"].nextInFocusChain(),
            panel.retry_batch_button,
        )
        self.assertIs(
            panel.retry_batch_button.nextInFocusChain(),
            panel.search_input,
        )
        self.assertIs(
            panel.search_input.nextInFocusChain(),
            panel.filter_buttons["all"],
        )

    def test_diagnostic_summary_copies_only_whitelisted_runtime_state(self) -> None:
        bundles = [
            {
                "status": "ready",
                "filename": "private-portrait.gif",
                "local_path": "/Users/person/private-portrait.gif",
                "origin_url": "https://secret.example/file.gif?token=value",
                "sha256": "a" * 64,
            },
            {"status": "pending", "source_key": "private/audio.m4a"},
        ]

        class FakeBundleService:
            def list_bundles(self):
                return bundles

        self.ball._storage_ready = True
        self.ball._backend_phase = "ready"
        self.ball.ai_provider_mode = "api"
        self.ball.ai_enabled = False
        self.ball._bundle_service = lambda: FakeBundleService()
        messages: list[tuple[str, str]] = []
        self.ball.show_toast = lambda message, tone="success": messages.append((message, tone))
        QApplication.clipboard().clear()

        with (
            patch.object(app_gui, "is_packaged_app", return_value=True),
            patch.object(app_gui, "read_build_commit", return_value="b" * 40),
        ):
            self.ball._handle_quick_menu_action("diagnostics")

        copied = QApplication.clipboard().text()
        payload = json.loads(copied)
        self.assertEqual(payload["diagnostic_version"], "haypile.desktop-diagnostics.v1")
        self.assertEqual(payload["runtime"], "packaged")
        self.assertEqual(payload["build_commit"], "b" * 40)
        self.assertEqual(payload["backend_phase"], "ready")
        self.assertEqual(
            payload["asset_counts"],
            {"total": 2, "ready": 1, "pending": 1, "missing": 0},
        )
        for private_value in (
            "private-portrait",
            "/Users/person",
            "secret.example",
            "token=value",
            "a" * 64,
            "private/audio",
        ):
            self.assertNotIn(private_value, copied)
        self.assertTrue(messages)
        self.assertEqual(messages[-1][1], "success")

    def test_diagnostic_summary_fails_closed_when_manifest_is_dirty(self) -> None:
        class DirtyBundleService:
            def list_bundles(self):
                raise app_gui.ManifestReadinessError(
                    "catalog_projection_dirty",
                    "dirty",
                )

        self.ball._storage_ready = True
        self.ball._bundle_service = lambda: DirtyBundleService()

        payload = self.ball._desktop_diagnostic_payload()

        self.assertEqual(payload["manifest_state"], "dirty")
        self.assertEqual(
            payload["asset_counts"],
            {"total": 0, "ready": 0, "pending": 0, "missing": 0},
        )

    def test_feedback_tones_are_semantic_and_distinct(self) -> None:
        menu = self.ball.quick_menu
        anchor = QRect(100, 100, 72, 72)
        available = QRect(0, 0, 800, 600)
        styles: dict[str, str] = {}

        for tone in ("progress", "success", "pending", "duplicate", "error"):
            menu.show_feedback(tone, tone, anchor, available)
            styles[tone] = self.ball.intake_card.status.styleSheet()
            self.assertEqual(self.ball.intake_card.status.property("feedbackTone"), tone)

        self.assertEqual(len(set(styles.values())), 5)
        self.assertIn("#4E5F3D", styles["success"])
        self.assertIn("#A67624", styles["pending"])
        self.assertIn("#625B4C", styles["duplicate"])
        self.assertIn("#9B4C37", styles["error"])

    def test_material_choices_use_groups_and_three_by_two_role_grid(self) -> None:
        panel = self.ball.material_panel
        panel._refresh_role_buttons("hero_image")
        self.assertTrue(panel.role_buttons["hero_image"].isChecked())
        panel._refresh_gif_role_buttons("reaction")
        self.assertTrue(panel.gif_role_buttons["reaction"].isChecked())
        self.assertFalse(panel.role_buttons["hero_image"].isChecked())

        positions = {
            panel.role_row.layout().getItemPosition(
                panel.role_row.layout().indexOf(button)
            )[:2]
            for button in panel.role_buttons.values()
        }
        self.assertEqual(
            positions,
            {(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)},
        )
        self.assertTrue(panel.filter_buttons["all"].isChecked())
        self.assertTrue(panel.scope_buttons["latest"].isChecked())
        self.assertIn("QLineEdit:focus", panel.search_input.styleSheet())

    def test_english_asset_status_casing_is_stable(self) -> None:
        app_gui.set_ui_language("en")
        self.ball.material_panel.retranslate()

        self.assertEqual(
            self.ball.material_panel.filter_buttons["ready"].text(),
            "Ready",
        )
        self.assertEqual(
            self.ball.material_panel._bundle_status_label("ready"),
            "Ready",
        )
        self.assertEqual(
            self.ball.material_panel._bundle_status_label("pending"),
            "Pending",
        )

    def test_enlarged_english_role_labels_reflow_without_clipping(self) -> None:
        app_gui.set_ui_language("en")
        self.ball._handle_quick_menu_action("assets")
        panel = self.ball.material_panel
        panel.retranslate()
        enlarged = QFont(panel.font())
        enlarged.setPointSizeF(max(18.0, enlarged.pointSizeF() * 1.75))
        for button in panel.role_buttons.values():
            button.setFont(enlarged)
        panel.role_row.show()
        panel.role_row.layout().activate()
        self.app.processEvents()

        for button in panel.role_buttons.values():
            required = button.fontMetrics().horizontalAdvance(button.text()) + 4
            self.assertGreaterEqual(button.width(), required, button.text())
        columns = {
            panel.role_row.layout().getItemPosition(
                panel.role_row.layout().indexOf(button)
            )[1]
            for button in panel.role_buttons.values()
        }
        self.assertLessEqual(max(columns), 2)

    def test_api_key_never_enters_gui_state_when_credential_store_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.ball._gui_state_path = Path(tmp) / "gui_state.json"
            self.ball.quick_menu.ai_api_base_input.setText("https://vision.example/v1")
            self.ball.quick_menu.ai_api_model_input.setText("vision-model")
            self.ball.quick_menu.ai_api_key_input.setText("session-secret")
            toasts: list[tuple[str, str]] = []
            self.ball.show_toast = lambda message, tone="success": toasts.append((message, tone))

            with patch.object(app_gui.SystemCredentialStore, "set", return_value=False):
                self.ball._save_api_provider()

            payload = json.loads(self.ball._gui_state_path.read_text(encoding="utf-8"))
            serialized = json.dumps(payload)
            self.assertNotIn("session-secret", serialized)
            self.assertEqual(payload["ai_provider"], "api")
            self.assertEqual(payload["ai_api_authorized_host"], "vision.example")
            self.assertFalse(payload["ai_api_key_present"])
            self.assertEqual(self.ball._session_api_key, "session-secret")
            self.assertTrue(
                any(tone == "success" and ("本次" in message or "session" in message) for message, tone in toasts)
            )

    def test_agent_primary_handoff_uses_resolved_latest_batch(self) -> None:
        bundle = {
            "id": "hero",
            "theme_id": "generic",
            "type": "image",
            "role": "hero_image",
            "status": "ready",
            "sha256": "a" * 64,
            "url": "/static/generic/images/hero.png",
            "access": "manifest_static",
            "source_key": "generic/images/hero.png",
        }

        class FakeBundleService:
            def get_latest_batch(self):
                return {"id": "batch-latest"}

            def list_bundles(self, **kwargs):
                self.kwargs = kwargs
                return [bundle]

        QApplication.clipboard().clear()
        with patch.object(app_gui, "BundleService", return_value=FakeBundleService()):
            self.ball._handle_quick_menu_action("latest_handoff")

        payload = json.loads(QApplication.clipboard().text())
        self.assertEqual(payload["batch_id"], "batch-latest")
        self.assertEqual(payload["assets"][0]["id"], "hero")
        self.assertIn('batch_id="latest"', self.ball.material_panel._agent_recipe_text())

    def test_grass_click_closes_management(self) -> None:
        for page in ("assets", "agent", "settings"):
            with self.subTest(page=page):
                self.ball._handle_quick_menu_action(page)
                self.assertTrue(self.ball.quick_menu.isVisible())
                self.ball._toggle_quick_menu()
                self.assertFalse(self.ball.quick_menu.isVisible())
                self.assertFalse(self.ball.intake_card.isVisible())


    def test_management_reopen_has_no_delayed_close(self) -> None:
        self.ball._handle_quick_menu_action("assets")
        self.ball._toggle_quick_menu()
        self.ball._handle_quick_menu_action("agent")
        QTest.qWait(210)
        self.assertTrue(self.ball.quick_menu.isVisible())
        self.assertEqual(self.ball.quick_menu.current_page(), "agent")


if __name__ == "__main__":
    unittest.main()
