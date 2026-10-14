"""Library snapshot locks and crash recovery, independent of archive and ingest code."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shutil

from app.core.file_lock import InterProcessFileLock

TRANSACTION = '.restore-transaction'
TARGETS = ('assets', 'themes', 'collections.json', 'index/storage_runtime.db',
           'index/storage_runtime.db-wal', 'index/storage_runtime.db-shm',
           'index/storage_format.json', 'index/assets_manifest.json')


class StorageTransactionError(ValueError):
    pass


@contextmanager
def library_snapshot_lock(root):
    with ExitStack() as stack:
        for name in ('index/ingest.transaction.lock', 'collections.lock', 'index/themes.transaction.lock'):
            lock = InterProcessFileLock(root/name)
            if not lock.acquire(timeout=0):
                raise StorageTransactionError('library_busy')
            stack.callback(lock.release)
        themes = sorted((root/'themes').glob('*.json'))
        for theme in themes:
            lock = InterProcessFileLock(root/'index/theme_locks'/f'{theme.stem}.lock')
            if not lock.acquire(timeout=0):
                raise StorageTransactionError('library_busy')
            stack.callback(lock.release)
        yield


def sync_directory(directory):
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def move_durable(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    sync_directory(source.parent)
    sync_directory(target.parent)


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def recover_interrupted_restore(root, *, locked=False):
    """Caller holds ingest lock. An unfinished install always rolls back, even after restart."""
    root = Path(root)
    transaction = root/TRANSACTION
    completed = root/(TRANSACTION + '-complete')
    if not transaction.exists() and not completed.exists():
        return
    if not locked:
        with InterProcessFileLock(root/'collections.lock'), InterProcessFileLock(root/'index/themes.transaction.lock'):
            return recover_interrupted_restore(root, locked=True)
    if completed.exists():
        if completed.is_symlink() or not (completed/'journal.json').is_file():
            raise StorageTransactionError('invalid_restore_journal')
        shutil.rmtree(completed)
        sync_directory(root)
    if not transaction.exists():
        return
    if transaction.is_symlink():
        raise StorageTransactionError('unsafe_library_path')
    journal = json.loads((transaction/'journal.json').read_text())
    if set(journal) != set(TARGETS) or any(type(v) is not bool for v in journal.values()):
        raise StorageTransactionError('invalid_restore_journal')
    for name in reversed(TARGETS):
        target, old, new = root/name, transaction/'old'/name, transaction/'new'/name
        if target.parent.is_symlink():
            raise StorageTransactionError('unsafe_library_path')
        if old.exists():
            _remove(target)
            move_durable(old, target)
        elif not journal[name] and not new.exists():
            _remove(target)
    shutil.rmtree(transaction)
    sync_directory(root)
