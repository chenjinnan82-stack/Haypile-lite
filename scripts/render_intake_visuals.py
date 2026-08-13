from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.gui.intake_visual import (
    GIF_EXPAND_SECONDS,
    GIF_SUCTION_SECONDS,
    GIF_TICK_SECONDS,
    IntakeEntryRenderer,
    IntakeVisualState,
)


SIZE = 300
PANEL = QRectF(79.5, 79.5, 141, 141)


def _smooth(value: float) -> float:
    value = max(0.0, min(value, 1.0))
    return value * value * (3.0 - 2.0 * value)


def _render(renderer: IntakeEntryRenderer, state: IntakeVisualState) -> QImage:
    image = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    image.fill(QColor("#F6F1E4"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.paint(
        painter,
        QRectF(0, 0, SIZE, SIZE),
        PANEL,
        state,
    )
    painter.end()
    return image


def _save_sheet(
    path: Path,
    frames: list[tuple[str, QImage]],
) -> None:
    sheet = QImage(
        SIZE * len(frames),
        SIZE + 42,
        QImage.Format.Format_ARGB32,
    )
    sheet.fill(QColor("#FFF9EA"))
    painter = QPainter(sheet)
    painter.setPen(QColor("#4E5F3D"))
    painter.setFont(QFont("Sans Serif", 12))
    for index, (label, frame) in enumerate(frames):
        x = index * SIZE
        painter.drawImage(x, 0, frame)
        painter.drawText(
            QRectF(x, SIZE, SIZE, 42),
            Qt.AlignmentFlag.AlignCenter,
            label,
        )
    painter.end()
    if not sheet.save(str(path)):
        raise RuntimeError(f"Could not write {path}")


def _to_pillow(image: QImage) -> Image.Image:
    return Image.frombuffer(
        "RGBA",
        (image.width(), image.height()),
        bytes(image.constBits()),
        "raw",
        "BGRA",
        image.bytesPerLine(),
        1,
    ).copy()


def render_reports(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    renderer = IntakeEntryRenderer(ROOT / "ui_assets")

    _save_sheet(
        output_dir / "entry-types.png",
        [
            ("IMAGE", _render(renderer, IntakeVisualState(kind="leaf", open_progress=1.0))),
            (
                "AUDIO",
                _render(
                    renderer,
                    IntakeVisualState(
                        kind="audio",
                        open_progress=1.0,
                        has_direction=True,
                        direction_angle=0.0,
                    ),
                ),
            ),
            (
                "GIF",
                _render(
                    renderer,
                    IntakeVisualState(
                        kind="gif",
                        open_progress=1.0,
                        gif_elapsed_seconds=GIF_EXPAND_SECONDS,
                    ),
                ),
            ),
        ],
    )

    motion_states = [
        ("0 ms", 0.0, 0.0),
        ("45 ms", 0.045, 0.0),
        ("90 ms", 0.09, 0.0),
        ("coral", GIF_EXPAND_SECONDS + GIF_TICK_SECONDS / 2, 0.0),
        ("gold", GIF_EXPAND_SECONDS + GIF_TICK_SECONDS * 1.5, 0.0),
        ("green", GIF_EXPAND_SECONDS + GIF_TICK_SECONDS * 2.5, 0.0),
        ("intake 25%", GIF_EXPAND_SECONDS, 0.25),
        ("intake 55%", GIF_EXPAND_SECONDS, 0.55),
        ("intake 100%", GIF_EXPAND_SECONDS, 1.0),
    ]
    _save_sheet(
        output_dir / "gif-motion-sheet.png",
        [
            (
                label,
                _render(
                    renderer,
                    IntakeVisualState(
                        kind="gif",
                        open_progress=1.0,
                        gif_suction_progress=suction,
                        gif_elapsed_seconds=elapsed,
                    ),
                ),
            )
            for label, elapsed, suction in motion_states
        ],
    )

    frames: list[Image.Image] = []
    frame_ms = 30
    suction_ms = round(GIF_SUCTION_SECONDS * 1000)
    total_ms = 210 + suction_ms + 170
    for elapsed_ms in range(0, total_ms + frame_ms, frame_ms):
        if elapsed_ms <= 210:
            open_progress = _smooth(elapsed_ms / 210)
            suction = 0.0
        elif elapsed_ms <= 210 + suction_ms:
            open_progress = 1.0
            suction = _smooth((elapsed_ms - 210) / suction_ms)
        else:
            open_progress = 1.0 - _smooth(
                (elapsed_ms - 210 - suction_ms) / 170
            )
            suction = 1.0
        frames.append(
            _to_pillow(
                _render(
                    renderer,
                    IntakeVisualState(
                        kind="gif",
                        open_progress=open_progress,
                        gif_suction_progress=suction,
                        gif_elapsed_seconds=elapsed_ms / 1000.0,
                    ),
                )
            )
        )
    frames[0].save(
        output_dir / "gif-programmatic-intake.gif",
        save_all=True,
        append_images=frames[1:],
        duration=frame_ms,
        loop=0,
        disposal=2,
    )
    del app


def main() -> None:
    parser = argparse.ArgumentParser(description="Render deterministic intake visuals.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    render_reports(args.out.resolve())


if __name__ == "__main__":
    main()
