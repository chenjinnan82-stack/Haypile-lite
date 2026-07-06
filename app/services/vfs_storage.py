from __future__ import annotations

import os
import random
import shutil
import time
from pathlib import Path


class VFSStorage:
    """
    Tiered ingest storage with graceful degradation:
    1) hard link -> 2) physical copy.
    """

    def __init__(self, copy_max_retries: int = 3, copy_base_delay: float = 1.0) -> None:
        self.copy_max_retries = max(1, copy_max_retries)
        self.copy_base_delay = max(0.1, copy_base_delay)

    def materialize(self, source: Path, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.link(source, destination)
            return "hardlink"
        except OSError:
            pass

        self._copy_with_retry(source, destination)
        return "copy"

    def _copy_with_retry(self, source: Path, destination: Path) -> None:
        last_error: Exception | None = None
        for attempt in range(self.copy_max_retries):
            try:
                shutil.copy2(source, destination)
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
