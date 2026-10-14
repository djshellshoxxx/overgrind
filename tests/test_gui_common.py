"""Shared helpers for the offscreen GUI tests (not itself a test module)."""
import os
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SETTINGS = tempfile.mkdtemp(prefix="ovg-settings-")
os.environ["OVERGRIND_SETTINGS_DIR"] = _SETTINGS
os.environ["OVERGRIND_DATA"] = tempfile.mkdtemp(prefix="ovg-data-")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # PySide6 (or its system libs) missing
    HAVE_QT = False


def qapp():
    from overgrind.gui.app import make_app
    return make_app(["test"])


def pump(cond=None, timeout=10.0, step=0.01):
    """Process Qt events until cond() is true (or timeout). Returns cond() result."""
    app = QApplication.instance()
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond is not None and cond():
            return True
        time.sleep(step)
    app.processEvents()
    return bool(cond()) if cond is not None else True


class GuiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAVE_QT:
            raise unittest.SkipTest("PySide6 not available")
        cls.app = qapp()

    def setUp(self):
        # Never block on a modal prompt in tests: answer "Discard"/"Yes" and remember what was asked.
        from PySide6.QtWidgets import QMessageBox
        self._orig_q = QMessageBox.question
        self.asked = []

        def answer(parent, title, text, buttons=None, *a, **k):
            self.asked.append(title)
            B = QMessageBox.StandardButton
            for pick in (B.Discard, B.Yes):
                if buttons is None or buttons & pick:
                    return pick
            return B.Ok
        QMessageBox.question = staticmethod(answer)

    def tearDown(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        for w in QApplication.topLevelWidgets():
            try:
                w.close()
            except Exception:
                pass
        pump(timeout=0.05)
        QMessageBox.question = self._orig_q
