from __future__ import annotations

import os
import random
import shutil
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class StagedAsset:
    path: Path
    sha256: str
    size: int


class VFSStorage:
    """Copy assets into Haypile-owned storage."""

    def __init__(self, copy_max_retries: int = 3, copy_base_delay: float = 1.0) -> None:
        self.copy_max_retries = max(1, copy_max_retries)
        self.copy_base_delay = max(0.1, copy_base_delay)

    def materialize(self, source: Path, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._copy_with_retry(source, destination)
        return "copy"

    def stage(
        self,
        source: Path,
        staging_dir: Path,
        token: str,
        *,
        should_stop: Callable[[], bool] | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> StagedAsset:
        staging_dir.mkdir(parents=True, exist_ok=True)
        partial = staging_dir / f"{token}.partial"
        suffix = source.suffix.lower()
        staged = staging_dir / f"{token}.staged{suffix}"
        digest = sha256()
        total = 0
        try:
            source_stat = source.stat()
            fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                output_file = os.fdopen(fd, "wb")
            except Exception:
                os.close(fd)
                raise
            with source.open("rb") as input_file, output_file:
                while True:
                    if should_stop is not None and should_stop():
                        raise InterruptedError("ingest_interrupted")
                    chunk = input_file.read(chunk_size)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output_file.write(chunk)
                    total += len(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
            if source.stat().st_size != source_stat.st_size or total != source_stat.st_size:
                raise OSError("source_changed_during_ingest")
            os.replace(partial, staged)
            self._fsync_directory(staging_dir)
            return StagedAsset(staged, digest.hexdigest(), total)
        except Exception:
            partial.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)
            raise

    def commit_staged(self, staged: Path, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, destination)
        if os.name != "nt":
            destination.chmod(0o600)
        self._fsync_directory(destination.parent)
        return "atomic-copy"

    def _copy_with_retry(self, source: Path, destination: Path) -> None:
        last_error: Exception | None = None
        for attempt in range(self.copy_max_retries):
            try:
                shutil.copy2(source, destination)
                if os.name != "nt" and destination.exists():
                    destination.chmod(0o600)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt >= self.copy_max_retries - 1:
                    break
                sleep_seconds = (self.copy_base_delay * (2**attempt)) + random.uniform(
                    0.0, 0.35
                )
                time.sleep(sleep_seconds)
            except OSError as exc:
                last_error = exc
                break
        if last_error is not None:
            raise last_error

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
