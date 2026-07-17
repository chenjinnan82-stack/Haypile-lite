from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, QRect, Qt
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
        self.previous_start = app_gui.HaypileFloatingBall.start_api_server
        app_gui.HaypileFloatingBall.start_api_server = lambda self: None
        self.ball = app_gui.HaypileFloatingBall()
        self.ball._available_geometry = lambda: QRect(0, 0, 1000, 760)
        self.ball.move(180, 260)
        self.app.processEvents()

    def tearDown(self) -> None:
        self.ball.close()
        self.app.processEvents()
        app_gui.HaypileFloatingBall.start_api_server = self.previous_start
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

        self.ball.quick_menu.leaveEvent(QEvent(QEvent.Type.Leave))
        QTest.qWait(50)
        self.assertTrue(self.ball.quick_menu.isVisible())
        self.assertFalse(self.ball.quick_menu._hide_timer.isActive())

    def test_opening_ring_does_not_probe_local_ai(self) -> None:
        self.ball.ai_enabled = True
        self.ball._ai_model_state = lambda: self.fail("opening the ring must not probe Ollama")

        self.ball._toggle_quick_menu()

        self.assertTrue(self.ball.quick_menu.isVisible())

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
        self.assertLessEqual(gap, 14)

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
            self.ball._handle_quick_menu_action("assets")
            menu = self.ball.quick_menu
            self.assertEqual(menu._drawer_motion.duration(), 150)
            self.assertNotEqual(menu._drawer_motion.startValue(), menu._drawer_motion.endValue())
            QTest.qWait(170)
            self.assertEqual(menu.drawer_shell.pos(), menu._drawer_motion.endValue())

            self.ball._handle_quick_menu_action("agent")
            self.ball._handle_quick_menu_action("settings")
            final_page_position = menu._page_slide.endValue()
            QTest.qWait(180)
            self.assertEqual(menu.drawer_stack.pos(), final_page_position)

            self.ball._toggle_quick_menu()
            self.assertEqual(menu._drawer_motion.duration(), 150)
            QTest.qWait(210)
            self.assertFalse(menu.isVisible())
        finally:
            if previous_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = previous_platform


if __name__ == "__main__":
    unittest.main()
