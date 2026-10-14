"""Step 1: choose what to test."""
from __future__ import annotations

import ntpath
import os
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QRadioButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..types import TargetRef
from . import backend
from .widgets import PageBase, muted
from .workers import run_async


class TargetPage(PageBase):
    title = "Pick the program to test"
    hint = "Select the window of the program you want to test, or browse to its EXE file. Then press Next."

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self._windows: list[dict] = []

        self.r_running = QRadioButton("A program that is already running")
        self.r_exe = QRadioButton("Start a program for me (browse for an EXE file)")
        self.r_running.setChecked(True)
        self.body.addWidget(self.r_running)

        # running list
        self.running_box = QWidget(); rl = QVBoxLayout(self.running_box); rl.setContentsMargins(24, 0, 0, 0)
        bar = QHBoxLayout()
        self.filter = QLineEdit(); self.filter.setPlaceholderText("Type to filter by program or window title"); self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName("Filter running programs")
        self.btn_refresh = QPushButton("Refresh list")
        bar.addWidget(self.filter, 1); bar.addWidget(self.btn_refresh)
        rl.addLayout(bar)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Program", "Window title", "PID", "Bits"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader(); h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents); h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(170)
        self.table.setAccessibleName("Running programs")
        rl.addWidget(self.table)
        self.list_note = muted("")
        rl.addWidget(self.list_note)
        self.body.addWidget(self.running_box)

        self.body.addWidget(self.r_exe)
        self.exe_box = QWidget(); el = QFormLayout(self.exe_box); el.setContentsMargins(24, 0, 0, 0)
        row = QHBoxLayout()
        self.exe_path = QLineEdit(); self.exe_path.setPlaceholderText(r"C:\Program Files\MyApp\myapp.exe")
        self.exe_path.setAccessibleName("EXE path")
        self.btn_browse = QPushButton("Browse...")
        row.addWidget(self.exe_path, 1); row.addWidget(self.btn_browse)
        el.addRow("Program file:", row)
        self.exe_args = QLineEdit(); self.exe_args.setPlaceholderText("Optional, e.g. --profile test"); self.exe_args.setAccessibleName("Arguments")
        el.addRow("Arguments:", self.exe_args)
        self.body.addWidget(self.exe_box)

        self.card = QGroupBox("Selected target")
        cf = QFormLayout(self.card)
        self.v_path = QLabel("-"); self.v_arch = QLabel("-"); self.v_pid = QLabel("-"); self.v_title = QLabel("-")
        for lab in (self.v_path, self.v_title, self.v_arch, self.v_pid):
            lab.setWordWrap(True); lab.setTextFormat(Qt.TextFormat.PlainText)
            lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cf.addRow("Path:", self.v_path); cf.addRow("Type:", self.v_arch); cf.addRow("Process ID:", self.v_pid); cf.addRow("Window title:", self.v_title)
        self.body.addWidget(self.card)
        self.body.addStretch(1)

        self.r_running.toggled.connect(self._mode)
        self.btn_refresh.clicked.connect(self.reload)
        self.filter.textChanged.connect(self._fill)
        self.table.itemSelectionChanged.connect(self._picked)
        self.btn_browse.clicked.connect(self._browse)
        self.exe_path.editingFinished.connect(self._exe_changed)
        self.exe_args.editingFinished.connect(self._exe_changed)
        self._mode()
        self.reload()
        self.refresh()

    def _mode(self) -> None:
        running = self.r_running.isChecked()
        self.running_box.setEnabled(running)
        self.exe_box.setEnabled(not running)
        if running:
            self._picked()
        else:
            self._exe_changed()

    def reload(self) -> None:
        self.btn_refresh.setEnabled(False); self.list_note.setText("Looking for open windows...")
        run_async(backend.list_windows, self._got, self._list_failed)

    def _list_failed(self, msg: str) -> None:
        self._windows = []
        self.btn_refresh.setEnabled(True)
        self._fill()
        self.list_note.setText(f"We could not list the open windows. {msg} You can still choose \"Start a program for me\" and browse for the EXE.")

    def _got(self, wins) -> None:
        self._windows = list(wins)
        self.btn_refresh.setEnabled(True)
        import sys
        self.list_note.setText("Showing example windows because this computer is not running Windows. Use File > Open demo project to explore."
                               if sys.platform != "win32" else "Only visible windows with a title are listed.")
        self._fill()

    def _fill(self) -> None:
        q = self.filter.text().lower()
        rows = [w for w in self._windows if q in (w["process"] + " " + w["title"]).lower()]
        keep = self.model.project.target.pid
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, w in enumerate(rows):
            for c, v in enumerate((w["process"], w["title"], str(w["pid"]), w["arch"])):
                it = QTableWidgetItem(v); it.setData(Qt.ItemDataRole.UserRole, w)
                self.table.setItem(r, c, it)
            if keep and w["pid"] == keep:
                self.table.selectRow(r)
        self.table.blockSignals(False)
        if not rows and self._windows is not None and self.list_note.text().startswith(("Looking", "Only visible", "Showing")):
            self.list_note.setText("No matching windows. Start your program, then press Refresh list.")

    def _picked(self) -> None:
        if not self.r_running.isChecked():
            return
        items = self.table.selectedItems()
        if not items:
            return
        w = items[0].data(Qt.ItemDataRole.UserRole)
        self.model.set_target(TargetRef(kind="process", exe_path=w["path"], process_name=w["process"], pid=w["pid"],
                                        title_regex=re.escape(w["title"])))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose the program to test", self.exe_path.text(), "Programs (*.exe);;All files (*)")
        if path:
            self.exe_path.setText(path)
            self._exe_changed()

    def _exe_changed(self) -> None:
        if self.r_running.isChecked():
            return
        path = self.exe_path.text().strip().strip('"').strip()      # a path pasted from Explorer often comes quoted
        if not path:
            return
        if path != self.exe_path.text():
            self.exe_path.setText(path)
        import shlex
        try:
            args = tuple(shlex.split(self.exe_args.text(), posix=False))
        except ValueError:
            args = tuple(self.exe_args.text().split())
        self.model.set_target(TargetRef(kind="exe", exe_path=path, args=args, process_name=ntpath.basename(path)))

    def refresh(self) -> None:
        t = self.model.project.target
        if t.kind == "exe":
            self.r_exe.blockSignals(True); self.r_exe.setChecked(True); self.r_exe.blockSignals(False)
            self.running_box.setEnabled(False); self.exe_box.setEnabled(True)
            if self.exe_path.text() != t.exe_path:
                self.exe_path.setText(t.exe_path)
            self.exe_args.setText(" ".join(t.args))
        path = t.exe_path or t.process_name
        self.v_path.setText(path or "Nothing selected yet. Choose a program above.")
        if t.exe_path and os.path.exists(t.exe_path):
            arch = backend.pe_arch(t.exe_path)
        else:
            arch = next((w["arch"] for w in self._windows if w["pid"] == t.pid), "-") if t.pid else "-"
        self.v_arch.setText(arch)
        self.v_pid.setText(str(t.pid) if t.pid else ("Will be set when started" if t.kind == "exe" and t.exe_path else "-"))
        self.v_title.setText(re.sub(r"\\(.)", r"\1", t.title_regex) if t.title_regex else "-")
