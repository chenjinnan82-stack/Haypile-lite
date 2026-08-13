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
    from PySide6.QtTest import QSignalSpy, QTest
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
        self.ball = app_gui.HaypileFloatingBall()
        self.ball._available_geometry = lambda: QRect(0, 0, 1000, 760)
        self.ball.move(180, 260)
        self.app.processEvents()

    def tearDown(self) -> None:
        self.ball.close()
        self.app.processEvents()
        app_gui.set_ui_language("auto")

    def test_three_layer_hub_keeps_grass_origin_and_fixed_drawer_shell(self) -> None:
        origin = self.ball.pos()
        self.ball._toggle_quick_menu()
        self.assertEqual(
            {action for action, _icon, _label in self.ball.quick_menu.actions},
            {"assets", "agent", "settings"},
        )
        self.assertFalse(self.ball.quick_menu._hide_timer.isActive())

        geometries = []
        for page in ("assets", "agent", "settings"):
            self.ball._handle_quick_menu_action(page)
            self.app.processEvents()
            self.assertEqual(self.ball.quick_menu.current_page(), page)
            self.assertTrue(self.ball.quick_menu.is_drawer_open())
            geometries.append(self.ball.quick_menu.drawer_shell.size())
            self.assertEqual(self.ball.pos(), origin)

        self.assertTrue(all(size == geometries[0] for size in geometries))
        self.assertEqual(geometries[0].height(), 392)
        self.assertGreaterEqual(geometries[0].width(), 408)
        self.assertTrue(self.ball.material_panel._embedded)
        self.assertIsNone(self.ball.material_panel.confirmation_preview)
        self.assertTrue(self.ball.material_panel.copy_ready_button.isHidden())
        self.assertFalse(self.ball.material_panel.scope_buttons["latest"].isHidden())

        self.ball.quick_menu.leaveEvent(QEvent(QEvent.Type.Leave))
        QTest.qWait(50)
        self.assertTrue(self.ball.quick_menu.isVisible())
        self.assertFalse(self.ball.quick_menu._hide_timer.isActive())

    def test_opening_ring_does_not_probe_local_ai(self) -> None:
        self.ball.ai_enabled = True
        self.ball._ai_model_state = lambda: self.fail("opening the ring must not probe Ollama")

        self.ball._toggle_quick_menu()

        self.assertTrue(self.ball.quick_menu.isVisible())

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
                initial_state={"sound_enabled": stored_value},
            )
            try:
                self.assertFalse(restored.sound_enabled)
                self.assertFalse(restored._sound_feedback.enabled)
                self.assertFalse(restored.quick_menu.sound_button.isChecked())
            finally:
                restored.close()
                self.app.processEvents()

    def test_ring_and_assets_drawer_respond_before_deferred_refresh(self) -> None:
        menu = self.ball.quick_menu
        refresh_calls = []
        menu.material_panel.refresh = lambda: refresh_calls.append("refresh")

        self.ball._toggle_quick_menu()
        self.assertEqual(menu._fade_animation.duration(), 125)
        self.assertEqual(menu._fade_animation.startValue(), 0.45)

        self.ball._handle_quick_menu_action("assets")
        self.assertTrue(menu.drawer_shell.isVisible())
        self.assertEqual(refresh_calls, [])
        gap = menu._drawer_global_rect.left() - self.ball._ball_anchor_rect().right()
        self.assertGreater(gap, 0)
        self.assertLessEqual(gap, menu.CONNECTOR_REACH)

        self.app.processEvents()
        self.assertEqual(refresh_calls, ["refresh"])

    def test_edge_layout_mirrors_drawer_without_moving_grass(self) -> None:
        self.ball.move(918, 280)
        origin = self.ball.pos()
        self.ball._handle_quick_menu_action("settings")
        self.app.processEvents()

        self.assertEqual(self.ball.pos(), origin)
        self.assertEqual(self.ball.quick_menu._drawer_side, "left")
        drawer = self.ball.quick_menu._drawer_global_rect
        self.assertGreaterEqual(drawer.left(), 0)
        self.assertLessEqual(drawer.right(), 999)
        self.assertGreaterEqual(drawer.width(), 408)
        track_global = self.ball.quick_menu.frameGeometry().topLeft() + self.ball.quick_menu._track_center.toPoint()
        self.assertLessEqual((track_global - self.ball._ball_anchor_rect().center()).manhattanLength(), 1)
        menu_origin = self.ball.quick_menu.frameGeometry().topLeft()
        for action, _icon, _label in self.ball.quick_menu.actions:
            slot = self.ball.quick_menu._slot_rect(action).toAlignedRect().translated(menu_origin)
            label = self.ball.quick_menu._label_rect(action).toAlignedRect().translated(menu_origin)
            self.assertFalse(drawer.intersects(slot), action)
            self.assertFalse(drawer.intersects(label), action)

    def test_selected_page_rotates_to_drawer_connector(self) -> None:
        self.ball.move(918, 280)
        menu = self.ball.quick_menu
        menu.show_attached(self.ball._ball_anchor_rect(), self.ball._available_geometry())

        positions = {}
        for page in ("agent", "settings", "assets"):
            menu.open_drawer(page)
            self.app.processEvents()
            selected = menu._slot_rect(page).center()
            positions[page] = selected
            self.assertLess(selected.x(), menu._track_center.x())
            self.assertLessEqual(abs(selected.y() - menu._track_center.y()), 1)

        self.assertEqual(len({(point.x(), point.y()) for point in positions.values()}), 1)

    def test_ring_rotation_animates_and_low_power_snaps(self) -> None:
        self.ball.move(918, 280)
        menu = self.ball.quick_menu
        menu.show_attached(self.ball._ball_anchor_rect(), self.ball._available_geometry())
        menu.open_drawer("agent")

        with patch.object(menu, "_animations_enabled", return_value=True):
            finished = QSignalSpy(menu._ring_rotation.finished)
            menu.open_drawer("settings")
            self.assertEqual(
                menu._ring_rotation.state(),
                app_gui.QVariantAnimation.State.Running,
            )
            self.assertTrue(finished.wait(1000))
            selected = menu._slot_rect("settings").center()
            self.assertLessEqual(abs(selected.y() - menu._track_center.y()), 1)

            menu._low_power_enabled = True
            menu.open_drawer("assets")
            self.assertEqual(
                menu._ring_rotation.state(),
                app_gui.QVariantAnimation.State.Stopped,
            )
            selected = menu._slot_rect("assets").center()
            self.assertLessEqual(abs(selected.y() - menu._track_center.y()), 1)

    def test_language_and_low_power_persist_without_losing_ai_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.ball._gui_state_path = Path(tmp) / "gui_state.json"
            self.ball.ai_enabled = True
            self.ball._ai_preference = True

            self.ball._set_language_mode("en")
            self.assertEqual(self.ball.language_mode, "en")
            self.assertEqual(self.ball.quick_menu.action_tooltips["assets"], "Assets")

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

    def test_ring_actions_are_native_accessible_buttons_and_follow_rotation(self) -> None:
        menu = self.ball.quick_menu
        self.ball._toggle_quick_menu()
        self.app.processEvents()

        self.assertEqual(set(menu._ring_action_buttons), {"assets", "agent", "settings"})
        self.assertFalse(
            any(button.hasFocus() for button in menu._ring_action_buttons.values())
        )
        QTest.keyClick(menu, Qt.Key.Key_Tab)
        self.assertTrue(menu._ring_action_buttons["assets"].hasFocus())
        for action, button in menu._ring_action_buttons.items():
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.StrongFocus)
            self.assertTrue(button.accessibleName())
            self.assertTrue(button.accessibleDescription())
            expected = (
                menu._slot_rect(action)
                .united(menu._label_rect(action))
                .translated(menu._content_shift)
                .toAlignedRect()
            )
            self.assertEqual(button.geometry(), expected)

        triggered: list[str] = []
        menu.set_action_handler(triggered.append)
        menu._ring_action_buttons["settings"].setFocus()
        QTest.keyClick(menu._ring_action_buttons["settings"], Qt.Key.Key_Return)
        self.assertEqual(triggered, ["settings"])

        menu.open_drawer("settings")
        QTest.qWait(190)
        self.app.processEvents()
        expected = (
            menu._slot_rect("settings")
            .united(menu._label_rect("settings"))
            .translated(menu._content_shift)
            .toAlignedRect()
        )
        self.assertEqual(menu._ring_action_buttons["settings"].geometry(), expected)

        menu.close_drawer()
        QTest.qWait(170)
        menu.show_feedback(
            "Working",
            "progress",
            self.ball._ball_anchor_rect(),
            self.ball._available_geometry(),
        )
        self.assertTrue(
            all(button.isHidden() for button in menu._ring_action_buttons.values())
        )

    def test_ring_reopen_keeps_neutral_mouse_state(self) -> None:
        menu = self.ball.quick_menu
        self.ball._toggle_quick_menu()
        self.app.processEvents()
        QTest.keyClick(menu, Qt.Key.Key_Tab)
        self.assertTrue(menu._ring_action_buttons["assets"].hasFocus())

        QTest.keyClick(menu, Qt.Key.Key_Escape)
        QTest.qWait(190)
        self.app.processEvents()
        self.assertFalse(menu.isVisible())

        self.ball._toggle_quick_menu()
        self.app.processEvents()
        self.assertFalse(
            any(button.hasFocus() for button in menu._ring_action_buttons.values())
        )

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
            styles[tone] = menu.feedback_label.styleSheet()
            self.assertEqual(menu.feedback_label.property("feedbackTone"), tone)

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
        self.assertLessEqual(max(columns), 1)

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

    def test_grass_click_closes_drawer_and_ring_together(self) -> None:
        for page in ("assets", "agent", "settings"):
            with self.subTest(page=page):
                self.ball._toggle_quick_menu()
                self.ball._handle_quick_menu_action(page)
                menu = self.ball.quick_menu
                self.assertTrue(menu.is_drawer_open())

                grass_pos = menu.mapFromGlobal(self.ball._ball_anchor_rect().center())
                QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=grass_pos)
                QTest.qWait(190)
                self.app.processEvents()

                self.assertFalse(menu.isVisible())
                self.assertFalse(menu.drawer_shell.isVisible())

    def test_drawer_and_rapid_page_motion_settle_before_close(self) -> None:
        previous_platform = os.environ.get("QT_QPA_PLATFORM")
        os.environ["QT_QPA_PLATFORM"] = "animation-test"
        try:
            menu = self.ball.quick_menu
            drawer_opened = QSignalSpy(menu._drawer_motion.finished)
            self.ball._handle_quick_menu_action("assets")
            self.assertEqual(menu._drawer_motion.duration(), 150)
            self.assertNotEqual(menu._drawer_motion.startValue(), menu._drawer_motion.endValue())
            self.assertTrue(drawer_opened.wait(1000))
            self.assertEqual(menu.drawer_shell.pos(), menu._drawer_motion.endValue())

            page_settled = QSignalSpy(menu._page_slide.finished)
            self.ball._handle_quick_menu_action("agent")
            self.ball._handle_quick_menu_action("settings")
            final_page_position = menu._page_slide.endValue()
            self.assertTrue(page_settled.wait(1000))
            self.assertEqual(menu.drawer_stack.pos(), final_page_position)

            menu_closed = QSignalSpy(menu._fade_animation.finished)
            self.ball._toggle_quick_menu()
            self.assertEqual(menu._drawer_motion.duration(), 150)
            self.assertTrue(menu_closed.wait(1000))
            self.app.processEvents()
            self.assertFalse(menu.isVisible())
            self.assertFalse(menu.drawer_shell.isVisible())
            self.assertFalse(menu._hide_finalize_timer.isActive())
        finally:
            if previous_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = previous_platform

    def test_reopening_during_close_invalidates_stale_hide_completion(self) -> None:
        previous_platform = os.environ.get("QT_QPA_PLATFORM")
        os.environ["QT_QPA_PLATFORM"] = "animation-test"
        try:
            self.ball._handle_quick_menu_action("assets")
            menu = self.ball.quick_menu
            self.ball._toggle_quick_menu()
            self.assertTrue(menu._hide_finalize_timer.isActive())

            self.ball._handle_quick_menu_action("agent")
            QTest.qWait(210)

            self.assertTrue(menu.isVisible())
            self.assertTrue(menu.is_drawer_open())
            self.assertEqual(menu.current_page(), "agent")
            self.assertFalse(menu._hide_finalize_timer.isActive())
            self.assertFalse(menu._hide_after_slide)
        finally:
            if previous_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = previous_platform


if __name__ == "__main__":
    unittest.main()
