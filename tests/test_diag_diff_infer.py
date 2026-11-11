import unittest
from dataclasses import replace

from overgrind import diffrun as df
from overgrind import infer as inf
from overgrind import reduce as rd
from overgrind.types import (
    Action, AdapterSpec, Case, ConstraintDef, FeatureDef, Health, Project, TargetRef,
)

from test_diag_common import SPEC, FakeAdapter, make_project


def cases():
    return [Case(0, {"A": "on"}, {"gain": 0.2, "vol": 5}, ("A",)),
            Case(1, {"A": "off"}, {"gain": 0.9, "vol": 50}, ("A",)),
            Case(2, {"A": "on"}, {"gain": 0.5, "vol": 9}, ("A",))]


class TestDiff(unittest.TestCase):
    def run_d(self, a, b, **kw):
        return df.differential(make_project(), SPEC(**a), SPEC(**b), cases=cases(), **kw)

    def test_identical_clean(self):
        r = self.run_d({}, {})
        self.assertTrue(r.clean)
        self.assertEqual(r.compared, 3)

    def test_regression_and_fix(self):
        r = self.run_d({}, {"fail_on_gain": 0.8})
        self.assertEqual([i.case_seq for i in r.regressions], [1])
        self.assertEqual(r.fixes, ())
        r2 = self.run_d({"fail_on_gain": 0.8}, {})
        self.assertEqual([i.case_seq for i in r2.fixes], [1])
        self.assertEqual(r2.regressions, ())
        self.assertIn("Regressions", r.to_markdown())

    def test_value_mismatch_tolerance(self):
        self.assertTrue(self.run_d({}, {"offset": 0.001}, tolerance=0.01).clean)
        r = self.run_d({}, {"offset": 0.1}, tolerance=0.01)
        self.assertEqual({m.target for m in r.mismatches}, {"gain"})
        self.assertEqual(len(r.mismatches), 3)

    def test_incomparable_missing_control(self):
        r = self.run_d({}, {"missing": ["gain"]})
        self.assertEqual({i.target for i in r.incomparable}, {"gain"})
        self.assertIn("B", r.incomparable[0].detail)
        self.assertEqual(r.regressions, ())
        self.assertIn("Cannot compare", r.to_markdown())

    def test_id_map_none_and_rename(self):
        r = self.run_d({}, {}, id_map={"gain": None})
        self.assertTrue(all(i.target == "gain" for i in r.incomparable))
        # renamed control: B build only knows 'gain2'
        r = self.run_d({}, {"missing": ["gain"]}, id_map={"gain": "gain2"})
        self.assertEqual(r.incomparable, ())

    def test_timing(self):
        t = {"v": 0.0}
        clk = lambda: t["v"]

        class Slow(FakeAdapter):
            def wait_quiescent(self, ms):
                t["v"] += 1.0 if self.delay else 0.1
        import sys
        sys.modules[__name__].Slow = Slow
        r = df.differential(make_project(), AdapterSpec(f"{__name__}:Slow", {}),
                            AdapterSpec(f"{__name__}:Slow", {"delay": 1}), cases=cases(), clock=clk)
        self.assertEqual({m.kind for m in r.mismatches}, {"timing"})

    def test_default_generator_deterministic(self):
        p = make_project()
        a, b = df.make_cases(p, 5, 3), df.make_cases(p, 5, 3)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 5)
        r = df.differential(p, SPEC(), SPEC(), n_cases=8, seed=2)
        self.assertTrue(r.clean)


def P():
    return Project("p", TargetRef(), features=tuple(FeatureDef(i) for i in ("A", "B", "C")),
                   controls=make_project().controls)


def flags(**kw):
    return {k: {"enabled": v, "visible": True} for k, v in kw.items()}


class TestInfer(unittest.TestCase):
    def test_scope_verified_and_inferred(self):
        base = ({}, flags(vol=False, gain=True))
        on = ({"A": "on"}, flags(vol=True, gain=True))
        props = inf.infer_dependencies([base, on, on, on])
        scope = [p for p in props if p.kind == "scope"]
        self.assertEqual(len(scope), 1)
        self.assertEqual((scope[0].subject, scope[0].features, scope[0].confidence), ("vol", ("A",), "verified"))
        self.assertEqual(scope[0].evidence, 4)
        weak = inf.infer_dependencies([base, on])
        self.assertEqual(weak[0].confidence, "inferred")

    def test_contradiction_demotes(self):
        base = ({}, flags(vol=False))
        props = inf.infer_dependencies([base, ({"A": "on"}, flags(vol=True)), ({"A": "on"}, flags(vol=True)),
                                        ({"A": "on"}, flags(vol=True)), ({"A": "on"}, flags(vol=False))])
        self.assertEqual(props[0].confidence, "inferred")
        self.assertEqual(props[0].contradictions, 1)

    def test_no_dependency_no_proposal(self):
        base = ({}, flags(gain=True))
        self.assertEqual(inf.infer_dependencies([base, ({"A": "on"}, flags(gain=True))]), [])

    def test_requires_between_features(self):
        obs = [({}, flags(B=False)), ({"A": "on"}, flags(B=True))]
        p = inf.infer_dependencies(obs, P())
        self.assertEqual((p[0].kind, p[0].subject, p[0].features), ("requires", "B", ("A",)))
        out = p[0].apply(P())
        self.assertIn(ConstraintDef("requires", features=("B",), needs=("A",)), out.constraints)

    def test_mutex(self):
        s = lambda **kw: {k: {"enabled": True, "visible": True, "state": v} for k, v in kw.items()}
        obs = [({"A": "on"}, s(A="on")), ({"B": "on"}, s(B="on")),
               ({"A": "on", "B": "on"}, s(A="off", B="on"))] * 3
        p = [x for x in inf.infer_dependencies(obs) if x.kind == "mutex"]
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].features, ("A", "B"))
        self.assertEqual(p[0].confidence, "verified")
        out = p[0].apply(P())
        self.assertEqual(out.constraints, (ConstraintDef("mutex", features=("A", "B")),))
        self.assertIs(p[0].apply(out), out)  # idempotent

    def test_apply_is_immutable_and_scoped(self):
        proj = P()
        prop = inf.Proposal("scope", "vol", ("A",), "verified", 5)
        new = prop.apply(proj)
        self.assertEqual(next(c for c in proj.controls if c.id == "vol").scope, ())
        c = next(c for c in new.controls if c.id == "vol")
        self.assertEqual(c.scope, ("A",))
        self.assertEqual(c.provenance.source, "inferred")
        with self.assertRaises(ValueError):
            inf.Proposal("scope", "nope", ("A",), "inferred", 1).apply(proj)

    def test_gather_restores_defaults(self):
        calls = []

        class Ad(FakeAdapter):
            def reset(self, project):
                calls.append("reset")
                super().reset(project)
        ad = Ad()

        def probe(adapter, project):
            return flags(vol=adapter.st.get("A") == "on")
        obs = inf.gather_observations(ad, P(), probe, pairs=True)
        self.assertEqual(ad.st, {})
        self.assertEqual(calls[-1], "reset")
        self.assertEqual(obs[0][0], {})
        self.assertIn(({"A": "on"}, flags(vol=True)), obs)
        self.assertEqual([p.kind for p in inf.infer_dependencies(obs)], ["scope"])
        self.assertTrue(any(len(a) == 2 for a, _ in obs))


class TestShrinkMore(unittest.TestCase):
    def test_float_and_text(self):
        c = rd.shrink_candidates(0.8, 0.0)
        self.assertEqual(c[0], 0.0)
        self.assertIn(0.4, c)
        t = rd.shrink_candidates("abcdef", "")
        self.assertEqual(t[0], "")
        self.assertIn("abc", t)
        self.assertEqual(rd.shrink_candidates(True, False), [False])
        self.assertEqual(rd.shrink_candidates(0, 0), [])


if __name__ == "__main__":
    unittest.main()
