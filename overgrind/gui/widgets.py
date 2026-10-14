"""Reusable widgets: PageBase, Atlas (Feature-State Atlas), Ring, StabilityMeter, issue text helpers."""
from __future__ import annotations

import math
import re

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QLabel, QScrollArea, QSizePolicy, QToolTip, QVBoxLayout, QWidget,
)

from ..types import Coverage
from . import theme

PAGE_NAMES = ["Target", "Inspect", "Features", "Controls", "Plan", "Run", "Errors"]


_Q = r"['\"]"
_RULES: list[tuple[str, str]] = [
    (r"^no enabled features$", "No features are switched on for testing. Add one from Inspect, or tick \u201cTest it\u201d on the Features page."),
    (r"^no features", "No features yet. Add one from Inspect, or press Add feature manually."),
    (r"^target is empty.*", "You have not chosen a program to test yet."),
    (r"^unsupported schema_version (\S+)", "This project file comes from a different version of Overgrind (format {0})."),
    (r"^feature '(.+?)': \w+ needs at least 2 states \(has (\d+)\)", "Feature '{0}' needs at least two states (it has {1})."),
    (r"^feature '(.+?)': duplicate states", "Feature '{0}' lists the same state twice."),
    (r"^feature '(.+?)': default_state '(.*?)' not in states .*", "Feature '{0}' starts as '{1}', which is not one of its states."),
    (r"^duplicate (feature|control) id '(.+?)'", "Two {0}s use the same internal name '{1}'. Rename one of them."),
    (r"^(?:feature|control) with empty id", "An item has no internal name. Remove it and add it again."),
    (r"^control '(.+?)': scope references unknown feature '(.+?)'", "Control '{0}' is only changed while feature '{1}' is on, but that feature does not exist any more."),
    (r"^control '(.+?)': default (.+) is outside the effective domain", "Control '{0}' starts as {1}, which is outside the range we will test. Change \u201cStarts as\u201d or widen the range."),
    (r"^control '(.+?)': fuzz_domain is not a subset of domain \((.*)\)", "Control '{0}': the range we will test goes outside the app's own range ({1})."),
    (r"^control '(.+?)' (?:domain|fuzz_domain): minimum (.+) > maximum (.+)", "Control '{0}': the lowest value ({1}) is higher than the highest ({2})."),
    (r"^control '(.+?)' (?:domain|fuzz_domain): (.*)", "Control '{0}': problem with its value range: {1}"),
    (r"^control '(.+?)': kind '.*' != domain kind '.*'", "Control '{0}': its type and its value range do not match."),
    (r"^constraint #(\d+) \(mutex\): (.*)", "Rule {0}: {1}"),
    (r"^constraint #(\d+) \(\w+\): unknown feature '(.+?)'", "Rule {0} uses feature '{1}', which does not exist any more. Remove the rule."),
    (r"^constraint #(\d+) \((\w+)\): (.*)", "Rule {0}: {2}"),
    (r"^feature '(.+?)' is marked dangerous and is enabled;.*", "Feature '{0}' is marked dangerous but is switched on, so it will be exercised. Untick \u201cTest it\u201d to leave it out."),
    (r"^feature '(.+?)' looks dangerous \((.*?)\);.*", "Feature '{0}' looks dangerous ({1}). Tick \u201cDangerous\u201d on the Features page so it is never used by accident."),
    (r"^run\.strategy_options has an unknown key '(.+?)'.*", "The plan has an option '{0}' that Overgrind does not know; it is ignored."),
    (r"^run\.hang_threshold_ms .*exceeds case_timeout_ms.*", "The hang limit is longer than the time allowed for a whole test case, so hangs may never be reported."),
    (r"^run\.op_timeout_ms .*exceeds case_timeout_ms.*", "One step may take longer than a whole test case is allowed to, so slow steps will be reported as hangs."),
]


def _plain(msg: str, names: dict[str, str]) -> str:
    low = msg.strip()
    for pat, out in _RULES:
        m = re.match(pat, low, re.IGNORECASE)
        if m:
            low = out.format(*m.groups()); break
    for fid, name in names.items():           # show what the tester named it, not the internal id
        low = low.replace(f"'{fid}'", f"'{name}'")
    return low


def friendly_issue(msg: str, project=None) -> tuple[str, str, int]:
    """validate() string -> (level 'warn'|'fail', plain sentence, page index to fix on)."""
    level = "fail"
    text = msg.strip()
    if text.lower().startswith("warning:"):
        level, text = "warn", text[8:].strip()
    low = text.lower()
    if re.match(r"(control\b|control #)", low):
        page = 3
    elif re.match(r"(feature\b|constraint\b|no enabled features|no features|duplicate (feature|control))", low):
        page = 2
    elif re.search(r"\b(target|exe|process|window)\b", low):
        page = 0
    elif re.search(r"\b(strategy|seed|reset|timeout|stop|interval|run)\b", low):
        page = 4
    else:
        page = 2
    names = {f.id: (f.name or f.id) for f in project.features} if project is not None else {}
    text = _plain(text, names)
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
        outer.addWidget(scroll, 1); self.scroll = scroll
        # Optional footer that stays visible while the body scrolls (e.g. Plan's estimate and its action buttons).
        self.footer = QFrame(); self.footer.setObjectName("Footer"); self.footer.setVisible(False)
        self.footer_layout = QVBoxLayout(self.footer); self.footer_layout.setContentsMargins(16, 6, 16, 6); self.footer_layout.setSpacing(4)
        outer.addWidget(self.footer)
        inner = QWidget(); scroll.setWidget(inner)
        self.body = QVBoxLayout(inner); self.body.setContentsMargins(16, 16, 16, 16); self.body.setSpacing(8)
        self.title_label = QLabel(self.title); self.title_label.setObjectName("PageTitle")
        self.title_label.setAccessibleName(f"Page: {self.title}")
        self.hint_label = QLabel(); self.hint_label.setObjectName("Hint"); self.hint_label.setWordWrap(True)
        self.hint_label.setTextFormat(Qt.TextFormat.RichText)
        self.body.addWidget(self.title_label); self.body.addWidget(self.hint_label)
        self.set_hint(self.hint)

    def show_below(self, widget: QWidget) -> None:
        """Scroll the body so `widget` (e.g. fresh results) is in view."""
        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(widget, 0, 40))

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(f"<b>What to do now:</b> {text}")
        self.hint_label.setAccessibleName("What to do now: " + text)

    def refresh(self) -> None:  # called when the page is shown or the project changes
        pass

    def shutdown(self) -> None:
        pass


def muted(text: str = "", wrap: bool = True) -> QLabel:
    lab = QLabel(text); lab.setWordWrap(wrap); lab.setTextFormat(Qt.TextFormat.PlainText)   # dynamic text: never parsed as markup
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
        self.setMinimumSize(150, 170)

    def set_value(self, frac: float | None, center: str, caption: str) -> None:
        self._frac, self._center, self._caption = frac, center, caption
        self.setAccessibleName(f"{caption}: {center}")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(170, 190)

    def paintEvent(self, _e) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height() - 56) - 12
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
        cap = QRectF(2, rect.bottom() + 6, self.width() - 4, 46)
        p.drawText(cap, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, self._caption)


# ------------------------------------------------------------------ Feature-State Atlas
class Atlas(QWidget):
    """Heat-map grid: one row per feature, one cell per state. Each cell prints its count (never colour alone).
    Cells wrap onto extra lines when a feature has many states, so nothing is ever clipped."""
    ROW_H = 28
    GAP = 6
    LABEL_MAX = 200
    LEGEND_H = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[tuple[str, str, tuple[str, ...]]] = []
        self._visits: dict[tuple[str, str], int] = {}
        self._errored: set[tuple[str, str]] = set()
        self._max = 0
        self.sampled = True
        self.setMouseTracking(True)
        self.setMinimumHeight(80)
        sp = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred); sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Feature-State Atlas")
        self._describe()

    # ---- data
    def set_features(self, rows) -> None:
        """rows: list of (feature id, display name, states)"""
        self._rows = [(a, b, tuple(c)) for a, b, c in rows]
        self.updateGeometry(); self.update(); self._describe()

    def set_visits(self, visits: dict[tuple[str, str], int]) -> None:
        self._visits = visits
        self._max = max(visits.values(), default=0)
        self.update(); self._describe()

    def set_errored(self, cells) -> None:
        self._errored = set(cells)
        self.update(); self._describe()

    def errored(self) -> set[tuple[str, str]]:
        return set(self._errored)

    def _describe(self) -> None:
        total = sum(len(s) for _, _, s in self._rows)
        seen = sum(1 for k, v in self._visits.items() if v > 0)
        how = "in the sampled actions" if self.sampled else "so far"
        text = f"{seen} of {total} feature states seen {how}."
        if self._errored:
            text += f" An error happened in {len(self._errored)} of them."
        self.setAccessibleDescription(text)

    # ---- geometry
    def _label_w(self) -> int:
        fm = QFontMetrics(self.font())
        return min(self.LABEL_MAX, max([fm.horizontalAdvance(n) for _, n, _ in self._rows] + [60]) + 16)

    def _cell_w(self) -> int:
        fm = QFontMetrics(self.font())
        longest = max([fm.horizontalAdvance(f"{st}  {max(self._max, 99)}") for _, _, s in self._rows for st in s] + [20])
        return max(44, min(150, longest + 16))

    def _per_line(self, width: int) -> int:
        return max(1, (width - self._label_w() - 8) // self._cell_w())

    def _lines(self, n_states: int, width: int) -> int:
        return max(1, -(-n_states // self._per_line(width)))

    def heightForWidth(self, w: int) -> int:
        per = self._per_line(w)
        body = sum(max(1, -(-len(s) // per)) * self.ROW_H + self.GAP for _, _, s in self._rows)
        return 8 + max(body, self.ROW_H) + self.LEGEND_H

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        return QSize(520, self.heightForWidth(520))

    def minimumSizeHint(self) -> QSize:
        return QSize(260, 60)

    def _cells(self):
        """Yield (fid, state, QRectF) for every cell at the current width."""
        label_w, cell_w, per = self._label_w(), self._cell_w(), self._per_line(self.width())
        y = 4
        for fid, _name, states in self._rows:
            for k, st in enumerate(states):
                line, col = divmod(k, per)
                yield fid, st, QRectF(label_w + col * cell_w + 1, y + line * self.ROW_H, cell_w - 2, self.ROW_H - 4)
            y += self._lines(len(states), self.width()) * self.ROW_H + self.GAP

    def cell_at(self, pos):
        for fid, st, r in self._cells():
            if r.contains(float(pos.x()), float(pos.y())):
                return fid, st
        return None

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            hit = self.cell_at(e.pos())
            if hit:
                n = self._visits.get(hit, 0)
                name = next((nm for fid, nm, _ in self._rows if fid == hit[0]), hit[0])
                tip = f"{name} = {hit[1]}: seen {n:,} time(s)" + (" (sampled)" if self.sampled else "")
                if hit in self._errored:
                    tip += ". An error happened in this state."
                QToolTip.showText(e.globalPos(), tip, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    # ---- painting
    def paintEvent(self, ev) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        if not self._rows:
            p.setPen(pal.placeholderText().color())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, "Add features to see which states have been visited.")
            return
        label_w = self._label_w()
        fm = QFontMetrics(self.font())
        clip = ev.rect()
        y = 4
        bad = theme.sem("fail")
        for fid, name, states in self._rows:
            h = self._lines(len(states), self.width()) * self.ROW_H
            if y + h >= clip.top() and y <= clip.bottom():           # only paint rows that are exposed (1,000 features stay quick)
                p.setPen(pal.windowText().color())
                p.drawText(QRectF(0, y, label_w - 8, self.ROW_H - 4), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                           fm.elidedText(name, Qt.TextElideMode.ElideRight, int(label_w - 12)))
                per, cell_w = self._per_line(self.width()), self._cell_w()
                for k, st in enumerate(states):
                    line, col = divmod(k, per)
                    n = self._visits.get((fid, st), 0)
                    frac = math.log1p(n) / math.log1p(self._max) if self._max else 0.0
                    bg = theme.heat_color(frac if n else 0.0)
                    rect = QRectF(label_w + col * cell_w + 1, y + line * self.ROW_H, cell_w - 2, self.ROW_H - 4)
                    err = (fid, st) in self._errored
                    pen = QPen(bad, 2.5) if err else QPen(pal.mid().color(), 1, Qt.PenStyle.SolidLine if n else Qt.PenStyle.DashLine)
                    p.setPen(pen); p.setBrush(bg); p.drawRoundedRect(rect, 4, 4)
                    p.setPen(theme.text_on(bg))
                    label = fm.elidedText(f"{st}  {n}" + (" !" if err else ""), Qt.TextElideMode.ElideRight, int(cell_w - 8))
                    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
            y += h + self.GAP
        p.setPen(pal.placeholderText().color())
        legend = ("Each cell: state name and how often it was seen" + (" (counted from the recent steps, so it can lag behind)" if self.sampled else "")
                  + ". Dashed = not seen yet. Red outline and ! = an error happened in that state.")
        p.drawText(QRectF(label_w, y, max(10, self.width() - label_w), self.LEGEND_H), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(legend, Qt.TextElideMode.ElideRight, max(10, self.width() - label_w)))


class ElidedLabel(QLabel):
    """One-line label that never forces its window wider: long text is cut with an ellipsis (full text in the tooltip)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = " ".join(str(text).split())
        self.setToolTip(self._full if len(self._full) > 20 else "")
        self._fit()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit()

    def _fit(self) -> None:
        fm = QFontMetrics(self.font())
        QLabel.setText(self, fm.elidedText(self._full, Qt.TextElideMode.ElideRight, max(20, self.width() - 4)))

    def sizeHint(self) -> QSize:
        return QSize(120, super().sizeHint().height())


def fit_table(view, rows: int, min_rows: int = 3, max_rows: int = 8) -> None:
    """Size a table to its rows (within bounds) so pages don't show big empty areas."""
    n = max(min_rows, min(max_rows, rows))
    h = view.horizontalHeader().height() or 26
    row_h = view.verticalHeader().defaultSectionSize() or 28
    view.setFixedHeight(h + n * row_h + 4)
