from __future__ import annotations

import os
import socket
import threading
from multiprocessing.connection import AuthenticationError, Listener
from typing import Any

import uvicorn
import logging

from app.core.config import get_settings
from app.core.file_lock import InterProcessFileLock
from app.core.ipc import (
    authenticate_ipc_connection,
    cleanup_unix_socket,
    get_ipc_address,
    get_ipc_authkey,
    send_ipc_request,
    start_ipc_listener,
)

logger = logging.getLogger(__name__)
ALLOW_START_ENVS = ("HAYPILE_BACKEND_HOST_ALLOW_START",)
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


class ControlChannelServer:
    def __init__(self, server: uvicorn.Server, host: str, port: int) -> None:
        self._server = server
        self._host = host
        self._port = port
        self._address = get_ipc_address()
        self._listener: Listener | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            self._listener = start_ipc_listener(address=self._address)
        except OSError as exc:
            logger.error("Failed to start IPC listener: error_type=%s", type(exc).__name__)
            return False
        except Exception as exc:
            logger.error("Unexpected IPC listener start error: error_type=%s", type(exc).__name__)
            return False
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()
        return True

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                logger.debug("Failed to close IPC listener cleanly")
            except Exception as exc:
                logger.warning("Unexpected IPC listener close error: error_type=%s", type(exc).__name__)
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=0.8)
        cleanup_unix_socket(self._address)

    def _serve_loop(self) -> None:
        if self._listener is None:
            return
        try:
            listener_socket = self._listener._listener._socket  # type: ignore[attr-defined]
            listener_socket.settimeout(0.4)
        except OSError:
            logger.debug("Failed to set timeout on IPC listener socket")
        except Exception as exc:
            logger.warning("Unexpected IPC listener timeout error: error_type=%s", type(exc).__name__)

        while not self._stop_event.is_set():
            try:
                conn = self._listener.accept()
            except socket.timeout:
                continue
            except (OSError, EOFError):
                if self._stop_event.is_set():
                    break
                continue

            try:
                authenticate_ipc_connection(
                    conn,
                    get_ipc_authkey(),
                    timeout=0.7,
                    server=True,
                )
                if not conn.poll(0.7):
                    raise TimeoutError("ipc_request_timeout")
                payload = conn.recv()
                response = self._handle_payload(payload)
                conn.send(response)
            except (AuthenticationError, OSError, EOFError, TimeoutError):
                continue
            except Exception as exc:
                logger.warning("Unexpected IPC handling error: error_type=%s", type(exc).__name__)
                continue
            finally:
                try:
                    conn.close()
                except OSError:
                    logger.debug("Failed to close IPC connection cleanly")
                except Exception as exc:
                    logger.warning("Unexpected IPC connection close error: error_type=%s", type(exc).__name__)

    def _handle_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "invalid_payload"}
        request_type = str(payload.get("type", "")).strip().lower()
        if request_type == "ping":
            ready = bool(getattr(self._server, "started", False))
            return {
                "ok": True,
                "product": "haypile",
                "protocol_version": 1,
                "pid": os.getpid(),
                "host": self._host,
                "port": self._port,
                "ready": ready,
                "phase": "ready" if ready else "starting",
            }
        if request_type == "stop":
            self._server.should_exit = True
            self._stop_event.set()
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": "unknown_request"}


def allow_start_requested() -> bool:
    return any(os.environ.get(name, "").strip().lower() in TRUTHY_ENV_VALUES for name in ALLOW_START_ENVS)


def main() -> int:
    if not allow_start_requested():
        print(
            "Direct Haypile backend start is disabled. "
            "Start app_gui.py, or set HAYPILE_BACKEND_HOST_ALLOW_START=1 for a manual backend smoke test."
        )
        return 2
    settings = get_settings()
    instance_lock = InterProcessFileLock(settings.INDEX_DIR / "backend.instance.lock")
    if not instance_lock.acquire(timeout=0.1):
        existing = send_ipc_request({"type": "ping"}, timeout=0.4)
        return 0 if existing and existing.get("ok") else 2
    from app.main import app as fastapi_app

    config = uvicorn.Config(
        fastapi_app,
        host=settings.HOST,
        port=settings.PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    control = ControlChannelServer(server=server, host=settings.HOST, port=settings.PORT)
    try:
        if not control.start():
            return 2
        server.run()
        return 0
    finally:
        control.shutdown()
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
