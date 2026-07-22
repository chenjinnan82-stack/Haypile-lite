from __future__ import annotations

import os
import time
from pathlib import Path


class InterProcessFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def acquire(self, timeout: float = 8.0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt" and os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._lock(descriptor)
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    return False
                time.sleep(0.05)
                continue
            self._descriptor = descriptor
            return True

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            self._unlock(descriptor)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "InterProcessFileLock":
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock: {self.path.name}")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()

    @staticmethod
    def _lock(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
