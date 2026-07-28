from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtSvg import QSvgRenderer


GIF_TICK_SECONDS = 0.11
GIF_FRAME_STAGGER_SECONDS = 0.045
GIF_EXPAND_SECONDS = 0.18
GIF_SUCTION_SECONDS = 0.19


@dataclass(frozen=True)
class IntakeVisualState:
    kind: Literal["leaf", "audio", "gif"] = "leaf"
    open_progress: float = 0.0
    audio_suction_progress: float = 0.0
    gif_suction_progress: float = 0.0
    gif_elapsed_seconds: float = 0.0
    direction_angle: float = -math.pi / 2
    has_direction: bool = False
    low_power: bool = False


@dataclass(frozen=True)
class GifFramePose:
    color: str
    scale: float
    opacity: float
    lift: float
    suction_progress: float


class IntakeEntryRenderer:
    """Draws intake visuals without constructing the Haypile application."""

    def __init__(self, assets_dir: Path) -> None:
        self.assets_dir = Path(assets_dir)
        self._drop_leaf_frame_runs = self._load_drop_leaf_frame_runs()
        self._drop_leaf_frame_renderer = QSvgRenderer(
            str(self.assets_dir / "drop-leaf-frame.svg")
        )
        self._drop_leaf_renderers = [
            renderer
            for renderer in (
                QSvgRenderer(str(self.assets_dir / f"drop-leaf-{index}.svg"))
                for index in range(1, 6)
            )
            if renderer.isValid()
        ]
        self._audio_leaf_layer_key: tuple[int, int, int] | None = None
        self._audio_leaf_layer_buffers: tuple[QPixmap, ...] = ()

    @property
    def leaf_frame_runs(self) -> tuple[tuple[int, ...], ...]:
        return tuple(self._drop_leaf_frame_runs)

    @property
    def leaf_renderer_count(self) -> int:
        return len(self._drop_leaf_renderers)

    def paint(
        self,
        painter: QPainter,
        canvas_rect: QRectF,
        panel_rect: QRectF,
        state: IntakeVisualState,
        *,
        device_pixel_ratio: float = 1.0,
    ) -> None:
        if state.kind == "audio":
            self._draw_audio_intake(
                painter,
                canvas_rect,
                panel_rect,
                state,
                device_pixel_ratio,
            )
            return
        self._draw_drop_leaf_frame(
            painter,
            canvas_rect,
            panel_rect,
            state.open_progress,
        )
        self._draw_drop_center_cutout(painter, panel_rect, state.open_progress)
        if state.kind == "gif":
            self._draw_gif_intake(painter, panel_rect, state)

    def gif_frame_poses(self, state: IntakeVisualState) -> tuple[GifFramePose, ...]:
        progress = self._clamp(state.open_progress)
        suction = self._clamp(state.gif_suction_progress)
        elapsed = max(0.0, state.gif_elapsed_seconds)
        rhythm_elapsed = max(0.0, elapsed - GIF_EXPAND_SECONDS)
        rhythm_order = (2, 1, 0)
        active_frame = rhythm_order[int(rhythm_elapsed / GIF_TICK_SECONDS) % 3]
        lift_phase = (rhythm_elapsed % GIF_TICK_SECONDS) / GIF_TICK_SECONDS
        lift = 3.0 * math.sin(lift_phase * math.pi)
        colors = ("#78945B", "#D5A73D", "#D9795F")
        suction_orders = (2, 1, 0)
        poses: list[GifFramePose] = []
        for index, (color, suction_order) in enumerate(zip(colors, suction_orders)):
            suction_start = suction_order * (
                GIF_FRAME_STAGGER_SECONDS / GIF_SUCTION_SECONDS
            )
            layer_suction = self._smooth(
                (suction - suction_start) / max(0.001, 1.0 - suction_start)
            )
            frame_lift = (
                lift
                if not state.low_power
                and suction <= 0.0
                and elapsed >= GIF_EXPAND_SECONDS
                and index == active_frame
                else 0.0
            )
            poses.append(
                GifFramePose(
                    color=color,
                    scale=(0.82 + 0.18 * progress) * (1.0 - 0.58 * layer_suction),
                    opacity=progress * (1.0 - layer_suction),
                    lift=frame_lift,
                    suction_progress=layer_suction,
                )
            )
        return tuple(poses)

    def _draw_audio_intake(
        self,
        painter: QPainter,
        canvas_rect: QRectF,
        panel_rect: QRectF,
        state: IntakeVisualState,
        device_pixel_ratio: float,
    ) -> None:
        progress = self._clamp(state.open_progress)
        suction = self._clamp(state.audio_suction_progress)
        if progress <= 0.0:
            return
        self._draw_audio_leaf_nest(
            painter,
            canvas_rect,
            panel_rect,
            progress,
            suction,
            state,
            device_pixel_ratio,
        )
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.transparent)
        painter.drawPath(self.audio_center_path(panel_rect, progress))
        painter.restore()

    def _draw_audio_leaf_nest(
        self,
        painter: QPainter,
        canvas_rect: QRectF,
        panel_rect: QRectF,
        progress: float,
        suction: float,
        state: IntakeVisualState,
        device_pixel_ratio: float,
    ) -> None:
        if not self._drop_leaf_renderers:
            self._draw_drop_leaf_frame(
                painter,
                canvas_rect,
                panel_rect,
                progress,
                leaf_width_scale=0.78,
            )
            return

        layers = (
            (
                QColor("#A8A96F"),
                (
                    (0, -165, 0.96, 0.40, 28, 0.42),
                    (1, -105, 0.92, 0.37, -25, 0.39),
                    (0, -45, 0.98, 0.41, 30, 0.43),
                    (1, 15, 0.93, 0.38, -26, 0.40),
                    (0, 75, 0.97, 0.40, 27, 0.42),
                    (1, 135, 0.92, 0.37, -24, 0.39),
                ),
            ),
            (
                QColor("#C4963C"),
                (
                    (2, -145, 0.86, 0.34, -24, 0.72),
                    (2, -85, 0.81, 0.32, 26, 0.69),
                    (2, -25, 0.88, 0.35, -22, 0.73),
                    (2, 35, 0.82, 0.33, 28, 0.70),
                    (2, 95, 0.87, 0.34, -25, 0.72),
                    (2, 155, 0.80, 0.32, 24, 0.69),
                ),
            ),
            (
                QColor("#4D582F"),
                (
                    (4, 179, 0.70, 0.33, -10, 0.92),
                    (3, -121, 0.75, 0.30, 8, 0.90),
                    (4, -61, 0.69, 0.32, -9, 0.93),
                    (3, -1, 0.74, 0.30, 11, 0.90),
                    (4, 59, 0.71, 0.33, -8, 0.92),
                    (3, 119, 0.76, 0.30, 10, 0.91),
                ),
            ),
        )
        buffers = self._audio_leaf_buffers(canvas_rect, device_pixel_ratio)
        center = panel_rect.center()
        source_angle = state.direction_angle if state.has_direction else -math.pi / 2
        source_degrees = math.degrees(source_angle)
        source_vector = QPointF(math.cos(source_angle), math.sin(source_angle))
        layer_delays = (0.0, 25.0 / 210.0, 50.0 / 210.0)
        tint_opacities = (0.82, 0.92, 1.0)

        for layer_index, ((color, placements), buffer) in enumerate(zip(layers, buffers)):
            buffer.fill(Qt.GlobalColor.transparent)
            layer_painter = QPainter(buffer)
            layer_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            layer_painter.setClipPath(self._drop_outer_path(panel_rect))
            for leaf_index, angle, radius_scale, width_scale, rotation_offset, opacity in placements:
                if leaf_index >= len(self._drop_leaf_renderers):
                    continue
                renderer = self._drop_leaf_renderers[leaf_index]
                svg_size = renderer.defaultSize()
                if svg_size.width() <= 0:
                    continue

                delta = (angle - source_degrees + 180.0) % 360.0 - 180.0
                source_weight = (
                    max(0.0, math.cos(math.radians(delta)))
                    if abs(delta) <= 65.0
                    else 0.0
                )
                delay = max(0.0, layer_delays[layer_index] - source_weight * (15.0 / 210.0))
                leaf_progress = self._staggered_progress(progress, delay)
                suction_delay = (55.0 / 150.0) * (abs(delta) / 180.0)
                leaf_suction = self._staggered_progress(suction, suction_delay)

                radians = math.radians(angle)
                radius = panel_rect.width() * radius_scale
                full_center = center + QPointF(
                    math.cos(radians) * radius,
                    math.sin(radians) * radius,
                )
                slide = 0.98 - 0.18 * leaf_progress
                draw_center = center + (full_center - center) * slide
                greeting = (
                    (6.0 if layer_index < 2 else 1.8)
                    * source_weight
                    * leaf_progress
                    * (1.0 - leaf_suction)
                )
                draw_center += source_vector * greeting
                toward_center = center - draw_center
                toward_length = math.hypot(toward_center.x(), toward_center.y())
                if toward_length > 0.5:
                    draw_center += toward_center * ((9.0 * leaf_suction) / toward_length)

                open_scale = 0.72 + 0.28 * leaf_progress
                suction_scale = 1.0 - 0.06 * leaf_suction
                draw_width = (
                    min(canvas_rect.width(), canvas_rect.height())
                    * width_scale
                    * open_scale
                    * 0.68
                    * suction_scale
                )
                draw_height = draw_width * svg_size.height() / svg_size.width()
                direction_turn = (
                    -math.sin(math.radians(delta))
                    * 10.0
                    * source_weight
                    * (1.0 - leaf_suction)
                )
                draw_rotation = (
                    angle
                    - 90
                    + rotation_offset * (1.0 - 0.38 * leaf_suction)
                    + direction_turn
                )

                layer_painter.save()
                layer_painter.setOpacity(
                    opacity
                    * (0.18 + 0.82 * leaf_progress)
                    * (1.0 - 0.68 * leaf_suction)
                )
                layer_painter.translate(draw_center)
                layer_painter.rotate(draw_rotation)
                clip_height = 0.42 if leaf_index in {3, 4} else 0.48
                layer_painter.setClipRect(
                    QRectF(
                        -draw_width * 0.56,
                        -draw_height * 0.52,
                        draw_width * 1.12,
                        draw_height * clip_height,
                    ),
                    Qt.ClipOperation.IntersectClip,
                )
                renderer.render(
                    layer_painter,
                    QRectF(-draw_width * 0.5, -draw_height * 0.5, draw_width, draw_height),
                )
                layer_painter.restore()

            layer_painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceIn
            )
            layer_color = QColor(color)
            layer_color.setAlphaF(tint_opacities[layer_index])
            layer_painter.fillRect(canvas_rect, layer_color)
            layer_painter.end()
            painter.drawPixmap(QPointF(canvas_rect.left(), canvas_rect.top()), buffer)

    def _audio_leaf_buffers(
        self,
        canvas_rect: QRectF,
        device_pixel_ratio: float,
    ) -> tuple[QPixmap, ...]:
        dpr = max(1.0, device_pixel_ratio)
        width = round(canvas_rect.width())
        height = round(canvas_rect.height())
        key = (width, height, round(dpr * 100))
        if self._audio_leaf_layer_key != key:
            pixel_size = canvas_rect.toRect().size() * dpr
            self._audio_leaf_layer_buffers = tuple(QPixmap(pixel_size) for _ in range(3))
            for buffer in self._audio_leaf_layer_buffers:
                buffer.setDevicePixelRatio(dpr)
            self._audio_leaf_layer_key = key
        return self._audio_leaf_layer_buffers

    @staticmethod
    def audio_center_path(panel_rect: QRectF, progress: float) -> QPainterPath:
        center = panel_rect.center()
        radius = panel_rect.width() * 0.155 * (0.72 + 0.28 * progress)
        points = (
            (-8, 0.93),
            (24, 1.13),
            (57, 0.86),
            (91, 1.08),
            (126, 0.90),
            (163, 1.16),
            (199, 0.84),
            (234, 1.09),
            (270, 0.89),
            (306, 1.14),
            (339, 0.87),
        )
        vertices = [
            center
            + QPointF(
                math.cos(math.radians(angle)) * radius * scale,
                math.sin(math.radians(angle)) * radius * scale,
            )
            for angle, scale in points
        ]
        path = QPainterPath()
        path.moveTo((vertices[-1] + vertices[0]) * 0.5)
        for index, point in enumerate(vertices):
            path.quadTo(point, (point + vertices[(index + 1) % len(vertices)]) * 0.5)
        path.closeSubpath()
        return path

    def _draw_gif_intake(
        self,
        painter: QPainter,
        panel_rect: QRectF,
        state: IntakeVisualState,
    ) -> None:
        progress = self._clamp(state.open_progress)
        suction = self._clamp(state.gif_suction_progress)
        if progress <= 0.0:
            return

        elapsed = max(0.0, state.gif_elapsed_seconds)
        width = min(52.0, panel_rect.width() * 0.38)
        height = width * 37.0 / 52.0
        center = panel_rect.center() + QPointF(0.0, -panel_rect.height() * 0.045)
        layers = (
            (-9.0, -9.0, -9.0),
            (7.0, 7.0, 7.0),
            (0.0, 0.0, 0.0),
        )
        poses = self.gif_frame_poses(state)

        painter.save()
        clip = QPainterPath()
        clip.addEllipse(panel_rect.adjusted(3.0, 3.0, -3.0, -3.0))
        painter.setClipPath(clip)
        inherited_opacity = painter.opacity()
        for index, ((offset_x, offset_y, rotation), pose) in enumerate(zip(layers, poses)):
            expand_start = index * GIF_FRAME_STAGGER_SECONDS
            expand = self._smooth(
                (elapsed - expand_start)
                / max(0.001, GIF_EXPAND_SECONDS - 2 * GIF_FRAME_STAGGER_SECONDS)
            )
            if state.low_power or suction > 0.0:
                expand = 1.0
            layer_center = center + QPointF(
                offset_x * expand,
                offset_y * expand - pose.lift + 22.0 * pose.suction_progress,
            )
            painter.save()
            painter.setOpacity(
                inherited_opacity
                * pose.opacity
                * (0.22 + 0.78 * expand)
            )
            painter.translate(layer_center)
            painter.rotate(rotation * expand)
            card = QRectF(
                -width * pose.scale / 2.0,
                -height * pose.scale / 2.0,
                width * pose.scale,
                height * pose.scale,
            )
            if index == 2 and pose.suction_progress <= 0.0:
                painter.setBrush(QColor(35, 28, 13, 28))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(card.translated(0.0, 1.6), 4.5, 4.5)
            painter.setBrush(QColor(pose.color))
            painter.setPen(QPen(QColor("#4E5F3D"), 1.15))
            painter.drawRoundedRect(card, 4.5, 4.5)
            if index == 2:
                inset_x = max(4.5, card.width() * 0.11)
                inset_y = max(4.0, card.height() * 0.13)
                core = card.adjusted(inset_x, inset_y, -inset_x, -inset_y)
                painter.setBrush(QColor("#FFF9EA"))
                painter.setPen(QPen(QColor("#4E5F3D"), 0.8))
                painter.drawRoundedRect(core, 2.8, 2.8)
            painter.restore()
        painter.restore()

    def _load_drop_leaf_frame_runs(self) -> list[tuple[int, ...]]:
        try:
            return [
                tuple(int(part) for part in line.split())
                for line in (self.assets_dir / "drop-leaf-frame-runs.txt")
                .read_text(encoding="ascii")
                .splitlines()
                if line.strip()
            ]
        except (OSError, ValueError):
            return []

    def _draw_drop_leaf_frame(
        self,
        painter: QPainter,
        canvas_rect: QRectF,
        panel_rect: QRectF,
        progress: float,
        *,
        leaf_width_scale: float = 1.0,
    ) -> None:
        progress = self._clamp(progress)
        if self._drop_leaf_renderers:
            self._draw_vector_leaf_frame(
                painter,
                canvas_rect,
                panel_rect,
                progress,
                leaf_width_scale,
            )
            return
        if self._drop_leaf_frame_renderer.isValid():
            size = min(canvas_rect.width(), canvas_rect.height())
            scale = 0.88 + 0.12 * progress
            draw_size = size * scale
            frame_rect = QRectF(
                canvas_rect.left() + (canvas_rect.width() - draw_size) * 0.5,
                canvas_rect.top() + (canvas_rect.height() - draw_size) * 0.5,
                draw_size,
                draw_size,
            )
            painter.save()
            painter.setOpacity(0.18 + 0.82 * progress)
            self._drop_leaf_frame_renderer.render(painter, frame_rect)
            painter.restore()
            return
        if not self._drop_leaf_frame_runs:
            return
        size = min(canvas_rect.width(), canvas_rect.height())
        scale = 0.88 + 0.12 * progress
        draw_size = size * scale
        offset_x = canvas_rect.left() + (canvas_rect.width() - draw_size) * 0.5
        offset_y = canvas_rect.top() + (canvas_rect.height() - draw_size) * 0.5
        step = draw_size / 512.0
        leaf_colors = (QColor("#7b9b3a"), QColor("#556729"), QColor("#3c4819"))
        fallback_color = leaf_colors[1]
        painter.save()
        painter.setOpacity(0.18 + 0.82 * progress)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for run in self._drop_leaf_frame_runs:
            x, y, width = run[:3]
            bucket = run[3] if len(run) > 3 else 1
            leaf_color = (
                leaf_colors[bucket]
                if 0 <= bucket < len(leaf_colors)
                else fallback_color
            )
            painter.fillRect(
                QRectF(offset_x + x * step, offset_y + y * step, width * step, step),
                leaf_color,
            )
        painter.restore()

    def _draw_vector_leaf_frame(
        self,
        painter: QPainter,
        canvas_rect: QRectF,
        panel_rect: QRectF,
        progress: float,
        leaf_width_scale: float,
    ) -> None:
        center = panel_rect.center()
        placements = [
            (0, -171, 0.91, 0.39, -10, 0.44),
            (1, -132, 0.93, 0.43, 8, 0.46),
            (0, -91, 0.90, 0.38, -7, 0.42),
            (1, -49, 0.94, 0.42, 10, 0.45),
            (0, -8, 0.91, 0.39, -8, 0.42),
            (1, 34, 0.93, 0.41, 9, 0.44),
            (0, 76, 0.89, 0.37, -9, 0.42),
            (1, 118, 0.94, 0.42, 7, 0.45),
            (0, 158, 0.90, 0.38, -10, 0.42),
            (2, -150, 0.78, 0.31, 7, 0.54),
            (2, -102, 0.75, 0.29, -6, 0.52),
            (2, -57, 0.77, 0.31, 8, 0.56),
            (2, -15, 0.74, 0.28, -7, 0.52),
            (2, 31, 0.76, 0.30, 6, 0.54),
            (2, 78, 0.74, 0.28, -9, 0.52),
            (2, 126, 0.77, 0.30, 8, 0.56),
            (4, -178, 0.68, 0.34, 6, 0.90),
            (3, -126, 0.67, 0.29, -8, 0.92),
            (4, -73, 0.69, 0.33, 9, 0.92),
            (3, -21, 0.66, 0.28, -7, 0.90),
            (4, 36, 0.68, 0.32, 8, 0.91),
            (3, 91, 0.66, 0.28, -9, 0.92),
            (4, 146, 0.69, 0.33, 7, 0.90),
        ]
        painter.save()
        painter.setClipPath(self._drop_outer_path(panel_rect))
        for leaf_index, angle, radius_scale, width_scale, rotation_offset, opacity in placements:
            if leaf_index >= len(self._drop_leaf_renderers):
                continue
            renderer = self._drop_leaf_renderers[leaf_index]
            svg_size = renderer.defaultSize()
            if svg_size.width() <= 0:
                continue
            radians = math.radians(angle)
            full_width = min(canvas_rect.width(), canvas_rect.height()) * width_scale
            full_height = full_width * svg_size.height() / svg_size.width()
            radius = panel_rect.width() * radius_scale
            full_center = center + QPointF(
                math.cos(radians) * radius,
                math.sin(radians) * radius,
            )
            slide = 0.98 - 0.18 * progress
            scale = 0.72 + 0.28 * progress
            draw_center = center + (full_center - center) * slide
            draw_width = full_width * scale * 0.84 * leaf_width_scale
            draw_height = full_height * scale * 0.84

            painter.save()
            painter.setOpacity(opacity * (0.18 + 0.82 * progress))
            painter.translate(draw_center)
            painter.rotate(angle - 90 + rotation_offset)
            clip_height = 0.42 if leaf_index in {3, 4} else 0.48
            painter.setClipRect(
                QRectF(
                    -draw_width * 0.56,
                    -draw_height * 0.52,
                    draw_width * 1.12,
                    draw_height * clip_height,
                ),
                Qt.ClipOperation.IntersectClip,
            )
            renderer.render(
                painter,
                QRectF(-draw_width * 0.5, -draw_height * 0.5, draw_width, draw_height),
            )
            painter.restore()
        painter.restore()

    @staticmethod
    def _drop_outer_path(panel_rect: QRectF) -> QPainterPath:
        center = panel_rect.center()
        radius = panel_rect.width() * 0.51
        points = [
            (-2, 0.94),
            (22, 1.08),
            (49, 0.96),
            (73, 1.06),
            (101, 0.93),
            (128, 1.09),
            (154, 0.97),
            (181, 1.07),
            (208, 0.94),
            (236, 1.08),
            (263, 0.93),
            (291, 1.07),
            (319, 0.96),
            (343, 1.05),
        ]
        outer_points = [
            center
            + QPointF(
                math.cos(math.radians(angle)) * radius * scale,
                math.sin(math.radians(angle)) * radius * scale,
            )
            for angle, scale in points
        ]
        path = QPainterPath()
        path.moveTo((outer_points[-1] + outer_points[0]) * 0.5)
        for index, point in enumerate(outer_points):
            next_point = outer_points[(index + 1) % len(outer_points)]
            path.quadTo(point, (point + next_point) * 0.5)
        path.closeSubpath()
        return path

    def _draw_drop_center_cutout(
        self,
        painter: QPainter,
        panel_rect: QRectF,
        progress: float,
    ) -> None:
        progress = self._clamp(progress)
        center = panel_rect.center()
        base = panel_rect.width() * (0.095 + 0.072 * progress)
        points = [
            (0, 0.82),
            (17, 1.22),
            (39, 0.78),
            (62, 1.08),
            (84, 0.90),
            (109, 1.18),
            (132, 0.76),
            (153, 1.05),
            (177, 0.84),
            (199, 1.24),
            (223, 0.86),
            (247, 1.12),
            (270, 0.79),
            (292, 1.18),
            (318, 0.81),
            (342, 1.10),
        ]
        cutout_points = [
            center
            + QPointF(
                math.cos(math.radians(angle)) * base * scale,
                math.sin(math.radians(angle)) * base * scale,
            )
            for angle, scale in points
        ]
        path = QPainterPath()
        path.moveTo((cutout_points[-1] + cutout_points[0]) * 0.5)
        for index, point in enumerate(cutout_points):
            next_point = cutout_points[(index + 1) % len(cutout_points)]
            path.quadTo(point, (point + next_point) * 0.5)
        path.closeSubpath()

        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 0))
        painter.drawPath(path)
        painter.restore()

        painter.save()
        mist = QRadialGradient(center, base * 1.15)
        mist.setColorAt(0.0, QColor(255, 252, 232, 30))
        mist.setColorAt(1.0, QColor(255, 252, 232, 18))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mist)
        painter.drawPath(path)
        painter.restore()

    @staticmethod
    def _staggered_progress(value: float, delay: float) -> float:
        if value <= delay:
            return 0.0
        return IntakeEntryRenderer._smooth(
            (value - delay) / max(0.001, 1.0 - delay)
        )

    @staticmethod
    def _smooth(value: float) -> float:
        progress = IntakeEntryRenderer._clamp(value)
        return progress * progress * (3.0 - 2.0 * progress)

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(float(value), 1.0))
