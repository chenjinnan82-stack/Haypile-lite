from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _candidate_temp_path(target: Path, attempt: int) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.{attempt}.tmp")


def atomic_write_json(path: Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        last_error: OSError | None = None
        for attempt in range(8):
            temp_path = _candidate_temp_path(target, attempt)
            try:
                fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError as exc:
                last_error = exc
                continue
        else:
            raise OSError(f"Unable to create atomic temp file for {target}") from last_error

        with os.fdopen(fd, "w", encoding="utf-8") as temp:
            json.dump(payload, temp, ensure_ascii=False, indent=2, allow_nan=False)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, target)
        temp_path = None
        if os.name != "nt":
            directory_fd = os.open(
                str(target.parent),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp_path:
            try:
                temp_path.unlink()
            except OSError:
                pass
