from __future__ import annotations

import math
import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from app.gui.intake_visual import (
        GIF_EXPAND_SECONDS,
        GIF_TICK_SECONDS,
        IntakeEntryRenderer,
        IntakeVisualState,
    )
except ImportError as exc:  # pragma: no cover - optional desktop runtime
    QApplication = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI runtime unavailable: {_IMPORT_ERROR}")
class IntakeEntryRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.renderer = IntakeEntryRenderer(
            Path(__file__).resolve().parents[1] / "ui_assets"
        )

    def render(self, state: IntakeVisualState) -> QImage:
        image = QImage(300, 300, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.renderer.paint(
            painter,
            QRectF(0, 0, 300, 300),
            QRectF(79.5, 79.5, 141, 141),
            state,
        )
        painter.end()
        return image

    def test_three_entry_types_are_distinct_and_gif_has_three_colors(self) -> None:
        leaf = self.render(IntakeVisualState(kind="leaf", open_progress=1.0))
        audio = self.render(
            IntakeVisualState(
                kind="audio",
                open_progress=1.0,
                has_direction=True,
                direction_angle=0.0,
            )
        )
        gif = self.render(
            IntakeVisualState(
                kind="gif",
                open_progress=1.0,
                gif_elapsed_seconds=GIF_EXPAND_SECONDS,
            )
        )
        self.assertNotEqual(leaf, audio)
        self.assertNotEqual(leaf, gif)
        self.assertNotEqual(audio, gif)

        colors = {
            gif.pixelColor(x, y).name().upper()
            for y in range(gif.height())
            for x in range(gif.width())
            if gif.pixelColor(x, y).alpha() > 200
        }
        self.assertTrue({"#78945B", "#D5A73D", "#D9795F"} <= colors)

        colored_x = [
            x
            for y in range(gif.height())
            for x in range(gif.width())
            if gif.pixelColor(x, y).name().upper()
            in {"#78945B", "#D5A73D", "#D9795F"}
        ]
        self.assertGreaterEqual(max(colored_x) - min(colored_x) + 1, 68)
        self.assertGreater(max(colored_x) - min(colored_x) + 1, 52)

    def test_gif_rhythm_uses_relative_time_and_coral_gold_green_order(self) -> None:
        lifted = []
        for step in range(3):
            poses = self.renderer.gif_frame_poses(
                IntakeVisualState(
                    kind="gif",
                    open_progress=1.0,
                    gif_elapsed_seconds=(
                        GIF_EXPAND_SECONDS
                        + step * GIF_TICK_SECONDS
                        + GIF_TICK_SECONDS / 2
                    ),
                )
            )
            lifted.append(max(range(3), key=lambda index: poses[index].lift))
        self.assertEqual(lifted, [2, 1, 0])

        start = IntakeVisualState(
            kind="gif",
            open_progress=1.0,
            gif_elapsed_seconds=GIF_EXPAND_SECONDS + GIF_TICK_SECONDS / 2,
        )
        self.assertEqual(
            self.renderer.gif_frame_poses(start),
            self.renderer.gif_frame_poses(start),
        )

    def test_gif_suction_is_coral_gold_green_and_ends_invisible_at_42_percent(self) -> None:
        mid = self.renderer.gif_frame_poses(
            IntakeVisualState(
                kind="gif",
                open_progress=1.0,
                gif_suction_progress=0.5,
                gif_elapsed_seconds=GIF_EXPAND_SECONDS,
            )
        )
        self.assertGreater(mid[2].suction_progress, mid[1].suction_progress)
        self.assertGreater(mid[1].suction_progress, mid[0].suction_progress)

        final = self.renderer.gif_frame_poses(
            IntakeVisualState(
                kind="gif",
                open_progress=1.0,
                gif_suction_progress=1.0,
                gif_elapsed_seconds=GIF_EXPAND_SECONDS,
            )
        )
        for pose in final:
            self.assertAlmostEqual(pose.scale, 0.42)
            self.assertEqual(pose.opacity, 0.0)
        final_image = self.render(
            IntakeVisualState(
                kind="gif",
                open_progress=1.0,
                gif_suction_progress=1.0,
                gif_elapsed_seconds=GIF_EXPAND_SECONDS,
            )
        )
        card_colors = {"#78945B", "#D5A73D", "#D9795F"}
        self.assertFalse(
            any(
                final_image.pixelColor(x, y).name().upper() in card_colors
                for y in range(final_image.height())
                for x in range(final_image.width())
            )
        )

    def test_low_power_is_static_and_fixed_state_is_deterministic(self) -> None:
        low_power_first = self.render(
            IntakeVisualState(
                kind="gif",
                open_progress=1.0,
                gif_elapsed_seconds=0.0,
                low_power=True,
            )
        )
        low_power_later = self.render(
            IntakeVisualState(
                kind="gif",
                open_progress=1.0,
                gif_elapsed_seconds=9.0,
                low_power=True,
            )
        )
        self.assertEqual(low_power_first, low_power_later)

        fixed = IntakeVisualState(
            kind="audio",
            open_progress=0.72,
            audio_suction_progress=0.35,
            direction_angle=math.pi / 2,
            has_direction=True,
        )
        self.assertEqual(self.render(fixed), self.render(fixed))


if __name__ == "__main__":
    unittest.main()
