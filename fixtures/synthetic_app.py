"""Synthetic PySide6 target for Overgrind end-to-end tests (Windows CI; also runs offscreen).

Exposes standard UIA fields (objectName -> AutomationId, accessibleName -> Name): 4 checkboxes
(cb_a..cb_d, where cb_c/cb_d are mutually exclusive), a 3-state combo (cmb_mode: Off/Low/High),
two sliders (sld_volume 0..100, sld_gain 0..10), a spinbox (spn_count 0..50), a line edit
(edt_name, max 20) and buttons that DELIBERATELY crash / hang / pop a modal dialog.

Deliberate defects (so CI can prove crash/hang/dialog detection end to end):

  buttons          btn_crash, btn_hang, btn_modal (btn_delete is a decoy "dangerous" button)
  command line     --crash-after-ms N   --hang-after-ms N   --modal-after-ms N
                   --crash-mode exit|segv      (exit = os._exit(3), segv = null dereference)
                   --quit-after-ms N           (clean exit; keeps CI from leaking processes)
  hidden bugs      --bug SPEC (repeatable) or env OVERGRIND_BUG="SPEC;SPEC"
                     combo:ab[:ACTION]            crash when checkboxes a AND b are both checked
                     value:volume>=90[:ACTION]    when the control satisfies the comparison
                   ACTION is crash (default) | hang | modal.  Controls: volume, gain, count.

The bug logic (`parse_bugs`, `evaluate_bugs`) is pure Python and unit-tested without Qt.
"""
from __future__ import annotations

import argparse
import operator
import os
import re
import sys
from typing import Any

OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq}
ACTIONS = ("crash", "hang", "modal")


def parse_bugs(text: str) -> list[dict[str, Any]]:
    """'combo:ab:hang;value:volume>=90' -> [{'kind','action',...}]. Raises ValueError on junk."""
    bugs: list[dict[str, Any]] = []
    for raw in filter(None, (p.strip() for p in (text or "").split(";"))):
        parts = raw.split(":")
        kind = parts[0]
        action = parts[2] if len(parts) > 2 else "crash"
        if action not in ACTIONS:
            raise ValueError(f"bad bug action {action!r} in {raw!r}")
        if kind == "combo" and len(parts) >= 2 and re.fullmatch(r"[a-d]{1,4}", parts[1]):
            bugs.append({"kind": "combo", "boxes": tuple(sorted(set(parts[1]))), "action": action, "spec": raw})
        elif kind == "value" and len(parts) >= 2:
            m = re.fullmatch(r"(volume|gain|count)(>=|<=|==|>|<)(-?\d+(?:\.\d+)?)", parts[1])
            if not m:
                raise ValueError(f"bad value bug {raw!r}")
            bugs.append({"kind": "value", "control": m[1], "op": m[2], "limit": float(m[3]),
                         "action": action, "spec": raw})
        else:
            raise ValueError(f"unknown bug spec {raw!r}")
    return bugs


def evaluate_bugs(bugs: list[dict[str, Any]], checks: dict[str, bool],
                  values: dict[str, float]) -> dict[str, Any] | None:
    """First bug whose trigger holds for the current UI state, else None."""
    for b in bugs:
        if b["kind"] == "combo" and all(checks.get(x, False) for x in b["boxes"]):
            return b
        if b["kind"] == "value" and OPS[b["op"]](values.get(b["control"], 0.0), b["limit"]):
            return b
    return None


def do_action(action: str, crash_mode: str = "exit", hwnd: int = 0) -> None:
    if action == "crash":
        if crash_mode == "segv":
            import ctypes
            ctypes.string_at(0)
        os._exit(3)
    elif action == "hang":
        while True:          # busy loop on the GUI thread => not responding
            pass
    elif action == "modal":
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(hwnd, "Synthetic fixture error", "Overgrind fixture", 0x10)
        else:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "Overgrind fixture", "Synthetic fixture error")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="synthetic_app")
    ap.add_argument("--crash-after-ms", type=int)
    ap.add_argument("--hang-after-ms", type=int)
    ap.add_argument("--modal-after-ms", type=int)
    ap.add_argument("--quit-after-ms", type=int)
    ap.add_argument("--crash-mode", choices=("exit", "segv"), default="exit")
    ap.add_argument("--bug", action="append", default=[])
    ap.add_argument("--title", default="Overgrind Synthetic App")
    return ap


def main(argv: list[str] | None = None) -> int:
    args, qt_args = build_parser().parse_known_args(argv)
    spec = ";".join(args.bug + ([os.environ["OVERGRIND_BUG"]] if os.environ.get("OVERGRIND_BUG") else []))
    bugs = parse_bugs(spec)

    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QGridLayout, QLabel, QLineEdit, QPushButton, QSlider,
        QSpinBox, QWidget,
    )

    app = QApplication([sys.argv[0], *qt_args])
    win = QWidget()
    win.setWindowTitle(args.title)
    win.setObjectName("main_window")
    lay = QGridLayout(win)

    def name(w: Any, oid: str, label: str) -> Any:
        w.setObjectName(oid)
        w.setAccessibleName(label)
        if hasattr(w, "setAccessibleIdentifier"):      # Qt >= 6.8 maps this to UIA AutomationId
            w.setAccessibleIdentifier(oid)
        return w

    boxes = {k: name(QCheckBox(f"Option {k.upper()}"), f"cb_{k}", f"Option {k.upper()}") for k in "abcd"}
    for i, k in enumerate("abcd"):
        lay.addWidget(boxes[k], i, 0)
    combo = name(QComboBox(), "cmb_mode", "Mode")
    combo.addItems(["Off", "Low", "High"])
    vol = name(QSlider(Qt.Horizontal), "sld_volume", "Volume")
    vol.setRange(0, 100)
    vol.setValue(50)
    gain = name(QSlider(Qt.Horizontal), "sld_gain", "Gain")
    gain.setRange(0, 10)
    gain.setValue(1)
    count = name(QSpinBox(), "spn_count", "Count")
    count.setRange(0, 50)
    edit = name(QLineEdit(), "edt_name", "Name")
    edit.setMaxLength(20)
    status = name(QLabel("ready"), "lbl_status", "Status")
    for r, w in enumerate((combo, vol, gain, count, edit), start=4):
        lay.addWidget(w, r, 0)
    buttons = {}
    for r, (oid, label) in enumerate((("btn_apply", "Apply"), ("btn_crash", "Crash now"),
                                      ("btn_hang", "Hang now"), ("btn_modal", "Show error"),
                                      ("btn_delete", "Delete all data")), start=9):
        buttons[oid] = name(QPushButton(label), oid, label)
        lay.addWidget(buttons[oid], r, 0)
    lay.addWidget(status, 14, 0)

    def fire(action: str) -> None:
        status.setText(f"fault: {action}")
        QTimer.singleShot(30, lambda: do_action(action, args.crash_mode, int(win.winId())))

    def mutex(changed: str, other: str) -> None:
        if boxes[changed].isChecked() and boxes[other].isChecked():
            boxes[other].setChecked(False)

    fired = {"v": False}

    def check() -> None:
        if fired["v"]:
            return
        bug = evaluate_bugs(bugs, {k: b.isChecked() for k, b in boxes.items()},
                            {"volume": vol.value(), "gain": gain.value(), "count": count.value()})
        if bug:
            fired["v"] = True
            fire(bug["action"])

    boxes["c"].toggled.connect(lambda _=None: (mutex("c", "d"), check()))
    boxes["d"].toggled.connect(lambda _=None: (mutex("d", "c"), check()))
    for k in "ab":
        boxes[k].toggled.connect(lambda _=None: check())
    for s in (vol, gain):
        s.valueChanged.connect(lambda _=None: check())
    count.valueChanged.connect(lambda _=None: check())
    buttons["btn_apply"].clicked.connect(lambda: status.setText("applied"))
    buttons["btn_crash"].clicked.connect(lambda: fire("crash"))
    buttons["btn_hang"].clicked.connect(lambda: fire("hang"))
    buttons["btn_modal"].clicked.connect(lambda: fire("modal"))
    buttons["btn_delete"].clicked.connect(lambda: status.setText("deleted everything (decoy)"))

    for ms, act in ((args.crash_after_ms, "crash"), (args.hang_after_ms, "hang"), (args.modal_after_ms, "modal")):
        if ms is not None:
            QTimer.singleShot(ms, lambda a=act: fire(a))
    if args.quit_after_ms is not None:
        QTimer.singleShot(args.quit_after_ms, app.quit)

    win.show()
    print("READY", flush=True)
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
