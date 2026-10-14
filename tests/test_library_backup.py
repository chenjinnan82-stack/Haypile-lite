from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
from app.core.config import Settings
from app.core.file_lock import InterProcessFileLock
from app.services import library_backup as backup
from app.services.asset_collections import save_collection, read_collections
from app.services.bundle_service import BundleService
from app.services.ingest_service import IngestService, IngestCandidate
from tests.test_font_support import write_test_font


class LibraryBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = Settings(_env_file=None, STORAGE_DIR=self.root/'source', LOG_DIR=self.root/'logs')
        self.destination = Settings(_env_file=None, STORAGE_DIR=self.root/'destination', LOG_DIR=self.root/'logs')
        self.archive = self.root/'library.haypile.zip'
        font, image = self.root/'test.ttf', self.root/'picture.png'
        write_test_font(font)
        Image.new('RGB', (16,16), 'red').save(image)
        result = self.ingest(self.source).ingest([IngestCandidate(font), IngestCandidate(image)])
        self.assertTrue(result.success)
        self.font_id = hashlib.sha256(font.read_bytes()).hexdigest()
        self.bundle(self.source).update_bundle_font(self.font_id, font_usage='heading',
                                                   annotation={'source_note':'example', 'license_note':'test'})
        save_collection(self.source.STORAGE_DIR/'collections.json', '收集', asset_id=self.font_id)
        (self.source.INDEX_DIR/'gui_state.json').write_text('{"api_key":"DO_NOT_EXPORT"}')
        self.assertEqual(backup.create_backup(self.source,self.archive), 2)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self,s):
        return IngestService(storage_dir=s.STORAGE_DIR, assets_dir=s.ASSETS_DIR,index_dir=s.INDEX_DIR,
                             themes_dir=s.THEMES_DIR,manifest_path=s.MANIFEST_PATH)

    def bundle(self,s):
        return BundleService(assets_dir=s.ASSETS_DIR, themes_dir=s.THEMES_DIR,
                             manifest_path=s.MANIFEST_PATH,runtime_db_path=s.INDEX_DIR/'storage_runtime.db')

    def test_portable_roundtrip_preserves_notes_roles_collections_and_recent(self):
        self.assertTrue(self.ingest(self.destination).recover_and_project().success)
        (self.destination.INDEX_DIR/'gui_state.json').write_text('{"keep":true}')
        info = backup.inspect_backup(self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertNotIn('index/gui_state.json',archive.namelist())
            self.assertNotIn('index/storage_runtime.db',archive.namelist())
            self.assertNotIn(str(self.source.STORAGE_DIR),archive.read('library.json').decode())
        self.assertEqual(backup.restore_backup(self.destination,self.archive,expected=info['fingerprint']),2)
        self.assertTrue(self.ingest(self.destination).recover_and_project().success)
        before, after = self.bundle(self.source), self.bundle(self.destination)
        self.assertEqual([x['id'] for x in before.list_recent_bundles()], [x['id'] for x in after.list_recent_bundles()])
        for key in ('status','font_usage','font_annotation','sha256'):
            self.assertEqual(before.get_bundle(self.font_id)[key],after.get_bundle(self.font_id)[key])
        self.assertEqual(read_collections(self.source.STORAGE_DIR/'collections.json'),read_collections(self.destination.STORAGE_DIR/'collections.json'))
        self.assertEqual((self.destination.INDEX_DIR/'gui_state.json').read_text(),'{"keep":true}')
        for bundle in after.list_bundles():
            self.assertEqual(hashlib.sha256((self.destination.ASSETS_DIR/bundle['source_key']).read_bytes()).hexdigest(),bundle['id'])

    def test_nonempty_library_is_never_overwritten(self):
        previous = self.source.MANIFEST_PATH.read_bytes()
        with self.assertRaisesRegex(backup.BackupError,'library_not_empty'):
            backup.restore_backup(self.source,self.archive)
        self.assertEqual(previous,self.source.MANIFEST_PATH.read_bytes())
        self.assertEqual(len(self.bundle(self.source).list_bundles()),2)

    def test_restore_preserves_unrecognized_theme_files(self):
        for name in ('notes.txt', 'saved/custom.json', 'generic.json.backup'):
            with self.subTest(name=name):
                settings = Settings(_env_file=None, STORAGE_DIR=self.root/name.replace('/', '_'), LOG_DIR=self.root/'logs')
                self.assertTrue(self.ingest(settings).recover_and_project().success)
                existing = settings.THEMES_DIR/name
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_bytes(b'{"user_content":"keep"}')
                manifest = settings.MANIFEST_PATH.read_bytes()
                with self.assertRaisesRegex(backup.BackupError, 'library_not_empty'):
                    backup.restore_backup(settings, self.archive)
                self.assertEqual(existing.read_bytes(), b'{"user_content":"keep"}')
                self.assertEqual(settings.MANIFEST_PATH.read_bytes(), manifest)
                self.assertFalse(list(settings.STORAGE_DIR.glob('.restore-*')))

    def test_bad_archives_cancel_space_and_changed_selection_leave_empty_library(self):
        info=backup.inspect_backup(self.archive)
        with self.assertRaisesRegex(backup.BackupError,'cancelled'):
            backup.restore_backup(self.destination,self.archive,cancel=lambda:True)
        with self.assertRaisesRegex(backup.BackupError,'archive_changed'):
            backup.restore_backup(self.destination,self.archive,expected='0'*64)
        with patch.object(backup.shutil,'disk_usage',return_value=type('Disk',(),{'free':0})()):
            with self.assertRaisesRegex(backup.BackupError,'insufficient_space'):
                backup.restore_backup(self.destination,self.archive)
        damaged=self.root/'damaged.zip'
        with zipfile.ZipFile(self.archive) as source,zipfile.ZipFile(damaged,'w') as target:
            for name in source.namelist():
                data=source.read(name)
                target.writestr(name, b'x'*len(data) if name.endswith('.ttf') else data)
        with self.assertRaisesRegex(backup.BackupError,'damaged_archive'):
            backup.restore_backup(self.destination,damaged)
        self.assertFalse(any(self.destination.ASSETS_DIR.rglob('*')))
        self.assertFalse(list(self.destination.STORAGE_DIR.glob('.restore-*')))

    def test_reject_traversal_duplicate_case_and_symlink_members(self):
        for malicious in ('assets/../../escape', 'assets/CON.ttf', 'assets/C:\\escape.ttf'):
            with self.subTest(name=malicious), self.assertRaises(backup.BackupError):
                backup._safe_name(malicious)
        path=self.root/'unsafe.zip'
        with zipfile.ZipFile(self.archive) as source,zipfile.ZipFile(path,'w') as target:
            for item in source.infolist():
                if item.filename.endswith('.ttf'):
                    item.external_attr=(0o120777<<16)
                target.writestr(item,source.read(item.filename))
        with self.assertRaises(backup.BackupError):backup.inspect_backup(path)
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(path,'w') as target:
            metadata=json.loads(source.read('library.json'))
            font=next(name for name in metadata['entries'] if name.endswith('.ttf'))
            metadata['entries']['assets/duplicate.ttf']=metadata['entries'][font]
            for name in source.namelist():
                if name!='library.json':target.writestr(name,source.read(name))
            target.writestr('assets/duplicate.ttf',source.read(font))
            target.writestr('library.json',json.dumps(metadata))
        with self.assertRaises(backup.BackupError):backup.inspect_backup(path)

    def test_failed_commit_rolls_back_and_crashed_commit_recovers_on_restart(self):
        for crash in (False,True):
            with self.subTest(crash=crash):
                self.assertTrue(self.ingest(self.destination).recover_and_project().success)
                manifest=self.destination.MANIFEST_PATH.read_bytes()
                original=backup._move
                def fail(source,target):
                    if Path(target)==self.destination.THEMES_DIR and 'new' in Path(source).parts:
                        raise KeyboardInterrupt() if crash else OSError('injected disk failure')
                    original(source,target)
                with patch.object(backup,'_move',side_effect=fail):
                    with self.assertRaises(KeyboardInterrupt if crash else OSError):
                        backup.restore_backup(self.destination,self.archive)
                if not crash:
                    self.assertEqual(self.destination.MANIFEST_PATH.read_bytes(),manifest)
                    self.assertEqual(self.bundle(self.destination).list_bundles(),[])
                self.assertTrue(self.ingest(self.destination).recover_and_project().success)
                self.assertEqual(self.bundle(self.destination).list_bundles(),[])
                self.assertFalse((self.destination.STORAGE_DIR/backup.TRANSACTION).exists())

    def test_backup_busy_cancel_and_destination_protection_preserve_previous_backup(self):
        previous=self.archive.read_bytes()
        lock=InterProcessFileLock(self.source.INDEX_DIR/'ingest.transaction.lock')
        with lock,self.assertRaisesRegex(backup.BackupError,'library_busy'):
            backup.create_backup(self.source,self.archive)
        with self.assertRaisesRegex(backup.BackupError,'cancelled'):
            backup.create_backup(self.source,self.archive,cancel=lambda:True)
        self.assertEqual(self.archive.read_bytes(),previous)
        with self.assertRaisesRegex(backup.BackupError,'destination_inside_library'):
            backup.create_backup(self.source,self.source.STORAGE_DIR/'backup.zip')

    def test_fresh_start_recovers_interrupted_restore_without_loading_archive_service(self):
        self.assertTrue(self.ingest(self.destination).recover_and_project().success)
        original = backup._move
        def crash(source, target):
            original(source, target)
            if Path(target) == self.destination.ASSETS_DIR:
                raise KeyboardInterrupt()
        with patch.object(backup, '_move', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            backup.restore_backup(self.destination, self.archive)
        self.assertTrue((self.destination.STORAGE_DIR / backup.TRANSACTION).exists())
        script = '''
import sys
from pathlib import Path
class NoArchiveService:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app.services.library_backup':
            raise ImportError('startup must not depend on archive service')
sys.meta_path.insert(0, NoArchiveService())
from app.services.ingest_service import IngestService
root = Path(sys.argv[1])
service = IngestService(storage_dir=root, assets_dir=root/'assets', index_dir=root/'index',
                       themes_dir=root/'themes', manifest_path=root/'index/assets_manifest.json')
assert service.recover_and_project().success
assert 'app.services.library_backup' not in sys.modules
'''
        result = subprocess.run(
            [sys.executable, '-c', script, str(self.destination.STORAGE_DIR)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bundle(self.destination).list_bundles(), [])
        self.assertFalse((self.destination.STORAGE_DIR / backup.TRANSACTION).exists())

    def test_settings_backup_restore_and_cancel_flows(self):
        import time
        import app_gui
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtTest import QTest
        app = QApplication.instance() or QApplication([])
        balls=[]
        def wait(ball):
            deadline=time.monotonic()+10
            while ball.library_worker is not None and time.monotonic()<deadline:
                app.processEvents()
                time.sleep(.005)
            self.assertIsNone(ball.library_worker)
        try:
            self.assertTrue(self.ingest(self.destination).recover_and_project().success)
            for settings in (self.source,self.destination):
                balls.append(app_gui.HaypileFloatingBall(settings=settings,initial_state={'ai_enabled':False,'ai_provider':'off','sound_enabled':False}))
            source,target=balls
            source._handle_quick_menu_action('settings')
            with patch.object(app_gui.QFileDialog,'getSaveFileName',return_value=(str(self.archive),'')):
                source.quick_menu.backup_button.click()
            self.assertIsNotNone(source.library_worker)
            self.assertFalse(source.acceptDrops())
            wait(source)
            self.assertTrue(source.acceptDrops())
            self.assertIn('2',source.quick_menu.feedback_label.text())
            target._handle_quick_menu_action('settings')
            with patch.object(app_gui.QFileDialog,'getOpenFileName',return_value=(str(self.archive),'')),patch.object(app_gui.QMessageBox,'question',return_value=QMessageBox.StandardButton.Cancel):
                target.quick_menu.restore_button.click()
                wait(target)
            self.assertEqual(self.bundle(self.destination).list_bundles(),[])
            with patch.object(app_gui.QFileDialog,'getOpenFileName',return_value=(str(self.archive),'')),patch.object(app_gui.QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):
                target.quick_menu.restore_button.click()
                wait(target)
            self.assertEqual(len(self.bundle(self.destination).list_bundles()),2)
            self.assertTrue(target.quick_menu.restore_button.isEnabled())
            self.assertFalse(target.quick_menu.backup_cancel_button.isVisible())
            def cancellable(settings,path,*,cancel,progress):
                while not cancel():time.sleep(.005)
                raise backup.BackupError('cancelled')
            with patch.object(app_gui,'create_backup',side_effect=cancellable):
                source._run_library_operation('backup',self.archive)
                source._close_attached_ui()
                source._toggle_quick_menu()
                self.assertTrue(source.quick_menu.isVisible())
                self.assertEqual(source.quick_menu.current_page(), 'settings')
                source.shutdown()
                wait(source)
                deadline=time.monotonic()+3
                while not source._cleanup_done and time.monotonic()<deadline:QTest.qWait(10)
                self.assertTrue(source._cleanup_done)
            def quit_in_confirmation(*args):
                target.shutdown()
                self.assertFalse(target._cleanup_done)
                return QMessageBox.StandardButton.Cancel
            with patch.object(app_gui.QMessageBox,'question',side_effect=quit_in_confirmation):
                target._run_library_operation('inspect',self.archive)
                wait(target)
            target._poll_shutdown()
            self.assertTrue(target._cleanup_done)
        finally:
            for ball in balls:
                if not ball._cleanup_done:ball.close()
            app.processEvents()

    def test_theme_reads_remain_available_during_backup_and_writes_are_locked(self):
        from app.services.theme_registry import ThemeRegistry
        registry=ThemeRegistry(self.source.THEMES_DIR)
        with backup._locks(self.source.STORAGE_DIR):
            payload,_,_=registry.ensure_theme_contract('generic')
            self.assertTrue(payload['physical_assets'])
        with InterProcessFileLock(self.source.INDEX_DIR/'themes.transaction.lock'):
            with self.assertRaisesRegex(backup.BackupError,'library_busy'):
                backup.create_backup(self.source,self.archive)

    def test_interruption_after_every_install_rename_recovers_without_mixed_library(self):
        original=backup._move
        for stop_at in range(1,15):
            with self.subTest(stop_at=stop_at):
                destination=Settings(_env_file=None,STORAGE_DIR=self.root/f'crash-{stop_at}',LOG_DIR=self.root/'logs')
                self.assertTrue(self.ingest(destination).recover_and_project().success)
                calls=[]
                def crash(source,target):
                    original(source,target)
                    calls.append((source,target))
                    if len(calls)==stop_at:raise KeyboardInterrupt()
                try:
                    with patch.object(backup,'_move',side_effect=crash):
                        backup.restore_backup(destination,self.archive)
                except KeyboardInterrupt:pass
                self.assertTrue(self.ingest(destination).recover_and_project().success)
                bundles=self.bundle(destination).list_bundles()
                self.assertIn(len(bundles),(0,2))
                self.assertFalse((destination.STORAGE_DIR/backup.TRANSACTION).exists())
                if bundles:
                    self.assertEqual(self.bundle(destination).get_bundle(self.font_id)['font_usage'],'heading')
                    self.assertEqual(len(read_collections(destination.STORAGE_DIR/'collections.json')),1)

    def test_other_media_and_portable_path_conflicts(self):
        import wave
        gif,wav=self.root/'motion.gif',self.root/'sound.wav'
        Image.new('RGB',(16,16),'blue').save(gif)
        with wave.open(str(wav),'wb') as audio:
            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000);audio.writeframes(b'\0\0'*800)
        self.assertTrue(self.ingest(self.source).ingest([IngestCandidate(gif),IngestCandidate(wav)]).success)
        self.assertEqual(backup.create_backup(self.source,self.archive),4)
        self.assertEqual(backup.restore_backup(self.destination,self.archive),4)
        restored=self.bundle(self.destination).list_bundles()
        for original in (gif,wav):
            digest=hashlib.sha256(original.read_bytes()).hexdigest()
            item=next(item for item in restored if item['id']==digest)
            self.assertEqual((self.destination.ASSETS_DIR/item['source_key']).read_bytes(),original.read_bytes())
        with self.assertRaises(backup.BackupError):
            backup._validate_names(['assets/Example/a.ttf','assets/example/b.ttf'])
