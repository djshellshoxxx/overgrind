"""Step 2: scan the app, read the element tree, add features/controls."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QSizePolicy, QSplitter,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..types import ControlDef, Domain, ElementInfo, FeatureDef, Provenance
from . import backend, theme
from .widgets import PageBase, StabilityMeter, empty_state, muted
from .workers import run_async


class InspectPage(PageBase):
    title = "Look inside the program"
    hint = "Press <b>Scan app</b>. We only read the screen; nothing is clicked. Pick an element, then add it as a feature (on/off things, modes, buttons) or a control (values we type or slide)."

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.elements: list[ElementInfo] = []
        self._items: list[QTreeWidgetItem] = []

        bar = QHBoxLayout()
        self.btn_scan = QPushButton("Scan app"); self.btn_scan.setObjectName("Primary")
        self.search = QLineEdit(); self.search.setPlaceholderText("Search by name, type or ID (Ctrl+F)"); self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search elements")
        self.status = muted("", wrap=False)
        bar.addWidget(self.btn_scan); bar.addWidget(self.search, 1); bar.addWidget(self.status)
        self.body.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal); split.setChildrenCollapsible(False)
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(["Element", "Type", "Looks like", "Source"])
        self.tree.setAlternatingRowColors(True); self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(300); self.tree.setMinimumWidth(320)
        self.tree.setAccessibleName("Element tree")
        self.tree.setColumnWidth(0, 170); self.tree.setColumnWidth(1, 80); self.tree.setColumnWidth(2, 70)
        split.addWidget(self.tree)

        self.detail = QFrame(); self.detail.setObjectName("Card")
        dv = QVBoxLayout(self.detail); dv.setContentsMargins(12, 12, 12, 12); dv.setSpacing(6)
        self.d_empty = empty_state("Nothing selected.\nScan the app, then click an element on the left.")
        dv.addWidget(self.d_empty)
        self.d_body = QWidget(); db = QVBoxLayout(self.d_body); db.setContentsMargins(0, 0, 0, 0); db.setSpacing(6)
        self.d_name = QLabel(); f = self.d_name.font(); f.setPointSizeF(f.pointSizeF() * 1.3); f.setBold(True); self.d_name.setFont(f)
        self.d_name.setWordWrap(True); self.d_name.setTextFormat(Qt.TextFormat.PlainText)
        self.d_name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        db.addWidget(self.d_name)
        form = QFormLayout(); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.d_type = QLabel(); self.d_addr = QLabel(); self.d_value = QLabel(); self.d_patterns = QLabel(); self.d_bounds = QLabel()
        for w in (self.d_type, self.d_addr, self.d_value, self.d_patterns, self.d_bounds):
            w.setWordWrap(True); w.setTextFormat(Qt.TextFormat.PlainText); w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)      # a long unbroken name wraps/clips, never widens the page
        form.addRow("Type:", self.d_type); form.addRow("Address:", self.d_addr); form.addRow("Current value:", self.d_value)
        form.addRow("Can do:", self.d_patterns); form.addRow("Position:", self.d_bounds)
        db.addLayout(form)
        sug = QHBoxLayout()
        self.d_suggest = QLabel(); self.d_suggest.setWordWrap(True)
        self.badge = theme.Badge()
        sug.addWidget(self.d_suggest, 1); sug.addWidget(self.badge)
        db.addLayout(sug)
        db.addWidget(QLabel("<b>How reliably can we find it again?</b>"))
        self.meter = StabilityMeter(); db.addWidget(self.meter)
        self.d_why = QLabel(); self.d_why.setWordWrap(True); self.d_why.setTextFormat(Qt.TextFormat.RichText)
        db.addWidget(self.d_why)
        self.fallbacks = QListWidget(); self.fallbacks.setMaximumHeight(90); self.fallbacks.setAccessibleName("Backup ways to find this element")
        self.fb_label = QLabel("<b>Backup ways to find it (best first)</b>")
        db.addWidget(self.fb_label); db.addWidget(self.fallbacks)
        btns = QHBoxLayout()
        self.btn_feat = QPushButton("Add as feature"); self.btn_ctrl = QPushButton("Add as control")
        btns.addWidget(self.btn_feat); btns.addWidget(self.btn_ctrl); btns.addStretch(1)
        db.addLayout(btns)
        self.msg = QLabel(); self.msg.setWordWrap(True)
        db.addWidget(self.msg)
        db.addStretch(1)
        dv.addWidget(self.d_body)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 4); split.setSizes([430, 470])
        self.body.addWidget(split, 1)
        self.tree.setMaximumHeight(16777215)

        self.btn_scan.clicked.connect(self.scan)
        self.search.textChanged.connect(self._filter)
        self.tree.currentItemChanged.connect(self._select)
        self.btn_feat.clicked.connect(self._add_feature)
        self.btn_ctrl.clicked.connect(self._add_control)
        self._show_detail(None)
        self._placeholder("No scan yet. Press <b>Scan app</b>.")

    # ---- scanning
    def scan(self) -> None:
        self.btn_scan.setEnabled(False); self.status.setText("Scanning...")
        spec = self.model.adapter_spec
        target = self.model.project.target
        run_async(lambda: backend.inspect_target(spec, target), self._scanned, self._scan_failed)

    def _scan_failed(self, msg: str) -> None:
        self.btn_scan.setEnabled(True); self.status.setText("")
        self._placeholder(f"We could not scan the program. {html.escape(msg)}<br>If you only want to look around, open the demo from File &gt; Open demo project.")

    def _placeholder(self, html: str) -> None:
        self.tree.clear(); self._items = []
        it = QTreeWidgetItem([""]); self.tree.addTopLevelItem(it)
        lab = QLabel(html); lab.setWordWrap(True); lab.setTextFormat(Qt.TextFormat.RichText); lab.setMargin(8)
        it.setFirstColumnSpanned(True)
        self.tree.setItemWidget(it, 0, lab)
        it.setFlags(Qt.ItemFlag.NoItemFlags)

    def _scanned(self, els) -> None:
        self.btn_scan.setEnabled(True)
        self.elements = list(els)
        self.status.setText(f"{len(self.elements)} elements")
        self.tree.clear(); self._items = []
        if not self.elements:
            self._placeholder("The scan finished but found no elements. Make sure the program window is open and not minimised, then scan again.")
            return
        for i, e in enumerate(self.elements):
            cols = [e.name or "(no name)", e.control_type, e.suggested_kind or "-", self._source(e)]
            it = QTreeWidgetItem(cols)
            it.setData(0, Qt.ItemDataRole.UserRole, i)
            if e.confidence in theme.CONFIDENCE:
                it.setIcon(3, theme.glyph(theme.CONFIDENCE[e.confidence][1], theme.sem(theme.CONFIDENCE[e.confidence][2])))
            parent = self._items[e.parent] if e.parent is not None and 0 <= e.parent < len(self._items) else None
            (parent.addChild(it) if parent else self.tree.addTopLevelItem(it))
            self._items.append(it)
        self.tree.expandAll()
        if self._items:
            self.tree.setCurrentItem(self._items[min(1, len(self._items) - 1)])
        self._filter()

    @staticmethod
    def _source(e: ElementInfo) -> str:
        return theme.CONFIDENCE.get(e.confidence, theme.CONFIDENCE["unknown"])[0]

    # ---- search
    def focus_search(self) -> None:
        self.search.setFocus(); self.search.selectAll()

    def _filter(self) -> None:
        q = self.search.text().strip().lower()
        for it in self._items:
            it.setHidden(False)
        if not q:
            return
        def match(i: int) -> bool:
            e = self.elements[i]
            return q in f"{e.name} {e.control_type} {e.selector.auto_id} {e.suggested_kind}".lower()
        keep = set()
        for i, e in enumerate(self.elements):
            if match(i):
                j: int | None = i
                while j is not None and j not in keep:
                    keep.add(j); j = self.elements[j].parent
        for i, it in enumerate(self._items):
            it.setHidden(i not in keep)

    # ---- details
    def _select(self, cur, _prev) -> None:
        idx = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        self._show_detail(self.elements[idx] if idx is not None and idx < len(self.elements) else None)

    def current_element(self) -> ElementInfo | None:
        cur = self.tree.currentItem()
        idx = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        return self.elements[idx] if idx is not None and idx < len(self.elements) else None

    def _show_detail(self, e: ElementInfo | None) -> None:
        self.d_empty.setVisible(e is None); self.d_body.setVisible(e is not None)
        self.msg.setText("")
        if e is None:
            return
        self.d_name.setText(e.name or "(no name)")
        self.d_type.setText(e.control_type or "-")
        self.d_addr.setText(backend.describe_selector(e.selector))
        self.d_value.setText("-" if e.value is None else str(e.value))
        self.d_patterns.setText(", ".join(e.patterns) or "-")
        self.d_bounds.setText("x={}, y={}, w={}, h={}".format(*e.bounds))
        kind = e.suggested_kind
        if kind:
            extra = f" ({backend.describe_domain(e.suggested_domain)})" if e.suggested_domain else ""
            self.d_suggest.setText(f"<b>Looks like:</b> {html.escape(self._friendly_kind(kind))}{html.escape(extra)}")
        else:
            self.d_suggest.setText("<b>Looks like:</b> we could not tell. You will need to confirm the type and range.")
        self.badge.set_confidence(e.confidence if e.confidence in theme.CONFIDENCE else "unknown")
        rep = backend.selector_report(e.selector)
        word, colour = backend.score_label(rep.score)
        self.meter.set_score(rep.score, word, colour)
        parts = [f"<b>Why:</b> {html.escape(rep.reason) or 'No reason given.'}"]
        if rep.estimated:
            parts.append("<i>(Quick estimate; the full locator check is not available in this build.)</i>")
        if rep.fixes and rep.score < 90:
            parts.append("<b>How to improve it:</b><ul style='margin:0'>" + "".join(f"<li>{html.escape(f)}</li>" for f in rep.fixes) + "</ul>")
        self.d_why.setText("<br>".join(parts))
        self.fallbacks.clear()
        for n, (desc, sc) in enumerate(rep.fallbacks, 1):
            self.fallbacks.addItem(f"{n}. {desc}" + (f"   ({sc}/100)" if sc is not None else ""))
        shown = bool(rep.fallbacks)
        self.fallbacks.setVisible(shown); self.fb_label.setVisible(shown)
        is_action = kind == "action"
        self.btn_ctrl.setEnabled(not is_action)
        self.btn_ctrl.setToolTip("Buttons are actions. Add them as features." if is_action else "")

    @staticmethod
    def _friendly_kind(k: str) -> str:
        return {"boolean": "an on/off switch", "integer": "a whole number", "float": "a decimal number", "choice": "a list of choices",
                "text": "a text box", "action": "a button (an action)"}.get(k, k)

    # ---- add
    def _add_feature(self) -> None:
        e = self.current_element()
        if not e:
            return
        if any(f.selector == e.selector for f in self.model.project.features):
            self.msg.setText("Already in your features."); self.msg.setStyleSheet(""); return
        fid = self.model.unique_id(e.selector.auto_id or e.name)
        prov = Provenance("uia", e.confidence if e.confidence in ("verified", "inferred") else "unknown", "added from Inspect")
        dom = e.suggested_domain
        if e.suggested_kind == "action":
            f = FeatureDef(fid, e.name or fid, "action", (), "", e.selector, provenance=prov)
        elif e.suggested_kind == "choice" and dom and dom.options:
            f = FeatureDef(fid, e.name or fid, "multistate", tuple(dom.options), dom.options[0], e.selector, provenance=prov)
        else:
            on = str(e.value).lower() in ("true", "on", "1", "checked")
            f = FeatureDef(fid, e.name or fid, "toggle", ("off", "on"), "on" if on else "off", e.selector, provenance=prov)
        self.model.add_feature(f)
        self.msg.setText(f"Added feature <b>{html.escape(f.name)}</b>. Review it on the Features page.")
        self.msg.setStyleSheet(f"color:{theme.sem_hex('ok')};")

    def _add_control(self) -> None:
        e = self.current_element()
        if not e or e.suggested_kind == "action":
            return
        if any(c.selector == e.selector for c in self.model.project.controls):
            self.msg.setText("Already in your controls."); self.msg.setStyleSheet(""); return
        cid = self.model.unique_id(e.selector.auto_id or e.name)
        known = e.suggested_kind in ("boolean", "integer", "float", "choice", "text") and e.suggested_domain is not None
        dom = e.suggested_domain if known else Domain("text", max_length=64)
        conf = e.confidence if known and e.confidence in ("verified", "inferred") else "unknown"
        c = ControlDef(cid, dom.kind, e.selector, dom, None, e.value, provenance=Provenance("uia", conf, "added from Inspect"))
        self.model.add_control(c)
        if known:
            self.msg.setText(f"Added control <b>{html.escape(e.name or cid)}</b>. Review its range on the Controls page.")
            self.msg.setStyleSheet(f"color:{theme.sem_hex('ok')};")
        else:
            self.msg.setText("Added, but we could not tell the type or range. Please set them on the Controls page; we never guess silently.")
            self.msg.setStyleSheet(f"color:{theme.sem_hex('warn')};")
