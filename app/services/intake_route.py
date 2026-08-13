from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROUTE_LOCAL_FILES = "local_files"
ROUTE_LOCAL_FILES_AND_REMOTE_URLS = "local_files_and_remote_urls"
ROUTE_RAW_GIF = "raw_gif"
ROUTE_REMOTE_URL = "remote_url"
ROUTE_REMOTE_URL_WITH_STATIC_PNG_FALLBACK = "remote_url_with_static_png_fallback"
ROUTE_STATIC_PNG = "static_png"
ROUTE_EMPTY_GIF = "empty_gif"
ROUTE_UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class IntakeRoute:
    name: str
    local_files: tuple[Path, ...]
    remote_urls: tuple[str, ...]
    raw_gif: bytes | None
    has_image: bool

    @property
    def has_remote(self) -> bool:
        return bool(self.remote_urls)

    @property
    def has_local_files(self) -> bool:
        return bool(self.local_files)


def resolve_intake_route(
    *,
    local_files: list[Path] | tuple[Path, ...] = (),
    remote_urls: list[str] | tuple[str, ...] = (),
    raw_gif: bytes | None = None,
    has_image: bool = False,
) -> IntakeRoute:
    """Single source of truth for drop / clipboard / diagnostics intake priority.

    Drop callers should pass ``raw_gif=None`` and ``has_image=False`` so pixel
    and raw-GIF clipboard routes stay clipboard-only.
    """
    files = tuple(Path(path) for path in local_files)
    urls = tuple(str(url) for url in remote_urls)
    if files:
        name = (
            ROUTE_LOCAL_FILES_AND_REMOTE_URLS if urls else ROUTE_LOCAL_FILES
        )
    elif raw_gif:
        name = ROUTE_RAW_GIF
    elif urls:
        name = (
            ROUTE_REMOTE_URL_WITH_STATIC_PNG_FALLBACK
            if has_image
            else ROUTE_REMOTE_URL
        )
    elif has_image:
        name = ROUTE_STATIC_PNG
    elif raw_gif == b"":
        name = ROUTE_EMPTY_GIF
    else:
        name = ROUTE_UNSUPPORTED
    return IntakeRoute(
        name=name,
        local_files=files,
        remote_urls=urls,
        raw_gif=raw_gif,
        has_image=bool(has_image),
    )
