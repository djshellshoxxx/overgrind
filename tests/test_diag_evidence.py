import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from overgrind import evidence as ev
from overgrind.types import (
    Action, Case, ErrorEvent, Health, HarnessError, Outcome, TargetFailure,
)


def make_event(i="e1", sig="s1", **kw):
    d = dict(
        id=i, ts="2026-01-01T00:00:00Z", run_id="r", category="crash", signature=sig,
        message="boom", case=Case(3, {"A": "on"}, {"vol": 7, "pw": "hunter2"}, ("A",), 5, ("epoch:1",)),
        history=[Action("state", "A", "on", 1.5), Action("control", "pw", "hunter2", 2.0, True)],
        env={"os": "x"}, attachments={}, count=1)
    d.update(kw)
    return ErrorEvent(**d)


class TestClassify(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(ev.classify(TargetFailure("hang", "x")).category, "hang")
        self.assertEqual(ev.classify(TargetFailure("assertion", "x")).category, "assertion")
        o = ev.classify(HarnessError("elem"))
        self.assertEqual(o.category, "harness")
        self.assertFalse(ev.is_app_defect(o))
        self.assertEqual(ev.classify(Health("dead", "gone")).category, "crash")
        self.assertEqual(ev.classify(Health("hung")).category, "hang")
        self.assertEqual(ev.classify(Health("modal")).category, "dialog")
        self.assertTrue(ev.classify(Health("ok")).ok)
        self.assertEqual(ev.classify(Health("missing")).category, "harness")
        self.assertEqual(ev.classify(ValueError("x")).category, "harness")


class TestFingerprint(unittest.TestCase):
    def test_stable_across_volatile(self):
        a = ev.fingerprint("hang", "no response after 5012 ms pid 4021 at 0x7FF61234", "h")
        b = ev.fingerprint("hang", "no response after 6100 ms pid 77 at 0x1A2B3C4D", "h")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        c = ev.fingerprint("crash", r"fault in C:\Users\bob\AppData\Local\Temp\tmp12ab\x.dll at 12:00:01")
        d = ev.fingerprint("crash", r"fault in C:\Users\al\AppData\Local\Temp\tmp99zz\x.dll at 13:59:59")
        self.assertEqual(c, d)
        self.assertEqual(ev.fingerprint("crash", "2026-01-01T10:00:00Z err"),
                         ev.fingerprint("crash", "2027-05-05T01:02:03Z err"))

    def test_different_faults_differ(self):
        base = dict(category="crash", message="fault")
        self.assertNotEqual(ev.fingerprint(**base, module="a.dll"), ev.fingerprint(**base, module="b.dll"))
        self.assertNotEqual(ev.fingerprint(**base, exc_type="ValueError"), ev.fingerprint(**base, exc_type="KeyError"))
        self.assertNotEqual(ev.fingerprint("crash", "x"), ev.fingerprint("hang", "x"))
        self.assertNotEqual(ev.fingerprint("crash", "x", stack=["f1", "g"]), ev.fingerprint("crash", "x", stack=["f2", "g"]))
        self.assertNotEqual(ev.fingerprint("crash", "x", hint="A"), ev.fingerprint("crash", "x", hint="B"))


class TestRing(unittest.TestCase):
    def test_bounded(self):
        r = ev.RingBuffer(3)
        for i in range(10):
            r.push(Action("wait", str(i)))
        self.assertEqual([a.target for a in r.snapshot()], ["7", "8", "9"])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_roundtrip_exact(self):
        s = ev.EvidenceStore(self.dir)
        e = make_event(history=[Action("state", "A", "on", 1.5, True)],
                       case=Case(3, {"A": "on"}, {"vol": 7}, ("A",), 5, ("epoch:1",)),
                       attachments={}, reliability="flaky", reduced={"x": [1, 2]})
        s.write(e)
        self.assertEqual(s.load("e1"), e)
        self.assertEqual(s.list(), [e])
        self.assertEqual(ev.event_from_dict(json.loads(json.dumps(ev.event_to_dict(e)))), e)

    def test_dedup_counts_and_cap(self):
        s = ev.EvidenceStore(self.dir, max_incidents=5)
        for i in range(12):
            s.write(make_event(f"e{i}", "sigA", ts=f"2026-01-01T00:00:{i:02d}Z"))
        s.write(make_event("o1", "sigB"))
        files = list((self.dir / "errors" / "sigA").glob("*.json"))
        self.assertEqual(len(files), 5)
        self.assertEqual(s.counts(), {"sigA": 12, "sigB": 1})
        self.assertEqual(len(s.list()), 6)
        self.assertEqual(max(e.count for e in s.list() if e.signature == "sigA"), 12)
        s.bump("sigB")
        self.assertEqual(s.counts()["sigB"], 2)
        with self.assertRaises(KeyError):
            s.bump("nope")

    def test_no_files_without_errors_and_harness_refused(self):
        s = ev.EvidenceStore(self.dir)
        self.assertEqual(list(self.dir.rglob("*")), [])
        with self.assertRaises(ValueError):
            s.write(make_event(category="harness"))
        self.assertEqual(list(self.dir.rglob("*.json")), [])

    def test_redaction(self):
        s = ev.EvidenceStore(self.dir, sensitive={"secretbox"})
        e = make_event(message="login failed password=hunter2 for user",
                       case=Case(1, {}, {"pw": "hunter2", "secretbox": "abc", "vol": 5}),
                       history=[Action("control", "secretbox", "abc"), Action("control", "vol", 5)])
        path = s.write(e)
        raw = Path(path).read_text()
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("abc", raw)
        got = s.load("e1")
        self.assertEqual(got.case.values, {"pw": "***", "secretbox": "***", "vol": 5})
        self.assertEqual(got.history[0].value, "***")
        self.assertEqual(got.history[1].value, 5)
        self.assertEqual(e.case.values["pw"], "hunter2")  # input untouched

    def test_atomic_write_crash(self):
        s = ev.EvidenceStore(self.dir)
        s.write(make_event("e1", "S"))
        before = s.index_path.read_text()
        with mock.patch.object(ev.os, "replace", side_effect=OSError("power loss")):
            with self.assertRaises(OSError):
                s.write(make_event("e2", "S"))
        self.assertEqual(s.index_path.read_text(), before)
        self.assertFalse(list(self.dir.rglob("*.tmp")))
        self.assertEqual([e.id for e in s.list()], ["e1"])
        # a stray temp file from a hard kill is ignored by list()
        (self.dir / "errors" / "S" / "e9.json.123.tmp").write_text("{trunc")
        self.assertEqual([e.id for e in s.list()], ["e1"])

    def test_retention_deletes_attachments_first(self):
        src = tempfile.TemporaryDirectory()
        self.addCleanup(src.cleanup)
        s = ev.EvidenceStore(self.dir / "st", max_bytes=4000)
        big = Path(src.name) / "shot.png"
        big.write_bytes(b"x" * 6000)
        s.write(make_event("e1", "S"), {"screenshot": str(big)})
        got = s.load("e1")  # incident kept, attachment trimmed
        self.assertEqual(got.attachments, {})
        self.assertLessEqual(s.total_bytes(), 4000)
        small = ev.EvidenceStore(self.dir / "k", max_bytes=10**9)
        small.write(make_event("e2", "T"), {"screenshot": str(big)})
        self.assertEqual(list(small.load("e2").attachments), ["screenshot"])

    def test_env_capture(self):
        exe = self.dir / "a.exe"
        exe.write_bytes(b"MZ123")
        e = ev.capture_env(str(exe))
        self.assertEqual(len(e["exe_sha256"]), 64)
        self.assertIn("python", e)
        self.assertEqual(ev.capture_env()["os"], ev.capture_env()["os"])


if __name__ == "__main__":
    unittest.main()
