from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtMultimedia import QSoundEffect


logger = logging.getLogger(__name__)

SOUND_FILES = {
    "nav": "haypile-nav.wav",
    "intake": "haypile-intake.wav",
    "duplicate": "haypile-duplicate.wav",
    "error": "haypile-error.wav",
}


class SoundFeedbackController(QObject):
    def __init__(
        self,
        sounds_dir: Path,
        *,
        enabled: bool = True,
        volume: float = 0.16,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sounds_dir = Path(sounds_dir)
        self._enabled = bool(enabled)
        self._volume = max(0.0, min(float(volume), 1.0))
        self._effects: dict[str, QSoundEffect] = {}
        self._unavailable: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self.stop_all()

    def prepare(self) -> int:
        if not self._enabled or not self._playback_allowed():
            return 0
        return sum(self._effect_for(cue) is not None for cue in SOUND_FILES)

    def play(self, cue: str) -> bool:
        if not self._enabled or not self._playback_allowed():
            return False
        effect = self._effect_for(cue)
        if effect is None:
            return False
        try:
            self.stop_all()
            effect.play()
        except RuntimeError:
            self._effects.pop(cue, None)
            return False
        return True

    def stop_all(self) -> None:
        for effect in tuple(self._effects.values()):
            try:
                effect.stop()
            except RuntimeError:
                continue

    @staticmethod
    def _playback_allowed() -> bool:
        app = QGuiApplication.instance()
        platform_name = getattr(app, "platformName", None)
        return bool(
            callable(platform_name)
            and platform_name().strip().lower() != "offscreen"
        )

    def _effect_for(self, cue: str) -> QSoundEffect | None:
        if cue not in SOUND_FILES or cue in self._unavailable:
            return None
        existing = self._effects.get(cue)
        if existing is not None:
            if existing.status() != QSoundEffect.Status.Error:
                return existing
            self._effects.pop(cue, None)
            existing.deleteLater()
        source = self._sounds_dir / SOUND_FILES[cue]
        if not source.is_file():
            self._unavailable.add(cue)
            logger.warning("Sound feedback asset is unavailable cue=%s", cue)
            return None
        try:
            effect = QSoundEffect(self)
            effect.setSource(QUrl.fromLocalFile(str(source.resolve())))
            effect.setLoopCount(1)
            effect.setVolume(self._volume)
        except RuntimeError:
            self._unavailable.add(cue)
            return None
        self._effects[cue] = effect
        if effect.status() == QSoundEffect.Status.Error:
            self._effects.pop(cue, None)
            effect.deleteLater()
            return None
        return effect
