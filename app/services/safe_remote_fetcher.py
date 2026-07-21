from __future__ import annotations

import http.client
import ipaddress
import logging
import mimetypes
import os
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping
from urllib.parse import SplitResult, unquote, urlparse, urlsplit

from app.services.asset_provenance import public_origin_url, write_asset_provenance
from app.services.media_types import AUDIO_CONTENT_TYPE_EXTENSIONS


MAX_REMOTE_URLS = 20
REMOTE_CONTENT_TYPE_EXTENSIONS: dict[str, tuple[str, str]] = {
    "image/png": ("image", ".png"),
    "image/jpeg": ("image", ".jpg"),
    "image/webp": ("image", ".webp"),
    "image/svg+xml": ("image", ".svg"),
    **{content_type: ("audio", extension) for content_type, extension in AUDIO_CONTENT_TYPE_EXTENSIONS.items()},
}
logger = logging.getLogger(__name__)


class SafeFetchError(ValueError):
    """Raised when a remote URL cannot be fetched within Haypile's boundary."""


def dedupe_remote_urls(urls: list[str], *, limit: int = MAX_REMOTE_URLS) -> list[str]:
    """Normalize and bound a browser drop before any network work starts."""
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        key = url.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
        if len(result) >= max(0, limit):
            break
    return result


def download_remote_media(
    url: str,
    incoming_dir: Path,
    index: int,
    *,
    max_bytes: int,
    timeout: float,
    should_stop: Callable[[], bool] | None = None,
    opener: Callable[..., "SafeRemoteResponse"] | None = None,
) -> tuple[Path | None, str]:
    """Fetch one supported media asset into a private, durable local file."""
    stop_requested = should_stop or (lambda: False)
    destination: Path | None = None
    with (opener or open_safe_remote)(url, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in REMOTE_CONTENT_TYPE_EXTENSIONS:
            return None, "unsupported"
        content_length = _content_length(response.headers.get("content-length"))
        byte_limit = max(0, int(max_bytes))
        if content_length > byte_limit:
            return None, "too_large"

        _kind, default_extension = REMOTE_CONTENT_TYPE_EXTENSIONS[content_type]
        destination = _destination_for(incoming_dir, url, content_type, default_extension, index)
        total = 0
        fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                target_file = os.fdopen(fd, "wb")
            except Exception:
                os.close(fd)
                raise
            with target_file as target:
                for chunk in response.iter_bytes():
                    if stop_requested():
                        destination.unlink(missing_ok=True)
                        return None, "interrupted"
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > byte_limit:
                        destination.unlink(missing_ok=True)
                        return None, "too_large"
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        if total <= 0:
            destination.unlink(missing_ok=True)
            return None, "empty"
        try:
            write_asset_provenance(
                destination,
                {
                    "origin_url": public_origin_url(url),
                    "content_type": content_type,
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except OSError:
            logger.debug("Failed to write browser asset provenance")
        return destination, ""


def _destination_for(
    incoming_dir: Path,
    url: str,
    content_type: str,
    default_extension: str,
    index: int,
) -> Path:
    path_name = Path(unquote(urlparse(url).path)).name
    stem = _safe_stem(Path(path_name).stem) or f"browser_asset_{index}"
    extension = Path(path_name).suffix.lower()
    if mimetypes.types_map.get(extension) != content_type:
        extension = default_extension
    candidate = incoming_dir / f"{stem}{extension}"
    counter = 1
    while candidate.exists():
        candidate = incoming_dir / f"{stem}_{counter}{extension}"
        counter += 1
    return candidate


def _content_length(value: str | None) -> int:
    try:
        return int(value or "0")
    except ValueError:
        return 0


def _safe_stem(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value).strip("_")[:72]


@dataclass(frozen=True)
class ValidatedRemoteURL:
    original_url: str
    parsed: SplitResult
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def request_target(self) -> str:
        path = self.parsed.path or "/"
        return f"{path}?{self.parsed.query}" if self.parsed.query else path

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.parsed.scheme == "https" else 80
        return host if self.port == default_port else f"{host}:{self.port}"


def _normalized_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_public_address(value: str) -> bool:
    address = _normalized_ip(value)
    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_private
        and not address.is_link_local
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_multicast
    )


def validate_remote_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple[object, ...]]] = socket.getaddrinfo,
) -> ValidatedRemoteURL:
    if (
        not isinstance(url, str)
        or not url
        or "\\" in url
        or any(ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise SafeFetchError("invalid_url")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SafeFetchError("unsupported_scheme")
    if parsed.username is not None or parsed.password is not None:
        raise SafeFetchError("userinfo_not_allowed")
    if parsed.fragment:
        raise SafeFetchError("fragment_not_allowed")
    host = parsed.hostname
    if not host:
        raise SafeFetchError("missing_host")
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise SafeFetchError("invalid_port") from exc
    port = explicit_port if explicit_port is not None else (443 if parsed.scheme.lower() == "https" else 80)
    if port < 1 or port > 65535:
        raise SafeFetchError("invalid_port")

    try:
        literal = _normalized_ip(host)
    except ValueError:
        try:
            infos = resolver(host, port, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror) as exc:
            raise SafeFetchError("dns_failed") from exc
        raw_addresses = [str(info[4][0]) for info in infos if len(info) >= 5 and info[4]]
    else:
        raw_addresses = [str(literal)]

    addresses: list[str] = []
    for raw in raw_addresses:
        try:
            normalized = str(_normalized_ip(raw))
        except ValueError as exc:
            raise SafeFetchError("invalid_dns_address") from exc
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise SafeFetchError("dns_failed")
    if any(not _is_public_address(address) for address in addresses):
        raise SafeFetchError("non_public_address")
    return ValidatedRemoteURL(url, parsed, host, port, tuple(addresses))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: ValidatedRemoteURL, address: str, timeout: float) -> None:
        super().__init__(target.host, target.port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        target: ValidatedRemoteURL,
        address: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(target.host, target.port, timeout=timeout, context=context)
        self._address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


class SafeRemoteResponse:
    def __init__(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        deadline: float,
    ) -> None:
        self._connection = connection
        self._response = response
        self.status = int(response.status)
        self.headers: Mapping[str, str] = {key.lower(): value for key, value in response.getheaders()}
        self._deadline = deadline

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise SafeFetchError("download_timeout")
            if self._connection.sock is not None:
                self._connection.sock.settimeout(max(0.01, remaining))
            chunk = self._response.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._response.close()
        self._connection.close()

    def __enter__(self) -> "SafeRemoteResponse":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def open_safe_remote(
    url: str,
    *,
    timeout: float = 15.0,
    resolver: Callable[..., list[tuple[object, ...]]] = socket.getaddrinfo,
    ssl_context: ssl.SSLContext | None = None,
) -> SafeRemoteResponse:
    deadline = time.monotonic() + max(0.1, timeout)
    target = validate_remote_url(url, resolver=resolver)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SafeFetchError("download_timeout")
    address = target.addresses[0]
    if target.parsed.scheme.lower() == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
            target,
            address,
            remaining,
            ssl_context or ssl.create_default_context(),
        )
    else:
        connection = _PinnedHTTPConnection(target, address, remaining)

    try:
        connection.request(
            "GET",
            target.request_target,
            headers={
                "Host": target.host_header,
                "Accept": "image/png,image/jpeg,image/webp,image/svg+xml,audio/*;q=0.9",
                "Connection": "close",
                "User-Agent": "Haypile/0.3",
            },
        )
        peer = connection.sock.getpeername()[0] if connection.sock is not None else ""
        if str(_normalized_ip(peer)) != str(_normalized_ip(address)):
            raise SafeFetchError("peer_address_mismatch")
        response = connection.getresponse()
        if time.monotonic() >= deadline:
            raise SafeFetchError("download_timeout")
        if 300 <= response.status < 400:
            raise SafeFetchError("redirect_not_allowed")
        if response.status < 200 or response.status >= 300:
            raise SafeFetchError(f"http_status_{response.status}")
        return SafeRemoteResponse(connection, response, deadline)
    except Exception:
        connection.close()
        raise
