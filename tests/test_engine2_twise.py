import itertools
import json
import math
import unittest

from overgrind import scheduler
from overgrind.strategies.twise import elementary
from overgrind.types import ConstraintDef
from test_engine2_common import control, multistate, project, take, toggles


def fixture(constraints=(), **opts):
    """6 features (4 toggles, one 3-state, one more toggle) + choice(4) + boolean + integer controls."""
    feats = toggles(4) + [multistate("m"), *toggles(1, "z")]
    ctrls = [control("ch", kind="choice", options=("a", "b", "c", "d")), control("bo", kind="boolean"),
             control("n", maximum=1000)]
    return project(feats, ctrls, constraints, strategy="twise", strategy_options=opts, seed=3)


def run_to_first_sweep(g, limit=5000):
    n = 0
    for c in g:
        n += 1
        if g.sweeps_done >= 1 or n >= limit:
            break
    return n


class Universe(unittest.TestCase):
    def test_elementary_matches_brute_force(self):
        for levels in ([2, 2, 2], [2, 3, 4, 5], [3, 3, 2, 2, 2]):
            for t in (1, 2, 3, 4):
                brute = sum(math.prod(c) for c in itertools.combinations(levels, t))
                self.assertEqual(elementary(levels, t)[t], brute)

    def test_universe_reported(self):
        g = scheduler.make_generator(fixture())
        lv = [2, 2, 2, 2, 3, 2] + [4, 2, 3 + 2]          # integer buckets: 3 ranges + min,max (+ zero?) checked below
        self.assertEqual(g.levels[:6], [2, 2, 2, 2, 3, 2])
        self.assertEqual(g.universe, elementary(g.levels, 2)[2])


class Pairwise(unittest.TestCase):
    def test_full_pair_coverage_fewer_than_product(self):
        p = fixture()
        g = scheduler.make_generator(p)
        sp_states = 2 ** 5 * 3
        product = sp_states * 4 * 2 * len(g.plan.buckets()[2])
        rows = []
        for c in g:
            rows.append(c)
            if g.sweeps_done >= 1 or len(rows) > 3000:
                break
        self.assertEqual(g.sweeps_done, 1)
        n_first = len(rows) - 1                          # last case belongs to sweep 2
        self.assertLess(n_first, product // 10)
        self.assertLess(n_first, 200)
        # sweep-1 progress reached 100% exactly; verify independently on feature + choice + bool factors
        first = rows[:n_first]
        names = ["f0", "f1", "f2", "f3", "m", "z0"]
        seen = set()
        for c in first:
            lab = [(n, c.state[n]) for n in names] + [("ch", c.values["ch"]), ("bo", c.values["bo"])]
            for a, b in itertools.combinations(lab, 2):
                seen.add((a, b))
        levels = {n: ("off", "on") for n in names}
        levels["m"] = ("off", "low", "high")
        levels["ch"] = ("a", "b", "c", "d")
        levels["bo"] = (False, True)
        order = names + ["ch", "bo"]
        want = {((a, x), (b, y)) for a, b in itertools.combinations(order, 2)
                for x in levels[a] for y in levels[b]}
        self.assertEqual(want - seen, set())
        cov = g.coverage()
        self.assertEqual(cov.tway_t, 2)
        self.assertEqual(cov.epochs, 1)

    def test_progress_exact_and_monotone(self):
        g = scheduler.make_generator(fixture())
        last = -1
        for i, c in enumerate(g):
            pr = g.progress()
            self.assertGreaterEqual(pr["covered"], last if g.sweeps_done == 0 else 0)
            last = pr["covered"]
            self.assertLessEqual(pr["covered"], pr["total"])
            if g.sweeps_done:
                break
        cov = g.coverage()
        self.assertEqual(g.universe, g.progress()["total"] + g.infeasible)
        self.assertIn("covered 1 time", cov.guarantee)

    def test_never_stops_and_new_values(self):
        p = fixture()
        g = scheduler.make_generator(p)
        n = 0
        vals = {}
        for c in g:
            n += 1
            vals.setdefault(g.sweep, []).append(c.values["n"])
            if g.sweeps_done >= 3 or n > 6000:
                break
        self.assertGreaterEqual(g.sweeps_done, 3)
        self.assertEqual(g.coverage().epochs, g.sweeps_done)
        self.assertNotEqual(vals[0][:20], vals[1][:20])       # fresh random values inside buckets

    def test_values_inside_domain_and_bucket(self):
        g = scheduler.make_generator(fixture())
        for c in take(g, 300):
            self.assertTrue(0 <= c.values["n"] <= 1000)
            self.assertIn(c.values["ch"], "abcd")


class Infeasible(unittest.TestCase):
    cons = [ConstraintDef("mutex", features=("f0", "f1"))]

    def test_exact_infeasible_under_mutex(self):
        clean = scheduler.make_generator(fixture())
        g = scheduler.make_generator(fixture(self.cons))
        self.assertEqual(g.infeasible, 1)                    # only (f0=on, f1=on)
        self.assertEqual(g.universe, clean.universe)
        self.assertEqual(g.progress()["total"], g.universe - 1)
        run_to_first_sweep(g)
        pr = g.progress()
        self.assertEqual(g.sweeps_done, 1)
        self.assertIn("1 infeasible tuple excluded", g.coverage().guarantee)
        self.assertTrue(g.progress()["infeasible_exact"])

    def test_no_invalid_state_and_mutex_triples(self):
        cons = [ConstraintDef("mutex", features=("f0", "f1")), ConstraintDef("requires", features=("f2",), needs=("f3",))]
        g = scheduler.make_generator(fixture(cons, t=3))
        # infeasible: 3-tuples containing (f0,f1 on) or (f2 on, f3 off): count them by brute force
        for c in take(g, 400):
            self.assertFalse(c.state["f0"] == "on" and c.state["f1"] == "on")
            self.assertFalse(c.state["f2"] == "on" and c.state["f3"] == "off")
        g = scheduler.make_generator(fixture(cons, t=3))
        n = run_to_first_sweep(g, 20000)
        self.assertEqual(g.sweeps_done, 1, n)
        levels = g.levels
        names = ["f0", "f1", "f2", "f3", "m", "z0"]
        bad = 0
        for combo in itertools.combinations(range(len(levels)), 3):
            for lv in itertools.product(*[range(levels[p]) for p in combo]):
                a = dict(zip(combo, lv))
                if (a.get(0) == 1 and a.get(1) == 1) or (a.get(2) == 1 and a.get(3) == 0):
                    bad += 1
        self.assertEqual(g.infeasible, bad)

    def test_indirect_infeasibility_and_lazy_discovery(self):
        # a requires b, b mutex c: (a on, c on) is infeasible although no single nogood says so
        cons = [ConstraintDef("requires", features=("f0",), needs=("f1",)),
                ConstraintDef("mutex", features=("f1", "f2"))]
        g = scheduler.make_generator(fixture(cons))
        self.assertEqual(g.infeasible, 3)           # (f0,f1:off) (f1,f2 on) (f0 on,f2 on)
        lazy = scheduler.make_generator(fixture(cons, scan_cap=0))
        self.assertEqual(lazy.infeasible, 0)
        self.assertIn("lower bound", (run_lazy(lazy), lazy.coverage().guarantee)[1])
        self.assertEqual(lazy.infeasible, 3)

    def test_all_off_exclusion(self):
        p = project(toggles(2), strategy="twise", include_all_off=False)
        g = scheduler.make_generator(p)
        # 2 toggles: (f0 off, f1 off) can only be completed to the excluded all-off state -> infeasible
        self.assertEqual(g.infeasible, 1)
        self.assertEqual(g.universe, 4)
        self.assertEqual(g.progress()["total"], 3)


def run_lazy(g):
    run_to_first_sweep(g, 20000)
    return g.sweeps_done


class Modes(unittest.TestCase):
    def test_t_range_and_smoke(self):
        for t in (1, 2, 3, 4):
            g = scheduler.make_generator(fixture(t=t))
            self.assertEqual(g.coverage().tway_t, t)
            n = run_to_first_sweep(g, 60000)
            self.assertEqual(g.sweeps_done, 1, (t, n))
        with self.assertRaises(ValueError):
            scheduler.make_generator(fixture(t=5))

    def test_t1_is_every_level_once(self):
        g = scheduler.make_generator(fixture(t=1))
        n = run_to_first_sweep(g)
        self.assertLessEqual(n, 12)                   # max levels per factor bounds a 1-way cover

    def test_sampled_mode_is_honest(self):
        g = scheduler.make_generator(fixture(tuple_cap=100))
        cases = take(g, 50)
        cov = g.coverage()
        self.assertEqual(g.mode, "sampled")
        self.assertIn("SAMPLED", cov.guarantee)
        self.assertEqual(cov.tway_covered, 0)
        self.assertEqual(len(cases), 50)
        self.assertEqual(g.sweeps_done, 0)

    def test_huge_universe_degrades_not_explodes(self):
        p = project(toggles(120), [control(f"c{i}") for i in range(40)], strategy="twise",
                    strategy_options={"t": 3})
        g = scheduler.make_generator(p)
        self.assertEqual(g.mode, "sampled")
        self.assertEqual(g.universe, elementary(g.levels, 3)[3])
        self.assertEqual(len(take(g, 20)), 20)

    def test_checkpoint_restores_bitset(self):
        p = fixture(t=2)
        g = scheduler.make_generator(p)
        take(g, 25)
        ck = json.loads(json.dumps(g.checkpoint()))
        rest = take(g, 60)
        g2 = scheduler.make_generator(p, checkpoint=ck)
        self.assertEqual(take(g2, 60), rest)
        self.assertEqual(g2.progress(), g.progress())

    def test_checkpoint_with_constraints_lazy(self):
        cons = [ConstraintDef("requires", features=("f0",), needs=("f1",)), ConstraintDef("mutex", features=("f1", "f2"))]
        p = fixture(cons, scan_cap=0)
        g = scheduler.make_generator(p)
        take(g, 40)
        ck = json.loads(json.dumps(g.checkpoint()))
        rest = take(g, 40)
        g2 = scheduler.make_generator(p, checkpoint=ck)
        self.assertEqual(take(g2, 40), rest)

    def test_controls_only_and_features_only(self):
        p = project([], [control("a"), control("b", kind="boolean")], strategy="twise")
        g = scheduler.make_generator(p)
        run_to_first_sweep(g)
        self.assertEqual(g.sweeps_done, 1)
        p = project(toggles(5), strategy="twise")
        g = scheduler.make_generator(p)
        n = run_to_first_sweep(g)
        self.assertLess(n, 32)
        p = project([], strategy="twise")
        self.assertEqual(len(take(scheduler.make_generator(p), 3)), 3)


if __name__ == "__main__":
    unittest.main()
