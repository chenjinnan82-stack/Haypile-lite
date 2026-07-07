from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.services.real_project_operations import (
    HaypileRealProjectOperationError,
    execute_haypile_minimal_real_project_reapply,
    execute_haypile_minimal_real_project_rollback,
)


class RealProjectOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reapply_copies_hashed_rehearsal_files_and_updates_reports(self) -> None:
        project_root, source_root, written_files = self._write_project(state="rolled_back")

        result = execute_haypile_minimal_real_project_reapply(
            project_root=project_root,
            human_confirmed=True,
        )

        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["passed"])
        self.assertEqual(result["written_files"], written_files)
        for path_ref in written_files:
            self.assertEqual((project_root / path_ref).read_bytes(), (source_root / path_ref).read_bytes())
        apply_report = self._read_json(project_root / "haypile-rehearsal-reports" / "real-project-minimal-apply-report.json")
        verification = self._read_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-post-apply-verification.json"
        )
        rollback = self._read_json(project_root / "haypile-rehearsal-reports" / "real-project-minimal-rollback-report.json")
        self.assertEqual(apply_report["status"], "applied")
        self.assertEqual(verification["status"], "verified")
        self.assertEqual(verification["remote_urls"], [])
        self.assertEqual(verification["unregistered_assets"], [])
        self.assertEqual(rollback["status"], "superseded_by_reapply")

    def test_reapply_requires_human_confirmation(self) -> None:
        project_root, _source_root, _written_files = self._write_project(state="rolled_back")

        with self.assertRaisesRegex(HaypileRealProjectOperationError, "human confirmation"):
            execute_haypile_minimal_real_project_reapply(
                project_root=project_root,
                human_confirmed=False,
            )

    def test_reapply_rejects_source_hash_mismatch(self) -> None:
        project_root, source_root, _written_files = self._write_project(state="rolled_back")
        (source_root / "haypile-hydration.html").write_text("changed", encoding="utf-8")

        with self.assertRaisesRegex(HaypileRealProjectOperationError, "hash mismatch"):
            execute_haypile_minimal_real_project_reapply(
                project_root=project_root,
                human_confirmed=True,
            )

    def test_rollback_removes_reapplied_files_and_writes_restored_report(self) -> None:
        project_root, _source_root, written_files = self._write_project(state="rolled_back")
        execute_haypile_minimal_real_project_reapply(
            project_root=project_root,
            human_confirmed=True,
        )

        result = execute_haypile_minimal_real_project_rollback(
            project_root=project_root,
            human_confirmed=True,
        )

        self.assertEqual(result["status"], "restored")
        self.assertTrue(result["passed"])
        self.assertEqual(result["removed_files"], written_files)
        self.assertEqual(result["remaining_written_files"], [])
        for path_ref in written_files:
            self.assertFalse((project_root / path_ref).exists())
        rollback = self._read_json(project_root / "haypile-rehearsal-reports" / "real-project-minimal-rollback-report.json")
        self.assertEqual(rollback["status"], "restored")
        self.assertEqual(rollback["remaining_written_files"], [])

    def test_rollback_requires_applied_verified_state(self) -> None:
        project_root, _source_root, _written_files = self._write_project(state="rolled_back")

        with self.assertRaisesRegex(HaypileRealProjectOperationError, "applied_verified"):
            execute_haypile_minimal_real_project_rollback(
                project_root=project_root,
                human_confirmed=True,
            )

    def _write_project(self, *, state: str) -> tuple[Path, Path, list[str]]:
        project_root = self.tmpdir / "signal-pool-demo"
        source_root = self.tmpdir / "signal-pool-demo-haypile-rehearsal"
        project_root.mkdir()
        source_root.mkdir()
        for path_ref, content in self._file_contents().items():
            source = source_root / path_ref
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)
        written_files = list(self._file_contents().keys())
        entries = [
            {
                "path_ref": path_ref,
                "existed_before": False,
                "source_sha256": hashlib.sha256((source_root / path_ref).read_bytes()).hexdigest(),
                "source_size": (source_root / path_ref).stat().st_size,
            }
            for path_ref in written_files
        ]
        self._write_json(
            project_root / ".haypile" / "rollback" / "haypile-real-project-minimal-apply.json",
            {
                "manifest_type": "haypile_real_project_minimal_apply_rollback_manifest",
                "version": "haypile_real_project_minimal_apply_rollback_manifest.v1",
                "source_rehearsal_root": source_root.as_posix(),
                "entries": entries,
            },
        )
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-apply-report.json",
            {
                "status": "applied",
                "passed": True,
                "source_rehearsal_root": source_root.as_posix(),
                "written_files": written_files,
            },
        )
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-post-apply-verification.json",
            {"status": "verified", "passed": True, "remote_urls": [], "unregistered_assets": []},
        )
        if state == "rolled_back":
            self._write_json(
                project_root / "haypile-rehearsal-reports" / "real-project-minimal-rollback-report.json",
                {
                    "status": "restored",
                    "passed": True,
                    "removed_files": written_files,
                    "remaining_written_files": [],
                },
            )
        elif state == "applied_verified":
            for path_ref, content in self._file_contents().items():
                target = project_root / path_ref
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        return project_root, source_root, written_files

    @staticmethod
    def _file_contents() -> dict[str, bytes]:
        return {
            "haypile-hydration.html": b"<!doctype html><title>Haypile</title>",
            "assets/images/water-drop.svg": b"<svg></svg>",
            "assets/css/hydration-theme.css": b":root { --hydration-primary: #38bdf8; }",
            "public/assets/images/water-drop.svg": b"<svg></svg>",
            "public/assets/css/hydration-theme.css": b":root { --hydration-primary: #38bdf8; }",
        }

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
