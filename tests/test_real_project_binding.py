from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.services.real_project_binding import (
    DoraemonRealProjectBindingError,
    clear_haypile_real_project_binding,
    clear_doraemon_real_project_binding,
    resolve_haypile_real_project_root,
    resolve_doraemon_real_project_root,
    write_haypile_real_project_binding,
    write_doraemon_real_project_binding,
)


class RealProjectBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.binding_path = self.tmpdir / "binding.json"
        self.previous_env = os.environ.get("DORAEMON_REAL_PROJECT_ROOT")
        self.previous_haypile_env = os.environ.get("HAYPILE_REAL_PROJECT_ROOT")
        os.environ.pop("DORAEMON_REAL_PROJECT_ROOT", None)
        os.environ.pop("HAYPILE_REAL_PROJECT_ROOT", None)

    def tearDown(self) -> None:
        if self.previous_env is None:
            os.environ.pop("DORAEMON_REAL_PROJECT_ROOT", None)
        else:
            os.environ["DORAEMON_REAL_PROJECT_ROOT"] = self.previous_env
        if self.previous_haypile_env is None:
            os.environ.pop("HAYPILE_REAL_PROJECT_ROOT", None)
        else:
            os.environ["HAYPILE_REAL_PROJECT_ROOT"] = self.previous_haypile_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_and_resolve_binding_file(self) -> None:
        project_root = self.tmpdir / "project"
        project_root.mkdir()

        payload = write_doraemon_real_project_binding(
            project_root=project_root,
            binding_path=self.binding_path,
        )
        binding = resolve_doraemon_real_project_root(binding_path=self.binding_path)

        self.assertEqual(payload["project_root"], project_root.resolve(strict=False).as_posix())
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding.project_root, project_root.resolve(strict=False))
        self.assertEqual(binding.source, "binding_file")

    def test_env_overrides_binding_file(self) -> None:
        bound_project = self.tmpdir / "bound"
        env_project = self.tmpdir / "env"
        bound_project.mkdir()
        env_project.mkdir()
        write_doraemon_real_project_binding(
            project_root=bound_project,
            binding_path=self.binding_path,
        )
        os.environ["DORAEMON_REAL_PROJECT_ROOT"] = env_project.as_posix()

        binding = resolve_doraemon_real_project_root(binding_path=self.binding_path)

        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding.project_root, env_project.resolve(strict=False))
        self.assertEqual(binding.source, "env")

    def test_haypile_env_takes_priority_over_legacy_env(self) -> None:
        haypile_project = self.tmpdir / "haypile-env"
        legacy_project = self.tmpdir / "legacy-env"
        haypile_project.mkdir()
        legacy_project.mkdir()
        os.environ["HAYPILE_REAL_PROJECT_ROOT"] = haypile_project.as_posix()
        os.environ["DORAEMON_REAL_PROJECT_ROOT"] = legacy_project.as_posix()

        binding = resolve_haypile_real_project_root(binding_path=self.binding_path)

        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding.project_root, haypile_project.resolve(strict=False))

    def test_clear_binding_is_idempotent(self) -> None:
        project_root = self.tmpdir / "project"
        project_root.mkdir()
        write_doraemon_real_project_binding(
            project_root=project_root,
            binding_path=self.binding_path,
        )

        clear_doraemon_real_project_binding(binding_path=self.binding_path)
        clear_doraemon_real_project_binding(binding_path=self.binding_path)

        self.assertIsNone(resolve_doraemon_real_project_root(binding_path=self.binding_path))

    def test_haypile_binding_writer_uses_haypile_contract_name(self) -> None:
        project_root = self.tmpdir / "project"
        project_root.mkdir()

        payload = write_haypile_real_project_binding(project_root=project_root, binding_path=self.binding_path)
        clear_haypile_real_project_binding(binding_path=self.binding_path)

        self.assertEqual(payload["binding_type"], "haypile_real_project_binding")
        self.assertFalse(self.binding_path.exists())

    def test_rejects_missing_project_root(self) -> None:
        with self.assertRaisesRegex(DoraemonRealProjectBindingError, "existing directory"):
            write_doraemon_real_project_binding(
                project_root=self.tmpdir / "missing",
                binding_path=self.binding_path,
            )


if __name__ == "__main__":
    unittest.main()
