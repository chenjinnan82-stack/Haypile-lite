from __future__ import annotations

import logging
import os
import socket
import tempfile
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def is_windows() -> bool:
    return os.name == "nt"


def get_ipc_address(channel: str | None = None) -> str:
    settings = get_settings()
    name = channel or settings.IPC_CHANNEL
    if is_windows():
        return rf"\\.\pipe\{name}"
    return str(Path(tempfile.gettempdir()) / f"{name}.sock")


def get_listener_family() -> str:
    return "AF_PIPE" if is_windows() else "AF_UNIX"


def cleanup_unix_socket(address: str) -> None:
    if is_windows():
        return
    socket_path = Path(address)
    if socket_path.exists():
        socket_path.unlink(missing_ok=True)


def get_ipc_authkey() -> bytes:
    return get_settings().IPC_AUTHKEY.encode("utf-8")


def send_ipc_request(
    payload: dict[str, Any],
    *,
    address: str | None = None,
    authkey: bytes | None = None,
    timeout: float = 0.7,
) -> dict[str, Any] | None:
    ipc_address = address or get_ipc_address()
    family = get_listener_family()
    effective_authkey = authkey or get_ipc_authkey()
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        conn = Client(ipc_address, family=family, authkey=effective_authkey)
        conn.send(payload)
        response = conn.recv()
        return response if isinstance(response, dict) else None
    except Exception as exc:
        logger.debug("Haypile IPC request failed family=%s error_type=%s", family, type(exc).__name__)
        return None
    finally:
        socket.setdefaulttimeout(previous_timeout)
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass


def start_ipc_listener(
    *,
    address: str | None = None,
    authkey: bytes | None = None,
) -> Listener:
    ipc_address = address or get_ipc_address()
    family = get_listener_family()
    cleanup_unix_socket(ipc_address)
    listener = Listener(ipc_address, family=family, authkey=authkey or get_ipc_authkey())
    if not is_windows() and Path(ipc_address).exists():
        Path(ipc_address).chmod(0o600)
    return listener
