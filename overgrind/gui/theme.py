"""Theme helpers: semantic colours (light/dark), painted glyph icons, QSS, badges."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

# name -> (light, dark). All chosen for >= 4.5:1 text contrast on the window colour.
_SEMANTIC = {
    "ok": ("#1b6e3c", "#6fd39a"),
    "warn": ("#8a5a00", "#f0b44c"),
    "fail": ("#b3261e", "#ff8a80"),
    "info": ("#1f5fae", "#8ab8f5"),
    "manual": ("#6b3fa0", "#c9a7f2"),
    "muted": ("#5f6368", "#a9adb3"),
}


def is_dark() -> bool:
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().color(app.palette().ColorRole.Window).lightness() < 128


def sem(name: str) -> QColor:
    light, dark = _SEMANTIC[name]
    return QColor(dark if is_dark() else light)


def sem_hex(name: str) -> str:
    return sem(name).name()


def heat_color(frac: float) -> QColor:
    """0..1 -> colour ramp; 0 is a near-background neutral (cell also prints its number)."""
    frac = max(0.0, min(1.0, frac))
    if is_dark():
        lo, hi = QColor("#2b2f36"), QColor("#2f9e6a")
    else:
        lo, hi = QColor("#eef0f3"), QColor("#2b8a5a")
    return QColor(*(int(lo.getRgb()[i] + (hi.getRgb()[i] - lo.getRgb()[i]) * frac) for i in range(3)))


def text_on(bg: QColor) -> QColor:
    return QColor("#ffffff") if bg.lightness() < 140 else QColor("#111111")


# ---------------------------------------------------------------- glyph icons
def glyph(kind: str, color: QColor | None = None, size: int = 16) -> QIcon:
    return QIcon(glyph_pixmap(kind, color, size))


def glyph_pixmap(kind: str, color: QColor | None = None, size: int = 16) -> QPixmap:
    app = QApplication.instance()
    dpr = 1.0
    if app is not None:
        screen = app.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = color or sem_hex_color(kind)
    pen = QPen(c, max(1.4, size / 10))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    s = float(size)
    m = s * 0.12
    r = QRectF(m, m, s - 2 * m, s - 2 * m)
    k = kind
    if k in ("ok", "verified"):
        p.setBrush(QBrush(c)); p.drawEllipse(r)
        p.setPen(QPen(QColor("#ffffff") if not is_dark() else QColor("#102018"), pen.widthF()))
        p.drawPolyline(QPolygonF([QPointF(s * .30, s * .52), QPointF(s * .45, s * .67), QPointF(s * .72, s * .35)]))
    elif k in ("warn", "inferred_warn"):
        p.setBrush(QBrush(c))
        p.drawPolygon(QPolygonF([QPointF(s / 2, m), QPointF(s - m, s - m), QPointF(m, s - m)]))
        p.setPen(QPen(QColor("#ffffff") if not is_dark() else QColor("#201400"), pen.widthF()))
        p.drawLine(QPointF(s / 2, s * .40), QPointF(s / 2, s * .64)); p.drawPoint(QPointF(s / 2, s * .78))
    elif k in ("fail", "crash"):
        p.setBrush(QBrush(c)); p.drawEllipse(r)
        p.setPen(QPen(QColor("#ffffff") if not is_dark() else QColor("#2a0a08"), pen.widthF()))
        p.drawLine(QPointF(s * .33, s * .33), QPointF(s * .67, s * .67)); p.drawLine(QPointF(s * .67, s * .33), QPointF(s * .33, s * .67))
    elif k == "inferred":
        p.setBrush(QBrush(c))
        p.drawPolygon(QPolygonF([QPointF(s / 2, m), QPointF(s - m, s / 2), QPointF(s / 2, s - m), QPointF(m, s / 2)]))
    elif k == "manual":
        p.setBrush(QBrush(c)); p.drawRoundedRect(r, s * .18, s * .18)
        p.setPen(QPen(QColor("#ffffff") if not is_dark() else QColor("#1b0f2b"), pen.widthF()))
        p.drawLine(QPointF(s * .32, s * .68), QPointF(s * .68, s * .32))
    elif k in ("unknown", "question"):
        p.setBrush(Qt.BrushStyle.NoBrush); p.drawEllipse(r)
        f = QFont(); f.setBold(True); f.setPixelSize(int(s * .6)); p.setFont(f); p.setPen(c)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, "?")
    elif k in ("hang", "clock"):
        p.setBrush(Qt.BrushStyle.NoBrush); p.drawEllipse(r)
        p.drawLine(QPointF(s / 2, s / 2), QPointF(s / 2, s * .28)); p.drawLine(QPointF(s / 2, s / 2), QPointF(s * .68, s * .58))
    elif k == "dialog":
        p.setBrush(Qt.BrushStyle.NoBrush); p.drawRoundedRect(r, 2, 2)
        p.drawLine(QPointF(m, s * .36), QPointF(s - m, s * .36))
    elif k in ("assertion", "info"):
        p.setBrush(QBrush(c)); p.drawEllipse(r)
        p.setPen(QPen(QColor("#ffffff") if not is_dark() else QColor("#0a1a30"), pen.widthF()))
        p.drawLine(QPointF(s / 2, s * .45), QPointF(s / 2, s * .72)); p.drawPoint(QPointF(s / 2, s * .30))
    elif k in ("resource", "harness"):
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([QPointF(s / 2, m), QPointF(s - m, s * .3), QPointF(s - m, s * .7), QPointF(s / 2, s - m), QPointF(m, s * .7), QPointF(m, s * .3)]))
    elif k == "play":
        p.setBrush(QBrush(c)); p.drawPolygon(QPolygonF([QPointF(s * .3, m), QPointF(s - m, s / 2), QPointF(s * .3, s - m)]))
    elif k == "pause":
        p.setBrush(QBrush(c)); p.drawRect(QRectF(s * .25, m, s * .17, s - 2 * m)); p.drawRect(QRectF(s * .58, m, s * .17, s - 2 * m))
    elif k == "stop":
        p.setBrush(QBrush(c)); p.drawRoundedRect(QRectF(s * .22, s * .22, s * .56, s * .56), 2, 2)
    else:  # dot
        p.setBrush(QBrush(c)); p.drawEllipse(QRectF(s * .3, s * .3, s * .4, s * .4))
    p.end()
    return px


def sem_hex_color(kind: str) -> QColor:
    mapping = {
        "ok": "ok", "verified": "ok", "warn": "warn", "fail": "fail", "crash": "fail", "hang": "warn",
        "inferred": "info", "info": "info", "assertion": "info", "manual": "manual", "unknown": "muted",
        "question": "muted", "dialog": "warn", "resource": "muted", "harness": "muted", "clock": "warn",
        "play": "ok", "pause": "warn", "stop": "fail",
    }
    return sem(mapping.get(kind, "muted"))


CONFIDENCE = {
    "verified": ("Verified", "verified", "ok", "Read directly from the app."),
    "inferred": ("Inferred", "inferred", "info", "Our best guess. Please check it."),
    "manual": ("Manual", "manual", "manual", "Entered by you."),
    "unknown": ("Unknown", "unknown", "muted", "We could not tell. Please confirm."),
}
RELIABILITY = {
    "reliable": ("Reliable", "ok", "Fails every time it is replayed."),
    "flaky": ("Flaky", "warn", "Fails only sometimes."),
    "not_reproducible": ("Not reproducible", "fail", "Did not fail again when replayed."),
    "unconfirmed": ("Unconfirmed", "muted", "Not replayed yet."),
}
CATEGORY_LABEL = {
    "crash": "Crash", "hang": "Hang", "dialog": "Error dialog", "assertion": "Check failed",
    "resource": "Resource use", "harness": "Overgrind problem",
}


class Badge(QWidget):
    """Icon + word pill (never colour alone)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self); lay.setContentsMargins(6, 2, 8, 2); lay.setSpacing(4)
        self._icon = QLabel(); self._text = QLabel()
        lay.addWidget(self._icon); lay.addWidget(self._text); lay.addStretch(0)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())

    def set_confidence(self, level: str) -> None:
        label, icon, colour, tip = CONFIDENCE.get(level, CONFIDENCE["unknown"])
        self._apply(label, icon, colour, tip)

    def set_reliability(self, level: str) -> None:
        label, colour, tip = RELIABILITY.get(level, RELIABILITY["unconfirmed"])
        self._apply(label, {"ok": "ok", "warn": "warn", "fail": "fail"}.get(colour, "unknown"), colour, tip)

    def _apply(self, label: str, icon: str, colour: str, tip: str) -> None:
        self._icon.setPixmap(glyph_pixmap(icon, sem(colour), 14))
        self._text.setText(label)
        c = sem(colour)
        self._text.setStyleSheet(f"color:{c.name()};font-weight:600;")
        self.setStyleSheet(f"Badge{{border:1px solid {c.name()};border-radius:9px;}}")
        self.setToolTip(tip)
        self.setAccessibleName(f"{label}. {tip}")


def qss() -> str:
    """Minimal polish, palette-driven so it follows light/dark."""
    return """
    QLabel#PageTitle { font-size: 18pt; font-weight: 600; }
    QLabel#Hint { padding: 8px; border-radius: 6px; background: palette(alternate-base); }
    QListWidget#Stepper { border: none; background: palette(alternate-base); font-size: 11pt; outline: 0; }
    QListWidget#Stepper::item { padding: 10px 12px; border-radius: 6px; margin: 2px 6px; }
    QListWidget#Stepper::item:selected { background: palette(highlight); color: palette(highlighted-text); }
    QPushButton { padding: 5px 14px; }
    QPushButton#Primary { font-weight: 600; padding: 8px 22px; }
    QPushButton#Big { font-size: 13pt; font-weight: 600; padding: 12px 28px; }
    QFrame#Card { border: 1px solid palette(mid); border-radius: 8px; }
    QFrame#Footer { border-top: 1px solid palette(mid); background: palette(alternate-base); }
    QFrame#Card[selected="true"] { border: 2px solid palette(highlight); }
    QGroupBox { margin-top: 12px; font-weight: 600; }
    QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
    QTableView, QTreeWidget { alternate-background-color: palette(alternate-base); }
    """
