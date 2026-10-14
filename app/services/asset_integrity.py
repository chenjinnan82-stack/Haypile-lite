"""Read stable file identities without changing the asset library."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import stat

from app.services.media_validator import MediaValidationError, validate_media


def _signature(value: os.stat_result) -> tuple[int, ...]:
    # CPython 3.12 Windows stat/fstat disagree on ctime; birthtime agrees.
    stamp = value.st_birthtime_ns if os.name == "nt" else value.st_ctime_ns
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, stamp)


@lru_cache(maxsize=512)
def _snapshot_digest(path: Path, signature: tuple[int, ...], legacy_type: str | None) -> str:
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if _signature(opened) != signature:
            return ""
        if legacy_type is not None:
            media = validate_media(path)
            if legacy_type and media.kind != legacy_type:
                return ""
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        finished = os.fstat(source.fileno())
        if (_signature(finished) != signature
                or finished.st_ctime_ns != opened.st_ctime_ns
                or _signature(path.stat(follow_symlinks=False)) != signature):
            return ""
        return digest


def asset_file_sha256(path: Path, *, legacy_type: str | None = None) -> str:
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return ""
        # ponytail: POSIX change-time invalidates edits even when mtime is restored.
        # Windows ctime is creation-time, so rehash there instead of trusting a cache.
        reader = _snapshot_digest if os.name != "nt" else _snapshot_digest.__wrapped__
        return reader(path, _signature(info), legacy_type)
    except (OSError, MediaValidationError, ValueError):
        return ""
