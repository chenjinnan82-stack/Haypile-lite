from __future__ import annotations

import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import TextIO

from PySide6.QtCore import QObject, QTimer, Signal

from app.core.config import Settings, runtime_mode_command
from app.core.ipc import send_ipc_request


logger = logging.getLogger(__name__)


class BackendRuntimeController(QObject):
    phase_changed = Signal(str)
    notice = Signal(str, object)
    stopped = Signal()

    def __init__(
        self,
        settings: Settings,
        project_root: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.project_root = Path(project_root)
        self._process: subprocess.Popen[str] | None = None
        self._owns_process = False
        self._phase = "idle"
        self._phase_started_at = 0.0
        self._wait_notice_shown = False
        self._log_handle: TextIO | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._poll)

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def is_stopped(self) -> bool:
        return self._process is None

    @property
    def owns_process(self) -> bool:
        return self._owns_process

    def start(self) -> None:
        if self._process is not None:
            if self._process.poll() is None:
                return
            self._finish_process(emit_stopped=False)

        existing = self._probe_backend_response()
        if self._is_configured_haypile_backend(existing):
            self._owns_process = False
            self._set_phase("ready")
            return
        if self._is_port_open(self.settings.HOST, self.settings.PORT):
            self._owns_process = False
            self._set_phase("conflict")
            self.notice.emit("port_conflict", {"port": self.settings.PORT})
            return

        allow_start = os.environ.get(
            "HAYPILE_GUI_ALLOW_BACKEND_START",
            "",
        ).strip().lower()
        if allow_start in {"0", "false", "no", "off"}:
            self._owns_process = False
            self._set_phase("disabled")
            self.notice.emit("auto_start_disabled", {})
            return

        command = runtime_mode_command("backend", source_root=self.project_root)
        env = os.environ.copy()
        env["HAYPILE_BACKEND_HOST_ALLOW_START"] = "1"
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        try:
            self.settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = self.settings.LOG_DIR / "backend-process.log"
            self._log_handle = log_path.open("a", encoding="utf-8", buffering=1)
            if os.name != "nt":
                log_path.chmod(0o600)
            self._process = subprocess.Popen(
                command,
                cwd=str(self.project_root),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._process = None
            self._owns_process = False
            self._timer.stop()
            self._close_log()
            self._set_phase("failed")
            logger.error(
                "Backend process launch failed error_type=%s",
                type(exc).__name__,
            )
            self.notice.emit(
                "launch_failed",
                {"error_type": type(exc).__name__},
            )
            return

        self._owns_process = True
        self._phase_started_at = time.monotonic()
        self._wait_notice_shown = False
        self._set_phase("starting")
        self._timer.start()

    def stop(self) -> None:
        process = self._process
        if process is None:
            was_active = self._phase != "idle" or self._log_handle is not None
            self._owns_process = False
            self._timer.stop()
            self._close_log()
            self._set_phase("idle")
            if was_active:
                self.stopped.emit()
            return
        if process.poll() is not None:
            self._finish_process()
            return
        if not self._owns_process:
            self._finish_process()
            return

        send_ipc_request({"type": "stop"}, timeout=0.6)
        self._phase_started_at = time.monotonic()
        self._set_phase("stopping")
        self._timer.start()

    def _poll(self) -> None:
        process = self._process
        if process is None:
            self._timer.stop()
            return
        if process.poll() is not None:
            failed_to_start = self._phase == "starting"
            self._finish_process()
            if failed_to_start:
                self.notice.emit("start_failed", {})
            return

        now = time.monotonic()
        if self._phase == "starting":
            response = self._probe_backend_response()
            if self._is_configured_haypile_backend(
                response,
                require_ready=True,
                expected_pid=getattr(process, "pid", None),
            ):
                self._set_phase("ready")
                self._timer.stop()
                return
            if (
                now - self._phase_started_at >= 5.0
                and not self._wait_notice_shown
            ):
                self._wait_notice_shown = True
                self.notice.emit("start_slow", {})
            return

        elapsed = now - self._phase_started_at
        if self._phase == "stopping" and elapsed >= 10.0:
            logger.warning("Backend graceful shutdown timed out; sending terminate")
            self._terminate_process(process)
            self._phase_started_at = now
            self._set_phase("terminating")
        elif self._phase == "terminating" and elapsed >= 3.0:
            logger.error("Backend terminate timed out; forcing process exit")
            self._force_kill(process)
            self._phase_started_at = now
            self._set_phase("killing")
        elif self._phase == "killing" and elapsed >= 2.0:
            self._force_kill(process)

    def _finish_process(self, *, emit_stopped: bool = True) -> None:
        self._process = None
        self._owns_process = False
        self._timer.stop()
        self._close_log()
        self._set_phase("idle")
        if emit_stopped:
            self.stopped.emit()

    def _close_log(self) -> None:
        handle, self._log_handle = self._log_handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _set_phase(self, phase: str) -> None:
        if phase == self._phase:
            return
        self._phase = phase
        self.phase_changed.emit(phase)

    @staticmethod
    def is_haypile_backend(
        response: object,
        *,
        require_ready: bool = False,
    ) -> bool:
        if not isinstance(response, dict):
            return False
        if response.get("ok") is not True:
            return False
        if (
            response.get("product") != "haypile"
            or response.get("protocol_version") != 1
        ):
            return False
        return not require_ready or response.get("ready") is True

    def _is_configured_haypile_backend(
        self,
        response: object,
        *,
        require_ready: bool = False,
        expected_pid: int | None = None,
    ) -> bool:
        if not self.is_haypile_backend(response, require_ready=require_ready):
            return False
        assert isinstance(response, dict)
        try:
            port = int(response.get("port"))
            pid = int(response.get("pid"))
        except (TypeError, ValueError):
            return False
        if response.get("host") != self.settings.HOST or port != self.settings.PORT:
            return False
        return expected_pid is None or pid == expected_pid

    @staticmethod
    def _probe_backend_response() -> dict[str, object] | None:
        response = send_ipc_request({"type": "ping"}, timeout=0.45)
        return response if isinstance(response, dict) else None

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.35)
            return sock.connect_ex((host, port)) == 0

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        try:
            process.terminate()
        except OSError as exc:
            logger.warning(
                "Backend terminate failed error_type=%s",
                type(exc).__name__,
            )

    @staticmethod
    def _force_kill(process: subprocess.Popen[str]) -> None:
        if sys.platform.startswith("win"):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except OSError as exc:
                logger.warning(
                    "Backend process-tree kill failed error_type=%s",
                    type(exc).__name__,
                )
            return
        try:
            process.kill()
        except OSError as exc:
            logger.warning(
                "Backend kill failed error_type=%s",
                type(exc).__name__,
            )
