from __future__ import annotations

import logging
import hmac
import os
import socket
import tempfile
from multiprocessing.connection import AuthenticationError, Client, Connection, Listener
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)
_CHALLENGE = b"#CHALLENGE#"
_WELCOME = b"#WELCOME#"
_FAILURE = b"#FAILURE#"
_DIGEST_PREFIX = b"{sha256}"
_CHALLENGE_BYTES = 40


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


def _recv_bytes_with_timeout(conn: Connection, timeout: float, max_length: int = 256) -> bytes:
    if not conn.poll(max(0.01, timeout)):
        raise TimeoutError("ipc_auth_timeout")
    return conn.recv_bytes(max_length)


def _challenge_response(authkey: bytes, message: bytes) -> bytes:
    return _DIGEST_PREFIX + hmac.new(authkey, message, "sha256").digest()


def _answer_challenge(conn: Connection, authkey: bytes, timeout: float) -> None:
    challenge = _recv_bytes_with_timeout(conn, timeout)
    if not challenge.startswith(_CHALLENGE):
        raise AuthenticationError("ipc_challenge_expected")
    message = challenge[len(_CHALLENGE) :]
    if not message.startswith(_DIGEST_PREFIX) or len(message) <= len(_DIGEST_PREFIX):
        raise AuthenticationError("ipc_challenge_invalid")
    conn.send_bytes(_challenge_response(authkey, message))
    if _recv_bytes_with_timeout(conn, timeout) != _WELCOME:
        raise AuthenticationError("ipc_challenge_rejected")


def _deliver_challenge(conn: Connection, authkey: bytes, timeout: float) -> None:
    message = _DIGEST_PREFIX + os.urandom(_CHALLENGE_BYTES)
    conn.send_bytes(_CHALLENGE + message)
    response = _recv_bytes_with_timeout(conn, timeout)
    expected = _challenge_response(authkey, message)
    if not hmac.compare_digest(response, expected):
        conn.send_bytes(_FAILURE)
        raise AuthenticationError("ipc_digest_invalid")
    conn.send_bytes(_WELCOME)


def authenticate_ipc_connection(
    conn: Connection,
    authkey: bytes,
    *,
    timeout: float,
    server: bool,
) -> None:
    if not isinstance(authkey, bytes) or not authkey:
        raise ValueError("ipc_authkey_required")
    if server:
        _deliver_challenge(conn, authkey, timeout)
        _answer_challenge(conn, authkey, timeout)
    else:
        _answer_challenge(conn, authkey, timeout)
        _deliver_challenge(conn, authkey, timeout)


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
    conn = None
    client_socket: socket.socket | None = None
    try:
        if family == "AF_UNIX":
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.settimeout(timeout)
            client_socket.connect(ipc_address)
            client_socket.settimeout(None)
            conn = Connection(client_socket.detach())
            client_socket = None
        else:
            if os.name == "nt":
                import _winapi

                _winapi.WaitNamedPipe(ipc_address, max(1, int(timeout * 1000)))
            conn = Client(ipc_address, family=family, authkey=None)
        authenticate_ipc_connection(
            conn,
            effective_authkey,
            timeout=timeout,
            server=False,
        )
        conn.send(payload)
        if not conn.poll(timeout):
            return None
        response = conn.recv()
        return response if isinstance(response, dict) else None
    except Exception as exc:
        logger.debug("Haypile IPC request failed family=%s error_type=%s", family, type(exc).__name__)
        return None
    finally:
        try:
            if client_socket is not None:
                client_socket.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def start_ipc_listener(
    *,
    address: str | None = None,
    authkey: bytes | None = None,
) -> Listener:
    ipc_address = address or get_ipc_address()
    family = get_listener_family()
    if not is_windows() and Path(ipc_address).exists():
        if send_ipc_request(
            {"type": "ping"},
            address=ipc_address,
            authkey=authkey or get_ipc_authkey(),
            timeout=0.25,
        ):
            raise OSError("Haypile IPC listener is already running")
        cleanup_unix_socket(ipc_address)
    listener = Listener(ipc_address, family=family, authkey=None)
    if not is_windows() and Path(ipc_address).exists():
        Path(ipc_address).chmod(0o600)
    return listener
