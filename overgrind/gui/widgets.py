"""Reusable widgets: PageBase, Atlas (Feature-State Atlas), Ring, StabilityMeter, issue text helpers."""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ..types import Coverage
from . import theme

PAGE_NAMES = ["Target", "Inspect", "Features", "Controls", "Plan", "Run", "Errors"]


def friendly_issue(msg: str) -> tuple[str, str, int]:
    """validate() string -> (level 'warn'|'fail', plain sentence, page index to fix on)."""
    level = "fail"
    text = msg.strip()
    if text.lower().startswith("warning:"):
        level, text = "warn", text[8:].strip()
    low = text.lower()
    if "target" in low or "exe" in low or "process" in low:
        page = 0
    elif "control" in low or "domain" in low or "range" in low or "scope" in low or "default" in low and "feature" not in low:
        page = 3
    elif any(w in low for w in ("strategy", "seed", "reset", "timeout", "stop", "run ", "interval")):
        page = 4
    else:
        page = 2
    if text:
        text = text[0].upper() + text[1:]
        if text[-1] not in ".!?":
            text += "."
    return level, text, page


class PageBase(QWidget):
    """A page: title + 'what to do now' line + scrollable body. Subclasses fill self.body (QVBoxLayout)."""
    title = ""
    hint = ""
    jump = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        inner = QWidget(); scroll.setWidget(inner)
        self.body = QVBoxLayout(inner); self.body.setContentsMargins(16, 16, 16, 16); self.body.setSpacing(8)
        self.title_label = QLabel(self.title); self.title_label.setObjectName("PageTitle")
        self.title_label.setAccessibleName(f"Page: {self.title}")
        self.hint_label = QLabel(); self.hint_label.setObjectName("Hint"); self.hint_label.setWordWrap(True)
        self.hint_label.setTextFormat(Qt.TextFormat.RichText)
        self.body.addWidget(self.title_label); self.body.addWidget(self.hint_label)
        self.set_hint(self.hint)

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(f"<b>What to do now:</b> {text}")
        self.hint_label.setAccessibleName("What to do now: " + text)

    def refresh(self) -> None:  # called when the page is shown or the project changes
        pass

    def shutdown(self) -> None:
        pass


def muted(text: str = "", wrap: bool = True) -> QLabel:
    lab = QLabel(text); lab.setWordWrap(wrap)
    lab.setStyleSheet(f"color:{theme.sem_hex('muted')};")
    return lab


def empty_state(text: str) -> QLabel:
    lab = QLabel(text); lab.setWordWrap(True); lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lab.setStyleSheet(f"color:{theme.sem_hex('muted')};padding:24px;")
    return lab


# ------------------------------------------------------------------ stability meter
class StabilityMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._score = 0
        self._label = "No element selected"
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_score(self, score: int | None, word: str = "", colour: str = "muted") -> None:
        self._score = 0 if score is None else score
        self._colour = colour
        self._label = "No element selected" if score is None else f"{score} / 100  -  {word}"
        self.setAccessibleName(f"Locator stability: {self._label}")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(260, 28)

    def paintEvent(self, _e) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 4.5, self.width() - 1, self.height() - 9)
        pal = self.palette()
        p.setPen(QPen(pal.mid().color(), 1)); p.setBrush(pal.alternateBase()); p.drawRoundedRect(r, 6, 6)
        colour = theme.sem(getattr(self, "_colour", "muted"))
        if self._score > 0:
            fr = QRectF(r.x(), r.y(), max(8.0, r.width() * self._score / 100), r.height())
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(colour); p.drawRoundedRect(fr, 6, 6)
        p.setPen(QPen(pal.windowText().color(), 1, Qt.PenStyle.DotLine))
        for mark in (45, 75):                     # thresholds: fragile | fair | stable
            x = r.x() + r.width() * mark / 100
            p.drawLine(int(x), int(r.y()), int(x), int(r.bottom()))
        # label on its own plate so contrast never depends on the bar colour
        f = self.font(); f.setBold(True); p.setFont(f)
        tw = QFontMetrics(f).horizontalAdvance(self._label) + 16
        plate = QRectF(r.x() + 6, r.y() + 2, tw, r.height() - 4)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(pal.window()); p.drawRoundedRect(plate, 4, 4)
        p.setPen(pal.windowText().color()); p.drawText(plate, Qt.AlignmentFlag.AlignCenter, self._label)


# ------------------------------------------------------------------ coverage ring
class Ring(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._frac: float | None = 0.0
        self._center = "0%"
        self._caption = ""
        self.setMinimumSize(110, 120)

    def set_value(self, frac: float | None, center: str, caption: str) -> None:
        self._frac, self._center, self._caption = frac, center, caption
        self.setAccessibleName(f"{caption}: {center}")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(130, 140)

    def paintEvent(self, _e) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height() - 30) - 12
        rect = QRectF((self.width() - side) / 2, 6, side, side)
        pal = self.palette()
        pen = QPen(pal.mid().color(), 9); pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen); p.drawEllipse(rect)
        if self._frac:
            pen = QPen(theme.sem("ok"), 9); pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)
            p.drawArc(rect, 90 * 16, int(-360 * 16 * min(1.0, self._frac)))
        p.setPen(pal.windowText().color())
        f = self.font(); f.setBold(True); f.setPointSizeF(f.pointSizeF() * 1.3); p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._center)
        f.setBold(False); f.setPointSizeF(f.pointSizeF() / 1.3); p.setFont(f)
        cap = QRectF(0, rect.bottom() + 6, self.width(), 24)
        p.drawText(cap, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, self._caption)


# ------------------------------------------------------------------ Feature-State Atlas
class Atlas(QWidget):
    """Heat-map grid: one row per feature, one cell per state. Each cell prints its count (never colour alone)."""
    ROW_H = 28
    LABEL_MAX = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[tuple[str, str, tuple[str, ...]]] = []
        self._visits: dict[tuple[str, str], int] = {}
        self._max = 0
        self.setMouseTracking(True)
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def set_features(self, rows) -> None:
        """rows: list of (feature id, display name, states)"""
        self._rows = [(a, b, tuple(c)) for a, b, c in rows]
        self.updateGeometry(); self.update()

    def set_visits(self, visits: dict[tuple[str, str], int]) -> None:
        self._visits = visits
        self._max = max(visits.values(), default=0)
        total = sum(len(s) for _, _, s in self._rows)
        seen = sum(1 for k, v in visits.items() if v > 0)
        self.setAccessibleName("Feature-State Atlas")
        self.setAccessibleDescription(f"{seen} of {total} feature states seen in the sampled actions.")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(520, 30 + self.ROW_H * max(1, len(self._rows)))

    def minimumSizeHint(self) -> QSize:
        return QSize(300, 60)

    def _geometry(self):
        fm = QFontMetrics(self.font())
        label_w = min(self.LABEL_MAX, max([fm.horizontalAdvance(n) for _, n, _ in self._rows] + [60]) + 16)
        max_states = max([len(s) for _, _, s in self._rows] + [1])
        cell_w = max(34, min(96, (self.width() - label_w - 8) // max_states))
        return label_w, cell_w

    def paintEvent(self, _e) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        if not self._rows:
            p.setPen(pal.placeholderText().color())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, "Add features to see which states have been visited.")
            return
        label_w, cell_w = self._geometry()
        fm = QFontMetrics(self.font())
        y = 4
        for fid, name, states in self._rows:
            p.setPen(pal.windowText().color())
            p.drawText(QRectF(0, y, label_w - 8, self.ROW_H - 4), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       fm.elidedText(name, Qt.TextElideMode.ElideRight, int(label_w - 12)))
            x = label_w
            for st in states:
                n = self._visits.get((fid, st), 0)
                frac = math.log1p(n) / math.log1p(self._max) if self._max else 0.0
                bg = theme.heat_color(frac if n else 0.0)
                rect = QRectF(x + 1, y, cell_w - 2, self.ROW_H - 4)
                p.setPen(QPen(pal.mid().color(), 1, Qt.PenStyle.SolidLine if n else Qt.PenStyle.DashLine))
                p.setBrush(bg); p.drawRoundedRect(rect, 4, 4)
                p.setPen(theme.text_on(bg))
                label = fm.elidedText(f"{st}  {n}", Qt.TextElideMode.ElideRight, int(cell_w - 8))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
                x += cell_w
            y += self.ROW_H
        p.setPen(pal.placeholderText().color())
        p.drawText(QRectF(label_w, y, self.width() - label_w, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "Cell = state name and how often it was seen (sampled). Dashed = not seen yet.")
