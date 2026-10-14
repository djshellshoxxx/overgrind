import itertools
import json
import resource
import tracemalloc
import unittest

from overgrind import scheduler
from overgrind.states import StateSpace
from overgrind.strategies import ControlPlan
from overgrind.types import CaseGenerator, ConstraintDef, Outcome
from test_engine2_common import control, key, multistate, project, take, toggles

ALL = ("state_cover", "ordered_pairs", "twise", "boundary", "adaptive", "chaos")


def rich(strategy, **run):
    feats = toggles(4) + [multistate("m")]
    ctrls = [control("i", scope=("f0",)), control("c", kind="choice", options=("a", "b", "c")),
             control("b", kind="boolean"), control("x", kind="float", minimum=0.0, maximum=1.0),
             control("t", kind="text", max_length=5), control("sk", skip=True)]
    cons = [ConstraintDef("mutex", features=("f1", "f2"))]
    return project(feats, ctrls, cons, strategy=strategy, **run)


class Protocol(unittest.TestCase):
    def test_all_strategies_basic(self):
        for s in ALL:
            with self.subTest(s):
                p = rich(s)
                g = scheduler.make_generator(p)
                self.assertIsInstance(g, CaseGenerator)
                cases = take(g, 60)
                self.assertEqual([c.seq for c in cases], list(range(60)))
                for c in cases:
                    self.assertNotIn("sk", c.values)                      # skipped control
                    self.assertEqual(set(c.values), {"i", "c", "b", "x", "t"})
                    self.assertEqual(set(c.state), {"f0", "f1", "f2", "f3", "m"})
                    self.assertFalse(c.state["f1"] == "on" and c.state["f2"] == "on")
                cov = g.coverage()
                self.assertTrue(cov.guarantee)
                json.dumps(g.checkpoint())

    def test_deterministic_and_checkpoint_resume(self):
        for s in ALL:
            with self.subTest(s):
                p = rich(s, seed=5)
                a = take(scheduler.make_generator(p), 120)
                self.assertEqual(a, take(scheduler.make_generator(p), 120))
                g = scheduler.make_generator(p)
                first = take(g, 50)
                fail = Outcome(ok=False, category="crash")
                for c in first[:10]:
                    g.feedback(c, fail)
                ck = json.loads(json.dumps(g.checkpoint()))
                rest = take(g, 70)
                g2 = scheduler.make_generator(p, checkpoint=ck)
                self.assertEqual(take(g2, 70), rest)
                if s != "adaptive":
                    self.assertEqual(first + rest, a)

    def test_checkpoint_wrong_strategy(self):
        ck = scheduler.make_generator(rich("chaos")).checkpoint()
        with self.assertRaises(ValueError):
            scheduler.make_generator(rich("boundary"), checkpoint=ck)

    def test_preview_is_pure(self):
        p = rich("state_cover")
        g = scheduler.make_generator(p)
        take(g, 5)
        ck = g.checkpoint()
        pv = scheduler.preview(p, 10)
        self.assertEqual(len(pv), 10)
        self.assertEqual(g.checkpoint(), ck)
        self.assertEqual(pv, scheduler.preview(p, 10))
        self.assertEqual(pv, take(scheduler.make_generator(p), 10))

    def test_checkpoint_small(self):
        for s in ("state_cover", "ordered_pairs", "boundary", "chaos"):
            g = scheduler.make_generator(rich(s))
            take(g, 100)
            self.assertLess(len(json.dumps(g.checkpoint())), 2000, s)


class StateCover(unittest.TestCase):
    def test_scoped_control_semantics(self):
        plan = ControlPlan(rich("state_cover"))
        sp = StateSpace(rich("state_cover"))
        idx = plan.ids.index("i")
        self.assertFalse(plan.in_scope(idx, {"f0": "off"}, sp_states(sp)))
        self.assertTrue(plan.in_scope(idx, {"f0": "on"}, sp_states(sp)))
        self.assertTrue(plan.in_scope(plan.ids.index("c"), {"f0": "off"}, sp_states(sp)))   # global
        p = project([multistate("m")], [control("k", scope=("m",))])
        plan = ControlPlan(p)
        st = {"m": ("off", "low", "high")}
        self.assertFalse(plan.in_scope(0, {"m": "off"}, st))
        self.assertTrue(plan.in_scope(0, {"m": "low"}, st))
        self.assertTrue(plan.in_scope(0, {"m": "high"}, st))
        # values are filled for scoped controls in every case, within the domain
        for c in take(scheduler.make_generator(p), 12):
            self.assertIn("k", c.values)
            self.assertTrue(0 <= c.values["k"] <= 100)

    def test_fresh_values_each_visit(self):
        p = project(toggles(2), [control("i", maximum=10 ** 6)])
        g = scheduler.make_generator(p)
        a = take(g, 4)
        b = take(g, 4)
        by = {}
        for c in a:
            by[key(c)] = c.values["i"]
        self.assertTrue(any(by[key(c)] != c.values["i"] for c in b))

    def test_budget_fallback(self):
        p = project(toggles(12), strategy_options={"budget": 100})
        g = scheduler.make_generator(p)
        take(g, 10)
        cov = g.coverage()
        self.assertIsNone(cov.states_total)
        self.assertIn("no coverage guarantee", cov.guarantee)

    def test_million_cases_bounded_memory(self):
        p = project(toggles(19))                      # 524,288 states, control-less
        g = scheduler.make_generator(p)
        it = iter(g)
        for _ in range(200_000):
            next(it)
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        tracemalloc.start()
        base = tracemalloc.get_traced_memory()[0]
        for _ in range(100_000):
            next(it)
        grown = tracemalloc.get_traced_memory()[0] - base
        tracemalloc.stop()
        self.assertLess(grown, 1_000_000)
        for _ in range(1_700_000):
            c = next(it)
        rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertEqual(c.seq, 1_999_999)
        self.assertLess(rss1 - rss0, 4 * 1024, f"maxrss grew {rss1 - rss0} KB")   # KB on Linux
        cov = g.coverage()
        self.assertEqual(cov.epochs, 3)                                    # 2,000,000 // 524,288
        self.assertIn("524288", cov.guarantee)


def sp_states(sp):
    return {i: s for i, s in zip(sp.ids, sp.states)}


class OrderedPairs(unittest.TestCase):
    def test_sweeps_repeat_without_cap(self):
        p = project(toggles(4), strategy="ordered_pairs")
        g = scheduler.make_generator(p)
        cases = take(g, 32)
        for sw in (cases[:16], cases[16:]):
            self.assertEqual(len({c.order for c in sw}), 16)
            self.assertEqual({c.order for c in sw}, set(itertools.product(["f0", "f1", "f2", "f3"], repeat=2)))
        self.assertNotEqual([c.order for c in cases[:16]], [c.order for c in cases[16:]])
        cov = g.coverage()
        self.assertEqual((cov.tway_total, cov.epochs, cov.tway_t), (16, 2, 2))
        for c in cases:
            self.assertEqual(c.state[c.order[0]], "on")

    def test_respects_mutex(self):
        p = project(toggles(3), constraints=[ConstraintDef("mutex", features=("f0", "f1"))], strategy="ordered_pairs")
        for c in take(scheduler.make_generator(p), 27):
            self.assertFalse(c.state["f0"] == "on" and c.state["f1"] == "on")


class Boundary(unittest.TestCase):
    def test_boundaries_and_invalid_optin(self):
        ctrls = [control("i", minimum=0, maximum=10), control("c", kind="choice", options=("a", "b"))]
        p = project(toggles(2), ctrls, strategy="boundary")
        cases = take(scheduler.make_generator(p), 200)
        self.assertTrue({0, 10} <= {c.values["i"] for c in cases})
        self.assertTrue(all(0 <= c.values["i"] <= 10 for c in cases))
        self.assertTrue(all("invalid" not in c.tags for c in cases))
        p = project(toggles(2), ctrls, strategy="boundary", invalid_values=True)
        g = scheduler.make_generator(p)
        cases = take(g, 400)
        bad = [c for c in cases if "invalid" in c.tags]
        self.assertTrue(bad)
        self.assertTrue(any(not (0 <= c.values["i"] <= 10) or c.values["c"] not in ("a", "b") for c in bad))
        cov = g.coverage()
        self.assertEqual(cov.buckets_visited, cov.buckets_total)


class Adaptive(unittest.TestCase):
    def test_no_starvation_and_rarity(self):
        p = project(toggles(6), strategy="adaptive")
        g = scheduler.make_generator(p)
        cases = take(g, 3000)
        seen = {key(c) for c in cases}
        self.assertGreater(len(seen), 60)                  # 64 states: nearly all, far above random chance
        self.assertTrue(any("explore" in c.tags for c in cases))
        frac = sum("explore" in c.tags for c in cases) / len(cases)
        self.assertTrue(0.08 < frac < 0.25, frac)

    def test_failure_neighbourhood(self):
        p = project(toggles(8), [control("i")], strategy="adaptive")
        g = scheduler.make_generator(p)
        c0 = take(g, 1)[0]
        g.feedback(c0, Outcome(ok=False, category="crash"))
        more = take(g, 300)
        near = [c for c in more if "neighbour" in c.tags]
        self.assertTrue(near)
        self.assertTrue(all(sum(a != b for a, b in zip(c.state.values(), c0.state.values())) <= 1 for c in near))

    def test_bounded_memory(self):
        g = scheduler.make_generator(project(toggles(8), strategy="adaptive"))
        for _ in range(100):
            for c in take(g, 10):
                g.feedback(c, Outcome(ok=False))
        self.assertLessEqual(len(g.hot), 32)
        self.assertEqual(len(g.sketch), 3 * 4096)


class Chaos(unittest.TestCase):
    def test_walk(self):
        feats = toggles(4) + [multistate("m")]
        from overgrind.types import FeatureDef
        feats.append(FeatureDef("go", kind="action", states=("idle",), default_state="idle"))
        p = project(feats, [control("i")], constraints=[ConstraintDef("mutex", features=("f0", "f1"))],
                    strategy="chaos", reset_interval=20)
        cases = take(scheduler.make_generator(p), 600)
        resets = [c for c in cases if "reset" in c.tags]
        self.assertTrue(2 < len(resets) < 100)
        self.assertIn("reset", cases[0].tags)
        self.assertTrue(len({c.dwell_ms for c in cases}) > 10)
        self.assertTrue(any("go" in c.order for c in cases))
        for a, b in zip(cases, cases[1:]):
            if "reset" not in b.tags:
                self.assertLessEqual(sum(a.state[k] != b.state[k] for k in a.state), 1)
            self.assertFalse(b.state["f0"] == "on" and b.state["f1"] == "on")
        self.assertNotIn("go", cases[0].state)


if __name__ == "__main__":
    unittest.main()
