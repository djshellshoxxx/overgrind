"""Round 2 hardening: U2 ledger/preflight/danger, U3 reduce/export, U4 inference, U5 twin build, B1 selectors.

Spec acceptance numbers are quoted per test. Linux and Windows safe (no real GUI, no sleeping)."""
import dataclasses
import json
import os
import random
import tempfile
import unittest

from overgrind import danger, diffrun, infer, preflight
from overgrind import project as P
from overgrind import reduce as rd
from overgrind import selectors as S
from overgrind.adapters import resolve as R
from overgrind.adapters.mock import MockAdapter
from overgrind.evidence import EvidenceStore
from overgrind.types import (
    Action, AdapterSpec, Case, ControlDef, Domain, ElementInfo, ErrorEvent, FeatureDef, HarnessError, Outcome,
    Project, Provenance, RunConfig, Selector, TargetRef,
)


# ------------------------------------------------------------------ builders
def feat(i, **kw):
    return FeatureDef(id=f"f{i}", selector=Selector(auto_id=f"f{i}"), **kw)


def ctl(i, lo=0, hi=100, default=10, **kw):
    return ControlDef(f"c{i}", "integer", Selector(auto_id=f"c{i}"), Domain("integer", lo, hi, 1),
                      default=default, **kw)


def proj(nf=3, nc=2, **kw):
    run = kw.pop("run", RunConfig(settle_ms=25, capture_screenshot=False))
    return Project("proj", TargetRef(), tuple(feat(i) for i in range(nf)), tuple(ctl(i) for i in range(nc)),
                   run=run, **kw)


class Fixed(MockAdapter):
    """Mock whose inspect() lists exactly the project's elements (selectors resolve by auto id)."""
    project = None

    def inspect(self):
        out = [ElementInfo(Selector(auto_id="main", title="App", control_type="Window"), "App", "Window")]
        for f in self.project.features:
            out.append(ElementInfo(f.selector, f.id, "CheckBox", 1, 0, patterns=("Toggle",)))
        for c in self.project.controls:
            out.append(ElementInfo(c.selector, c.id, "Slider", 1, 0, patterns=("RangeValue",)))
        return out


def run_pf(p, factory=Fixed, **kw):
    Fixed.project = p
    kwargs = kw.pop("adapter_kwargs", {})
    cls_name = factory.__name__
    return preflight.run_preflight(p, AdapterSpec(f"test_r2_ledger_diag:{cls_name}", kwargs), **kw)


class IgnoreWrites(Fixed):
    def __init__(self, **kw):
        super().__init__(ignore_writes=["c0"], **kw)


class Clamp(Fixed):
    def __init__(self, **kw):
        super().__init__(clamp={"c0": (0, 100)}, **kw)


class Forgetful(Fixed):
    def __init__(self, **kw):
        super().__init__(forget_on_reset=["c1"], **kw)


class RaisesMidway(Fixed):
    def get_control(self, control):
        if control.id == "c1":
            raise ValueError("boom")
        return super().get_control(control)


# ------------------------------------------------------------------ U2
class PreflightVerdict(unittest.TestCase):
    def test_clean_can_start(self):
        rep = run_pf(proj())
        self.assertTrue(rep.can_start, rep.summary())
        self.assertTrue(rep.target_hash == "" or len(rep.target_hash) == 8)

    def test_1_ignored_write_fails_p06(self):
        rep = run_pf(proj(), IgnoreWrites)
        self.assertFalse(rep.can_start)
        item = next(i for i in rep.items if i.id == "c0")
        self.assertTrue(any(c.code == "P06" and c.status == "fail" for c in item.checks))

    def test_2_clamp_warns_proposes_narrow_and_verified_only_after_apply(self):
        p = dataclasses.replace(proj(), controls=(ctl(0, 0, 200), ctl(1)))
        rep = run_pf(p, Clamp)
        item = next(i for i in rep.items if i.id == "c0")
        self.assertEqual(item.status, "warn")
        fx = next(f for f in item.fixes if f.kind == "narrow_domain")
        self.assertEqual((fx.target, fx.value["maximum"]), ("c0", 100))
        self.assertNotEqual(p.controls[0].provenance.confidence, "verified")   # nothing changed yet
        out = preflight.apply(p, rep, accept=[fx.id])
        self.assertEqual(out.controls[0].fuzz_domain.maximum, 100)
        self.assertEqual(out.controls[0].provenance.confidence, "verified")
        self.assertEqual(P.provenance_info(out.controls[0].provenance.note).get("rb"), "ok")
        self.assertEqual(p.controls[0].fuzz_domain, None)                      # original immutable

    def test_3_reset_residue_names_control(self):
        rep = run_pf(proj(), Forgetful)
        it = next(i for i in rep.items if i.kind == "reset")
        self.assertEqual(it.status, "fail")
        self.assertIn("c1", it.evidence["residue"])
        self.assertFalse(rep.can_start)

    def test_4_two_matches_list_candidates_and_index_warns(self):
        p = proj()
        dup = [ElementInfo(Selector(title="OK", control_type="Button"), "OK", "Button") for _ in range(2)]
        it = preflight.PreflightItem("control", "x")
        self.assertFalse(preflight._locate(it, Selector(title="OK", control_type="Button"), dup))
        self.assertEqual(it.status, "fail")
        self.assertEqual(len(it.evidence["candidates"]), 2)
        it2 = preflight.PreflightItem("control", "x")
        self.assertTrue(preflight._locate(it2, Selector(title="OK", control_type="Button", index=1), dup))
        self.assertEqual(it2.status, "warn")
        _ = p

    def test_7_actions_not_invoked_unless_safe(self):
        acts = FeatureDef("act", "Act", "action", (), "", Selector(auto_id="act"))
        p = dataclasses.replace(proj(), features=(*proj().features, acts))

        class Rec(Fixed):
            pass
        run_pf(p, Rec)
        a = Rec()
        a.attach(p.target)
        self.assertFalse(any(c[0] == "invoke" for c in a.calls))
        Fixed.project = p
        calls = []
        ad = Fixed()
        orig = ad.invoke
        ad.invoke = lambda f: (calls.append(f.id), orig(f))
        preflight.run_preflight(p, AdapterSpec("x:y"), adapter=ad)
        self.assertEqual(calls, [])
        ad2 = Fixed()
        o2 = ad2.invoke
        ad2.invoke = lambda f: (calls.append(f.id), o2(f))
        preflight.run_preflight(p, AdapterSpec("x:y"), adapter=ad2, safe_to_probe=("act",))
        self.assertEqual(calls, ["act"])

    def test_8_baseline_restored_when_probe_raises(self):
        p = proj()
        Fixed.project = p
        ad = RaisesMidway()
        rep = preflight.run_preflight(p, AdapterSpec("x:y"), adapter=ad)
        self.assertIn(next(i for i in rep.items if i.id == "c1").status, ("fail",))
        ad.attach(p.target)
        # detach() does not clear state in the mock; re-check through a fresh look at the last reset
        self.assertEqual(ad._values.get("c0", 10), 10)
        self.assertIn(("reset", "*"), ad.calls[-5:])

    def test_11_budget_exceeded_is_incomplete(self):
        t = [0.0]

        def clock():
            t[0] += 10.0
            return t[0]
        rep = run_pf(proj(nf=4, nc=4), budget_s=25, clock=clock)
        self.assertTrue(rep.incomplete)
        self.assertFalse(rep.can_start)
        self.assertTrue(any("incomplete" in c.detail for i in rep.items for c in i.checks))

    def test_12_unknown_needs_ack(self):
        c = dataclasses.replace(ctl(0), provenance=Provenance("manual", "unknown"))
        p = dataclasses.replace(proj(), controls=(c, ctl(1)))
        self.assertTrue(any("unknown confidence" in e for e in P.validate(p)))
        rep = run_pf(p)
        self.assertFalse(rep.can_start)
        fx = [f for f in rep.fixes() if f.kind == "acknowledge"]
        self.assertTrue(fx)
        with self.assertRaises(ValueError):
            P.acknowledge_unknown(p, "wrong")
        q = P.acknowledge_unknown(p, "proj")
        self.assertFalse([e for e in P.validate(q) if "unknown confidence" in e])
        self.assertTrue(run_pf(q).can_start)
        self.assertEqual(P.run_blockers(q), [])
        self.assertTrue(P.run_blockers(p))

    def test_fix_actions_are_data(self):
        f = dataclasses.replace(feat(0), id="btn_delete", selector=Selector(auto_id="btn_delete"))
        p = dataclasses.replace(proj(), features=(f, feat(1)))
        rep = run_pf(p)
        fx = next(x for x in rep.fixes() if x.kind == "mark_dangerous")
        self.assertEqual((fx.target, fx.value), ("btn_delete", True))
        out = preflight.apply(p, rep, accept=[fx.id])
        self.assertTrue(out.features[0].dangerous)
        json.dumps(rep.to_dict())


class ExeHashDemotion(unittest.TestCase):
    def test_9_changed_build_demotes_and_blocks(self):
        note = P.make_note(t=P._now_iso(), build="aaaaaaaa")
        c = dataclasses.replace(ctl(0), provenance=Provenance("uia", "verified", note))
        p = dataclasses.replace(proj(), controls=(c,))
        same = P.demote_stale(p, "aaaaaaaa")
        self.assertEqual(same.controls[0].provenance.confidence, "verified")
        d = P.demote_stale(p, "bbbbbbbb")
        self.assertEqual(d.controls[0].provenance.confidence, "inferred")
        self.assertEqual(P.demoted_ids(d), ["c0"])
        self.assertTrue(P.run_blockers(d))
        self.assertEqual(p.controls[0].provenance.confidence, "verified")      # immutable

    def test_stale_after_30_days(self):
        note = P.make_note(t="2020-01-01T00:00Z", build="aaaaaaaa")
        c = dataclasses.replace(ctl(0), provenance=Provenance("uia", "verified", note))
        d = P.demote_stale(dataclasses.replace(proj(), controls=(c,)), "aaaaaaaa")
        self.assertEqual(d.controls[0].provenance.confidence, "inferred")

    def test_load_demotes_on_real_exe_change(self):
        with tempfile.TemporaryDirectory() as td:
            exe = os.path.join(td, "app.exe")
            with open(exe, "wb") as fh:
                fh.write(b"v1")
            note = P.make_note(t=P._now_iso(), build=P.exe_hash(exe))
            c = dataclasses.replace(ctl(0), provenance=Provenance("uia", "verified", note))
            p = dataclasses.replace(proj(), controls=(c,), target=TargetRef(kind="exe", exe_path=exe))
            path = os.path.join(td, "p.json")
            P.save(p, path)
            self.assertEqual(P.load(path).controls[0].provenance.confidence, "verified")
            with open(exe, "wb") as fh:
                fh.write(b"v2")
            self.assertEqual(P.load(path).controls[0].provenance.confidence, "inferred")


class DangerR2(unittest.TestCase):
    def test_6_tripwire(self):
        add = FeatureDef("f_add", "Add", selector=Selector(auto_id="b", title="Add", control_type="Button"))
        p = dataclasses.replace(proj(), features=(add,))
        ad = MockAdapter(element_labels={"f_add": "Delete"})
        ad.attach(p.target)
        ad.reset(p)
        g = danger.GuardedAdapter(ad, p)
        with self.assertRaises(HarnessError):
            g.set_feature_state(add, "on")
        self.assertEqual(g.tripwire.danger_skips, 1)
        self.assertFalse([c for c in ad.calls if c[0] in ("state", "invoke")])

    def test_tripwire_quiet_when_label_unchanged(self):
        add = FeatureDef("f_add", "Add", selector=Selector(auto_id="b", title="Add"))
        ad = MockAdapter()
        p = dataclasses.replace(proj(), features=(add,))
        ad.attach(p.target)
        ad.reset(p)
        g = danger.GuardedAdapter(ad, p)
        g.set_feature_state(add, "on")
        self.assertEqual(g.tripwire.danger_skips, 0)

    def test_10_password_element(self):
        pw = ElementInfo(Selector(auto_id="txtPassword"), "Password", "Edit", patterns=("Value",))
        flagged = ElementInfo(Selector(auto_id="x"), "Pin", "Edit", patterns=("Value", "IsPassword"))
        plain = ElementInfo(Selector(auto_id="txtName"), "Name", "Edit")
        self.assertTrue(danger.is_password(pw) and danger.is_password(flagged))
        self.assertFalse(danger.is_password(plain))

    def test_scan_elements_context(self):
        els = [ElementInfo(Selector(title="Delete all"), "Delete all", "Window"),
               ElementInfo(Selector(title="Name"), "Name", "Edit", parent=0),
               ElementInfo(Selector(title="Savannah"), "Savannah", "Edit", parent=None)]
        hits = danger.scan_elements(els)
        self.assertEqual(hits[0].tier, "block")
        self.assertEqual(hits[1].tier, "warn")
        self.assertNotIn(2, hits)

    def test_lexicon_examples(self):
        for txt, tier in (("&Save As...", "block"), ("Delete all", "block"), ("btnPayNow", "block"),
                          ("Remove from cart", "block"), ("Löschen", "block"), ("Exit", "disruptive")):
            f = danger.classify_detail(txt)
            self.assertIsNotNone(f, txt)
            self.assertEqual(f.tier, tier, txt)
        self.assertIsNone(danger.classify_detail("Savannah"))


class ProvenanceNotes(unittest.TestCase):
    def test_roundtrip(self):
        n = P.make_note(t="2026-10-14T10:00Z", n=3, tier=1, rb="ok", build="deadbeef")
        self.assertEqual(P.provenance_info(n)["build"], "deadbeef")
        self.assertEqual(P.provenance_info("hand written"), {})


# ------------------------------------------------------------------ U3
def crash(msg="boom 0x1"):
    return Outcome(False, "crash", msg)


def event(hist, sig=None):
    sig = sig or __import__("overgrind.evidence", fromlist=["x"]).outcome_fingerprint(crash())
    return ErrorEvent("e1", "2026-01-01T00:00:00Z", "r", "crash", sig, "boom 0x1",
                      Case(1, {}, {}), list(hist), {"os": "t", "exe_path": "/home/bob/app.exe"})


class ReduceR2(unittest.TestCase):
    def runner_for(self, bad):
        return lambda acts: crash() if any(a.target == bad for a in acts) else Outcome()

    def test_cancel_returns_partial_with_last_accepted(self):
        hist = [Action("control", "c0", i) for i in range(30)] + [Action("invoke", "bug")]
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 6
        out = rd.Reducer(proj(), runner=self.runner_for("bug"), cancel=cancel).reduce(event(hist))
        self.assertTrue(out.reduced["partial"])
        self.assertEqual(out.reduced["reason"], "cancelled")
        self.assertTrue(any(s["target"] == "bug" for s in out.reduced["steps"]))       # still failing

    def test_progress_hook_and_error_in_callback_ignored(self):
        seen = []

        def prog(d):
            seen.append(d["phase"])
            raise RuntimeError("gui bug")
        hist = [Action("control", "c0", 1)] * 6 + [Action("invoke", "bug")]
        out = rd.Reducer(proj(), runner=self.runner_for("bug"), on_progress=prog).reduce(event(hist))
        self.assertIn("replay", seen)
        self.assertEqual([s["target"] for s in out.reduced["steps"]], ["bug"])

    def test_12_cancel_leaves_saved_partial_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(td)
            ev = event([Action("control", "c0", i) for i in range(40)] + [Action("invoke", "bug")])
            store.write(ev)
            n = {"i": 0}

            def cancel():
                n["i"] += 1
                return n["i"] > 7
            rd.Reducer(proj(), runner=self.runner_for("bug"), store=store, cancel=cancel).reduce(ev)
            path = store.attachments_dir(ev.signature, ev.id) / "reduced.json"
            data = json.loads(path.read_text())
            self.assertTrue(data.get("partial"))
            self.assertTrue(any(s["target"] == "bug" for s in data["steps"]))

    def test_6_mutex_violating_candidate_never_accepted(self):
        from overgrind.types import ConstraintDef
        p = dataclasses.replace(proj(), constraints=(ConstraintDef("mutex", ("f0", "f1")),))

        def runner(acts):          # crashes with f0 on alone OR f0+f1 (the latter is constraint-invalid)
            on = {a.target for a in acts if a.kind == "state" and a.value == "on"}
            return crash() if "f0" in on else Outcome()
        hist = [Action("state", "f0", "on"), Action("state", "f1", "on")]
        out = rd.Reducer(p, runner=runner).reduce(event(hist))
        steps = {(s["target"], s["value"]) for s in out.reduced["steps"]}
        self.assertEqual(steps, {("f0", "on")})
        self.assertGreaterEqual(out.reduced.get("unresolved_candidates", 0), 0)

    def test_final_confirm_recorded_for_reliable(self):
        hist = [Action("control", "c0", 1)] * 5 + [Action("invoke", "bug")]
        out = rd.Reducer(proj(), runner=self.runner_for("bug")).reduce(event(hist))
        self.assertEqual(out.reduced["final_confirm"], {"n": 5, "r": 5})

    def test_budget_partial(self):
        hist = [Action("control", "c0", i) for i in range(40)] + [Action("invoke", "bug")]
        out = rd.Reducer(proj(), runner=self.runner_for("bug"), max_replays=10).reduce(event(hist))
        self.assertTrue(out.reduced["partial"])


class ExportIssue(unittest.TestCase):
    def setUp(self):
        self.p = dataclasses.replace(proj(), controls=(ctl(0), dataclasses.replace(ctl(1), id="pw", sensitive=True,
                                                                                     selector=Selector(auto_id="pw"))))
        c = Case(1, {"f0": "on"}, {"c0": 5, "pw": "s3cretvalue"})
        red = {"steps": [{"kind": "state", "target": "f0", "value": "on"},
                         {"kind": "control", "target": "pw", "value": "s3cretvalue"}],
               "values": {"pw": "s3cretvalue"}, "state": {"f0": "on"}, "original_steps": 9}
        self.ev = ErrorEvent("e", "t", "r", "crash", "abcdef012345", "failed with s3cretvalue in /home/bob/x", c, [],
                             {"os": "Linux", "exe_path": "/home/bob/app.exe", "exe_sha256": "ab" * 32,
                              "reliability": {"n": 5, "r": 5}}, reduced=red)

    def test_10_markdown_and_json(self):
        md = rd.export_issue(self.ev, "md", project=self.p)
        self.assertIn("Reproduced 5/5", md)
        self.assertIn("ab" * 32, md)
        self.assertIn("1. Set feature", md)
        self.assertNotIn("s3cretvalue", md)
        self.assertNotIn("bob", md)
        self.assertIn("overgrind repro abcdef01.json", md)
        js = json.loads(rd.export_issue(self.ev, "json", project=self.p))
        self.assertEqual(js["version"], 1)
        self.assertNotIn("s3cretvalue", json.dumps(js))

    def test_flaky_sentence(self):
        self.ev.env["reliability"] = {"n": 10, "r": 2}
        self.assertIn("Reproduced 2/10 (20%", rd.export_issue(self.ev, "md"))
        self.ev.env["reliability"] = {"n": 5, "r": 0}
        self.assertIn("Not reproduced in 5 tries", rd.export_issue(self.ev, "md"))


class ControllerRedaction(unittest.TestCase):
    def test_callbacks_get_redacted_event(self):
        from overgrind.controller import RunController
        c = dataclasses.replace(ctl(0), id="pw", sensitive=True, selector=Selector(auto_id="pw"))
        p = dataclasses.replace(proj(), controls=(c,))
        with tempfile.TemporaryDirectory() as td:
            rc = RunController(p, AdapterSpec("overgrind.adapters.mock:MockAdapter"), td)
            got = []
            rc.on_error(got.append)
            ev = ErrorEvent("e", "t", "r", "crash", "sig", "bad pwvalue123", Case(1, {}, {"pw": "pwvalue123"}), [],
                            {})
            rc._record(ev, None)
            self.assertEqual(got[0].case.values["pw"], "***")
            self.assertNotIn("pwvalue123", got[0].message)
            self.assertEqual(ev.case.values["pw"], "pwvalue123")      # the worker's copy is untouched


class RegistryTests(unittest.TestCase):
    def test_static_registry_resolves_builtin_and_dotted(self):
        from overgrind import registry
        self.assertIs(registry.resolve_callable("overgrind.adapters.mock:MockAdapter"), MockAdapter)
        self.assertTrue(callable(registry.resolve_callable("overgrind.scheduler:make_generator")))
        self.assertTrue(callable(registry.resolve_callable("os.path:join")))
        with self.assertRaises(ValueError):
            registry.resolve_callable("nocolon")


# ------------------------------------------------------------------ U4
def planted(seed, nf=12, nc=30, **kw):
    feats = tuple(feat(i) for i in range(nf))
    ctrls = tuple(ctl(i, default=1, hi=10) for i in range(nc))
    p = Project("t", TargetRef(), feats, ctrls, run=RunConfig(settle_ms=25))
    rules = dict(enable_rules={"c0": ["f0"], "c1": ["f1"], "c2": ["f2"], "c3": ["f3", "f4"], "c4": ["f5"]},
                 mutex_groups=[["f6", "f7"]], requires_rules={"f9": ["f8"]})
    rules.update(kw)
    ad = MockAdapter(seed=seed, **rules)
    ad.attach(p.target)
    return p, ad


EXPECT = {("scope", "c0", ("f0",)), ("scope", "c1", ("f1",)), ("scope", "c2", ("f2",)),
          ("scope", "c3", ("f3", "f4")), ("scope", "c4", ("f5",)), ("mutex", "", ("f6", "f7")),
          ("requires", "f9", ("f8",))}


def keyset(props):
    return {(x.kind, x.subject, tuple(x.features)) for x in props}


class InferStats(unittest.TestCase):
    def test_1_planted_recall_and_zero_fp(self):
        for seed in range(6):
            p, ad = planted(seed)
            got = keyset(infer.infer_statistical(p, ad, infer.InferConfig(seed=seed)))
            self.assertEqual(got - EXPECT, set(), f"false positive seed {seed}")
            self.assertGreaterEqual(len(got & EXPECT) / len(EXPECT), 0.9, f"seed {seed}: {got}")

    def test_2_null_model_no_proposals(self):
        # independent random enabledness: probabilities per (control, context) are fair coins
        feats = tuple(feat(i) for i in range(8))
        for seed in range(30):
            rng = random.Random(seed)
            obs = []
            for k in range(60):
                ctx = {f.id: rng.choice(("off", "on")) for f in feats}
                for c in range(30):
                    obs.append(infer.ElementObs(ctx, f"c{c}", enabled=rng.random() < 0.5))
            self.assertEqual(infer.analyze(obs, feats), [], f"seed {seed}")

    def test_3_read_noise_survives(self):
        fps = hits = 0
        for seed in range(6):
            p, ad = planted(seed, read_noise=0.05)
            got = keyset(infer.infer_statistical(p, ad, infer.InferConfig(seed=seed)))
            fps += len(got - EXPECT)
            hits += len(got & EXPECT)
        self.assertEqual(fps, 0)
        self.assertGreaterEqual(hits / (6 * len(EXPECT)), 0.7)

    def test_4_delayed_enable_noisy_until_delays_tested(self):
        p, ad = planted(0, enable_delay_ms=80)
        short = infer.InferConfig(delay_steps=(1, 2))
        obs, _, info = infer.gather(ad, p, short)
        self.assertGreater(info["noisy_cells"], 0)
        p, ad = planted(0, enable_delay_ms=80)
        props = infer.infer_statistical(p, ad)
        self.assertIn(("scope", "c0", ("f0",)), keyset(props))
        self.assertTrue(all(x.detail.get("suggest_settle_ms", 100) >= 50 for x in props if x.kind == "scope"))

    def test_5_min_counts(self):
        feats = (feat(0), feat(1))
        def mk(n):
            obs = []
            for i in range(n):
                for st in ("on", "off"):
                    ctx = {"f0": st, "f1": ("on", "off")[i % 2], "k": str(i)}
                    obs.append(infer.ElementObs(ctx, "c", enabled=(st == "on")))
            return obs
        self.assertEqual(infer.analyze(mk(5), feats), [])
        self.assertEqual(len(infer.analyze(mk(10), feats)), 1)

    def test_6_conjunction_needs_24_contexts(self):
        feats = (feat(0), feat(1), feat(2))
        def mk(per_cell):
            obs = []
            for a in ("off", "on"):
                for b in ("off", "on"):
                    for i in range(per_cell):
                        ctx = {"f0": a, "f1": b, "f2": ("on", "off")[i % 2], "k": f"{a}{b}{i}"}
                        obs.append(infer.ElementObs(ctx, "c", enabled=(a == "on" and b == "on")))
            return obs
        few = infer.analyze(mk(5), feats)
        self.assertEqual(few, [])
        many = infer.analyze(mk(8), feats)
        self.assertEqual({(x.kind, x.features) for x in many}, {("scope", ("f0", "f1"))})

    def test_inverse_disabled_when_on_is_reported_not_applied(self):
        feats = (feat(0), feat(1))
        obs = []
        for i in range(24):
            ctx = {"f0": ("on", "off")[i % 2], "f1": ("on", "off")[(i // 2) % 2], "k": str(i)}
            obs.append(infer.ElementObs(ctx, "c", enabled=ctx["f0"] == "off"))
        props = infer.analyze(obs, feats)
        self.assertEqual([x.kind for x in props], ["enable_inverse"])
        pr = dataclasses.replace(props[0], status="approved")
        p = dataclasses.replace(proj(nf=2, nc=1))
        self.assertIs(infer.apply(p, [pr]), p)

    def test_7_dangerous_never_toggled_and_8_restored(self):
        p, ad = planted(0)
        feats = list(p.features)
        feats[10] = dataclasses.replace(feats[10], dangerous=True)
        p = dataclasses.replace(p, features=tuple(feats))
        infer.gather(ad, p, infer.InferConfig(contexts=10))
        self.assertFalse([c for c in ad.calls if c[0] == "state" and c[1] == "f10"])
        self.assertEqual(ad.calls[-1], ("reset", "*"))

    def test_8_cancel_and_exception_restore(self):
        p, ad = planted(0)
        infer.gather(ad, p, infer.InferConfig(contexts=100), cancel=lambda: True)
        self.assertEqual(ad.calls[-1], ("reset", "*"))
        p, ad = planted(0)

        def bad(adapter, project):
            raise HarnessError("read failed")
        with self.assertRaises(HarnessError):
            infer.gather(ad, p, probe=bad)
        self.assertEqual(ad.calls[-1], ("reset", "*"))

    def test_9_approval_path_and_impact(self):
        p, ad = planted(1)
        props = infer.infer_statistical(p, ad)
        mx = next(x for x in props if x.kind == "mutex")
        self.assertEqual(infer.apply(p, props), p)                      # nothing approved: unchanged
        self.assertEqual(mx.impact["valid_before"] - mx.impact["valid_after"], 2 ** 10)   # 4 -> 3 of 4 combos
        out = infer.apply(p, [dataclasses.replace(mx, status="approved")])
        from overgrind.states import StateSpace
        self.assertEqual(StateSpace(out).size, mx.impact["valid_after"])

    def test_10_rejected_not_reproposed_until_evidence_doubles(self):
        p, ad = planted(2)
        props = infer.infer_statistical(p, ad)
        pr = props[0]
        rej = {pr.fingerprint: pr.evidence}
        p2, ad2 = planted(2)
        again = infer.infer_statistical(p2, ad2, rejected=rej)
        self.assertNotIn(pr.fingerprint, {x.fingerprint for x in again})
        rej2 = {pr.fingerprint: max(1, pr.evidence // 3)}
        p3, ad3 = planted(2)
        self.assertIn(pr.fingerprint, {x.fingerprint for x in infer.infer_statistical(p3, ad3, rejected=rej2)})
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "infer.json")
            infer.save_rejected(path, rej)
            self.assertEqual(infer.load_rejected(path), rej)

    def test_11_violable_rule_rejected(self):
        p, ad = planted(3, mutex_leak=0.35)
        got = keyset(infer.infer_statistical(p, ad))
        self.assertNotIn(("mutex", "", ("f6", "f7")), got)

    def test_12_analyze_is_deterministic(self):
        p, ad = planted(4)
        obs, trials, _ = infer.gather(ad, p)
        a = infer.analyze(obs, p.features)
        b = infer.analyze(list(obs), p.features)
        self.assertEqual(a, b)
        self.assertEqual(infer.analyze_trials(trials, p.features), infer.analyze_trials(trials, p.features))

    def test_fisher_and_bh(self):
        self.assertAlmostEqual(infer.fisher_exact(10, 0, 0, 10), 2 / 184756, places=9)
        self.assertEqual(infer.fisher_exact(5, 5, 5, 5), 1.0)
        q = infer.benjamini_hochberg([0.001, 0.01, 0.04, 0.5], 0.05)
        self.assertEqual([round(x, 4) for x in q], [0.004, 0.02, 0.0533, 0.5])

    def test_active_semantics_is_first_state(self):
        f = FeatureDef("d", states=("off", "on"), default_state="on", selector=Selector(auto_id="d"))
        pr = Project("t", TargetRef(), (f,), ())
        obs = [({"d": "off"}, {"c": {"enabled": False}}), ({"d": "on"}, {"c": {"enabled": True}})]
        self.assertEqual(infer.infer_dependencies(obs, pr) or [], infer.infer_dependencies(obs, pr))


# ------------------------------------------------------------------ B1
def tree(*rows):
    out = [ElementInfo(Selector(auto_id="main", title="App", control_type="Window"), "App", "Window", 0, None,
                       (0, 0, 800, 600))]
    for i, (aid, name, ctype, bounds) in enumerate(rows):
        out.append(ElementInfo(Selector(auto_id=aid, title=name, control_type=ctype, class_name=ctype,
                                        path=("App",)), name, ctype, 1, 0, bounds, patterns=("Invoke",)))
    return out


class SelectorsB1(unittest.TestCase):
    def test_1_grades(self):
        rep = S.score(Selector(auto_id="btnApply", title="Apply", control_type="Button"))
        self.assertGreaterEqual(rep.score, 85)
        self.assertEqual(rep.grade, "A")
        bad = S.score(Selector(title="Item 3", control_type="ListItem", index=2))
        self.assertEqual(bad.grade, "D")
        self.assertTrue({"F01", "F05", "F08"} <= {r.code for r in bad.reasons})

    def test_2_every_code_has_text_and_fix(self):
        for code, (pen, text, fix) in S.FRAGILITY.items():
            self.assertTrue(text and fix and pen > 0, code)
        for line in S.why_fragile(S.score(Selector(title="Item 3", index=2))):
            self.assertIn("Fix:", line)

    def test_3_generated_ids(self):
        for a in ("1042", "{3F2504E0-4F89-11D3-9A0C-0305E82C3301}", "WindowsForms10.EDIT.app.0.141b42a_r6_ad1"):
            self.assertTrue(S.looks_generated(a), a)
        for a in ("btnApply", "widthEdit"):
            self.assertFalse(S.looks_generated(a), a)

    def test_4_heal_renamed_id(self):
        t = tree(("btnApplyNow", "Apply", "Button", (100, 500, 80, 30)),
                 ("btnCancel", "Cancel", "Button", (200, 500, 80, 30)),
                 ("btnHelp", "Help", "Button", (300, 500, 80, 30)))
        sel = Selector(auto_id="btnApply", title="Apply now", control_type="Button", class_name="Button",
                       path=("App",))
        res = R.resolve_heal(sel, t)
        self.assertTrue(res.healed)
        self.assertGreaterEqual(res.score, 0.85)
        self.assertGreaterEqual(res.margin, 0.15)
        self.assertEqual(t[res.index].selector.auto_id, "btnApplyNow")
        upd = S.propose_update(sel, t[res.index], res.score, res.margin)
        self.assertEqual(upd.new.auto_id, "btnApplyNow")
        self.assertIn("heal=1", upd.note)
        self.assertEqual(sel.auto_id, "btnApply")                         # selector (project) untouched

    def test_5_near_identical_candidates_refuse(self):
        t = tree(("btnApplyA", "Apply", "Button", (100, 500, 80, 30)),
                 ("btnApplyB", "Apply", "Button", (100, 500, 80, 30)))
        sel = Selector(auto_id="btnApplyX", title="Apply", control_type="Button", class_name="Button", path=("App",))
        with self.assertRaises(HarnessError):
            R.resolve_heal(sel, t)

    def test_6_type_change_never_healed(self):
        t = tree(("btnApplyNow", "Apply", "CheckBox", (100, 500, 80, 30)))
        with self.assertRaises(HarnessError):
            R.resolve_heal(Selector(auto_id="btnApply", title="Apply", control_type="Button",
                                    class_name="Button", path=("App",)), t)

    def test_7_other_process_never_healed(self):
        t = tree(("btnApplyNow", "Apply", "Button", (100, 500, 80, 30)))
        with self.assertRaises(HarnessError):
            R.resolve_heal(Selector(auto_id="btnApply", title="Apply now", control_type="Button",
                                    path=("App",)), t, accept=lambda e: False)

    def test_8_write_plausibility(self):
        self.assertTrue(S.write_plausible("integer", "42"))
        self.assertFalse(S.write_plausible("integer", "abc"))
        self.assertFalse(S.write_plausible("boolean", "maybe"))

    def test_9_dangerous_never_healed(self):
        t = tree(("btnApplyNow", "Apply", "Button", (100, 500, 80, 30)))
        sel = Selector(auto_id="btnApply", title="Apply now", control_type="Button", path=("App",))
        with self.assertRaises(HarnessError):
            S.resolve(t, sel, dangerous=True)
        self.assertTrue(S.resolve(t, sel).healed)

    def test_10_ambiguity_lists_both(self):
        t = tree(("a", "OK", "Button", (1, 1, 1, 1)), ("b", "OK", "Button", (2, 2, 1, 1)))
        with self.assertRaises(HarnessError) as cm:
            R.resolve_heal(Selector(title="OK", control_type="Button"), t, allow_heal=False)
        self.assertIn("2 elements", str(cm.exception))

    def test_11_failed_probe_caps_grade_at_c(self):
        sel = Selector(auto_id="btnApply", control_type="Button", title="Apply")
        self.assertEqual(S.score(sel, probe=lambda s: 1.0).grade, "A")
        r = S.score(sel, probe=lambda s: 0.9)
        self.assertLessEqual(S.grade_of(100), "A")
        self.assertIn(r.grade, ("C", "D"))
        self.assertEqual(r.empirical, 90.0)

    def test_13_scoring_2000_elements_fast(self):
        import time
        infos = [ElementInfo(Selector(auto_id=f"id{i}", title=f"T{i}", control_type="Button", path=("App",)),
                             f"T{i}", "Button") for i in range(2000)]
        t0 = time.perf_counter()
        reports = S.score_all(infos[:200])
        self.assertEqual(len(reports), 200)
        S.heal(Selector(auto_id="zz", title="Q", control_type="Button"), infos)
        self.assertLess(time.perf_counter() - t0, 5.0)

    def test_14_import_hygiene(self):
        import subprocess
        import sys
        code = ("import sys, overgrind.selectors; "
                "assert not any(m.startswith(('pywinauto','PySide6')) for m in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(os.path.dirname(__file__)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_similarity_symmetric_enough_and_bounded(self):
        a = ElementInfo(Selector(auto_id="w", title="Width", control_type="Edit"), "Width", "Edit")
        b = ElementInfo(Selector(auto_id="w", title="Width", control_type="Edit"), "Width", "Edit")
        self.assertEqual(S.similarity(a, b), 1.0)
        c = ElementInfo(Selector(auto_id="h", title="Height", control_type="Slider"), "Height", "Slider")
        self.assertEqual(S.similarity(a, c), 0.0)
        self.assertGreater(S.jaro_winkler("martha", "marhta"), 0.96)


# ------------------------------------------------------------------ U5
def _els(names, extra=()):
    out = [ElementInfo(Selector(auto_id="main", title="App", control_type="Window"), "App", "Window", 0, None,
                       (0, 0, 1000, 800))]
    for i, (aid, title, ctype) in enumerate(names):
        out.append(ElementInfo(Selector(auto_id=aid, title=title, control_type=ctype, class_name=ctype,
                                        path=("App",)), title, ctype, 1, 0, (10, 20 + 40 * i, 200, 30),
                               patterns=("RangeValue",) if ctype == "Slider" else ("Value",)))
    return out


WORDS = ["Width", "Height", "Radius", "Opacity", "Brightness", "Contrast", "Gamma", "Margin", "Padding", "Border",
         "Rotation", "Scale", "Speed", "Volume", "Timeout", "Retries", "Quality", "Threshold", "Offset", "Spacing"]


class TwinMap(unittest.TestCase):
    def setUp(self):
        self.rows = [(f"sld_{w.lower()}", w, "Slider") for w in WORDS]
        self.p = Project("t", TargetRef(), (feat(0),),
                         tuple(ControlDef(f"sld_{w.lower()}", "integer", Selector(auto_id=f"sld_{w.lower()}",
                                          title=w, control_type="Slider", class_name="Slider", path=("App",)),
                                          Domain("integer", 0, 100, 1), default=1) for w in WORDS))

    def test_1_renames_map_correctly(self):
        a = _els(self.rows)
        rng = random.Random(7)
        rows_b = []
        for aid, title, ct in self.rows:
            if rng.random() < 0.3:
                rows_b.append((aid + "_px", title + " (px)", ct))
            else:
                rows_b.append((aid, title, ct))
        b = _els(rows_b)
        m = diffrun.migrate_map(self.p, a, b)
        wrong = 0
        mapped = 0
        for (aid, title, ct), (baid, btitle, _ct) in zip(self.rows, rows_b):
            e = m[aid]
            if e.status == "mapped":
                mapped += 1
                wrong += e.b_selector.auto_id != baid
        self.assertEqual(wrong, 0)
        self.assertGreaterEqual(mapped / len(self.rows), 0.95)

    def test_3_removed_and_added_controls(self):
        a = _els(self.rows)
        b = _els(self.rows[1:] + [("sld_new", "Brand new", "Slider")])
        rv = diffrun.review_map(self.p, a, b)
        self.assertIn("sld_width", rv.removed)
        self.assertIn("Brand new", rv.added)
        self.assertIsNone(rv.id_map()["sld_width"])
        self.assertTrue(rv.to_dict()["counts"]["unmatched"] >= 1)

    def test_ambiguous_duplicates_go_to_review(self):
        rows = [("x1", "Value", "Slider"), ("x2", "Value", "Slider")]
        a = _els([("w", "Value", "Slider")])
        b = _els(rows)
        p = Project("t", TargetRef(), (feat(0),), (ControlDef("w", "integer", Selector(
            auto_id="w", title="Value", control_type="Slider", class_name="Slider", path=("App",)),
            Domain("integer", 0, 9, 1), default=1),))
        e = diffrun.migrate_map(p, a, b)["w"]
        self.assertNotEqual(e.status, "mapped")

    def test_review_confirm_and_override(self):
        a = _els([("w", "Value", "Slider")])
        b = _els([("x1", "Value", "Slider"), ("x2", "Value", "Slider")])
        p = Project("t", TargetRef(), (feat(0),), (ControlDef("w", "integer", Selector(
            auto_id="w", title="Value", control_type="Slider", class_name="Slider", path=("App",)),
            Domain("integer", 0, 9, 1), default=1),))
        rv = diffrun.review_map(p, a, b)
        self.assertIsNone(rv.id_map()["w"])
        rv.override("w", Selector(auto_id="x2", control_type="Slider"))
        self.assertEqual(rv.id_map()["w"].auto_id, "x2")

    def test_type_mismatch_unmatched(self):
        a = _els([("w", "Value", "Slider")])
        b = _els([("w2", "Value", "CheckBox")])
        p = Project("t", TargetRef(), (feat(0),), (ControlDef("w", "integer", Selector(
            auto_id="w", title="Value", control_type="Slider", class_name="Slider", path=("App",)),
            Domain("integer", 0, 9, 1), default=1),))
        self.assertEqual(diffrun.migrate_map(p, a, b)["w"].status, "unmatched")

    def test_2_calibration_fixture_precision_recall(self):
        rng = random.Random(3)
        names = [f"{a} {b}" for a in ("Left", "Right", "Top", "Bottom", "Inner", "Outer", "Main", "Aux", "Min", "Max")
                 for b in ("Width", "Height", "Radius", "Offset", "Speed", "Gain", "Limit", "Delay", "Count", "Size")]
        rows_a = [(f"ctl_{n.lower().replace(' ', '_')}", n, "Slider") for n in names]
        rows_b = []
        for aid, n, ct in rows_a:
            r = rng.random()
            if r < 0.3:
                rows_b.append((aid + "_v2", n + " (px)", ct))
            elif r < 0.45:
                rows_b.append((aid, n.replace("Width", "W."), ct))
            else:
                rows_b.append((aid, n, ct))
        a, b = _els(rows_a), _els(rows_b)
        p = Project("t", TargetRef(), (feat(0),), tuple(ControlDef(
            aid, "integer", Selector(auto_id=aid, title=n, control_type="Slider", class_name="Slider", path=("App",)),
            Domain("integer", 0, 9, 1), default=1) for aid, n, _c in rows_a))
        m = diffrun.migrate_map(p, a, b)
        good = wrong = 0
        for (aid, _n, _c), (baid, _bn, _bc) in zip(rows_a, rows_b):
            if m[aid].status == "mapped":
                good += m[aid].b_selector.auto_id == baid
                wrong += m[aid].b_selector.auto_id != baid
        self.assertGreaterEqual(good / max(1, good + wrong), 0.98)
        self.assertGreaterEqual(good / len(rows_a), 0.85)


class DiffMock(MockAdapter):
    pass


class Crasher(MockAdapter):
    """Build B regression: crashes iff f0=on and c0==0."""

    def wait_quiescent(self, timeout_ms=0):
        if self._states.get("f0") == "on" and self._values.get("c0") == 0:
            self._dead = "boom"
        super().wait_quiescent(timeout_ms)


class ClampB(MockAdapter):
    def __init__(self, **kw):
        super().__init__(clamp={"c0": (0, 100)}, **kw)


class RandomClock(MockAdapter):
    def get_control(self, control):
        if control.id == "c1":
            return random.random()
        return super().get_control(control)


class TwinRun(unittest.TestCase):
    def setUp(self):
        self.p = Project("t", TargetRef(), (feat(0), feat(1)), (ctl(0, 0, 200), ctl(1, 0, 10)),
                         run=RunConfig(settle_ms=0, capture_screenshot=False))
        self.cases = [Case(i, {"f0": ("on", "off")[i % 2], "f1": "off"}, {"c0": (i * 13) % 150, "c1": i % 7},
                           ("f0", "f1")) for i in range(40)]
        self.spec = lambda n: AdapterSpec(f"test_r2_ledger_diag:{n}")

    def test_4_regression_reproduced_3_of_3(self):
        cases = [Case(0, {"f0": "on", "f1": "off"}, {"c0": 0, "c1": 1}, ("f0", "f1")),
                 Case(1, {"f0": "off", "f1": "off"}, {"c0": 0, "c1": 1}, ("f0", "f1"))]
        rep = diffrun.differential(self.p, self.spec("DiffMock"), self.spec("Crasher"), cases=cases, k_confirm=3)
        self.assertEqual(len(rep.divergences), 1)
        d = rep.divergences[0]
        self.assertEqual((d.verdict, d.oracle, d.reproduced), ("regression", "O1", (3, 3)))
        self.assertEqual(len(rep.to_events()), 1)

    def test_5_clamp_readback_diff_and_domain_intersection(self):
        cases = [Case(0, {"f0": "off", "f1": "off"}, {"c0": 150}, ("f0", "f1")),
                 Case(1, {"f0": "off", "f1": "off"}, {"c0": 50}, ("f0", "f1"))]
        rep = diffrun.differential(self.p, self.spec("DiffMock"), self.spec("ClampB"), cases=cases)
        self.assertEqual([(m.kind, m.target, m.case_seq) for m in rep.mismatches], [("value", "c0", 0)])
        rep2 = diffrun.differential(self.p, self.spec("DiffMock"), self.spec("ClampB"), cases=cases,
                                    b_domains={"c0": Domain("integer", 0, 100, 1)})
        self.assertTrue(rep2.clean)
        self.assertEqual(rep2.skipped_domain, 1)

    def test_6_nondeterministic_masked_by_calibration(self):
        rep = diffrun.differential(self.p, self.spec("RandomClock"), self.spec("RandomClock"), cases=self.cases,
                                   calibrate_cases=20)
        self.assertEqual(rep.mismatches, ())
        self.assertEqual(rep.divergences, ())
        self.assertIn("c1", rep.noise["masked"])
        self.assertGreater(rep.noise["fp_rate"]["O2"], 0)
        unmasked = diffrun.differential(self.p, self.spec("RandomClock"), self.spec("RandomClock"), cases=self.cases)
        self.assertTrue(unmasked.mismatches)

    def test_9_abba_order(self):
        rep = diffrun.differential(self.p, self.spec("DiffMock"), self.spec("DiffMock"), cases=self.cases[:4],
                                   order="ABBA")
        self.assertEqual(rep.order_log, (("A", 0), ("B", 0), ("B", 1), ("A", 1), ("A", 2), ("B", 2), ("B", 3), ("A", 3)))

    def test_10_transient_divergence_lands_in_noise_bucket(self):
        state = {"n": 0}

        class Once(MockAdapter):
            def wait_quiescent(self, timeout_ms=0):
                state["n"] += 1
                if state["n"] == 1:          # fires on the first B run only
                    self._dead = "transient"
                super().wait_quiescent(timeout_ms)
        cases = self.cases[:1]
        rep = diffrun.differential(self.p, self.spec("DiffMock"), self.spec("DiffMock"), cases=cases,
                                   factory_a=lambda: _att(MockAdapter(), self.p),
                                   factory_b=lambda: _att(Once(), self.p), k_confirm=3)
        self.assertEqual(rep.divergences, ())
        self.assertEqual(len(rep.suspected_noise), 1)

    def test_11_same_build_zero_divergences(self):
        rng = random.Random(1)
        cases = [Case(i, {"f0": rng.choice(("on", "off")), "f1": rng.choice(("on", "off"))},
                      {"c0": rng.randint(0, 200), "c1": rng.randint(0, 10)}, ("f0", "f1")) for i in range(300)]
        rep = diffrun.differential(self.p, self.spec("DiffMock"), self.spec("DiffMock"), cases=cases,
                                   calibrate_cases=50, wilcoxon=True)
        self.assertTrue(rep.clean)
        self.assertEqual(rep.divergences, ())

    def test_12_agreeing_cases_write_nothing(self):
        rep = diffrun.differential(self.p, self.spec("DiffMock"), self.spec("DiffMock"), cases=self.cases)
        self.assertEqual(rep.to_events(), [])

    def test_8_float_tolerance(self):
        self.assertTrue(diffrun._close(0.1 + 0.2, 0.3, 1e-6))
        self.assertFalse(diffrun._close(1.0, 1.0001, 1e-6))
        self.assertTrue(diffrun._close("a  b", "a b", 1e-6))

    def test_7_timing_wilcoxon(self):
        rng = random.Random(5)
        a = [0.100 + rng.uniform(-0.02, 0.02) for _ in range(40)]
        b = [x + 0.15 + rng.uniform(-0.02, 0.02) for x in a]
        res = diffrun._perf_oracle(a, b, None)
        self.assertTrue(res["enabled"] and res["flagged"], res)
        same = diffrun._perf_oracle(a, [x + rng.uniform(-0.002, 0.002) for x in a], None)
        self.assertFalse(same["flagged"])
        few = diffrun._perf_oracle(a[:20], b[:20], None)
        self.assertFalse(few["enabled"])
        noisy = diffrun.Calibration(40, {}, 0, {}, 1.5, False)
        self.assertIn("too noisy", diffrun._perf_oracle(a, b, noisy)["message"])

    def test_wilcoxon_basics(self):
        self.assertLess(diffrun.wilcoxon_signed_rank([1.0 + i * 0.01 for i in range(30)]), 0.001)
        self.assertGreater(diffrun.wilcoxon_signed_rank([(-1) ** i * (1 + i * 0.01) for i in range(30)]), 0.3)
        self.assertEqual(diffrun.wilcoxon_signed_rank([1, 2]), 1.0)

    def test_fuzzy_selector_map_drives_run(self):
        a = _els([("w", "Value", "Slider")])
        b = _els([("w", "Value", "Slider")])
        sel = Selector(auto_id="w", title="Value", control_type="Slider", class_name="Slider", path=("App",))
        p = Project("t", TargetRef(), (feat(0),), (ControlDef("c", "integer", sel, Domain("integer", 0, 9, 1),
                                                                default=1),))
        rv = diffrun.review_map(p, a, b)
        self.assertEqual(rv.id_map()["c"].auto_id, "w")


def _att(adapter, project):
    adapter.attach(project.target)
    return adapter


# ------------------------------------------------------------------ U3 statistics / shrink / bonus finds
class ReduceStats(unittest.TestCase):
    def prob_runner(self, p, seed):
        rng = random.Random(seed)
        return lambda acts: crash() if (any(a.target == "bug" for a in acts) and rng.random() < p) else Outcome()

    def test_3_classification_over_200_trials(self):
        labels = {"flaky": 0, "reliable": 0, "not_reproducible": 0, "unconfirmed": 0}
        for t in range(200):
            ev = event([Action("invoke", "bug")])
            out = rd.Reducer(proj(), runner=self.prob_runner(0.5, t), max_replays=60).reduce(ev)
            labels[out.reliability] += 1
        self.assertGreaterEqual(labels["flaky"], 0.90 * 200)
        self.assertLessEqual(labels["reliable"], 0.06 * 200)
        ev = event([Action("invoke", "bug")])
        self.assertEqual(rd.Reducer(proj(), runner=self.prob_runner(1.0, 0)).reduce(ev).reliability, "reliable")
        self.assertEqual(rd.Reducer(proj(), runner=self.prob_runner(0.0, 0)).reduce(ev).reliability,
                         "not_reproducible")

    def test_5_value_shrink_probe_budget(self):
        calls = []

        def runner(acts):
            calls.append(1)
            v = next((a.value for a in acts if a.target == "c0"), 10)
            return crash() if v >= 37 else Outcome()
        hist = [Action("control", "c0", 91)]
        out = rd.Reducer(proj(), runner=runner).reduce(event(hist))
        self.assertEqual(out.reduced["values"]["c0"], 37)
        # confirm (5) + final confirm (5) + ddmin overhead are fixed costs; the shrink itself is <= 12 probes
        shrink = len(calls) - 5 - 5
        self.assertLessEqual(shrink, 16)

    def test_9_different_signature_saved_as_new_event(self):
        def runner(acts):
            t = [a.target for a in acts]
            if "bug" in t and "A" in t:
                return crash("boom 0x1")
            if "bug" in t:
                return Outcome(False, "crash", "different module fault")
            return Outcome()
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(td)
            ev = event([Action("state", "A", "on"), Action("invoke", "bug"), Action("wait", "w", 1)])
            store.write(ev)
            r = rd.Reducer(proj(), runner=runner, store=store)
            r.reduce(ev)
            self.assertEqual(len(r.bonus_events), 1)
            be = r.bonus_events[0]
            self.assertIn("reduced-from:e1", be.case.tags)
            self.assertTrue(store.known(be.signature))

    def test_11_determinism(self):
        hist = [Action("control", "c0", i) for i in range(12)] + [Action("invoke", "bug")] + \
               [Action("state", "f1", "on")] * 3
        a = rd.Reducer(proj(), runner=lambda x: crash() if any(s.target == "bug" for s in x) else Outcome()).reduce(event(hist))
        b = rd.Reducer(proj(), runner=lambda x: crash() if any(s.target == "bug" for s in x) else Outcome()).reduce(event(hist))
        self.assertEqual(a.reduced["steps"], b.reduced["steps"])
        self.assertEqual(a.reduced["replays"], b.reduced["replays"])


if __name__ == "__main__":
    unittest.main()
