from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

import app_gui
from app.core.config import Settings


FRAME_WIDTH = 1000
FRAME_HEIGHT = 760
SCALE = 0.58
LABEL_HEIGHT = 34


def _render_state(
    app: QApplication,
    *,
    storage_root: Path,
    language: str,
    page: str,
    ball_x: int,
) -> QImage:
    settings = Settings(
        _env_file=None,
        BASE_DIR=ROOT,
        STORAGE_DIR=storage_root,
        LOG_DIR=storage_root / "logs",
        IPC_AUTHKEY="visual-smoke",
    )
    ball = app_gui.HaypileFloatingBall(
        settings=settings,
        initial_state={"language": language, "low_power_enabled": True},
        deferred_runtime=True,
    )
    available = QRect(0, 0, FRAME_WIDTH, FRAME_HEIGHT)
    ball._available_geometry = lambda: QRect(available)
    ball.material_panel._bundle_service = lambda: SimpleNamespace(
        list_bundles=lambda **_filters: [],
        get_latest_batch=lambda: None,
        theme_recoveries=[],
    )
    ball.move(ball_x, 330)
    ball.show()
    ball._handle_quick_menu_action(page)
    app.processEvents()

    image = QImage(FRAME_WIDTH, FRAME_HEIGHT, QImage.Format.Format_ARGB32)
    image.fill(QColor("#F6F1E4"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.drawImage(ball.pos(), ball.grab().toImage())
    menu = ball.quick_menu
    painter.drawImage(menu.frameGeometry().topLeft(), menu.grab().toImage())
    painter.end()

    drawer = menu._drawer_global_rect
    if not available.contains(drawer):
        raise RuntimeError(f"Drawer escaped the visual frame: {language}/{page}")
    menu_origin = menu.frameGeometry().topLeft()
    for action, _icon, _label in menu.actions:
        slot = menu._slot_rect(action).toAlignedRect().translated(menu_origin)
        label = menu._label_rect(action).toAlignedRect().translated(menu_origin)
        if drawer.intersects(slot) or drawer.intersects(label):
            raise RuntimeError(f"Drawer overlaps {action}: {language}/{page}")

    ball.close()
    app.processEvents()
    return image


def render_report(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    states = [
        ("中文 · 素材 · 右侧", "zh", "assets", 180),
        ("中文 · Agent · 右侧", "zh", "agent", 180),
        ("中文 · 设置 · 右侧", "zh", "settings", 180),
        ("EN · Assets · left", "en", "assets", 918),
        ("EN · Agent · left", "en", "agent", 918),
        ("EN · Settings · left", "en", "settings", 918),
    ]
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        frames = [
            (
                label,
                _render_state(
                    app,
                    storage_root=root / f"state-{index}",
                    language=language,
                    page=page,
                    ball_x=ball_x,
                ),
            )
            for index, (label, language, page, ball_x) in enumerate(states)
        ]

    tile_width = round(FRAME_WIDTH * SCALE)
    tile_height = round(FRAME_HEIGHT * SCALE)
    sheet = QImage(
        tile_width * 3,
        (tile_height + LABEL_HEIGHT) * 2,
        QImage.Format.Format_ARGB32,
    )
    sheet.fill(QColor("#FFF9EA"))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setPen(QColor("#4E5F3D"))
    painter.setFont(QFont("Sans Serif", 11))
    for index, (label, frame) in enumerate(frames):
        column = index % 3
        row = index // 3
        x = column * tile_width
        y = row * (tile_height + LABEL_HEIGHT)
        painter.drawImage(
            QRectF(x, y, tile_width, tile_height),
            frame,
        )
        painter.drawText(
            QRectF(x, y + tile_height, tile_width, LABEL_HEIGHT),
            Qt.AlignmentFlag.AlignCenter,
            label,
        )
    painter.end()

    destination = output_dir / "hub-layout.png"
    if not sheet.save(str(destination)):
        raise RuntimeError(f"Could not write {destination}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the attached Haypile hub states.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    render_report(args.out.resolve())


if __name__ == "__main__":
    main()
