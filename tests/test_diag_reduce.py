import copy
import json
import tempfile
import unittest
from pathlib import Path

from overgrind import evidence as ev
from overgrind import reduce as rd
from overgrind.types import Action, Case, ErrorEvent, Outcome

from test_diag_common import SPEC, make_project


def crash(msg="boom 0x1234"):
    return Outcome(False, "crash", msg)


def event_with(history, sig=None, **kw):
    o = crash()
    d = dict(id="e1", ts="2026-01-01T00:00:00Z", run_id="r", category="crash",
             signature=sig or ev.outcome_fingerprint(o), message="boom 0x1234",
             case=Case(0, {}, {}), history=history, env={"os": "t"})
    d.update(kw)
    return ErrorEvent(**d)


class TestReliability(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(rd.classify_reliability([True, True]), "reliable")
        self.assertEqual(rd.classify_reliability([True, False, True]), "flaky")
        self.assertEqual(rd.classify_reliability([False, False]), "not_reproducible")
        self.assertEqual(rd.classify_reliability([]), "unconfirmed")
        self.assertEqual(rd.classify_reliability([Outcome(), crash()]), "flaky")


class TestDdmin(unittest.TestCase):
    def test_single_culprit_in_40(self):
        hist = [Action("control", "vol", i) for i in range(40)]
        hist[27] = Action("invoke", "bug")
        calls = []

        def runner(acts):
            calls.append(len(acts))
            return crash() if any(a.target == "bug" for a in acts) else Outcome()
        out = rd.Reducer(make_project(), runner=runner).reduce(event_with(hist))
        self.assertEqual([s["target"] for s in out.reduced["steps"]], ["bug"])
        self.assertEqual(out.reliability, "reliable")
        self.assertLess(len(calls), 60)

    def test_two_step_interaction(self):
        hist = [Action("control", "gain", 0.5)] * 10 + [Action("state", "A", "on")] + \
               [Action("wait", "x", 1)] * 12 + [Action("state", "B", "on")] + [Action("wait", "y", 2)] * 15

        def runner(acts):
            idx = [(a.kind, a.target) for a in acts if a.kind == "state"]
            idx = [(k, tg) for (k, tg), a in zip(idx, [a for a in acts if a.kind == "state"]) if a.value == "on"]
            ok = ("state", "A") in idx and ("state", "B") in idx and idx.index(("state", "A")) < idx.index(("state", "B"))
            return crash() if ok else Outcome()
        out = rd.Reducer(make_project(), runner=runner).reduce(event_with(hist))
        self.assertEqual([(s["target"], s["value"]) for s in out.reduced["steps"]], [("A", "on"), ("B", "on")])

    def test_not_reproducible_skips_reduction(self):
        out = rd.Reducer(make_project(), runner=lambda a: Outcome()).reduce(event_with([Action("invoke", "bug")] * 5))
        self.assertEqual(out.reliability, "not_reproducible")
        self.assertEqual(out.reduced["reduced_steps"], 5)

    def test_flaky(self):
        n = {"i": 0}

        def runner(acts):
            n["i"] += 1
            return crash() if n["i"] % 2 else Outcome()
        r = rd.Reducer(make_project(), runner=runner, confirm_runs=4)
        out = r.reduce(event_with([Action("invoke", "bug")]))
        self.assertEqual(out.reliability, "flaky")
        self.assertEqual(out.reduced["reliability_pct"], 50)

    def test_different_failure_not_accepted(self):
        # removing the 'A' step turns the crash into a *different* fault: must keep A
        def runner(acts):
            t = [a.target for a in acts]
            if "bug" in t and "A" in t:
                return crash("boom 0x1")
            if "bug" in t:
                return Outcome(False, "crash", "different module fault")
            return Outcome()
        out = rd.Reducer(make_project(), runner=runner).reduce(
            event_with([Action("state", "A", "on"), Action("invoke", "bug"), Action("wait", "w", 1)]))
        self.assertEqual([s["target"] for s in out.reduced["steps"]], ["A", "bug"])

    def test_harness_errors_never_match(self):
        r = lambda a: Outcome(False, "harness", "elem missing")
        out = rd.Reducer(make_project(), runner=r).reduce(event_with([Action("invoke", "bug")]))
        self.assertEqual(out.reliability, "not_reproducible")

    def test_budget(self):
        hist = [Action("control", "vol", i) for i in range(40)]
        hist[3] = Action("invoke", "bug")
        r = rd.Reducer(make_project(), max_replays=5,
                       runner=lambda a: crash() if any(x.target == "bug" for x in a) else Outcome())
        out = r.reduce(event_with(hist))
        self.assertTrue(out.reduced["budget_exhausted"])
        self.assertLessEqual(r.replays, 5)
        self.assertTrue(any(s["target"] == "bug" for s in out.reduced["steps"]))


class TestShrink(unittest.TestCase):
    def test_candidates(self):
        c = rd.shrink_candidates(100, 0)
        self.assertEqual(c[0], 0)
        self.assertIn(50, c)
        self.assertEqual(rd.shrink_candidates("hello", "")[0:2], ["", "h"][0:1] + ["h"])
        self.assertNotIn(5, rd.shrink_candidates(5, 0))

    def test_value_and_state_shrink(self):
        hist = [Action("state", "A", "on"), Action("state", "C", "on"),
                Action("control", "vol", 87), Action("control", "name", "hello world")]

        def runner(acts):  # crash needs vol >= 10 only
            for a in acts:
                if a.kind == "control" and a.target == "vol" and a.value >= 10:
                    return crash()
            return Outcome()
        out = rd.Reducer(make_project(), runner=runner).reduce(event_with(hist))
        self.assertEqual(out.reduced["steps"], [{"kind": "control", "target": "vol", "value": 10}])

    def test_value_goes_to_default(self):
        hist = [Action("state", "A", "on"), Action("control", "vol", 40)]
        runner = lambda acts: crash() if any(a.target == "vol" for a in acts) else Outcome()
        out = rd.Reducer(make_project(), runner=runner).reduce(event_with(hist))
        self.assertEqual(out.reduced["values"], {"vol": 0})  # default reached
        self.assertEqual(out.reduced["state"], {})


class TestOriginalUntouched(unittest.TestCase):
    def test_no_mutation_and_reduced_file(self):
        import overgrind.evidence as E
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = E.EvidenceStore(tmp.name)
        hist = [Action("control", "vol", 50), Action("invoke", "bug")]
        e = event_with(hist)
        path = Path(store.write(e))
        raw = path.read_text()
        snap = copy.deepcopy(e)
        out = rd.Reducer(make_project(), store=store,
                         runner=lambda a: crash() if any(x.target == "bug" for x in a) else Outcome()).reduce(e)
        self.assertEqual(e, snap)
        self.assertIsNot(out, e)
        self.assertIsNone(e.reduced)
        self.assertEqual(path.read_text(), raw)
        red = path.with_suffix("") / "reduced.json"
        self.assertTrue(red.is_file())
        self.assertEqual(json.loads(red.read_text())["reduced_steps"], 1)
        loaded = store.load("e1")
        self.assertEqual(loaded.reduced["reduced_steps"], 1)
        self.assertEqual(loaded.reliability, "reliable")


class TestExport(unittest.TestCase):
    def test_recipe(self):
        hist = [Action("control", "vol", 50), Action("invoke", "bug")]
        out = rd.Reducer(make_project(), runner=lambda a: crash() if any(x.target == "bug" for x in a) else Outcome()).reduce(
            event_with(hist))
        md, data = rd.export_recipe(out)
        self.assertIn("Invoke `bug`", md)
        self.assertIn("100%", md)
        self.assertIn(out.signature, md)
        self.assertEqual(data["reliability"], "reliable")
        self.assertEqual(len(data["steps"]), 1)
        json.dumps(data)
        md2, d2 = rd.export_recipe(event_with(hist))  # unreduced still works
        self.assertEqual(len(d2["steps"]), 2)


class TestDefaultRunner(unittest.TestCase):
    def test_adapter_replay_seq(self):
        p = make_project()
        hist = [Action("control", "vol", 3), Action("state", "C", "on"), Action("state", "A", "on"),
                Action("control", "name", "zz"), Action("state", "B", "on"), Action("wait", "", 5)]
        o = ev.classify(__import__("overgrind.types", fromlist=["Health"]).Health("dead", "access violation at 0x7ff61234 in pid 5012"))
        e = event_with(hist, sig=ev.outcome_fingerprint(o))
        r = rd.Reducer(p, SPEC(rule="seq"))
        out = r.reduce(e)
        r.close()
        self.assertEqual(out.reliability, "reliable")
        self.assertEqual([(s["target"], s["value"]) for s in out.reduced["steps"]], [("A", "on"), ("B", "on")])

    def test_actions_from_case(self):
        acts = rd.actions_from_case(make_project(), Case(0, {"A": "on"}, {"vol": 2}, ("A",)))
        self.assertEqual([a.kind for a in acts], ["reset", "state", "control"])


if __name__ == "__main__":
    unittest.main()
