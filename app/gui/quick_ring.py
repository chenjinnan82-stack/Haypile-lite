"""Small shortcut ring with a shaped native window."""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from app.gui.desktop_input import DesktopInputState


class QuickRing(QWidget):
    action_requested = Signal(str)

    def __init__(self, parent, translate, *, anchor_geometry, available_geometry):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.anchor_geometry = anchor_geometry
        self.available_geometry = available_geometry
        self.desktop_input = DesktopInputState()
        self.translate = translate
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("Haypile")
        self.buttons = {}
        self.track = QPainterPath()
        for action in ("recent", "assets", "agent", "settings"):
            button = QPushButton(self)
            button.setFixedSize(44, 44)
            button.setMask(QRegion(button.rect(), QRegion.RegionType.Ellipse))
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIcon(self._icon(action))
            button.setIconSize(button.size() / 2)
            button.setStyleSheet(
                "QPushButton { background: #4E5F3D; border: 1px solid #D4DAC7; border-radius: 22px; }"
                "QPushButton:hover { background: #65784F; }"
                "QPushButton:pressed { background: #35452A; }"
                "QPushButton:focus { border: 2px solid #E9B636; }"
            )
            button.clicked.connect(lambda _checked=False, key=action: self._activate_action(key))
            self.buttons[action] = button
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll_outside)
        QApplication.instance().installEventFilter(self)
        QApplication.instance().applicationStateChanged.connect(self._application_state_changed)
        self.hide()

    def _activate_action(self, action):
        self.hide()
        self.action_requested.emit(action)

    @staticmethod
    def _icon(action):
        pixmap = QPixmap(44, 44)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(11, 11)
        painter.setPen(QPen(QColor("#FFF9EA"), 1.6, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if action == "recent":
            painter.drawEllipse(QRectF(-8, -8, 16, 16))
            painter.drawLine(QPointF(0, -5), QPointF(0, 0))
            painter.drawLine(QPointF(0, 0), QPointF(4, 2))
        elif action == "assets":
            path = QPainterPath(QPointF(-8, -5))
            for x, y in ((-2, -5), (0, -2), (8, -2), (8, 7), (-8, 7)):
                path.lineTo(x, y)
            path.closeSubpath()
            painter.drawPath(path)
        elif action == "agent":
            for x, y in ((-6, -5), (6, -5), (0, 6)):
                painter.drawEllipse(QRectF(x - 2.5, y - 2.5, 5, 5))
            painter.drawLine(QPointF(-3, -5), QPointF(3, -5))
            painter.drawLine(QPointF(0, -5), QPointF(0, 3))
        else:
            for x, y in ((-6, -3), (0, 4), (6, -1)):
                painter.drawLine(QPointF(x, -8), QPointF(x, y - 2))
                painter.drawLine(QPointF(x, y + 2), QPointF(x, 8))
                painter.drawEllipse(QRectF(x - 2, y - 2, 4, 4))
        painter.end()
        return QIcon(pixmap)

    def reposition(self):
        anchor = self.anchor_geometry().center()
        available = self.available_geometry().adjusted(2, 2, -2, -2)
        radius = 92
        choices = ((-110, -37, 37, 110), (70, 143, 217, 290),
                   (-160, -113, -67, -20), (20, 67, 113, 160),
                   (0, 30, 60, 90), (90, 120, 150, 180),
                   (-180, -150, -120, -90), (-90, -60, -30, 0))
        def slots(angles):
            return [QRect(anchor.x() + round(radius * math.cos(math.radians(a))) - 22,
                          anchor.y() + round(radius * math.sin(math.radians(a))) - 22, 44, 44)
                    for a in angles]
        angles = next((a for a in choices if all(available.contains(r) for r in slots(a))), choices[0])
        self.setGeometry(QRect(anchor.x() - 120, anchor.y() - 120, 240, 240).intersected(available))
        center = QPointF(anchor - self.pos())
        arc = QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
        self.track = QPainterPath()
        self.track.arcMoveTo(arc, -angles[0])
        self.track.arcTo(arc, -angles[0], -(angles[-1] - angles[0]))
        stroke = QPainterPathStroker()
        stroke.setWidth(20)
        stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
        region = QRegion(stroke.createStroke(self.track).toFillPolygon().toPolygon())
        for button, rect in zip(self.buttons.values(), slots(angles)):
            button.move(rect.topLeft() - self.pos())
            region |= QRegion(button.geometry(), QRegion.RegionType.Ellipse)
        # Exclude the center and corners from the native window mask.
        self.setMask(region)
        self.update()

    def open(self):
        labels = (self.translate("最近收纳", "Recent assets"), self.translate("全部素材", "All assets"),
                  "Agent", self.translate("设置", "Settings"))
        for button, label in zip(self.buttons.values(), labels):
            button.setToolTip(label)
            button.setAccessibleName(label)
        self.reposition()
        self._last_buttons, self._last_escape = self.desktop_input.buttons(), self.desktop_input.escape()
        self.show()
        self.raise_()
        self.activateWindow()
        self.buttons["recent"].setFocus(Qt.FocusReason.MouseFocusReason)
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def _poll_outside(self):
        buttons, escape = self.desktop_input.buttons(), self.desktop_input.escape()
        cursor = QCursor.pos()
        inside = self.mask().contains(self.mapFromGlobal(cursor)) or self.parentWidget().frameGeometry().contains(cursor)
        if (escape and not self._last_escape) or (buttons != self._last_buttons and not inside):
            self.hide()
        self._last_buttons, self._last_escape = buttons, escape

    def _application_state_changed(self, state):
        if state == Qt.ApplicationState.ApplicationInactive:
            self.hide()

    def eventFilter(self, watched, event):
        if self.isVisible():
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.hide()
                return True
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
                if watched.window() not in (self, self.parentWidget()):
                    self.hide()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#D4DAC7"), 16, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(self.track)
        painter.setPen(QPen(QColor("#4E5F3D"), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(self.track)
