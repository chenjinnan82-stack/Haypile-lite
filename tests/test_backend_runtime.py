from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from app.core.config import Settings
    from app.gui.backend_runtime import BackendRuntimeController
except ImportError as exc:  # pragma: no cover - optional desktop runtime
    QApplication = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


class FakeProcess:
    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI runtime unavailable: {_IMPORT_ERROR}")
class BackendRuntimeControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(
            STORAGE_DIR=root / "storage",
            LOG_DIR=root / "logs",
            HOST="127.0.0.1",
            PORT=18110,
        )
        self.controller = BackendRuntimeController(self.settings, root)
        self.previous_auto_start = os.environ.get("HAYPILE_GUI_ALLOW_BACKEND_START")
        os.environ.pop("HAYPILE_GUI_ALLOW_BACKEND_START", None)

    def tearDown(self) -> None:
        self.controller._timer.stop()
        self.controller._close_log()
        self.controller._process = None
        if self.previous_auto_start is None:
            os.environ.pop("HAYPILE_GUI_ALLOW_BACKEND_START", None)
        else:
            os.environ["HAYPILE_GUI_ALLOW_BACKEND_START"] = self.previous_auto_start
        self.tempdir.cleanup()

    def configured_response(
        self,
        *,
        pid: int = 12345,
        ready: bool = True,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "product": "haypile",
            "protocol_version": 1,
            "host": self.settings.HOST,
            "port": self.settings.PORT,
            "pid": pid,
            "ready": ready,
        }

    def test_reuses_matching_backend_without_taking_ownership(self) -> None:
        phases: list[str] = []
        self.controller.phase_changed.connect(phases.append)
        self.controller._probe_backend_response = self.configured_response
        self.controller._is_port_open = lambda _host, _port: self.fail(
            "matching IPC identity must win before the port probe"
        )

        with patch(
            "app.gui.backend_runtime.subprocess.Popen",
            side_effect=AssertionError("must not launch"),
        ):
            self.controller.start()

        self.assertEqual(self.controller.phase, "ready")
        self.assertFalse(self.controller.owns_process)
        self.assertTrue(self.controller.is_stopped)
        self.assertEqual(phases, ["ready"])

    def test_reports_port_conflict_and_disabled_auto_start(self) -> None:
        notices: list[tuple[str, object]] = []
        self.controller.notice.connect(
            lambda code, details: notices.append((code, details))
        )
        self.controller._probe_backend_response = lambda: None
        self.controller._is_port_open = lambda _host, _port: True

        self.controller.start()

        self.assertEqual(self.controller.phase, "conflict")
        self.assertEqual(
            notices,
            [("port_conflict", {"port": self.settings.PORT})],
        )

        notices.clear()
        os.environ["HAYPILE_GUI_ALLOW_BACKEND_START"] = "0"
        self.controller._is_port_open = lambda _host, _port: False
        self.controller.start()

        self.assertEqual(self.controller.phase, "disabled")
        self.assertEqual(notices, [("auto_start_disabled", {})])

    def test_launches_becomes_ready_and_stops_owned_process(self) -> None:
        process = FakeProcess()
        calls: list[tuple[list[str], dict[str, object]]] = []
        stopped: list[bool] = []
        self.controller.stopped.connect(lambda: stopped.append(True))
        self.controller._probe_backend_response = lambda: None
        self.controller._is_port_open = lambda _host, _port: False

        def fake_popen(command, **kwargs):
            calls.append((command, kwargs))
            return process

        with (
            patch(
                "app.gui.backend_runtime.runtime_mode_command",
                return_value=["python", "backend_host.py"],
            ),
            patch("app.gui.backend_runtime.subprocess.Popen", side_effect=fake_popen),
        ):
            self.controller.start()

        self.assertEqual(self.controller.phase, "starting")
        self.assertTrue(self.controller.owns_process)
        self.assertEqual(calls[0][0], ["python", "backend_host.py"])
        self.assertEqual(Path(calls[0][1]["cwd"]), self.controller.project_root)
        self.assertEqual(
            calls[0][1]["env"]["HAYPILE_BACKEND_HOST_ALLOW_START"],
            "1",
        )
        log_handle = calls[0][1]["stdout"]
        self.assertFalse(log_handle.closed)

        self.controller._probe_backend_response = lambda: self.configured_response(
            pid=process.pid
        )
        self.controller._poll()
        self.assertEqual(self.controller.phase, "ready")
        self.assertFalse(self.controller._timer.isActive())

        with patch(
            "app.gui.backend_runtime.send_ipc_request",
            return_value={"ok": True},
        ) as stop_request:
            self.controller.stop()
        stop_request.assert_called_once_with({"type": "stop"}, timeout=0.6)
        self.assertEqual(self.controller.phase, "stopping")

        process.returncode = 0
        self.controller._poll()
        self.assertTrue(self.controller.is_stopped)
        self.assertFalse(self.controller.owns_process)
        self.assertEqual(stopped, [True])
        self.assertTrue(log_handle.closed)

    def test_launch_failure_closes_log_and_allows_retry(self) -> None:
        process = FakeProcess()
        notices: list[str] = []
        self.controller.notice.connect(lambda code, _details: notices.append(code))
        self.controller._probe_backend_response = lambda: None
        self.controller._is_port_open = lambda _host, _port: False

        with patch(
            "app.gui.backend_runtime.subprocess.Popen",
            side_effect=[OSError("blocked"), process],
        ):
            self.controller.start()
            self.assertEqual(self.controller.phase, "failed")
            self.assertIsNone(self.controller._log_handle)
            self.assertEqual(notices, ["launch_failed"])

            self.controller.start()

        self.assertEqual(self.controller.phase, "starting")
        self.assertTrue(self.controller.owns_process)

    def test_slow_start_notifies_once_without_terminating(self) -> None:
        process = FakeProcess()
        notices: list[str] = []
        self.controller.notice.connect(lambda code, _details: notices.append(code))
        self.controller._process = process
        self.controller._owns_process = True
        self.controller._set_phase("starting")
        self.controller._phase_started_at = time.monotonic() - 5.1
        self.controller._probe_backend_response = lambda: None

        self.controller._poll()
        self.controller._poll()

        self.assertEqual(notices, ["start_slow"])
        self.assertEqual(process.terminated, 0)
        self.assertEqual(self.controller.phase, "starting")

    def test_finished_start_reports_failure_and_releases_state(self) -> None:
        process = FakeProcess()
        process.returncode = 2
        notices: list[str] = []
        stopped: list[bool] = []
        self.controller.notice.connect(lambda code, _details: notices.append(code))
        self.controller.stopped.connect(lambda: stopped.append(True))
        self.controller._process = process
        self.controller._owns_process = True
        self.controller._set_phase("starting")

        self.controller._poll()

        self.assertEqual(notices, ["start_failed"])
        self.assertEqual(stopped, [True])
        self.assertTrue(self.controller.is_stopped)
        self.assertEqual(self.controller.phase, "idle")

    def test_stop_escalates_at_existing_deadlines(self) -> None:
        process = FakeProcess()
        terminated: list[bool] = []
        killed: list[bool] = []
        self.controller._process = process
        self.controller._owns_process = True
        self.controller._set_phase("ready")
        self.controller._terminate_process = lambda _process: terminated.append(True)
        self.controller._force_kill = lambda _process: killed.append(True)

        with patch(
            "app.gui.backend_runtime.send_ipc_request",
            return_value={"ok": True},
        ):
            self.controller.stop()
        self.controller._phase_started_at = time.monotonic() - 10.1
        self.controller._poll()
        self.assertEqual(terminated, [True])
        self.assertEqual(self.controller.phase, "terminating")

        self.controller._phase_started_at = time.monotonic() - 3.1
        self.controller._poll()
        self.assertEqual(killed, [True])
        self.assertEqual(self.controller.phase, "killing")

        self.controller._phase_started_at = time.monotonic() - 2.1
        self.controller._poll()
        self.assertEqual(killed, [True, True])

    def test_external_backend_stop_is_idempotent_and_sends_no_ipc(self) -> None:
        stopped: list[bool] = []
        self.controller.stopped.connect(lambda: stopped.append(True))
        self.controller._set_phase("ready")

        with patch(
            "app.gui.backend_runtime.send_ipc_request",
            side_effect=AssertionError("external backend must not be stopped"),
        ):
            self.controller.stop()
            self.controller.stop()

        self.assertEqual(self.controller.phase, "idle")
        self.assertEqual(stopped, [True])

    def test_identity_requires_product_protocol_host_port_and_expected_pid(self) -> None:
        response = self.configured_response()
        self.assertTrue(
            self.controller._is_configured_haypile_backend(
                response,
                require_ready=True,
                expected_pid=12345,
            )
        )
        self.assertFalse(
            self.controller._is_configured_haypile_backend(
                {**response, "product": "other"},
                require_ready=True,
            )
        )
        self.assertFalse(
            self.controller._is_configured_haypile_backend(
                {**response, "port": self.settings.PORT + 1},
                require_ready=True,
            )
        )
        self.assertFalse(
            self.controller._is_configured_haypile_backend(
                response,
                require_ready=True,
                expected_pid=999,
            )
        )


if __name__ == "__main__":
    unittest.main()
