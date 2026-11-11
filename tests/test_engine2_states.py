import itertools
import unittest

from overgrind import constraints, scheduler
from overgrind.states import KeyedPermutation, StateSpace
from overgrind.types import ConstraintDef
from test_engine2_common import control, key, multistate, project, take, toggles


def epoch(p, n):
    return take(scheduler.make_generator(p), n)


class SpaceCounts(unittest.TestCase):
    def test_three_toggles_eight_and_seven(self):
        p = project(toggles(3))
        self.assertEqual(StateSpace(p).size, 8)
        cases = epoch(p, 8)
        self.assertEqual(len({key(c) for c in cases}), 8)
        p7 = project(toggles(3), include_all_off=False)
        self.assertEqual(StateSpace(p7).size, 7)
        cases = epoch(p7, 7)
        self.assertEqual(len({key(c) for c in cases}), 7)
        self.assertTrue(all("on" in c.state.values() for c in cases))

    def test_ten_toggles_1024_no_repeat_and_shuffled(self):
        p = project(toggles(10))
        g = scheduler.make_generator(p)
        e1 = take(g, 1024)
        e2 = take(g, 1024)
        self.assertEqual(len({key(c) for c in e1}), 1024)
        self.assertEqual(len({key(c) for c in e2}), 1024)
        self.assertNotEqual([key(c) for c in e1], [key(c) for c in e2])
        self.assertNotEqual([key(c) for c in e1], sorted(key(c) for c in e1))
        self.assertEqual({c.tags for c in e1}, {("epoch:1",)})
        self.assertEqual({c.tags for c in e2}, {("epoch:2",)})
        cov = g.coverage()
        self.assertEqual((cov.states_total, cov.states_visited, cov.epochs), (1024, 1024, 2))
        self.assertIn("1024", cov.guarantee)

    def test_mutex_never_both_and_denominator(self):
        p = project(toggles(3, "x") + [], constraints=[ConstraintDef("mutex", features=("x0", "x1"))])
        sp = StateSpace(p)
        self.assertEqual(sp.size, 6)             # 3 of 4 (A,B) combos x 2
        cases = epoch(p, 12)
        self.assertEqual(len({key(c) for c in cases[:6]}), 6)
        for c in cases:
            self.assertFalse(c.state["x0"] == "on" and c.state["x1"] == "on")
        self.assertEqual(scheduler.make_generator(p).coverage().states_total, 6)

    def test_three_state_feature(self):
        p = project([multistate("m"), *toggles(1)])
        self.assertEqual(StateSpace(p).size, 6)
        cases = epoch(p, 6)
        self.assertEqual({c.state["m"] for c in cases}, {"off", "low", "high"})
        self.assertEqual(len({key(c) for c in cases}), 6)

    def test_mutex_over_multistate_modes(self):
        p = project([multistate("a"), multistate("b")], constraints=[ConstraintDef("mutex", features=("a", "b"))])
        # active = state != states[0]; both inactive 1 + a active(2) + b active(2) = 5
        self.assertEqual(StateSpace(p).size, 5)
        for c in epoch(p, 10):
            self.assertFalse(c.state["a"] != "off" and c.state["b"] != "off")

    def test_requires_and_forbid(self):
        p = project(toggles(3), constraints=[ConstraintDef("requires", features=("f0",), needs=("f1", "f2"))])
        sp = StateSpace(p)
        self.assertEqual(sp.size, 5)             # f0 on needs both: 8 - 3 invalid
        p = project(toggles(2), constraints=[ConstraintDef("forbid", assignment={"f0": "on", "f1": "off"})])
        self.assertEqual(StateSpace(p).size, 3)

    def test_unsatisfiable(self):
        p = project(toggles(1), constraints=[ConstraintDef("forbid", assignment={"f0": "on"}),
                                             ConstraintDef("forbid", assignment={"f0": "off"})])
        self.assertTrue(StateSpace(p).unsatisfiable)
        with self.assertRaises(ValueError):
            scheduler.make_generator(p)
        ns = constraints.compile_constraints(p.features, p.constraints)
        self.assertTrue(ns.unsatisfiable())
        # all-off excluded and a single toggle forbidden on
        p = project(toggles(1), constraints=[ConstraintDef("forbid", assignment={"f0": "on"})],
                    include_all_off=False)
        self.assertTrue(StateSpace(p).unsatisfiable)

    def test_nogood_compile_and_check(self):
        feats = toggles(3)
        ns = constraints.compile_constraints(feats, [ConstraintDef("mutex", features=("f0", "f1", "f2"))])
        self.assertEqual(len(ns), 3)
        self.assertTrue(ns.is_valid({"f0": "on", "f1": "off", "f2": "off"}))
        self.assertFalse(ns.is_valid({"f0": "on", "f1": "on", "f2": "off"}))
        self.assertFalse(ns.unsatisfiable())

    def test_disabled_and_dangerous_are_fixed(self):
        from dataclasses import replace
        f = toggles(3)
        f[1] = replace(f[1], enabled=False)
        f[2] = replace(f[2], dangerous=True)
        p = project(f)
        self.assertEqual(StateSpace(p).size, 2)
        for c in epoch(p, 4):
            self.assertNotIn("f1", c.state)
            self.assertEqual(c.state["f2"], "off")

    def test_large_space_lazy(self):
        p = project(toggles(40))
        sp = StateSpace(p)
        self.assertEqual(sp.size, 2 ** 40)
        perm = sp.permutation(7)
        seen = {perm(i) for i in range(5000)}
        self.assertEqual(len(seen), 5000)
        self.assertEqual(len({tuple(sp.unrank(v)) for v in seen}), 5000)
        g = scheduler.make_generator(p)
        self.assertIsNone(g.coverage().states_total)         # beyond budget -> sampled
        self.assertEqual(len(take(g, 50)), 50)

    def test_constrained_huge(self):
        cons = [ConstraintDef("mutex", features=("f0", "f1", "f2", "f3"))]
        p = project(toggles(50), constraints=cons)
        sp = StateSpace(p)
        self.assertEqual(sp.size, 5 * 2 ** 46)
        for c in epoch(p, 100):
            self.assertLessEqual(sum(c.state[f"f{i}"] == "on" for i in range(4)), 1)


class Permutation(unittest.TestCase):
    def test_bijection_small_and_odd_sizes(self):
        for n in list(range(1, 70)) + [255, 256, 257, 1000]:
            for key_ in (0, 1, 99):
                perm = KeyedPermutation(n, key_)
                self.assertEqual(sorted(perm(i) for i in range(n)), list(range(n)), (n, key_))

    def test_keys_differ_and_bigint(self):
        a = [KeyedPermutation(64, 1)(i) for i in range(64)]
        b = [KeyedPermutation(64, 2)(i) for i in range(64)]
        self.assertNotEqual(a, b)
        perm = KeyedPermutation(2 ** 200 + 12345, 3)
        vals = {perm(i) for i in range(300)}
        self.assertEqual(len(vals), 300)
        self.assertTrue(all(v < 2 ** 200 + 12345 for v in vals))

    def test_permutations_of_8_vary(self):
        orders = {tuple(KeyedPermutation(8, k)(i) for i in range(8)) for k in range(40)}
        self.assertGreater(len(orders), 20)


class Estimate(unittest.TestCase):
    def test_estimate(self):
        p = project(toggles(10), [control("a", maximum=99), control("b", kind="boolean")])
        e = scheduler.estimate(p)
        self.assertEqual(e["valid_states"], 1024)
        self.assertEqual(e["cartesian"], 1024 * 100 * 2)
        self.assertIn("2^10 states", e["text"])
        p = project(toggles(10), [control("f", kind="float", minimum=0.0, maximum=1.0)])
        e = scheduler.estimate(p)
        self.assertIsNone(e["cartesian"])
        self.assertIn("continuous", e["text"])
        p = project(toggles(3), [control("a", maximum=10 ** 6)] * 2)
        self.assertIn("e+12", scheduler.estimate(p)["text"])

    def test_unknown_strategy(self):
        with self.assertRaises(ValueError):
            scheduler.make_generator(project(toggles(2), strategy="nope"))


if __name__ == "__main__":
    unittest.main()
