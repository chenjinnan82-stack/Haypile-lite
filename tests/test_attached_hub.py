from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, QPoint, QRect, Qt
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
        self.assertTrue(self.ball.quick_ring.isVisible())
        self.assertFalse(self.ball.intake_card.isVisible())
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
        self.assertTrue(self.ball.quick_ring.isVisible())

    def test_default_ai_is_off_and_explicit_saved_choices_survive(self) -> None:
        with patch.dict(os.environ):
            for key in list(os.environ):
                if key.upper() in {"VISION_CLASSIFIER_ENABLED", "HAYPILE_LOW_POWER_MODE"}:
                    os.environ.pop(key)
            settings = app_gui.Settings(_env_file=None, STORAGE_DIR=Path(self.temp.name) / "fresh")
        self.assertFalse(settings.VISION_CLASSIFIER_ENABLED)
        self.assertFalse(app_gui.StyleClassifier(settings=settings).enabled)
        for state, mode, enabled in (
            ({}, "off", False),
            ({"ai_enabled": True}, "local", True),
            ({"ai_provider": "local", "ai_enabled": True}, "local", True),
            ({"ai_provider": "off", "ai_enabled": True}, "off", False),
            ({"ai_provider": "local", "ai_enabled": True, "low_power_enabled": True}, "local", False),
        ):
            with self.subTest(state=state), patch.object(app_gui.httpx, "get", side_effect=AssertionError("no model probe")):
                ball = app_gui.HaypileFloatingBall(settings=settings, initial_state=state)
                try:
                    self.assertEqual(ball.ai_provider_mode, mode)
                    self.assertEqual(ball.ai_enabled, enabled)
                    self.assertEqual(ball._current_ai_provider_config().enabled, enabled)
                finally:
                    ball.close()
                    self.app.processEvents()

    def test_ai_off_hides_retry_and_ignores_stale_click(self) -> None:
        panel = self.ball.material_panel
        bundle = {"id": "current", "type": "image", "status": "pending", "source_key": "image.png"}
        panel._selected_bundle_id = "current"
        panel.detail_label.setText("Saved asset")
        with patch.object(panel, "_get_bundle_safely", return_value=bundle), patch.object(app_gui, "AIRefreshWorker") as worker:
            self.ball._set_ai_provider_mode("local")
            self.assertFalse(panel.retry_ai_button.isHidden())
            with patch.object(panel, "ai_refresh_worker") as active_worker:
                self.ball._set_ai_provider_mode("off")
                active_worker.requestInterruption.assert_called_once_with()
                self.assertIs(panel.ai_refresh_worker, active_worker)
            self.assertTrue(panel.retry_ai_button.isHidden())
            panel._retry_ai_classification()
            worker.assert_not_called()
            self.assertEqual(panel.detail_label.text(), "Saved asset")

    def test_opening_ai_settings_does_not_probe_model(self) -> None:
        self.ball._set_ai_provider_mode("local")
        with patch.object(app_gui.httpx, "get", side_effect=AssertionError("explicit check only")):
            self.ball._handle_quick_menu_action("ai_setup")
            self.ball.material_panel.refresh()
        self.assertEqual(self.ball.quick_menu.current_page(), "ai")

    def test_switching_ai_provider_cancels_owned_work_before_using_new_provider(self) -> None:
        for old, new in (("local", "api"), ("api", "local"), ("api", "off")):
            with self.subTest(old=old, new=new), \
                 patch.object(self.ball, "ai_batch_worker") as batch, \
                 patch.object(self.ball.material_panel, "ai_refresh_worker") as single:
                self.ball.ai_provider_mode = old
                self.ball.ai_enabled = True
                self.ball._ai_batch_queue = ["queued-under-old-provider"]
                self.ball._set_ai_provider_mode(new)
                batch.requestInterruption.assert_called_once_with()
                single.requestInterruption.assert_called_once_with()
                self.assertIs(self.ball.ai_batch_worker, batch)
                self.assertIs(self.ball.material_panel.ai_refresh_worker, single)
                self.assertEqual(self.ball._ai_batch_queue, [])

    def test_saving_valid_api_configuration_cancels_old_work_but_invalid_input_does_not(self) -> None:
        menu = self.ball.quick_menu
        with patch.object(self.ball, "ai_batch_worker") as batch, \
             patch.object(self.ball.material_panel, "ai_refresh_worker") as single, \
             patch.object(app_gui.SystemCredentialStore, "set", return_value=False):
            self.ball._ai_batch_queue = ["old"]
            menu.ai_api_base_input.setText("invalid")
            self.ball._save_api_provider()
            batch.requestInterruption.assert_not_called()
            self.assertEqual(self.ball._ai_batch_queue, ["old"])
            menu.ai_api_base_input.setText("https://vision.example/v1")
            menu.ai_api_model_input.setText("vision-model")
            menu.ai_api_key_input.setText("session-secret")
            self.ball._save_api_provider()
            batch.requestInterruption.assert_called_once_with()
            single.requestInterruption.assert_called_once_with()
            self.assertEqual(self.ball._ai_batch_queue, [])
            self.assertIs(self.ball.ai_batch_worker, batch)

    def test_single_ai_initialization_failure_restores_retry_and_keeps_ui_usable(self) -> None:
        self.ball._set_ai_provider_mode("local")
        panel = self.ball.material_panel
        panel._selected_bundle_id = "image"
        bundle = {"id": "image", "type": "image", "role": "unknown", "theme_id": "generic",
                  "status": "pending", "source_key": "image.png", "url": "/static/image.png",
                  "sha256": "a" * 64, "access": "manifest_static"}
        notices = []
        panel.set_toast_handler(lambda message, tone="success": notices.append((message, tone)))
        with patch.object(panel, "_get_bundle_safely", return_value=bundle), \
             patch.object(app_gui, "AIRefreshWorker", side_effect=ValueError("api_host_not_authorized")):
            panel._retry_ai_classification()
        self.assertIsNone(panel.ai_refresh_worker)
        self.assertTrue(panel.retry_ai_button.isEnabled())
        self.assertIn("素材已保存", notices[-1][0])
        self.assertEqual(notices[-1][1], "pending")
        self.ball._handle_quick_menu_action("assets")
        self.assertEqual(self.ball.quick_menu.current_page(), "assets")

    def test_batch_ai_initialization_failure_clears_queue_without_blocking_core(self) -> None:
        self.ball._set_ai_provider_mode("local")
        notices = []
        self.ball.show_toast = lambda message, tone="success": notices.append((message, tone))
        bundle = {"id": "image", "type": "image", "status": "pending", "source_key": "image.png"}
        with patch.object(self.ball, "_bundle_service") as service, \
             patch.object(app_gui, "AIBatchWorker", side_effect=OSError("AI metadata unavailable")):
            service.return_value.list_bundles.return_value = [bundle]
            self.ball._ai_batch_queue = ["first", "second"]
            self.ball._start_next_ai_batch()
            notices.clear()
            service.return_value.get_latest_batch.return_value = {"id": "latest"}
            self.ball._retry_latest_ai_batch()
        self.assertIsNone(self.ball.ai_batch_worker)
        self.assertEqual(self.ball._ai_batch_queue, [])
        self.assertIn("素材已保存", notices[-1][0])
        self.assertEqual(notices[-1][1], "pending")
        self.assertEqual(len(notices), 1)
        self.ball._handle_quick_menu_action("assets")
        self.assertEqual(self.ball.quick_menu.current_page(), "assets")

    def test_model_inventory_is_not_a_capability_test_and_handles_invalid_responses(self) -> None:
        import httpx
        self.ball._set_ai_provider_mode("local")
        model = self.settings.VISION_CLASSIFIER_MODEL
        for code, payload, expected in (
            (200, {"models": [{"name": model}]}, "installed"),
            (200, {"models": []}, "missing"),
            (200, [], "offline"),
            (200, {"models": None}, "offline"),
            (503, {"models": [{"name": model}]}, "offline"),
        ):
            with self.subTest(payload=payload), patch.object(app_gui.httpx, "get", return_value=httpx.Response(
                code, json=payload, request=httpx.Request("GET", "http://127.0.0.1:11434/api/tags"),
            )):
                self.assertEqual(self.ball._ai_model_state()[0], expected)
        self.ball.ai_provider_mode = "api"
        self.ball._session_api_key = "session-only-test"
        self.assertEqual(self.ball._ai_model_state()[0], "configured")

    def test_ai_installation_check_does_not_enable_or_write_preferences(self) -> None:
        state_path = self.ball._gui_state_path
        before = state_path.read_bytes() if state_path.exists() else None
        with patch.object(self.ball, "_ai_model_state", return_value=("installed", "Installed; capability unverified")):
            for low_power in (False, True):
                self.ball.low_power_enabled = low_power
                self.ball._recheck_ai_setup()
                self.assertFalse(self.ball.ai_enabled)
                self.assertEqual(self.ball.ai_provider_mode, "off")
        self.assertEqual(state_path.read_bytes() if state_path.exists() else None, before)

    def test_standalone_panel_restores_local_opt_in_without_global_default(self) -> None:
        panel = self.ball.material_panel
        panel._ai_enabled_callback = None
        self.settings.INDEX_DIR.mkdir(parents=True, exist_ok=True)
        path = self.settings.INDEX_DIR / "gui_state.json"
        for state, expected in (
            ({}, False), ({"ai_enabled": True}, True),
            ({"ai_provider": "off", "ai_enabled": True}, False),
            ({"ai_provider": "api", "ai_enabled": True}, False),
            ({"ai_enabled": True, "low_power_enabled": True}, False),
        ):
            with self.subTest(state=state):
                path.write_text(json.dumps(state))
                self.assertEqual(panel._panel_ai_enabled(), expected)

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

    def test_local_html_explains_next_step_while_web_link_uses_remote_route(self) -> None:
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent
        app_gui.set_ui_language("zh")
        local_page = Path(self.temp.name) / "browser.html"
        local_page.write_text('<a href="https://www.joshwcomeau.com/">website</a>')
        worker = app_gui.IngestWorker([local_page], self.settings.ASSETS_DIR, settings=self.settings)
        messages = []
        worker.finished_signal.connect(lambda message, ok: messages.append((message, ok)))
        worker._run_ingest()
        self.assertFalse(messages[-1][1])
        self.assertIn("本地网页文件", messages[-1][0])
        self.assertIn("地址栏", messages[-1][0])
        self.assertEqual(worker.result.accepted_count, 0)
        self.ball._last_remote_urls = ["https://previous.example/"]
        self.ball._start_worker([local_page])
        self.assertTrue(self.ball.worker.wait(3000))
        self.app.processEvents()
        self.assertEqual(self.ball._last_remote_urls, [])
        self.assertFalse(self.ball.intake_card.store_button.isVisible())
        mime = QMimeData()
        mime.setUrls([QUrl("https://www.joshwcomeau.com/")])
        mime.setText("https://www.joshwcomeau.com/")
        drop = QDropEvent(QPointF(36, 36), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        with patch.object(self.ball, "_start_remote_download_worker") as fetch:
            self.ball.dropEvent(drop)
            fetch.assert_called_once_with(["https://www.joshwcomeau.com/"], [])
        self.assertTrue(drop.isAccepted())

    def test_ring_has_native_hole_and_keeps_all_actions_on_screen(self) -> None:
        self.ball._available_geometry = lambda: QRect(0, 0, 960, 640)
        for x, y in ((0, 0), (888, 0), (0, 568), (888, 568), (444, 284)):
            self.ball.move(x, y)
            origin = self.ball.geometry()
            self.ball.quick_ring.open()
            ring = self.ball.quick_ring
            center = ring.mapFromGlobal(self.ball._ball_anchor_rect().center())
            self.assertFalse(ring.mask().contains(center))
            self.assertLess(ring.mask().boundingRect().width(), 241)
            for button in ring.buttons.values():
                self.assertTrue(button.accessibleName())
                self.assertTrue(ring.mask().contains(button.geometry().center()))
                # Near screen corners the button rectangles overlap, but circles must not steal clicks.
                for dx, dy in ((-14, -14), (-14, 14), (14, -14), (14, 14)):
                    point = button.geometry().center() + QPoint(dx, dy)
                    self.assertIs(ring.childAt(point), button)
                self.assertTrue(self.ball._available_geometry().contains(
                    QRect(ring.mapToGlobal(button.pos()), button.size())))
            self.assertEqual(self.ball.geometry(), origin)
            QTest.keyClick(ring.buttons["recent"], Qt.Key.Key_Escape)
            self.assertFalse(ring.isVisible())

    def test_ring_outside_click_closes_without_consuming_the_button_action(self) -> None:
        from PySide6.QtWidgets import QPushButton
        outside = QPushButton("Outside")
        clicks = []
        outside.clicked.connect(lambda: clicks.append(True))
        outside.show()
        try:
            self.ball._toggle_quick_menu()
            QTest.mouseClick(outside, Qt.MouseButton.LeftButton)
            self.assertFalse(self.ball.quick_ring.isVisible())
            self.assertFalse(self.ball.quick_ring.timer.isActive())
            self.assertEqual(clicks, [True])
        finally:
            outside.close()

    def test_ring_action_opens_recent_card_and_closes_ring(self) -> None:
        self.ball._toggle_quick_menu()
        QTest.keyClick(self.ball.quick_ring.buttons["recent"], Qt.Key.Key_Space)
        self.assertFalse(self.ball.quick_ring.isVisible())
        self.assertTrue(self.ball.intake_card.isVisible())
        self.assertEqual(self.ball.intake_card.mode, "recent")

    def test_panel_owns_shutdown_and_ignores_late_ai_display_updates(self) -> None:
        from unittest.mock import Mock
        panel = self.ball.material_panel
        worker = Mock()
        worker.isRunning.return_value = True
        panel.ai_refresh_worker = worker
        try:
            panel.begin_shutdown()
            worker.requestInterruption.assert_called_once()
            self.assertTrue(panel.shutdown_pending)
            worker.isRunning.return_value = False
            with patch.object(panel, "_toast_callback") as toast, \
                 patch.object(panel, "_get_bundle_safely") as read:
                panel._on_ai_refresh_finished("late-asset", "done", True)
            toast.assert_not_called()
            read.assert_not_called()
            worker.deleteLater.assert_called_once()
            self.assertFalse(panel.shutdown_pending)
        finally:
            panel.ai_refresh_worker = None

    def test_ai_result_does_not_release_worker_before_thread_exit(self) -> None:
        import threading
        import time
        from PySide6.QtCore import QThread, Signal
        gate = threading.Event()
        class DelayedWorker(QThread):
            finished_signal = Signal(str, str, bool)
            def run(self):
                self.finished_signal.emit("asset", "done", True)
                gate.wait(3)
        panel = self.ball.material_panel
        worker = DelayedWorker()
        panel.ai_refresh_worker = worker
        panel._ai_refresh_result = None
        worker.finished_signal.connect(panel._remember_ai_refresh_result)
        worker.finished.connect(panel._finish_ai_refresh)
        try:
            worker.start()
            deadline = time.monotonic() + 2
            while panel._ai_refresh_result is None and time.monotonic() < deadline:
                QTest.qWait(5)
            self.assertEqual(panel._ai_refresh_result, ("asset", "done", True))
            self.assertTrue(worker.isRunning())
            self.assertIs(panel.ai_refresh_worker, worker)
            panel.begin_shutdown()
            self.assertTrue(panel.shutdown_pending)
            gate.set()
            deadline = time.monotonic() + 2
            while panel.shutdown_pending and time.monotonic() < deadline:
                QTest.qWait(5)
            self.assertFalse(panel.shutdown_pending)
        finally:
            gate.set()
            if panel.ai_refresh_worker is not None:
                worker.wait(4000)
                panel.poll_shutdown()

    def test_ai_retry_waits_for_queued_completion_and_refuses_shutdown(self) -> None:
        from PySide6.QtCore import QThread, Signal
        class CompletedWorker(QThread):
            finished_signal = Signal(str, str, bool)
            def run(self):
                self.finished_signal.emit("previous", "done", True)
        panel = self.ball.material_panel
        worker = CompletedWorker()
        panel.ai_refresh_worker = worker
        panel._ai_refresh_result = None
        panel._selected_bundle_id = "current"
        bundle = {"id": "current", "type": "image", "status": "ready", "source_key": "image.png"}
        worker.finished_signal.connect(panel._remember_ai_refresh_result)
        worker.finished.connect(panel._finish_ai_refresh)
        try:
            with patch.object(app_gui, "AIRefreshWorker") as factory, \
                 patch.object(panel, "_panel_ai_enabled", return_value=True), \
                 patch.object(panel, "_get_bundle_safely", return_value=bundle), \
                 patch.object(panel, "_toast_callback"):
                stale_worker = CompletedWorker()
                stale_worker.finished_signal.connect(panel._remember_ai_refresh_result)
                stale_worker.finished.connect(panel._finish_ai_refresh)
                stale_worker.finished_signal.emit("stale", "stale result", True)
                stale_worker.finished.emit()
                self.assertIsNone(panel._ai_refresh_result)
                self.assertIs(panel.ai_refresh_worker, worker)
                worker.start()
                self.assertTrue(worker.wait(2000))
                # The thread is stopped, but Qt has not delivered its queued completion yet.
                self.assertFalse(worker.isRunning())
                self.assertIs(panel.ai_refresh_worker, worker)
                panel._refresh_retry_ai_button(bundle)
                self.assertFalse(panel.retry_ai_button.isEnabled())
                panel._retry_ai_classification()
                factory.assert_not_called()
                self.assertIs(panel.ai_refresh_worker, worker)
                self.app.processEvents()
                self.assertIsNone(panel.ai_refresh_worker)
                panel.begin_shutdown()
                panel._refresh_retry_ai_button(bundle)
                self.assertFalse(panel.retry_ai_button.isEnabled())
                panel._retry_ai_classification()
                factory.assert_not_called()
        finally:
            if panel.ai_refresh_worker is not None:
                worker.wait(2000)
                panel.poll_shutdown()

    def test_ai_batch_queue_keeps_owner_until_thread_exit(self) -> None:
        import threading
        import time
        from types import SimpleNamespace
        from PySide6.QtCore import QThread, Signal
        gate = threading.Event()
        created = []
        class DelayedBatch(QThread):
            finished_signal = Signal(str, str, str)
            def __init__(self, batch_id, *_args, **_kwargs):
                super().__init__()
                self.batch_id = batch_id
                created.append(self)
            def run(self):
                self.finished_signal.emit(self.batch_id, "done", "success")
                if self.batch_id == "first":
                    gate.wait(3)
        ball = self.ball
        ball.ai_enabled = True
        ball.low_power_enabled = False
        service = SimpleNamespace(list_bundles=lambda **kwargs: [{"source_key": "image.png"}])
        try:
            with patch.object(app_gui, "AIBatchWorker", DelayedBatch), \
                 patch.object(ball, "_bundle_service", return_value=service), \
                 patch.object(ball, "_refresh_pending_badge"):
                ball._enqueue_ai_batch("first")
                ball._enqueue_ai_batch("second")
                deadline = time.monotonic() + 2
                while ball._ai_batch_result is None and time.monotonic() < deadline:
                    QTest.qWait(5)
                self.assertEqual(ball._ai_batch_result, ("first", "done", "success"))
                self.assertEqual(len(created), 1)
                self.assertTrue(created[0].isRunning())
                self.assertIs(ball.ai_batch_worker, created[0])
                self.assertEqual(ball._ai_batch_queue, ["second"])
                gate.set()
                deadline = time.monotonic() + 2
                while (ball.ai_batch_worker is not None or ball._ai_batch_queue) and time.monotonic() < deadline:
                    QTest.qWait(5)
                self.assertEqual([w.batch_id for w in created], ["first", "second"])
                self.assertIsNone(ball.ai_batch_worker)
                self.assertEqual(ball._ai_batch_queue, [])
        finally:
            gate.set()
            ball._ai_batch_queue.clear()
            if ball.ai_batch_worker is not None:
                ball.ai_batch_worker.wait(4000)
                ball._shutdown_ai_batch_worker()

    def test_ring_works_with_plain_widget_without_card_or_application_controller(self) -> None:
        from PySide6.QtWidgets import QWidget
        from app.gui.quick_ring import QuickRing
        anchor = QWidget()
        anchor.setGeometry(200, 200, 72, 72)
        ring = QuickRing(anchor, lambda zh, en: en, anchor_geometry=anchor.frameGeometry,
                         available_geometry=lambda: QRect(0, 0, 1000, 760))
        actions = []
        ring.action_requested.connect(actions.append)
        try:
            with patch.object(ring.desktop_input, "buttons", return_value=(1, 0, 0)) as buttons, \
                 patch.object(ring.desktop_input, "escape", return_value=False) as escape, \
                 patch.object(app_gui.QCursor, "pos", return_value=QPoint(-100, -100)):
                for action, button in ring.buttons.items():
                    ring.open()
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                    self.assertEqual(actions[-1], action)
                    self.assertFalse(ring.isVisible())
                    self.assertFalse(ring.timer.isActive())
                self.assertEqual(actions, list(ring.buttons))
                ring.open()
                buttons.return_value = (2, 0, 0)
                ring._poll_outside()
                self.assertFalse(ring.isVisible())
                ring.open()
                escape.return_value = True
                ring._poll_outside()
                self.assertFalse(ring.isVisible())
                self.assertFalse(ring.timer.isActive())
        finally:
            ring.hide()
            ring.deleteLater()
            anchor.close()
            anchor.deleteLater()
            self.app.processEvents()

    def test_ring_keyboard_navigation_and_repeated_window_transitions(self) -> None:
        self.ball.show()
        self.app.processEvents()
        ring = self.ball.quick_ring
        for _round in range(3):
            for index, action in enumerate(("recent", "assets", "agent", "settings")):
                self.ball._toggle_quick_menu()
                self.app.processEvents()
                self.assertIs(self.app.focusWidget(), ring.buttons["recent"])
                for _ in range(index):
                    QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Tab)
                self.assertIs(self.app.focusWidget(), ring.buttons[action])
                QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Space)
                self.app.processEvents()
                self.assertFalse(ring.isVisible())
                self.assertFalse(ring.timer.isActive())
                card, manager = self.ball.intake_card, self.ball.quick_menu
                self.assertEqual(card.isVisible(), action == "recent")
                self.assertEqual(manager.isVisible(), action != "recent")
                if action != "recent":
                    self.assertEqual(manager.current_page(), action)
                QTest.keyClick(card if action == "recent" else manager, Qt.Key.Key_Escape)
                self.assertFalse(card.isVisible())
                self.assertFalse(manager.isVisible())

    def test_hover_reentry_resumes_opening_without_another_dwell(self) -> None:
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/animation.gif")])
        ball = self.ball
        ball._drag_hover = True
        ball._drop_visual_kind = "gif"
        ball._open_drop_target()
        gif_started = ball._gif_expand_started_at
        ball._drop_open_animation.stop()
        ball._set_drop_open_progress(0.6)
        ball.dragLeaveEvent(QDragLeaveEvent())
        enter = QDragEnterEvent(QPoint(36, 36), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        ball.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        self.assertFalse(ball._drag_prepare_timer.isActive())
        self.assertFalse(ball._collapse_timer.isActive())
        self.assertEqual(ball._drop_open_animation.endValue(), 1.0)
        self.assertAlmostEqual(ball._drop_open_progress, 0.6)
        self.assertEqual(ball._gif_expand_started_at, gif_started)

    def test_hover_animation_is_reused_and_repeated_open_does_not_restart(self) -> None:
        ball = self.ball
        ball._animate_drop_open(True)
        animation = ball._drop_open_animation
        animation_count = len(ball.findChildren(app_gui.QVariantAnimation))
        animation.setCurrentTime(90)
        ball._animate_drop_open(True)
        self.assertEqual(animation.currentTime(), 90)
        self.assertIs(ball._drop_open_animation, animation)
        for _ in range(30):
            before = ball._drop_open_progress
            ball._animate_drop_open(False)
            self.assertAlmostEqual(ball._drop_open_progress, before)
            animation.setCurrentTime(25)
            before = ball._drop_open_progress
            ball._animate_drop_open(True)
            self.assertAlmostEqual(ball._drop_open_progress, before)
            animation.setCurrentTime(90)
        self.assertIs(ball._drop_open_animation, animation)
        self.assertEqual(len(ball.findChildren(app_gui.QVariantAnimation)), animation_count)

    def test_repeated_suction_releases_finished_and_cancelled_animations(self) -> None:
        ball = self.ball
        ball.low_power_enabled = False
        ball._animate_drop_open(True)
        ball._drop_open_animation.stop()
        baseline = len(ball.findChildren(app_gui.QVariantAnimation))
        for kind in ("audio", "gif"):
            for completed in (False, True):
                with self.subTest(kind=kind, completed=completed):
                    for _ in range(20):
                        getattr(ball, f"_animate_{kind}_suction")()
                        animation = getattr(ball, f"_{kind}_suction_animation")
                        if completed:
                            animation.setCurrentTime(animation.duration())
                        else:
                            getattr(ball, f"_cancel_{kind}_suction")()
                        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.assertIsNone(getattr(ball, f"_{kind}_suction_animation"))
                    self.assertEqual(len(ball.findChildren(app_gui.QVariantAnimation)), baseline)

    def test_shutdown_wait_can_outlive_cancelled_suction_animation(self) -> None:
        for kind in ("audio", "gif"):
            with self.subTest(kind=kind):
                ball = app_gui.HaypileFloatingBall(settings=self.settings, managed_shutdown=True)
                try:
                    ball.low_power_enabled = False
                    getattr(ball, f"_animate_{kind}_suction")()
                    ball.shutdown()
                    # The real backend may still be stopping after Qt deletes the animation.
                    self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    ball._reset_drop_visual_state()
                    self.assertIsNone(getattr(ball, f"_{kind}_suction_animation"))
                finally:
                    ball._audio_suction_animation = None
                    ball._gif_suction_animation = None
                    ball.complete_shutdown()
                    self.app.processEvents()

    def test_collapse_waits_for_last_animation_frame(self) -> None:
        ball = self.ball
        origin = ball.geometry()
        ball._drag_hover = True
        ball._open_drop_target()
        animation = ball._drop_open_animation
        animation.setCurrentTime(animation.duration())
        ball._drag_hover = False
        ball._close_drop_target()
        animation.setCurrentTime(80)
        # A timer can be delivered before the animation's last frame under load.
        ball._collapse_timer.timeout.emit()
        self.assertTrue(ball.is_expanded)
        self.assertGreater(ball._drop_open_progress, 0.0)
        animation.setCurrentTime(animation.duration())
        self.assertEqual(ball.geometry(), origin)
        self.assertEqual(ball._drop_open_progress, 0.0)
        self.assertFalse(ball._collapse_timer.isActive())

    def test_programmatic_gif_prepares_window_before_starting_animation(self) -> None:
        ball = self.ball
        start = ball._animate_drop_open
        def check_start(opened):
            self.assertTrue(ball.is_expanded)
            self.assertIsNotNone(ball._drop_anchor_global)
            start(opened)
        with patch.object(ball, "_animate_drop_open", side_effect=check_start):
            ball._begin_programmatic_gif_intake()

    def test_transparent_corner_press_does_not_start_drag_or_toggle_ring(self) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QMouseEvent
        ball = self.ball
        origin = ball.pos()
        with patch.object(ball, "_toggle_quick_menu") as toggle:
            press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(0, 0),
                               QPointF(ball.mapToGlobal(QPoint(0, 0))), Qt.MouseButton.LeftButton,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            ball.mousePressEvent(press)
            self.assertFalse(press.isAccepted())
            move = QMouseEvent(QEvent.Type.MouseMove, QPointF(30, 30),
                              QPointF(ball.mapToGlobal(QPoint(30, 30))), Qt.MouseButton.NoButton,
                              Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            ball.mouseMoveEvent(move)
            self.assertEqual(ball.pos(), origin)
            release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(30, 30),
                                 QPointF(ball.mapToGlobal(QPoint(30, 30))), Qt.MouseButton.LeftButton,
                                 Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
            ball.mouseReleaseEvent(release)
            toggle.assert_not_called()
            self.assertFalse(ball._pointer_press_owned)

    def test_hide_during_window_drag_allows_next_external_drop_hover(self) -> None:
        from PySide6.QtGui import QMouseEvent
        ball = self.ball
        ball.show()
        center = ball.rect().center()
        for kind, point in ((QEvent.Type.MouseButtonPress, center),
                            (QEvent.Type.MouseMove, center + QPoint(20, 20))):
            event = QMouseEvent(kind, point, ball.mapToGlobal(point),
                               Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress
                               else Qt.MouseButton.NoButton,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(ball, event)
        self.assertTrue(ball._window_drag_active)
        ball.hide()  # Native hiding can prevent the drag's release from reaching us.
        ball.show()
        self.app.processEvents()
        self.assertFalse(ball._pointer_press_owned)
        self.assertFalse(ball._window_drag_active)
        self.assertFalse(ball._drag_moved)
        self.assertTrue(ball._drag_velocity.isNull())
        ball._drag_hover = True
        with patch.object(ball, "_global_left_button_down", return_value=True):
            ball._poll_external_drag_candidate()
        self.assertTrue(ball._drag_hover)

    def test_double_click_toggles_once_per_release(self) -> None:
        from PySide6.QtGui import QMouseEvent
        center = self.ball.rect().center()
        with patch.object(self.ball, "_toggle_quick_menu") as toggle:
            for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                         QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonRelease):
                event = QMouseEvent(kind, center, self.ball.mapToGlobal(center), Qt.MouseButton.LeftButton,
                                    Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease
                                    else Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                QApplication.sendEvent(self.ball, event)
            self.assertEqual(toggle.call_count, 2)

    def test_management_cancels_hidden_web_search_and_ignores_late_result(self) -> None:
        incoming = self.settings.STORAGE_DIR / "incoming" / "browser"
        incoming.mkdir(parents=True, exist_ok=True)
        worker = app_gui.RemoteDownloadWorker(["https://example.com/"], incoming)
        self.ball.remote_worker = worker
        self.ball.intake_card.show_message("Loading", "progress")
        self.ball.intake_card.dismiss("outside")
        with patch.object(worker, "requestInterruption") as stop:
            self.ball._handle_quick_menu_action("settings")
            stop.assert_called_once()
        late_file = incoming / "late.png"
        late_file.write_bytes(b"late response")
        self.ball._on_remote_download_finished([late_file], "", True, [])
        self.assertTrue(self.ball.quick_menu.isVisible())
        self.assertFalse(self.ball.intake_card.isVisible())
        self.assertIsNone(self.ball.worker)
        self.assertFalse(late_file.exists())
        self.assertFalse(self.ball._remote_ingest_paths)

    def test_temp_cleanup_failure_does_not_strand_finished_ingest(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        incoming = self.settings.STORAGE_DIR / "incoming" / "browser"
        incoming.mkdir(parents=True, exist_ok=True)
        source = Path(self.temp.name) / "original.png"
        source.write_bytes(b"original")
        original_unlink = Path.unlink
        for blocked_sidecar in (False, True):
            with self.subTest(blocked_sidecar=blocked_sidecar):
                downloaded = incoming / "download.png"
                sidecar = app_gui.provenance_path_for(downloaded)
                other = incoming / "other.png"
                for path in (downloaded, sidecar, other):
                    path.write_bytes(b"temporary")
                blocked = sidecar if blocked_sidecar else downloaded
                def unlink(path, *args, **kwargs):
                    if path == blocked:
                        raise PermissionError("busy temporary file")
                    return original_unlink(path, *args, **kwargs)
                worker = SimpleNamespace(isRunning=lambda: False, result=None, deleteLater=Mock())
                self.ball.worker = worker
                self.ball._remote_ingest_paths = {downloaded, other}
                with patch.object(Path, "unlink", unlink), \
                     patch.object(self.ball, "_finalize_ingest_finish") as finish, \
                     self.assertLogs(app_gui.logger, level="WARNING"):
                    self.ball._on_ingest_finished("stored", True)
                worker.deleteLater.assert_called_once()
                finish.assert_called_once_with("stored", True, None)
                self.assertFalse(self.ball._ingest_busy())
                self.assertTrue(blocked.exists())
                self.assertFalse((downloaded if blocked_sidecar else sidecar).exists())
                self.assertFalse(other.exists())
                self.assertEqual(source.read_bytes(), b"original")
                blocked.unlink()

    def test_temp_cleanup_failure_does_not_block_shutdown(self) -> None:
        incoming = self.settings.STORAGE_DIR / "incoming" / "browser"
        incoming.mkdir(parents=True, exist_ok=True)
        downloaded = incoming / "locked.png"
        downloaded.write_bytes(b"temporary")
        self.ball._remote_ingest_paths.add(downloaded)
        original_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == downloaded:
                raise PermissionError("busy temporary file")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink), self.assertLogs(app_gui.logger, level="WARNING"):
            self.ball.shutdown()
        self.assertTrue(self.ball._shutdown_ready_emitted)
        self.assertTrue(self.ball._cleanup_done)
        self.assertFalse(self.ball._shutdown_timer.isActive())
        self.assertTrue(downloaded.exists())

    def test_busy_drop_preserves_the_active_web_search(self) -> None:
        from PySide6.QtCore import QMimeData, QPoint, QPointF, QUrl
        from PySide6.QtGui import QDragEnterEvent, QDropEvent
        worker = app_gui.RemoteDownloadWorker(["https://first.example/"], self.settings.STORAGE_DIR / "incoming")
        self.ball.remote_worker = worker
        self.ball.intake_card.show_message("Loading first page", "progress")
        mime = QMimeData()
        mime.setUrls([QUrl("https://second.example/")])
        with patch.object(worker, "requestInterruption") as stop, patch.object(self.ball, "_start_remote_download_worker") as start:
            enter = QDragEnterEvent(QPoint(36, 36), Qt.DropAction.CopyAction, mime,
                                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.ball.dragEnterEvent(enter)
            self.assertFalse(enter.isAccepted())
            drop = QDropEvent(QPointF(36, 36), Qt.DropAction.CopyAction, mime,
                              Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.ball.dropEvent(drop)
            self.assertFalse(drop.isAccepted())
            stop.assert_not_called()
            start.assert_not_called()
        self.assertFalse(self.ball._remote_request_cancelled)
        self.assertIs(self.ball.remote_worker, worker)
        self.assertTrue(self.ball.intake_card.isVisible())
        self.assertEqual(self.ball.intake_card.status.text(), "Loading first page")

    def test_unsupported_new_input_does_not_retry_a_previous_website(self) -> None:
        from PySide6.QtCore import QMimeData, QPointF
        from PySide6.QtGui import QDropEvent
        mime = QMimeData()
        mime.setText("not a media file or link")
        for entry in ("clipboard", "drop"):
            with self.subTest(entry=entry):
                self.ball._last_remote_urls = ["https://previous.example/"]
                if entry == "clipboard":
                    self.ball._ingest_clipboard_data(mime)
                else:
                    self.ball.dropEvent(QDropEvent(QPointF(36, 36), Qt.DropAction.CopyAction, mime,
                                                  Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
                self.assertEqual(self.ball._last_remote_urls, [])
                self.assertFalse(self.ball.intake_card.store_button.isVisible())

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
        self.ball._handle_quick_menu_action("recent")
        card = self.ball.intake_card
        self.app.processEvents()
        self.assertEqual(card.close_button.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertTrue(card.close_button.hasFocus())
        QTest.keyClick(card.close_button, Qt.Key.Key_Space)
        self.assertFalse(card.isVisible())

    def test_card_reopen_restores_keyboard_focus(self) -> None:
        self.ball._handle_quick_menu_action("recent")
        card = self.ball.intake_card
        QTest.keyClick(card, Qt.Key.Key_Escape)
        self.assertFalse(card.isVisible())
        self.ball._handle_quick_menu_action("recent")
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
            panel.collection_picker,
        )
        self.assertIs(
            panel.collection_picker.nextInFocusChain(),
            panel.new_collection_button,
        )
        self.assertIs(
            panel.retry_batch_button.nextInFocusChain(),
            panel.collection_name,
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
        self.assertIn("#7D581B", styles["pending"])
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

    def test_credential_exceptions_leave_core_ui_and_session_key_usable(self) -> None:
        menu = self.ball.quick_menu
        menu.ai_api_base_input.setText("https://vision.example/v1")
        menu.ai_api_model_input.setText("vision-model")
        self.ball.ai_api_authorized_host = "vision.example"
        with patch.object(app_gui.SystemCredentialStore, "get", side_effect=OSError("unavailable")), \
             patch.object(app_gui.SystemCredentialStore, "set", side_effect=OSError("unavailable")) as store:
            self.ball._save_api_provider()
            self.assertFalse(self.ball.ai_enabled)
            store.assert_not_called()
            menu.ai_api_key_input.setText("session-only-secret")
            self.ball._save_api_provider()
            self.assertTrue(self.ball.ai_enabled)
            self.assertEqual(self.ball._session_api_key, "session-only-secret")
            self.assertFalse(self.ball.ai_api_key_present)
            self.assertNotIn("session-only-secret", self.ball._gui_state_path.read_text())
            self.ball._handle_quick_menu_action("assets")
            self.assertEqual(menu.current_page(), "assets")

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
