"""Step 3: features table + rules between features."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QStackedWidget, QStyledItemDelegate, QTableView,
    QVBoxLayout, QWidget,
)

from ..types import ConstraintDef, FeatureDef, Provenance
from . import backend, theme
from .widgets import PageBase, empty_state, muted
from .workers import run_async

KIND_LABEL = {"toggle": "On/off switch", "multistate": "Several states", "action": "Button / action"}
KIND_FROM_LABEL = {v: k for k, v in KIND_LABEL.items()}
COLS = ["Name", "Kind", "States", "Starts as", "Test it", "Dangerous", "Source"]


class ComboDelegate(QStyledItemDelegate):
    def __init__(self, items_for: Callable[[QModelIndex], list[str]], parent=None):
        super().__init__(parent)
        self._items_for = items_for

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        cb.addItems(self._items_for(index))
        return cb

    def setEditorData(self, editor, index):
        i = editor.findText(str(index.data(Qt.ItemDataRole.EditRole)))
        editor.setCurrentIndex(max(0, i))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)


class FeatureTableModel(QAbstractTableModel):
    def __init__(self, pm, parent=None):
        super().__init__(parent)
        self.pm = pm

    def rows(self) -> tuple[FeatureDef, ...]:
        return self.pm.project.features

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows())

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[s]
        return None

    def reload(self) -> None:
        self.beginResetModel(); self.endResetModel()

    def flags(self, ix):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        c = ix.column()
        fe = self.rows()[ix.row()]
        if c in (4, 5):
            return f | Qt.ItemFlag.ItemIsUserCheckable
        if c == 6:
            return f
        if fe.kind == "action" and c in (2, 3):
            return f
        return f | Qt.ItemFlag.ItemIsEditable

    def data(self, ix, role=Qt.ItemDataRole.DisplayRole):
        f = self.rows()[ix.row()]; c = ix.column()
        if c == 4 and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if f.enabled else Qt.CheckState.Unchecked
        if c == 5 and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if f.dangerous else Qt.CheckState.Unchecked
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if c == 0:
                return f.name or f.id
            if c == 1:
                return KIND_LABEL.get(f.kind, f.kind)
            if c == 2:
                return "(fires once)" if f.kind == "action" else ", ".join(f.states)
            if c == 3:
                return "-" if f.kind == "action" else f.default_state
            if c == 6 and role == Qt.ItemDataRole.DisplayRole:
                return theme.CONFIDENCE.get(f.provenance.confidence, theme.CONFIDENCE["unknown"])[0]
        if role == Qt.ItemDataRole.DecorationRole and c == 6:
            lvl = f.provenance.confidence if f.provenance.confidence in theme.CONFIDENCE else "unknown"
            return theme.glyph(theme.CONFIDENCE[lvl][1], theme.sem(theme.CONFIDENCE[lvl][2]))
        if role == Qt.ItemDataRole.ToolTipRole:
            if c == 5:
                return "Dangerous features are never switched on automatically (for example Delete all)."
            if c == 4:
                return "Untick to leave this feature out of the test."
            if c == 6:
                lvl = f.provenance.confidence
                return theme.CONFIDENCE.get(lvl, theme.CONFIDENCE["unknown"])[3]
            if c == 2:
                return "Type the states separated by commas, for example: off, low, high"
        return None

    def setData(self, ix, value, role=Qt.ItemDataRole.EditRole):
        f = self.rows()[ix.row()]; c = ix.column()
        if role == Qt.ItemDataRole.CheckStateRole and c in (4, 5):
            on = Qt.CheckState(value) == Qt.CheckState.Checked
            self.pm.update_feature(f.id, **({"enabled": on} if c == 4 else {"dangerous": on}))
            return True
        if role != Qt.ItemDataRole.EditRole:
            return False
        text = str(value).strip()
        if c == 0:
            self.pm.update_feature(f.id, name=text or f.id)
        elif c == 1:
            kind = KIND_FROM_LABEL.get(text, text)
            if kind == f.kind:
                return False
            if kind == "toggle":
                self.pm.update_feature(f.id, kind=kind, states=("off", "on"), default_state="off")
            elif kind == "multistate":
                st = f.states if len(f.states) >= 2 else ("off", "low", "high")
                self.pm.update_feature(f.id, kind=kind, states=st, default_state=st[0])
            else:
                self.pm.update_feature(f.id, kind="action", states=(), default_state="")
        elif c == 2:
            st = tuple(dict.fromkeys(s.strip() for s in text.split(",") if s.strip()))
            if len(st) < 2:
                return False
            self.pm.update_feature(f.id, states=st, default_state=f.default_state if f.default_state in st else st[0])
        elif c == 3:
            if text not in f.states:
                return False
            self.pm.update_feature(f.id, default_state=text)
        else:
            return False
        return True


class FeaturesPage(PageBase):
    title = "Name the features to test"
    hint = "Check each feature: a clear name, its possible states, and the state it starts in. Add <b>rules</b> below for things that can't happen together."

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.tm = FeatureTableModel(model, self)
        self.table = QTableView(); self.table.setModel(self.tm)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True); self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(190); self.table.setAccessibleName("Features")
        h = self.table.horizontalHeader(); h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch); h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setItemDelegateForColumn(1, ComboDelegate(lambda ix: list(KIND_LABEL.values()), self.table))
        self.table.setItemDelegateForColumn(3, ComboDelegate(lambda ix: list(self.tm.rows()[ix.row()].states), self.table))
        self.body.addWidget(self.table)
        self.empty = empty_state("No features yet.\nGo to Inspect and press <i>Add as feature</i>, or press <b>Add feature manually</b>.")
        self.body.addWidget(self.empty)
        row = QHBoxLayout()
        self.btn_add = QPushButton("Add feature manually"); self.btn_del = QPushButton("Remove selected")
        self.btn_suggest = QPushButton("Suggest dependencies...")
        self.btn_suggest.setToolTip("Toggles features one at a time (and puts them back) to find rules you might have missed.")
        row.addWidget(self.btn_add); row.addWidget(self.btn_del); row.addStretch(1); row.addWidget(self.btn_suggest)
        self.body.addLayout(row)
        self.sug_status = muted("")
        self.body.addWidget(self.sug_status)

        box = QGroupBox("Rules between features")
        bl = QVBoxLayout(box)
        self.rules = QListWidget(); self.rules.setMaximumHeight(110); self.rules.setAccessibleName("Rules")
        bl.addWidget(self.rules)
        self.btn_rule_del = QPushButton("Remove selected rule")
        bl.addWidget(self.btn_rule_del, 0, Qt.AlignmentFlag.AlignLeft)
        bl.addWidget(QLabel("<b>Add a rule</b>"))
        self.rule_kind = QComboBox()
        self.rule_kind.addItems(["These can't be on at the same time", "This needs other features on first", "This exact combination is not allowed"])
        self.rule_kind.setAccessibleName("Rule type")
        bl.addWidget(self.rule_kind)
        self.stack = QStackedWidget()
        # mutex
        self.mx_list = QListWidget(); self.mx_list.setMaximumHeight(110)
        self.stack.addWidget(self._wrap("Tick two or more features:", self.mx_list))
        # requires
        self.rq_feature = QComboBox(); self.rq_needs = QListWidget(); self.rq_needs.setMaximumHeight(90)
        w = QWidget(); wl = QVBoxLayout(w); wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(QLabel("When this feature is used:")); wl.addWidget(self.rq_feature)
        wl.addWidget(QLabel("these must already be on:")); wl.addWidget(self.rq_needs)
        self.stack.addWidget(w)
        # forbid
        self.fb_rows: list[tuple[QComboBox, QComboBox]] = []
        self.fb_host = QWidget(); self.fb_lay = QVBoxLayout(self.fb_host); self.fb_lay.setContentsMargins(0, 0, 0, 0)
        w2 = QWidget(); w2l = QVBoxLayout(w2); w2l.setContentsMargins(0, 0, 0, 0)
        w2l.addWidget(QLabel("Never allow all of these at once:")); w2l.addWidget(self.fb_host)
        self.btn_fb_more = QPushButton("+ Add another condition"); w2l.addWidget(self.btn_fb_more, 0, Qt.AlignmentFlag.AlignLeft)
        self.stack.addWidget(w2)
        bl.addWidget(self.stack)
        r2 = QHBoxLayout(); self.btn_rule_add = QPushButton("Add rule"); self.rule_msg = QLabel()
        r2.addWidget(self.btn_rule_add); r2.addWidget(self.rule_msg, 1)
        bl.addLayout(r2)
        self.body.addWidget(box)
        self.body.addStretch(1)

        self.rule_kind.currentIndexChanged.connect(self.stack.setCurrentIndex)
        self.btn_add.clicked.connect(self._add)
        self.btn_del.clicked.connect(self._remove)
        self.btn_rule_add.clicked.connect(self._add_rule)
        self.btn_rule_del.clicked.connect(self._del_rule)
        self.btn_fb_more.clicked.connect(self._fb_add_row)
        self.btn_suggest.clicked.connect(self._suggest)
        self._sig: tuple = ()
        self.refresh()

    @staticmethod
    def _wrap(label: str, w: QWidget) -> QWidget:
        host = QWidget(); l = QVBoxLayout(host); l.setContentsMargins(0, 0, 0, 0)
        l.addWidget(QLabel(label)); l.addWidget(w)
        return host

    def refresh(self) -> None:
        self.tm.reload()
        feats = self.model.project.features
        self.empty.setVisible(not feats)
        self.table.setVisible(bool(feats))
        sig = tuple((f.id, f.name, f.states) for f in feats)
        if sig != self._sig:
            self._sig = sig
            self._rebuild_builder()
        self.rules.clear()
        for i, c in enumerate(self.model.project.constraints):
            it = QListWidgetItem(backend.describe_constraint(c, self.model.project)); it.setData(Qt.ItemDataRole.UserRole, i)
            self.rules.addItem(it)
        if not self.model.project.constraints:
            self.rules.addItem("No rules. Every combination of states is allowed.")
            self.rules.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        self.btn_del.setEnabled(bool(feats))

    def _rebuild_builder(self) -> None:
        feats = [f for f in self.model.project.features if f.kind != "action"]
        for lst in (self.mx_list, self.rq_needs):
            lst.clear()
            for f in feats:
                it = QListWidgetItem(f.name or f.id); it.setData(Qt.ItemDataRole.UserRole, f.id)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable); it.setCheckState(Qt.CheckState.Unchecked)
                lst.addItem(it)
        self.rq_feature.clear()
        for f in feats:
            self.rq_feature.addItem(f.name or f.id, f.id)
        for cb, sb in self.fb_rows:
            cb.parentWidget().deleteLater()
        self.fb_rows = []
        self._fb_add_row()

    def _fb_add_row(self) -> None:
        feats = [f for f in self.model.project.features if f.kind != "action"]
        host = QWidget(); hl = QHBoxLayout(host); hl.setContentsMargins(0, 0, 0, 0)
        cf, cs = QComboBox(), QComboBox()
        for f in feats:
            cf.addItem(f.name or f.id, f.id)

        def fill():
            cs.clear()
            f = next((x for x in feats if x.id == cf.currentData()), None)
            cs.addItems(list(f.states) if f else [])
        cf.currentIndexChanged.connect(fill); fill()
        hl.addWidget(cf, 1); hl.addWidget(QLabel("is")); hl.addWidget(cs, 1)
        self.fb_lay.addWidget(host); self.fb_rows.append((cf, cs))

    # ---- actions
    def _add(self) -> None:
        fid = self.model.unique_id("feature")
        self.model.add_feature(FeatureDef(fid, f"New feature {len(self.model.project.features) + 1}", provenance=Provenance("manual", "manual", "")))
        self.rule_msg.setText("")
        self.table.selectRow(len(self.model.project.features) - 1)
        self.table.edit(self.tm.index(len(self.model.project.features) - 1, 0))

    def _remove(self) -> None:
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return
        f = self.model.project.features[sel[0].row()]
        used = [c for c in self.model.project.constraints if f.id in c.features or f.id in c.needs or f.id in c.assignment]
        if used and QMessageBox.question(self, "Remove feature", f"'{f.name or f.id}' is used in {len(used)} rule(s). Removing it also removes those rules. Continue?") != QMessageBox.StandardButton.Yes:
            return
        self.model.remove_feature(f.id)

    def _say(self, text: str, level: str = "fail") -> None:
        self.rule_msg.setText(text); self.rule_msg.setStyleSheet(f"color:{theme.sem_hex(level)};")

    def _add_rule(self) -> None:
        k = self.rule_kind.currentIndex()
        checked = lambda lst: tuple(lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count()) if lst.item(i).checkState() == Qt.CheckState.Checked)  # noqa: E731
        if k == 0:
            ids = checked(self.mx_list)
            if len(ids) < 2:
                return self._say("Tick at least two features.")
            c = ConstraintDef("mutex", features=ids)
        elif k == 1:
            subject, needs = self.rq_feature.currentData(), checked(self.rq_needs)
            if not subject or not needs:
                return self._say("Choose a feature and at least one it needs.")
            if subject in needs:
                return self._say("A feature can't need itself.")
            c = ConstraintDef("requires", features=(subject,), needs=needs)
        else:
            asg = {}
            for cf, cs in self.fb_rows:
                if cf.currentData():
                    asg[cf.currentData()] = cs.currentText()
            if not asg:
                return self._say("Add at least one condition.")
            c = ConstraintDef("forbid", assignment=asg)
        if c in self.model.project.constraints:
            return self._say("That rule already exists.", "warn")
        self.model.add_constraint(c)
        self._say("Rule added.", "ok")

    def _del_rule(self) -> None:
        it = self.rules.currentItem()
        if it and it.data(Qt.ItemDataRole.UserRole) is not None:
            self.model.remove_constraint(it.data(Qt.ItemDataRole.UserRole))

    # ---- inference
    def _suggest(self) -> None:
        if not self.model.project.features:
            self.sug_status.setText("Add some features first.")
            return
        self.btn_suggest.setEnabled(False)
        self.sug_status.setText("Watching how the app reacts... we switch features one at a time and put everything back.")
        proj, spec = self.model.project, self.model.adapter_spec
        run_async(lambda: backend.suggest_dependencies(proj, spec), self._got, self._fail)

    def _fail(self, msg: str) -> None:
        self.btn_suggest.setEnabled(True)
        self.sug_status.setText(f"Could not suggest dependencies. {msg}")

    def _got(self, props: list[dict]) -> None:
        self.btn_suggest.setEnabled(True)
        if not props:
            self.sug_status.setText("No dependencies found. Nothing changed when we toggled features, so no rules are suggested.")
            return
        self.sug_status.setText(f"{len(props)} suggestion(s) found.")
        dlg = SuggestDialog(props, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            for p in dlg.accepted():
                self.model.edit("Accept suggested rule", lambda proj, p=p: p.apply(proj))


class SuggestDialog(QDialog):
    def __init__(self, props: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Suggested dependencies")
        self.resize(560, 360)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("We noticed these relationships. Tick the ones that are right and press Accept. Nothing is changed until you accept."))
        self.list = QListWidget()
        for p in props:
            it = QListWidgetItem(f"{p['text']}   [{theme.CONFIDENCE.get(p['confidence'], theme.CONFIDENCE['inferred'])[0]}, seen {p.get('evidence', '?')}x]")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable); it.setCheckState(Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, p["proposal"])
            self.list.addItem(it)
        lay.addWidget(self.list)
        bb = QDialogButtonBox(); self.ok = bb.addButton("Accept ticked", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton("Reject all", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def accepted(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]
