"""QA-BACKEND regression tests for evidence / reduce / diffrun (written before the fixes)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from overgrind import evidence as ev
from overgrind import reduce as rd
from overgrind.types import Action, Case, ErrorEvent, Outcome

from test_diag_common import make_project


def mk_event(eid="e1", sig="S1", message="boom", case=None, history=(), category="crash", **kw):
    return ErrorEvent(id=eid, ts="2026-01-01T00:00:00+00:00", run_id="r", category=category, signature=sig,
                      message=message, case=case or Case(1, {"A": "on"}, {}), history=list(history),
                      env={"os": "t"}, **kw)


class EvidenceRobustness(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="og-qa-"))

    def test_sensitive_value_in_message_and_env_is_scrubbed(self):
        s = ev.EvidenceStore(self.dir, sensitive={"field7"})
        e = mk_event(message="Access violation while parsing 'Zq9-secret-Zq9' in field",
                     case=Case(1, {}, {"field7": "Zq9-secret-Zq9", "vol": 5}),
                     history=[Action("control", "field7", "Zq9-secret-Zq9"), Action("control", "vol", 5)])
        e.env["note"] = "typed Zq9-secret-Zq9"
        s.write(e)
        blob = "".join(p.read_text(encoding="utf-8") for p in self.dir.rglob("*.json"))
        self.assertNotIn("Zq9-secret-Zq9", blob)
        got = s.load("e1")
        self.assertEqual(got.case.values["vol"], 5)
        self.assertEqual(got.case.values["field7"], "***")

    def test_short_sensitive_values_do_not_mangle_message(self):
        s = ev.EvidenceStore(self.dir, sensitive={"field7"})
        e = mk_event(message="crash in module a", case=Case(1, {}, {"field7": "a"}),
                     history=[Action("control", "field7", "a")])
        s.write(e)
        self.assertEqual(s.load("e1").message, "crash in module a")     # 1-char secrets are too ambiguous to scrub

    def test_reduced_json_scrubs_sensitive(self):
        s = ev.EvidenceStore(self.dir, sensitive={"field7"})
        e = mk_event()
        s.write(e)
        red = {"steps": [{"kind": "control", "target": "field7", "value": "Zq9-secret-Zq9"}],
               "values": {"field7": "Zq9-secret-Zq9"}, "message": "bad Zq9-secret-Zq9"}
        path = s.write_reduced(e, red)
        self.assertNotIn("Zq9-secret-Zq9", Path(path).read_text(encoding="utf-8"))

    def test_replace_retries_on_windows_sharing_violation(self):
        real = os.replace
        calls = []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "sharing violation")
            return real(a, b)
        with mock.patch.object(ev.os, "replace", side_effect=flaky):
            ev.atomic_write_text(self.dir / "x.json", "{}")
        self.assertEqual(len(calls), 3)
        self.assertEqual((self.dir / "x.json").read_text(encoding="utf-8"), "{}")
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def test_incident_without_index_entry_is_not_duplicated_after_a_crash(self):
        """Hard kill between the incident file and index.json: the next occurrence must only bump."""
        s = ev.EvidenceStore(self.dir)
        s.write(mk_event("e1"))
        s.index_path.unlink()                       # simulate the lost/never-written index
        self.assertTrue(s.known("S1"))
        self.assertEqual(s.bump("S1"), 2)
        s.write(mk_event("e2"))
        self.assertEqual(len([e for e in s.list()]), 2)
        self.assertEqual(s.counts()["S1"], 3)

    def test_non_json_values_do_not_break_the_write(self):
        s = ev.EvidenceStore(self.dir)
        e = mk_event(history=[Action("control", "p", Path("/x"))])
        e.env["obj"] = object()
        s.write(e)                                  # used to raise TypeError -> 'evidence write failed'
        self.assertEqual(len(s.list()), 1)


class ShrinkEdges(unittest.TestCase):
    def test_huge_integers_shrink_without_overflow(self):
        c = rd.shrink_candidates(10 ** 400, 0)
        self.assertTrue(c)
        self.assertTrue(all(isinstance(x, int) for x in c))
        self.assertIn(0, c)

    def test_big_ints_converge_to_exact_halving(self):
        c = rd.shrink_candidates(2 ** 60 + 1, 0)
        self.assertIn(1, c)
        self.assertEqual(len(set(c)), len(c))


class ReliabilityPolicy(unittest.TestCase):
    """Spec U3 3.1: 'reliable' needs n>=3, 'not_reproducible' needs n>=5, otherwise 'unconfirmed'."""

    def test_labels(self):
        cr = rd.classify_reliability
        self.assertEqual(cr([True] * 3), "reliable")
        self.assertEqual(cr([True] * 5), "reliable")
        self.assertEqual(cr([True, True]), "unconfirmed")
        self.assertEqual(cr([False] * 5), "not_reproducible")
        self.assertEqual(cr([False] * 4), "unconfirmed")
        self.assertEqual(cr([True, False, True]), "flaky")
        self.assertEqual(cr([]), "unconfirmed")

    def test_binomial_bound(self):
        self.assertAlmostEqual(rd.reproduce_lower_bound(5, 5), 0.549, places=3)
        self.assertAlmostEqual(rd.reproduce_lower_bound(3, 3), 0.368, places=3)
        self.assertAlmostEqual(rd.reproduce_upper_bound(0, 5), 0.451, places=3)

    def test_reducer_extends_replays_on_misses_and_flakiness(self):
        calls = []

        def never(acts):
            calls.append(1)
            return Outcome()
        p = make_project()
        out = rd.Reducer(p, runner=never, confirm_runs=3).reduce(mk_event(history=[Action("state", "A", "on")]))
        self.assertEqual(len(calls), 5)                           # 3 planned, extended to 5 before giving up
        self.assertEqual(out.reliability, "not_reproducible")

        n = []

        def flaky(acts):
            n.append(1)
            return Outcome(False, "crash", "boom") if len(n) % 2 == 0 else Outcome()
        rd.Reducer(p, runner=flaky, confirm_runs=3, max_replays=40).reduce(
            mk_event(sig=ev.outcome_fingerprint(Outcome(False, "crash", "boom")),
                     history=[Action("state", "A", "on")]))
        self.assertGreaterEqual(len(n), 10)                        # mixed results pin p_hat with n=10

    def test_reducer_default_runs_at_least_three(self):
        calls = []

        def always(acts):
            calls.append(1)
            return Outcome(False, "crash", "boom 0x1234")
        p = make_project()
        out = rd.Reducer(p, runner=always, confirm_runs=2, max_replays=3).reduce(
            mk_event(sig=ev.outcome_fingerprint(Outcome(False, "crash", "boom 0x1234")),
                     history=[Action("state", "A", "on")]))
        self.assertEqual(out.reduced["confirm"]["runs"], 3)


class PrefixCut(unittest.TestCase):
    def test_history_before_the_last_reset_is_dropped(self):
        """The ring holds many earlier cases; only the failing case (since its reset) matters."""
        hist = []
        for i in range(30):
            hist += [Action("reset", "*"), Action("state", "A", "on"), Action("control", "vol", i),
                     Action("wait", "quiesce")]
        hist += [Action("reset", "*"), Action("state", "B", "on"), Action("invoke", "bug"),
                 Action("wait", "quiesce")]
        calls = []

        def runner(acts):
            calls.append(len(acts))
            return Outcome(False, "crash", "boom 0x1234") if any(a.target == "bug" for a in acts) else Outcome()
        sig = ev.outcome_fingerprint(Outcome(False, "crash", "boom 0x1234"))
        out = rd.Reducer(make_project(), runner=runner).reduce(mk_event(sig=sig, history=hist))
        self.assertEqual([s["target"] for s in out.reduced["steps"]], ["bug"])
        self.assertLessEqual(max(calls), 5)                       # never replays the 120-action ring
        self.assertEqual(out.reduced["original_steps"], len(hist))
        self.assertFalse(out.reduced.get("assumes_unknown_prestate"))

    def test_no_reset_marks_unknown_prestate(self):
        hist = [Action("state", "A", "on"), Action("invoke", "bug")]
        sig = ev.outcome_fingerprint(Outcome(False, "crash", "boom 0x1234"))
        out = rd.Reducer(make_project(), runner=lambda a: Outcome(False, "crash", "boom 0x1234")
                         if any(x.target == "bug" for x in a) else Outcome()).reduce(mk_event(sig=sig, history=hist))
        self.assertTrue(out.reduced.get("assumes_unknown_prestate"))


class ScopeSemantics(unittest.TestCase):
    def test_diff_scope_uses_first_state_as_off(self):
        from overgrind import diffrun
        from overgrind.types import FeatureDef, Selector
        f = FeatureDef("mode", kind="multistate", states=("Low", "High"), default_state="Low",
                       selector=Selector(auto_id="m"))
        self.assertFalse(diffrun._active({"mode": "Low"}, "mode", {"mode": f}))
        self.assertTrue(diffrun._active({"mode": "High"}, "mode", {"mode": f}))


if __name__ == "__main__":
    unittest.main()


class ThresholdShrink(unittest.TestCase):
    def test_crash_iff_x_ge_37_shrinks_to_37(self):
        from overgrind.types import ControlDef, Domain, FeatureDef, Project, Selector, TargetRef
        pr = Project("p", TargetRef(), (FeatureDef("A"),),
                     (ControlDef("x", "integer", Selector(auto_id="x"), Domain("integer", 0, 100), default=10),))
        n = []

        def r(acts):
            n.append(1)
            v = [a.value for a in acts if a.kind == "control"]
            return Outcome(False, "crash", "boom") if v and v[0] >= 37 else Outcome()
        sig = ev.outcome_fingerprint(Outcome(False, "crash", "boom"))
        e = mk_event(sig=sig, history=[Action("reset", "*"), Action("control", "x", 91)])
        out = rd.Reducer(pr, runner=r).reduce(e)
        self.assertEqual(out.reduced["values"], {"x": 37})
        self.assertLessEqual(len(n), 25)
