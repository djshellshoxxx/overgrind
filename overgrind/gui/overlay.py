"""Screen overlays for the Inspect page. Both are Windows-only (see backend.overlay_supported) and guarded.

BoundsOverlay: a translucent, frameless, click-through outline around one element (never takes mouse or focus).
PickOverlay:   a see-through sheet over the screen with a crosshair cursor. It swallows the click (so the tested program
               never receives it), outlines the element under the pointer and reports it on click. Esc cancels."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import backend, theme


def to_logical(bounds, dpr: float) -> QRect:
    """UIA bounds are physical screen pixels; Qt window geometry is device-independent."""
    x, y, w, h = bounds[:4]
    d = dpr or 1.0
    return QRect(round(x / d), round(y / d), max(1, round(w / d)), max(1, round(h / d)))


def _dpr_at(x: float, y: float) -> float:
    scr = QGuiApplication.primaryScreen()
    return scr.devicePixelRatio() if scr else 1.0


class BoundsOverlay(QWidget):
    FLAGS = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
             | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)

    def __init__(self, force: bool = False):
        super().__init__(None, self.FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.enabled = force or backend.overlay_supported()[0]
        self.label = ""

    def show_bounds(self, bounds, label: str = "") -> bool:
        """Outline `bounds` (x, y, w, h in screen pixels). Returns False (and shows nothing) where unsupported."""
        if not self.enabled or not bounds or len(bounds) < 4 or bounds[2] <= 0 or bounds[3] <= 0:
            self.hide(); return False
        self.label = label
        r = to_logical(bounds, _dpr_at(bounds[0], bounds[1])).adjusted(-3, -3, 3, 3)
        self.setGeometry(r)
        self.show()
        return True

    def clear(self) -> None:
        self.hide()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        c = theme.sem("info")
        fill = QColor(c); fill.setAlpha(40)
        p.setBrush(fill)
        p.setPen(QPen(QColor("#ffffff"), 5)); p.drawRect(self.rect().adjusted(2, 2, -3, -3))     # white under-stroke: visible on any background
        p.setPen(QPen(c, 3, Qt.PenStyle.DashLine)); p.drawRect(self.rect().adjusted(2, 2, -3, -3))   # dashed, so not colour alone


class PickOverlay(QWidget):
    picked = Signal(int)        # index into the element list
    cancelled = Signal()

    FLAGS = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool

    def __init__(self, elements, force: bool = False):
        super().__init__(None, self.FLAGS)
        self.elements = list(elements)
        self.enabled = force or backend.overlay_supported()[0]
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._hover: int | None = None
        self._dpr = 1.0

    def begin(self) -> bool:
        if not self.enabled:
            return False
        scr = QGuiApplication.primaryScreen()
        self._dpr = scr.devicePixelRatio() if scr else 1.0
        self.setGeometry(scr.virtualGeometry() if scr else QRect(0, 0, 800, 600))
        self.show(); self.raise_(); self.activateWindow(); self.setFocus()
        return True

    def _index_at(self, pos: QPoint) -> int | None:
        gx, gy = self.mapToGlobal(pos).x() * self._dpr, self.mapToGlobal(pos).y() * self._dpr
        return backend.hit_test(self.elements, gx, gy)

    def mouseMoveEvent(self, e) -> None:
        i = self._index_at(e.position().toPoint())
        if i != self._hover:
            self._hover = i; self.update()

    def mousePressEvent(self, e) -> None:
        i = self._index_at(e.position().toPoint())
        self.hide()
        if i is None or e.button() != Qt.MouseButton.LeftButton:
            self.cancelled.emit()
        else:
            self.picked.emit(i)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.hide(); self.cancelled.emit()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))                   # alpha 1: invisible, but it receives the mouse
        pal = self.palette()
        if self._hover is not None:
            e = self.elements[self._hover]
            r = to_logical(e.bounds, self._dpr).translated(-self.geometry().topLeft())
            c = theme.sem("info"); f = QColor(c); f.setAlpha(50)
            p.setBrush(f); p.setPen(QPen(QColor("#ffffff"), 5)); p.drawRect(r)
            p.setPen(QPen(c, 3, Qt.PenStyle.DashLine)); p.drawRect(r)
            txt = f"{e.name or '(no name)'}  -  {e.control_type}"
            font = QFont(self.font()); font.setBold(True); p.setFont(font)
            w = p.fontMetrics().horizontalAdvance(txt) + 16
            tag = QRectF(r.x(), max(0, r.y() - 26), w, 22)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#1b1b1b")); p.drawRoundedRect(tag, 4, 4)
            p.setPen(QColor("#ffffff")); p.drawText(tag, Qt.AlignmentFlag.AlignCenter, txt)
        hint = "Click an element to pick it. Esc to cancel."
        p.setFont(self.font())
        hw = p.fontMetrics().horizontalAdvance(hint) + 24
        box = QRectF((self.width() - hw) / 2, 12, hw, 28)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#1b1b1b")); p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor("#ffffff")); p.drawText(box, Qt.AlignmentFlag.AlignCenter, hint)
