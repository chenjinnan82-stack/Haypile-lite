"""Portable, verified library archives; credentials and runtime databases stay local."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile

from app.services.asset_collections import read_collections
from app.services.json_io import atomic_write_json
from app.services.media_validator import validate_media
from app.services.storage_runtime import StorageRuntimeDB
from app.services.storage_transactions import (
    StorageTransactionError as BackupError,
    TRANSACTION, TARGETS,
    library_snapshot_lock as _locks,
    recover_interrupted_restore,
    move_durable as _move, sync_directory as _sync,
)

CHUNK = 1024 * 1024
MAX_METADATA = 20 * CHUNK
MAX_FILES = 100_000
MAX_TOTAL = 100 * 1024 * CHUNK


def _check_cancel(cancel):
    if cancel():
        raise BackupError('cancelled')


def _safe_name(name):
    if not isinstance(name, str) or len(name) > 1024:
        raise BackupError('invalid_archive')
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or '/'.join(parts) != name:
        raise BackupError('invalid_archive')
    for part in parts:
        if (part in ('.', '..') or part.endswith((' ', '.')) or
                any(ord(c) < 32 or c in '\\:*?"<>|' for c in part) or
                re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
            raise BackupError('invalid_archive')
    if not (name.startswith('assets/') or name.startswith('themes/') or name == 'collections.json'):
        raise BackupError('invalid_archive')
    return name


def _layout(settings):
    root = settings.STORAGE_DIR.resolve()
    for value, expected in ((settings.ASSETS_DIR, root/'assets'), (settings.THEMES_DIR, root/'themes'),
                            (settings.INDEX_DIR, root/'index'), (settings.MANIFEST_PATH, root/'index/assets_manifest.json')):
        if value.resolve() != expected or value.is_symlink():
            raise BackupError('custom_storage_layout')
    return root


def _validate_names(names):
    seen = {}
    for name in names:
        _safe_name(name)
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:index])
            folded = unicodedata.normalize('NFC', prefix).casefold()
            if folded in seen and seen[folded] != prefix:
                raise BackupError('invalid_archive')
            seen[folded] = prefix


def _inventory(root):
    paths = []
    for directory in ('assets', 'themes'):
        base = root/directory
        for path in sorted(base.rglob('*')):
            if path.is_symlink():
                raise BackupError('unsafe_library_path')
            if path.name in ('.DS_Store', 'Thumbs.db'):
                continue
            if path.is_file():
                paths.append(path)
    if (root/'collections.json').exists():
        paths.append(root/'collections.json')
    result = {}
    for path in paths:
        if path.is_symlink():
            raise BackupError('unsafe_library_path')
        name = _safe_name(path.relative_to(root).as_posix())
        info = path.stat()
        result[name] = (info.st_size, info.st_mtime_ns, info.st_ino)
    _validate_names(result)
    return result


def _stream(source, destination, cancel):
    digest = hashlib.sha256()
    size = 0
    while block := source.read(CHUNK):
        _check_cancel(cancel)
        size += len(block)
        digest.update(block)
        if destination is not None:
            destination.write(block)
    return size, digest.hexdigest()


def create_backup(settings, destination, *, cancel=lambda: False, progress=lambda text: None):
    root = _layout(settings)
    destination = Path(destination).absolute()
    if destination.resolve().is_relative_to(root):
        raise BackupError('destination_inside_library')
    temporary = None
    try:
        with _locks(root):
            if (root/TRANSACTION).exists():
                raise BackupError('library_busy')
            before = _inventory(root)
            if len(before) > MAX_FILES or sum(s[0] for s in before.values()) > MAX_TOTAL:
                raise BackupError('archive_limit')
            if shutil.disk_usage(destination.parent).free < sum(s[0] for s in before.values()) + 256 * CHUNK:
                raise BackupError('insufficient_space')
            read_collections(root/'collections.json')
            recorded = StorageRuntimeDB.read_recorded_asset_hash_index(root/'index/storage_runtime.db', root/'assets')
            recent = StorageRuntimeDB.read_recent_asset_hashes(root/'index/storage_runtime.db')
            fd, name = tempfile.mkstemp(prefix='.haypile-backup-', suffix='.tmp', dir=destination.parent)
            temporary = Path(name)
            os.close(fd)
            entries = {}
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for number, name in enumerate(before, 1):
                    _check_cancel(cancel)
                    if name.endswith('.json'):
                        if before[name][0] > MAX_METADATA or not isinstance(json.loads((root/name).read_text()), dict):
                            raise BackupError('invalid_library_metadata')
                    progress(f'{number}/{len(before)}')
                    with (root/name).open('rb') as source, archive.open(name, 'w', force_zip64=True) as output:
                        size, digest = _stream(source, output, cancel)
                    entries[name] = {'size': size, 'sha256': digest}
                for digest, path in recorded.items():
                    name = path.relative_to(root).as_posix()
                    if name not in entries or entries[name]['sha256'] != digest:
                        raise BackupError('library_changed')
                assets = [name for name in entries if name.startswith('assets/') and not name.endswith('.provenance.json')]
                if set(assets) != {p.relative_to(root).as_posix() for p in recorded.values()}:
                    raise BackupError('unindexed_assets')
                metadata = {'format': 'haypile-library', 'version': 1, 'entries': entries,
                            'recent': recent, 'asset_count': len(assets),
                            'created_at': datetime.now(timezone.utc).isoformat()}
                raw = json.dumps(metadata, ensure_ascii=False).encode()
                if len(raw) > MAX_METADATA:
                    raise BackupError('archive_limit')
                archive.writestr('library.json', raw)
            if before != _inventory(root):
                raise BackupError('library_changed')
            _check_cancel(cancel)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            temporary = None
            _sync(destination.parent)
            return metadata['asset_count']
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_archive(path, output=None, *, cancel=lambda: False, progress=lambda text: None, expected=None):
    with Path(path).open('rb') as source:
        _, fingerprint = _stream(source, None, cancel)
        if expected is not None and fingerprint != expected:
            raise BackupError('archive_changed')
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES + 1 or sum(i.file_size for i in infos) > MAX_TOTAL:
                raise BackupError('archive_limit')
            names = [i.filename for i in infos]
            if names.count('library.json') != 1 or len(set(names)) != len(names):
                raise BackupError('invalid_archive')
            if archive.getinfo('library.json').file_size > MAX_METADATA:
                raise BackupError('archive_limit')
            metadata = json.loads(archive.read('library.json'))
            if not isinstance(metadata, dict) or metadata.get('format') != 'haypile-library' or metadata.get('version') != 1:
                raise BackupError('unsupported_archive')
            entries = metadata.get('entries')
            if not isinstance(entries, dict) or set(entries) != set(names) - {'library.json'}:
                raise BackupError('invalid_archive')
            if output is not None and shutil.disk_usage(output).free < sum(i.file_size for i in infos) + 256 * CHUNK:
                raise BackupError('insufficient_space')
            _validate_names(entries)
            asset_hashes = set()
            for number, (name, entry) in enumerate(entries.items(), 1):
                info = archive.getinfo(name)
                if (info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1 or
                        not isinstance(entry, dict) or type(entry.get('size')) is not int or
                        entry['size'] != info.file_size or not re.fullmatch('[0-9a-f]{64}', str(entry.get('sha256', '')))):
                    raise BackupError('invalid_archive')
                is_asset = name.startswith('assets/') and not name.endswith('.provenance.json')
                if info.file_size > (500 * CHUNK if is_asset else MAX_METADATA):
                    raise BackupError('archive_limit')
                if is_asset:
                    if entry['sha256'] in asset_hashes:
                        raise BackupError('invalid_archive')
                    asset_hashes.add(entry['sha256'])
                progress(f'{number}/{len(entries)}')
                with archive.open(info) as data:
                    if output is None:
                        size, digest = _stream(data, None, cancel)
                    else:
                        target = output/name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open('xb') as file:
                            size, digest = _stream(data, file, cancel)
                            file.flush()
                            os.fsync(file.fileno())
                if size != entry['size'] or digest != entry['sha256']:
                    raise BackupError('damaged_archive')
            recent = metadata.get('recent')
            if (type(metadata.get('asset_count')) is not int or metadata['asset_count'] != len(asset_hashes) or
                    not isinstance(recent, list) or any(not isinstance(x, str) or x not in asset_hashes for x in recent) or
                    len(set(recent)) != len(recent)):
                raise BackupError('invalid_archive')
            metadata['fingerprint'] = fingerprint
            return metadata


def inspect_backup(path, *, cancel=lambda: False, progress=lambda text: None):
    return _read_archive(path, cancel=cancel, progress=progress)


def _empty(root):
    from app.services.theme_registry import DEFAULT_UI_DEV_INSTRUCTION
    if any(p.is_file() or p.is_symlink() for p in (root/'assets').rglob('*')):
        raise BackupError('library_not_empty')
    if read_collections(root/'collections.json'):
        raise BackupError('library_not_empty')
    for theme in (root/'themes').glob('*'):
        if theme.is_symlink() or not theme.is_file():
            raise BackupError('library_not_empty')
        if theme.name in ('.DS_Store', 'Thumbs.db'):
            continue
        if theme.suffix != '.json':
            raise BackupError('library_not_empty')
        payload = json.loads(theme.read_text())
        if not isinstance(payload, dict) or any(payload.get(key) for key in ('physical_assets', 'css_variables', 'tailwind_extend', 'fonts')):
            raise BackupError('library_not_empty')
        if (set(payload) - {'schema_version', 'revision', 'theme_name', 'physical_assets', 'css_variables', 'tailwind_extend', 'fonts', 'ui_dev_instruction'} or
                payload.get('ui_dev_instruction', DEFAULT_UI_DEV_INSTRUCTION) != DEFAULT_UI_DEV_INSTRUCTION):
            raise BackupError('library_not_empty')
    if (root/'index/storage_runtime.db').exists():
        if StorageRuntimeDB(root/'index/storage_runtime.db').has_asset_records():
            raise BackupError('library_not_empty')


def restore_backup(settings, archive, *, expected=None, cancel=lambda: False, progress=lambda text: None):
    from app.services.ingest_service import IngestService
    from app.services.scanner import mark_manifest_dirty, clear_manifest_dirty
    root = _layout(settings)
    root.mkdir(parents=True, exist_ok=True)
    with _locks(root):
        recover_interrupted_restore(root, locked=True)
        _empty(root)
    stage = Path(tempfile.mkdtemp(prefix='.restore-stage-', dir=root))
    try:
        new = stage/'new'
        new.mkdir()
        metadata = _read_archive(archive, new, cancel=cancel, progress=progress, expected=expected)
        if shutil.disk_usage(root).free < 256 * CHUNK:
            raise BackupError('insufficient_space')
        for name in metadata['entries']:
            _check_cancel(cancel)
            if name.startswith('assets/') and not name.endswith('.provenance.json'):
                validate_media(new/name)
            else:
                payload = json.loads((new/name).read_text())
                if not isinstance(payload, dict):
                    raise BackupError('invalid_archive')
        read_collections(new/'collections.json')
        assets = {e['sha256'] for n,e in metadata['entries'].items() if n.startswith('assets/') and not n.endswith('.provenance.json')}
        if any(asset not in assets for c in read_collections(new/'collections.json') for asset in c['asset_ids']):
            raise BackupError('invalid_archive')
        service = IngestService(storage_dir=new, assets_dir=new/'assets', index_dir=new/'index',
                                themes_dir=new/'themes', manifest_path=new/'index/assets_manifest.json')
        if not service.recover_and_project().success:
            raise BackupError('restore_validation_failed')
        runtime = StorageRuntimeDB(new/'index/storage_runtime.db')
        if set(runtime.asset_hash_index(new/'assets')) != assets:
            raise BackupError('restore_validation_failed')
        runtime.prepare_restored_library(new/'assets', root/'assets', metadata['recent'])
        for directory in ('assets','themes'):
            (new/directory).mkdir(exist_ok=True)
        for path in sorted(new.rglob('*'), reverse=True):
            if path.is_dir():
                _sync(path)
            else:
                with path.open('rb') as file:
                    os.fsync(file.fileno())
        _sync(new)
        with _locks(root):
            recover_interrupted_restore(root, locked=True)
            _empty(root)
            _check_cancel(cancel)
            # Flush the empty runtime before swapping it; refuse active readers/writers.
            if (root/'index/storage_runtime.db').exists():
                if not StorageRuntimeDB(root/'index/storage_runtime.db').checkpoint():
                    raise BackupError('library_busy')
            journal = {name: (root/name).exists() for name in TARGETS}
            if any((root/name).is_symlink() for name in TARGETS):
                raise BackupError('unsafe_library_path')
            atomic_write_json(stage/'journal.json', journal)
            _move(stage, root/TRANSACTION)
            stage = root/TRANSACTION
            was_dirty = settings.MANIFEST_PATH.with_name(settings.MANIFEST_PATH.name + '.dirty').exists()
            mark_manifest_dirty(settings.MANIFEST_PATH)
            try:
                # Cancellation is deferred for this bounded rename-only commit.
                for name in TARGETS:
                    if journal[name]:
                        _move(root/name, stage/'old'/name)
                    if (stage/'new'/name).exists():
                        _move(stage/'new'/name, root/name)
                # Removing the journal directory name is the durable commit point.
                completed = root/(stage.name + '-complete')
                _move(stage, completed)
                stage = completed
                clear_manifest_dirty(settings.MANIFEST_PATH)
                _sync(settings.INDEX_DIR)
            except Exception:
                recover_interrupted_restore(root, locked=True)
                if not was_dirty:
                    clear_manifest_dirty(settings.MANIFEST_PATH)
                raise
        return metadata['asset_count']
    finally:
        if stage.exists() and stage.name != TRANSACTION:
            shutil.rmtree(stage)
