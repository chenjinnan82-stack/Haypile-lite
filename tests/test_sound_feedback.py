from __future__ import annotations

import hashlib
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.gui.sound_feedback import SOUND_FILES, SoundFeedbackController


class _FakeSoundEffect:
    instances: list["_FakeSoundEffect"] = []
    initial_status = "ready"

    class Status:
        Ready = "ready"
        Error = "error"

    def __init__(self, parent) -> None:
        self.parent = parent
        self.source = ""
        self.loop_count = 0
        self.volume = 0.0
        self.play_count = 0
        self.stop_count = 0
        self.deleted = False
        self.status_value = self.initial_status
        self.instances.append(self)

    def setSource(self, source) -> None:
        self.source = source.toLocalFile()

    def setLoopCount(self, loop_count: int) -> None:
        self.loop_count = loop_count

    def setVolume(self, volume: float) -> None:
        self.volume = volume

    def play(self) -> None:
        self.play_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def status(self):
        return self.status_value

    def deleteLater(self) -> None:
        self.deleted = True


class SoundFeedbackControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.sounds_dir = Path(self.tempdir.name)
        for filename in SOUND_FILES.values():
            (self.sounds_dir / filename).write_bytes(b"RIFF")
        _FakeSoundEffect.instances.clear()
        _FakeSoundEffect.initial_status = _FakeSoundEffect.Status.Ready

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_prepare_and_switch_reuse_four_low_volume_effects(self) -> None:
        with (
            patch("app.gui.sound_feedback.QSoundEffect", _FakeSoundEffect),
            patch.object(SoundFeedbackController, "_playback_allowed", return_value=True),
        ):
            controller = SoundFeedbackController(self.sounds_dir)
            self.assertEqual(controller.prepare(), 4)
            self.assertEqual(controller.prepare(), 4)
            self.assertEqual(len(_FakeSoundEffect.instances), 4)
            for effect in _FakeSoundEffect.instances:
                self.assertEqual(effect.loop_count, 1)
                self.assertAlmostEqual(effect.volume, 0.16)
                self.assertTrue(Path(effect.source).is_file())
                effect.stop_count = 0

            self.assertTrue(controller.play("nav"))
            nav = controller._effects["nav"]
            self.assertEqual(nav.play_count, 1)
            self.assertTrue(controller.play("intake"))
            self.assertEqual(nav.stop_count, 2)
            self.assertEqual(controller._effects["intake"].play_count, 1)

    def test_disabled_unknown_and_missing_cues_are_silent(self) -> None:
        with (
            patch("app.gui.sound_feedback.QSoundEffect", _FakeSoundEffect),
            patch.object(SoundFeedbackController, "_playback_allowed", return_value=True),
        ):
            controller = SoundFeedbackController(self.sounds_dir, enabled=False)
            self.assertFalse(controller.play("nav"))
            self.assertEqual(controller.prepare(), 0)
            self.assertEqual(_FakeSoundEffect.instances, [])

            controller.set_enabled(True)
            self.assertFalse(controller.play("unknown"))
            (self.sounds_dir / SOUND_FILES["error"]).unlink()
            with self.assertLogs("app.gui.sound_feedback", level="WARNING"):
                self.assertFalse(controller.play("error"))
            self.assertEqual(_FakeSoundEffect.instances, [])

    def test_disabling_stops_loaded_effects(self) -> None:
        with (
            patch("app.gui.sound_feedback.QSoundEffect", _FakeSoundEffect),
            patch.object(SoundFeedbackController, "_playback_allowed", return_value=True),
        ):
            controller = SoundFeedbackController(self.sounds_dir)
            controller.prepare()
            for effect in _FakeSoundEffect.instances:
                effect.stop_count = 0
            controller.set_enabled(False)
            self.assertFalse(controller.enabled)
            self.assertTrue(all(effect.stop_count == 1 for effect in _FakeSoundEffect.instances))
            self.assertFalse(controller.play("duplicate"))

    def test_error_effect_is_replaced_and_no_gui_application_stays_silent(self) -> None:
        class _NoGuiApplication:
            @staticmethod
            def instance():
                return None

        with patch("app.gui.sound_feedback.QGuiApplication", _NoGuiApplication):
            controller = SoundFeedbackController(self.sounds_dir)
            self.assertEqual(controller.prepare(), 0)

        with (
            patch("app.gui.sound_feedback.QSoundEffect", _FakeSoundEffect),
            patch.object(SoundFeedbackController, "_playback_allowed", return_value=True),
        ):
            controller = SoundFeedbackController(self.sounds_dir)
            controller.prepare()
            failed = controller._effects["nav"]
            failed.status_value = _FakeSoundEffect.Status.Error
            self.assertTrue(controller.play("nav"))
            self.assertTrue(failed.deleted)
            self.assertIsNot(controller._effects["nav"], failed)

            _FakeSoundEffect.initial_status = _FakeSoundEffect.Status.Error
            controller._effects.pop("error", None)
            self.assertFalse(controller.play("error"))


class SoundAssetContractTests(unittest.TestCase):
    EXPECTED = {
        "haypile-nav.wav": "f0c897e44c92bb65e8093a5109257552f684099d57bef1204dc0580cc6db0f91",
        "haypile-intake.wav": "5c202ce083b5cf6695f6fff93983d799417d56d212b12b4c8e517d72da5840be",
        "haypile-duplicate.wav": "2c5bf5eff2346d802a22dcccf34548f67984b8e052065edf85d98b326563d8b3",
        "haypile-error.wav": "0f69cd383abf29f32d108107733001ac5ede4c8fb4458e5bf3fd847161d5d072",
    }

    def test_approved_assets_are_original_pcm_wav_bytes(self) -> None:
        sounds_dir = Path(__file__).resolve().parents[1] / "ui_assets" / "sounds"
        self.assertEqual(set(SOUND_FILES.values()), set(self.EXPECTED))
        for filename, expected_sha256 in self.EXPECTED.items():
            path = sounds_dir / filename
            payload = path.read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_sha256)
            with wave.open(str(path), "rb") as sound:
                self.assertEqual(sound.getnchannels(), 1)
                self.assertEqual(sound.getsampwidth(), 2)
                self.assertEqual(sound.getframerate(), 48_000)


if __name__ == "__main__":
    unittest.main()
