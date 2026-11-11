import unittest

from overgrind.adapters.mock import MockAdapter, simple_generator
from overgrind.types import HarnessError, TargetAdapter
from test_runtime_common import make_project


def run_case(a, project, case):
    a.reset(project)
    feats = {f.id: f for f in project.features}
    ctrls = {c.id: c for c in project.controls}
    for fid, st in case.state.items():
        a.set_feature_state(feats[fid], st)
    for cid, v in case.values.items():
        a.set_control(ctrls[cid], v)
    a.wait_quiescent(10)
    return a.health()


class MockAdapterTests(unittest.TestCase):
    def setUp(self):
        self.p = make_project()

    def test_protocol_and_inspect_tree(self):
        a = MockAdapter()
        self.assertIsInstance(a, TargetAdapter)
        a.attach(self.p.target)
        els = a.inspect()
        self.assertGreaterEqual(len(els), 20)
        kinds = {e.suggested_kind for e in els}
        self.assertTrue({"boolean", "integer", "float", "choice", "text", "action"} <= kinds)
        self.assertEqual(els[0].parent, None)
        self.assertTrue(all(e.parent is None or e.parent < i for i, e in enumerate(els)))
        self.assertTrue(any(e.control_type == "Slider" and e.confidence == "verified" for e in els))

    def test_failures_are_deterministic_functions_of_state(self):
        gen = simple_generator(self.p)
        cases = [next(gen) for _ in range(300)]
        out = []
        for _ in range(2):
            a = MockAdapter(crash_rate=0.1, hang_rate=0.05, dialog_rate=0.05, seed=7)
            a.attach(self.p.target)
            row = []
            for c in cases:
                row.append(run_case(a, self.p, c).status)
                a.detach(kill=True)
                a.attach(self.p.target)
            out.append(row)
        self.assertEqual(out[0], out[1])
        self.assertTrue({"dead", "hung", "modal", "ok"} <= set(out[0]))
        other = MockAdapter(crash_rate=0.1, hang_rate=0.05, dialog_rate=0.05, seed=8)
        other.attach(self.p.target)
        row = []
        for c in cases:
            row.append(run_case(other, self.p, c).status)
            other.detach(kill=True)
            other.attach(self.p.target)
        self.assertNotEqual(row, out[0])

    def test_bad_combo_is_monotone_and_reset_requires_restart(self):
        a = MockAdapter(bad_combo={"cb_a": "on", "cb_b": "on"})
        a.attach(self.p.target)
        a.reset(self.p)
        f = {x.id: x for x in self.p.features}
        a.set_feature_state(f["cb_a"], "on")
        a.wait_quiescent(1)
        self.assertEqual(a.health().status, "ok")
        a.set_feature_state(f["cb_b"], "on")
        self.assertEqual(a.health().status, "dead")
        with self.assertRaises(HarnessError):
            a.set_feature_state(f["cb_g"], "on")
        a.detach(kill=True)
        a.attach(self.p.target)
        self.assertEqual(a.health().status, "ok")

    def test_harness_errors_and_dangerous_guard(self):
        a = MockAdapter(harness_rate=1.0)
        a.attach(self.p.target)
        with self.assertRaises(HarnessError):
            a.set_feature_state(self.p.features[0], "on")
        b = MockAdapter()
        b.attach(self.p.target)
        d = make_project(dangerous=True).features[-1]
        with self.assertRaises(HarnessError):
            b.invoke(d)

    def test_readback_and_reset(self):
        a = MockAdapter()
        a.attach(self.p.target)
        c = self.p.controls[0]
        a.set_control(c, 77)
        self.assertEqual(a.get_control(c), 77)
        a.reset(self.p)
        self.assertEqual(a.get_control(c), c.default)

    def test_simple_generator_checkpoint_roundtrip(self):
        g = simple_generator(self.p)
        first = [next(g) for _ in range(10)]
        ck = g.checkpoint()
        rest = [next(g) for _ in range(5)]
        g2 = simple_generator(self.p, checkpoint=ck)
        self.assertEqual([next(g2) for _ in range(5)], rest)
        self.assertEqual(first[0].seq, 1)


if __name__ == "__main__":
    unittest.main()
