import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from overgrind import project as pj
from overgrind import types as T
from overgrind.types import (ConstraintDef, ControlDef, Domain, FeatureDef, Project,
                             Provenance, RunConfig, Selector, StopConditions, TargetRef)

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo.json"


def good() -> Project:
    return Project(
        name="p", uuid="u-1",
        target=TargetRef(kind="exe", exe_path="a.exe", args=("-x", "y"), title_regex="A.*"),
        features=(
            FeatureDef("filter", "Filter", selector=Selector(auto_id="filterEnabled", control_type="CheckBox"),
                       provenance=Provenance("uia", "verified", "n")),
            FeatureDef("mode", kind="multistate", states=("Off", "Low", "High"), default_state="Off",
                       selector=Selector(title="Mode", control_type="Tab"),
                       state_selectors={"Low": Selector(auto_id="low"), "High": Selector(path=("w", "m"), control_type="Radio")}),
            FeatureDef("go", kind="action", states=(), default_state="", selector=Selector(auto_id="goBtn")),
        ),
        controls=(
            ControlDef("drive", "float", Selector(auto_id="driveSlider"), Domain("float", 0, 100),
                       fuzz_domain=Domain("float", 10, 50), default=20.5, scope=("filter",)),
            ControlDef("stages", "integer", Selector(auto_id="stagesSpin"), Domain("integer", 1, 16, 1), default=4),
            ControlDef("os", "choice", Selector(auto_id="osCombo"), Domain("choice", options=("a", "b")), default="a"),
            ControlDef("by", "boolean", Selector(auto_id="byCheck"), Domain("boolean"), default=False, sensitive=True),
            ControlDef("nm", "text", Selector(auto_id="nameEdit"), Domain("text", max_length=20, alphabet="abc"), default="ab"),
        ),
        constraints=(ConstraintDef("mutex", features=("filter", "mode")),
                     ConstraintDef("requires", features=("mode",), needs=("filter",)),
                     ConstraintDef("forbid", assignment={"filter": "on", "mode": "High"})),
        run=RunConfig(strategy="twise", strategy_options={"t": 2}, seed=9),
    )


def problems(p):
    return pj.validate(p)


class RoundTrip(unittest.TestCase):
    def test_project_roundtrip_and_json(self):
        p = good()
        d = pj.to_dict(p)
        self.assertEqual(d["schema_version"], 2)
        self.assertEqual(list(d)[:3], ["name", "target", "features"])
        self.assertIsInstance(d["target"]["args"], list)
        back = pj.from_dict(Project, json.loads(json.dumps(d)))
        self.assertEqual(back, p)
        self.assertIsInstance(back.target.args, tuple)
        self.assertEqual(pj.project_from_dict(pj.project_to_dict(p)), p)

    def test_every_dataclass_roundtrips(self):
        case = T.Case(3, {"a": "on"}, {"c": 1.5}, ("a",), 5, ("epoch:1",))
        act = T.Action("state", "a", "on", 1.5, True)
        samples = [
            Selector("a", "t", "Button", "cls", ("w", "x"), 2), Provenance("uia", "verified", "n"),
            Domain("integer", 1, 5, 1, (), None, ""), good().features[0], good().controls[0],
            good().constraints[2], StopConditions(max_seconds=3.0, first_error=True), RunConfig(),
            TargetRef("exe", "x", ("a",), "n", 4, "r"), good(), case, act, T.Health("hung", "d"),
            T.ElementInfo(Selector(auto_id="a"), "n", "Edit", 1, 0, (1, 2, 3, 4), "v", ("Value",), "text",
                          Domain("text", max_length=3), "inferred"),
            T.Outcome(False, "crash", "m", {"k": [1]}),
            T.ErrorEvent("e", "ts", "r", "crash", "sig", "m", case, [act], {"os": "w"}, {"s": "p"}, 2, "flaky", {"x": 1}),
            T.Coverage(4, 2, 1, 2, 10, 3, 5, 2, "g"),
            T.RunStats("running", 1.5, 3, 2.0, 1, 1, 1, 0, 0, 0, T.Coverage(), "cur", (act,), ""),
            T.AdapterSpec("a.b:C", {"x": 1}),
        ]
        for obj in samples:
            blob = json.loads(json.dumps(pj.to_dict(obj)))
            self.assertEqual(pj.from_dict(type(obj), blob), obj, type(obj).__name__)

    def test_unknown_keys_and_types_rejected(self):
        d = pj.to_dict(good())
        for mutate in (
            lambda x: x.update(bogus=1),
            lambda x: x["target"].update(extra=1),
            lambda x: x["features"][0]["selector"].update(zzz=1),
            lambda x: x.update(schema_version=3),
            lambda x: x.update(name=5),
            lambda x: x["controls"][0]["domain"].update(minimum=True),
            lambda x: x["target"].update(args="notalist"),
            lambda x: x.pop("features"),
        ):
            bad = json.loads(json.dumps(d))
            mutate(bad)
            with self.assertRaises(ValueError):
                pj.from_dict(Project, bad)

    def test_hash(self):
        p = good()
        h = pj.project_hash(p)
        self.assertEqual(len(h), 64)
        self.assertEqual(h, pj.project_hash(dataclasses.replace(p, name="other", uuid="zz")))
        self.assertNotEqual(h, pj.project_hash(dataclasses.replace(p, run=RunConfig(seed=2))))

    def test_save_load_atomic(self):
        p = good()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "proj.json"
            pj.save(p, path)
            self.assertEqual(pj.load(path), p)
            pj.save(dataclasses.replace(p, name="v2"), path)
            self.assertEqual(pj.load(path).name, "v2")
            self.assertEqual([f.name for f in path.parent.iterdir()], ["proj.json"])
            self.assertIn("\n  ", path.read_text(encoding="utf-8"))
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"schema_version": 7}', encoding="utf-8")
            with self.assertRaises(ValueError):
                pj.load(bad)

    def test_save_utf8(self):
        p = dataclasses.replace(good(), name="Prüfung ✓")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "u.json"
            pj.save(p, path)
            self.assertIn("Prüfung ✓", path.read_text(encoding="utf-8"))
            self.assertEqual(pj.load(path).name, "Prüfung ✓")


class Migration(unittest.TestCase):
    def test_demo(self):
        d = json.loads(DEMO.read_text(encoding="utf-8"))
        p = pj.migrate_v1(d)
        self.assertEqual(p.target.title_regex, "Demo Audio App.*")
        self.assertEqual([f.id for f in p.features], ["filter", "delay", "reverb"])
        self.assertTrue(all(f.kind == "toggle" and f.states == ("off", "on") for f in p.features))
        self.assertEqual(p.features[0].selector, Selector(auto_id="filterEnabled", control_type="CheckBox"))
        self.assertEqual(p.features[0].provenance.source, "v1-import")
        c = {c.id: c for c in p.controls}
        self.assertEqual(c["drive"].domain, Domain("float", 0, 100))
        self.assertEqual(c["oversampling"].domain.options, ("Off", "2x", "4x", "8x"))
        self.assertEqual(c["preset_name"].domain.max_length, 20)
        self.assertEqual(c["preset_name"].provenance.source, "v1-import")
        self.assertEqual(p.schema_version, 2)
        self.assertEqual([e for e in pj.validate(p) if not e.startswith("warning:")], [])

    def test_invoke_and_requires(self):
        p = pj.migrate_v1({"title_regex": "x", "features": [
            {"id": "a", "mode": "invoke", "selector": {"title": "Go"}},
            {"id": "b", "mode": "enable", "selector": {"auto_id": "b"}}],
            "controls": [{"id": "c", "kind": "boolean", "selector": {"auto_id": "c"}, "requires_features": ["b"]}]})
        self.assertEqual(p.features[0].kind, "action")
        self.assertEqual(p.controls[0].scope, ("b",))
        self.assertEqual(pj.validate(p), [])

    def test_bad_v1(self):
        with self.assertRaises(ValueError):
            pj.migrate_v1({"title_regex": "x", "features": []})

    def test_load_auto_migrates(self):
        p = pj.load(DEMO)
        self.assertEqual(p.name, "demo")
        self.assertEqual(len(p.controls), 5)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "m.json"
            pj.save(p, out)
            self.assertEqual(pj.load(out), p)


class Validation(unittest.TestCase):
    def test_good_is_clean(self):
        self.assertEqual(problems(good()), [])

    def has(self, p, text):
        errs = problems(p)
        self.assertTrue(any(text in e for e in errs), (text, errs))
        return errs

    def with_feature(self, **kw):
        base = good()
        return dataclasses.replace(base, features=base.features + (FeatureDef(**kw),))

    def with_control(self, **kw):
        base = good()
        return dataclasses.replace(base, controls=base.controls + (ControlDef(**kw),))

    def test_duplicates(self):
        self.has(self.with_feature(id="filter"), "duplicate feature id")
        c = good().controls[0]
        self.has(dataclasses.replace(good(), controls=good().controls + (c,)), "duplicate control id")

    def test_feature_states(self):
        self.has(self.with_feature(id="t", states=("on",), default_state="on"), "at least 2 states")
        self.has(self.with_feature(id="m", kind="multistate", states=("a",), default_state="a"), "at least 2 states")
        self.has(self.with_feature(id="d", default_state="zzz"), "default_state")
        self.has(self.with_feature(id="k", kind="weird"), "unknown kind")
        self.has(self.with_feature(id="s", state_selectors={"nope": Selector(auto_id="x")}), "state_selectors")
        self.assertEqual([e for e in problems(self.with_feature(id="act", kind="action", states=(), default_state=""))], [])

    def test_constraints(self):
        b = good()
        def con(c):
            return dataclasses.replace(b, constraints=b.constraints + (c,))
        self.has(con(ConstraintDef("mutex", features=("filter", "ghost"))), "unknown feature 'ghost'")
        self.has(con(ConstraintDef("requires", features=("filter",), needs=("ghost",))), "unknown feature 'ghost'")
        self.has(con(ConstraintDef("forbid", assignment={"ghost": "on"})), "unknown feature 'ghost'")
        self.has(con(ConstraintDef("forbid", assignment={"filter": "maybe"})), "unknown state 'maybe'")
        self.has(con(ConstraintDef("forbid", assignment={"go": "on"})), "action")
        self.has(con(ConstraintDef("mutex", features=("filter",))), "at least 2")
        self.has(con(ConstraintDef("nope")), "unknown kind")

    def test_scope(self):
        self.has(self.with_control(id="z", kind="boolean", selector=Selector(auto_id="z"),
                                   domain=Domain("boolean"), scope=("ghost",)), "scope references unknown feature")

    def test_default_outside(self):
        mk = lambda dom, default, **kw: self.with_control(id="z", kind=dom.kind, selector=Selector(auto_id="z"),
                                                          domain=dom, default=default, **kw)
        self.has(mk(Domain("integer", 1, 5), 9), "outside the effective domain")
        self.has(mk(Domain("integer", 1, 5), 4, fuzz_domain=Domain("integer", 1, 3)), "outside the effective domain")
        self.has(mk(Domain("choice", options=("a",)), "q"), "outside the effective domain")
        self.assertEqual(problems(mk(Domain("integer", 0, 100, 10), 15)), [])   # off-grid default tolerated

    def test_fuzz_subset(self):
        mk = lambda dom, fz: self.with_control(id="z", kind=dom.kind, selector=Selector(auto_id="z"),
                                               domain=dom, fuzz_domain=fz)
        self.has(mk(Domain("integer", 1, 5), Domain("integer", 0, 5)), "not a subset")
        self.has(mk(Domain("integer", 1, 5), Domain("integer", 1, 9)), "not a subset")
        self.has(mk(Domain("choice", options=("a",)), Domain("choice", options=("a", "b"))), "not a subset")
        self.has(mk(Domain("text", max_length=4), Domain("text", max_length=9)), "not a subset")
        self.has(mk(Domain("text", max_length=4, alphabet="ab"), Domain("text", max_length=4, alphabet="abc")), "not a subset")
        self.has(mk(Domain("integer", 1, 5), Domain("float", 1, 5)), "kind")

    def test_domain_rules(self):
        mk = lambda dom: self.with_control(id="z", kind=dom.kind, selector=Selector(auto_id="z"), domain=dom)
        self.has(mk(Domain("text")), "max_length")
        self.has(mk(Domain("text", max_length=5000)), "max_length")
        self.has(mk(Domain("text", max_length=-1)), "max_length")
        self.has(mk(Domain("choice")), "at least one option")
        self.has(mk(Domain("integer", 5, 1)), "minimum 5 > maximum 1")
        self.has(mk(Domain("float", float("nan"), 1)), "finite")
        self.has(mk(Domain("float", 0, float("inf"))), "finite")
        self.has(mk(Domain("float", 0, 1, 0)), "step")
        self.has(mk(Domain("float", 0, 1, -1)), "step")
        self.has(mk(Domain("integer", None, 5)), "minimum is required")
        self.has(mk(Domain("blob")), "unknown domain kind")
        self.has(self.with_control(id="z", kind="integer", selector=Selector(auto_id="z"), domain=Domain("boolean")), "domain kind")

    def test_dangerous_warning(self):
        errs = problems(self.with_feature(id="del", dangerous=True, selector=Selector(auto_id="del")))
        w = [e for e in errs if e.startswith("warning:")]
        self.assertEqual(len(w), 1)
        self.assertIn("'del'", w[0])
        self.assertEqual([e for e in errs if not e.startswith("warning:")], [])
        off = problems(self.with_feature(id="del", dangerous=True, enabled=False))
        self.assertEqual(off, [])

    def test_no_enabled(self):
        b = good()
        p = dataclasses.replace(b, features=tuple(dataclasses.replace(f, enabled=False) for f in b.features))
        self.has(p, "no enabled features")
        self.has(dataclasses.replace(b, features=()), "no enabled features")

    def test_schema_version(self):
        self.has(dataclasses.replace(good(), schema_version=1), "schema_version")


class SelectorScore(unittest.TestCase):
    def score(self, **kw):
        return pj.selector_score(Selector(**kw))[0]

    def test_ordering(self):
        s_auto = self.score(auto_id="driveSlider")
        s_path = self.score(path=("Main", "Panel"), control_type="Slider")
        s_title = self.score(title="Drive", control_type="Slider")
        s_class = self.score(class_name="QSlider")
        s_idx = self.score(index=3)
        self.assertTrue(90 <= s_auto <= 100)
        self.assertEqual(s_path, 70)
        self.assertEqual(s_title, 55)
        self.assertEqual(s_class, 30)
        self.assertEqual(s_idx, 10)
        self.assertGreater(s_auto, s_path)
        self.assertGreater(s_path, s_title)
        self.assertGreater(s_title, s_class)
        self.assertGreater(s_class, s_idx)
        self.assertEqual(self.score(), 0)

    def test_generated_ids_penalized(self):
        good_id = self.score(auto_id="driveSlider")
        for bad in ("12345", "Button12", "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "deadbeef01234567",
                    "x" * 41, "WindowsForms10.EDIT.app.0.141b42a_r6_ad1", "qt_scrollarea_viewport"):
            sc, why = pj.selector_score(Selector(auto_id=bad))
            self.assertLess(sc, good_id, bad)
            self.assertIn("AutomationId", why)
        self.assertLess(self.score(auto_id="12345"), self.score(title="Drive", control_type="Slider"))

    def test_title_volatility(self):
        base = self.score(title="Drive", control_type="Slider")
        self.assertLess(self.score(title="Document1 - Notepad", control_type="Edit"), base)
        self.assertLess(self.score(title="Report 2024-05-01", control_type="Edit"),
                        self.score(title="Document1", control_type="Edit"))

    def test_reason_is_actionable(self):
        _, why = pj.selector_score(Selector(title="Drive", control_type="Slider"))
        self.assertIn("AutomationId", why)
        self.assertIn("Hint", why)
        self.assertIn("AutomationId", pj.selector_score(Selector(index=1))[1])

    def test_fallbacks(self):
        s = Selector(auto_id="drive", title="Drive", control_type="Slider", class_name="QSlider",
                     path=("Main", "FX"), index=1)
        fb = pj.selector_fallbacks(s)
        self.assertNotIn(s, fb)
        scores = [pj.selector_score(x)[0] for x in fb]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(fb[0], Selector(auto_id="drive", control_type="Slider"))
        self.assertTrue(any(x.path and not x.auto_id for x in fb))
        self.assertTrue(any(x.title and not x.path and not x.auto_id for x in fb))
        self.assertEqual(len(fb), len(set(fb)))
        self.assertEqual(pj.selector_fallbacks(Selector()), [])
        self.assertEqual(pj.selector_fallbacks(Selector(auto_id="x")), [])


if __name__ == "__main__":
    unittest.main()
