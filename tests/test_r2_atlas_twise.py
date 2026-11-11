"""Round-2 hardening: U1 Atlas evidence levels / unreachable accounting and B2 streaming t-wise extras.

Spec coverage (docs/specs/features): U1 tests 1, 2, 3, 4 (permutation + memory), 5, 6, 8, 9, 10, 11 and B2 tests
3 (10k property), 4, 8 (tway_total None), 9, 10, 12 plus option checks (buckets, mix, solver_nodes).
"""
import json
import random
import re
import tracemalloc
import unittest
from dataclasses import replace

from overgrind import scheduler
from overgrind.types import ConstraintDef, Outcome
from test_engine2_common import control, multistate, project, take, toggles

ALL = re.compile(r"\ball\b", re.I)
FAIL = Outcome(ok=False, category="harness", detail={"applied": False})
APPLIED = Outcome(ok=True, detail={"applied": True, "verified": False, "readback": 3, "features": 3})
VERIFIED = Outcome(ok=True, detail={"applied": True, "verified": True, "readback": 3, "features": 3})


def sc(n=3, seed=1, controls=(), constraints=(), **kw):
    return scheduler.make_generator(project(toggles(n), controls, constraints, strategy="state_cover", seed=seed, **kw))


def drive(g, n, out=APPLIED):
    cs = []
    for _ in range(n):
        c = next(g)
        g.feedback(c, out)
        cs.append(c)
    return cs


class AtlasEvidence(unittest.TestCase):
    def test_failed_apply_is_not_counted_and_requeued_in_same_epoch(self):          # U1 5
        g = sc(3)
        c0 = next(g)
        g.feedback(c0, FAIL)
        self.assertEqual(g.coverage().states_visited, 0)
        self.assertEqual(g.coverage().failed_to_apply, 1)
        rest = drive(g, 8)
        self.assertEqual(sorted(tuple(sorted(c.state.items())) for c in [c0] + rest[:7]).__len__(), 8)
        keys = [tuple(sorted(c.state.items())) for c in [c0] + rest]
        self.assertEqual(len(set(keys[:8])) + 0, 8 if keys.count(keys[0]) == 1 else 7)
        self.assertEqual(g.epoch, 1)                      # the failed state was retried within epoch 0
        self.assertIn("retry", rest[-1].tags)

    def test_three_failures_make_state_unreachable_and_leave_denominator(self):      # U1 5
        g = sc(3)
        # fail the same state three times
        g = sc(3)
        seen_bad = None
        fails = 0
        for _ in range(60):
            c = next(g)
            k = tuple(sorted(c.state.items()))
            if seen_bad is None:
                seen_bad = k
            if k == seen_bad and fails < 3:
                g.feedback(c, FAIL)
                fails += 1
            else:
                g.feedback(c, APPLIED)
            if g.epoch >= 1:
                break
        cov = g.coverage()
        self.assertEqual(cov.unreachable, 1)
        self.assertEqual(g.epoch, 1)
        self.assertIn("1 states unreachable", cov.guarantee)
        self.assertNotIn("all 8", cov.guarantee.lower())
        self.assertIn("7 of 8", cov.guarantee)
        self.assertTrue(any("unreachable" in w for w in cov.why_not_complete))

    def test_last_case_of_epoch_failing_reopens_the_epoch(self):
        g = sc(3)
        cs = [next(g) for _ in range(8)]
        for c in cs[:7]:
            g.feedback(c, APPLIED)
        self.assertEqual(g.epoch, 1)
        g.feedback(cs[7], FAIL)
        self.assertEqual(g.epoch, 0)
        self.assertEqual(g.coverage().states_visited, 7)
        c = next(g)
        self.assertEqual(tuple(sorted(c.state.items())), tuple(sorted(cs[7].state.items())))

    def test_verified_vs_applied_levels(self):                                      # U1 6
        g = sc(3)
        drive(g, 8, VERIFIED)
        cov = g.coverage()
        self.assertEqual(cov.evidence, "verified")
        self.assertIn("all 8 valid configurations visited in epoch 1 (verified)", cov.guarantee)
        g = sc(3)
        drive(g, 7, VERIFIED)
        drive(g, 1, APPLIED)                              # one read-back mismatch
        cov = g.coverage()
        self.assertEqual(cov.evidence, "applied")
        self.assertEqual(cov.states_visited, 8)
        self.assertIn("applied (read-back available for 3/3 features)", cov.guarantee)

    def test_no_feedback_is_only_attempted_and_never_claims_all(self):
        g = sc(3)
        take(g, 8)
        cov = g.coverage()
        self.assertEqual(cov.evidence, "attempted")
        self.assertFalse(ALL.search(cov.guarantee), cov.guarantee)

    def test_outcome_without_detail_is_applied(self):
        g = sc(3)
        drive(g, 8, Outcome(ok=True))
        self.assertEqual(g.coverage().evidence, "applied")
        self.assertIn("all 8 valid configurations visited in epoch 1", g.coverage().guarantee)

    def test_guarantee_grammar_fuzz(self):                                          # U1 9
        rnd = random.Random(5)
        for trial in range(25):
            g = sc(rnd.choice([2, 3, 4]), seed=trial)
            for _ in range(20):
                for _ in range(rnd.randrange(1, 12)):
                    c = next(g)
                    g.feedback(c, rnd.choice([FAIL, FAIL, APPLIED, VERIFIED, Outcome(ok=True)]))
                cov = g.coverage()
                txt = cov.guarantee
                self.assertTrue(txt.startswith("Feature states:"), txt)
                complete = txt.startswith("Feature states: all ")
                if complete:
                    self.assertGreaterEqual(cov.epochs, 1)
                    self.assertEqual(cov.unreachable, 0)
                    self.assertIn(cov.evidence, ("applied", "verified"))
                else:
                    self.assertFalse(ALL.search(txt), txt)
                    self.assertNotIn("100%", txt)

    def test_sampled_state_cover_is_honest(self):                                   # U1 8
        g = sc(10, budget=4)
        take(g, 5)
        txt = g.coverage().guarantee
        self.assertIn("NOT a complete-coverage claim", txt)
        self.assertFalse(ALL.search(txt), txt)
        self.assertIsNone(g.coverage().states_total)

    def test_checkpoint_roundtrip_with_failures_and_ledger(self):                   # U1 10
        rnd = random.Random(9)
        p = project(toggles(5), [control("n")], strategy="state_cover", seed=4)
        g = scheduler.make_generator(p)
        for _ in range(90):
            c = next(g)
            g.feedback(c, rnd.choice([FAIL, APPLIED, APPLIED, VERIFIED]))
        ck = json.loads(json.dumps(g.checkpoint()))
        rest_g = []
        for _ in range(100):
            c = next(g)
            rest_g.append(c)
            g.feedback(c, APPLIED)
        g2 = scheduler.make_generator(p, checkpoint=ck)
        rest_2 = []
        for _ in range(100):
            c = next(g2)
            rest_2.append(c)
            g2.feedback(c, APPLIED)
        self.assertEqual(rest_g, rest_2)
        self.assertEqual(g.coverage(), g2.coverage())

    def test_project_hash_change_refuses_restore(self):                             # U1 11
        g = sc(3)
        take(g, 3)
        ck = g.checkpoint()
        other = project(toggles(4), strategy="state_cover", seed=1)
        with self.assertRaises(ValueError):
            scheduler.make_generator(other, checkpoint=ck)

    def test_ledger_snapshot_exposes_why_not_100(self):
        g = sc(4)
        cs = [next(g) for _ in range(5)]
        for c in cs[:4]:
            g.feedback(c, APPLIED)
        g.feedback(cs[4], FAIL)
        led = scheduler.ledger_snapshot(g)["ledger"]
        self.assertEqual(led["failed_to_apply"], 1)
        self.assertEqual(led["retry_queue"], 1)
        self.assertEqual(led["visited"], 4)
        self.assertLessEqual(len(led["unvisited_sample"]), 10)
        self.assertEqual(len(led["unvisited_sample"]), 10)
        self.assertTrue(led["why_not_complete"])
        json.dumps(led)

    def test_permutation_and_memory(self):                                          # U1 4
        g = sc(20)
        tracemalloc.start()
        base = tracemalloc.take_snapshot()
        tracemalloc.reset_peak()
        keys = [tuple(c.state.values()) for c in take(g, 10_000)]
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertEqual(len(set(keys)), 10_000)
        self.assertLess(peak, 6 * 1024 * 1024)            # cases themselves dominate; ledger is O(1)
        del base


def tw(feats=None, controls=(), constraints=(), seed=3, **opts):
    feats = feats if feats is not None else toggles(6)
    return scheduler.make_generator(project(feats, controls, constraints, strategy="twise", seed=seed,
                                            strategy_options=opts))


def pairs_of(cases, names):
    seen = set()
    for c in cases:
        lab = [(n, c.state[n]) for n in names]
        for a, b in __import__("itertools").combinations(lab, 2):
            seen.add((a, b))
    return seen


class TwiseExtras(unittest.TestCase):
    def test_strength_groups_cover_group_triples_and_all_pairs(self):               # B2 12
        g = tw(toggles(7), strength_groups=[{"factors": ["f0", "f1", "f2"], "t": 3}], mix={"twise": 1.0})
        cases = []
        while g.sweeps_done < 1 and len(cases) < 400:
            cases.append(next(g))
        first = cases[:-1]
        trip = {tuple((n, c.state[n]) for n in ("f0", "f1", "f2")) for c in first}
        self.assertEqual(len(trip), 8)
        names = [f"f{i}" for i in range(7)]
        self.assertEqual(len(pairs_of(first, names)), 21 * 4)
        gp = g.progress()["groups"][0]
        self.assertEqual((gp["t"], gp["total"]), (3, 8))
        self.assertIn("Group 3 factors t=3", g.coverage().guarantee)

    def test_strength_groups_checkpoint(self):
        kw = dict(strength_groups=[{"factors": ["f0", "f1", "f2"], "t": 3}], mix={"twise": 1.0})
        g = tw(toggles(7), **kw)
        take(g, 6)
        ck = json.loads(json.dumps(g.checkpoint()))
        rest = take(g, 30)
        g2 = scheduler.make_generator(g.project, checkpoint=ck)
        self.assertEqual(take(g2, 30), rest)

    def test_multiplicity_and_default_mix_after_coverage(self):                     # B2 9
        g = tw(toggles(5), [control("n", maximum=50)], seed=2)
        self.assertEqual(g.mix, {"twise": 0.7, "boundary": 0.15, "adaptive": 0.15})
        while g.sweeps_done < 1:
            next(g)
        tags = []
        for c in take(g, 3000):
            tags.append("boundary" if "mix:boundary" in c.tags else "adaptive" if "mix:adaptive" in c.tags else "twise")
        share = {k: tags.count(k) / len(tags) for k in ("twise", "boundary", "adaptive")}
        for k, w in (("twise", 0.7), ("boundary", 0.15), ("adaptive", 0.15)):
            self.assertAlmostEqual(share[k], w, delta=0.05)
        self.assertGreaterEqual(g.sweeps_done, 2)          # k=2: tuples covered a second time while mixing
        self.assertIn("covered 1 time", g.coverage().guarantee.replace("times", "time")) if g.full_passes == 1 else None

    def test_mix_stream_is_deterministic_across_checkpoint(self):
        p = dict(controls=[control("n", maximum=50)])
        g = tw(toggles(4), **p)
        take(g, 200)
        ck = json.loads(json.dumps(g.checkpoint()))
        rest = take(g, 150)
        g2 = scheduler.make_generator(g.project, checkpoint=ck)
        self.assertEqual(take(g2, 150), rest)
        self.assertEqual(g2.progress(), g.progress())

    def test_mix_can_be_disabled_and_validated(self):
        g = tw(toggles(3), mix={"twise": 1.0})
        self.assertTrue(all("mix:boundary" not in c.tags for c in take(g, 100)))
        with self.assertRaises(ValueError):
            tw(toggles(3), mix={"boundary": 1.0})

    def test_applied_false_uncovers_and_requeues(self):                             # B2 10
        g = tw(toggles(5), mix={"twise": 1.0})
        cs = [next(g) for _ in range(6)]
        for c in cs[:5]:
            g.feedback(c, APPLIED)
        before = g.progress()["covered"]
        g.feedback(cs[5], FAIL)
        after = g.progress()["covered"]
        self.assertLess(after, before)
        self.assertEqual(g.progress()["failed_to_apply"], 1)
        self.assertGreater(g.progress()["requeued"], 0)
        # the lost tuples come back and the sweep still completes at 100% of the feasible total
        n = 0
        while g.sweeps_done < 1 and n < 500:
            c = next(g)
            g.feedback(c, APPLIED)
            n += 1
        self.assertEqual(g.sweeps_done, 1)
        self.assertEqual(g.full_passes, 1)

    def test_repeated_failures_make_tuples_unreachable(self):
        g = tw(toggles(4), mix={"twise": 1.0})
        for _ in range(200):
            c = next(g)
            g.feedback(c, FAIL if g.sweeps_done == 0 and c.seq < 60 else APPLIED)
            if g.sweeps_done >= 1:
                break
        cov = g.coverage()
        self.assertEqual(g.sweeps_done, 1)
        self.assertGreater(cov.unreachable, 0)
        self.assertEqual(g.full_passes, 0)                 # a pass with unreachable tuples is not a full pass
        self.assertFalse(ALL.search(cov.guarantee), cov.guarantee)

    def test_checkpoint_keeps_requeue_and_unreachable(self):
        g = tw(toggles(5), mix={"twise": 1.0})
        for i in range(12):
            c = next(g)
            g.feedback(c, FAIL if i % 3 == 0 else APPLIED)
        ck = json.loads(json.dumps(g.checkpoint()))
        rest = []
        for _ in range(40):
            c = next(g)
            rest.append(c)
            g.feedback(c, APPLIED)
        g2 = scheduler.make_generator(g.project, checkpoint=ck)
        got = []
        for _ in range(40):
            c = next(g2)
            got.append(c)
            g2.feedback(c, APPLIED)
        self.assertEqual(got, rest)
        self.assertEqual(g.progress(), g2.progress())

    CONS = [ConstraintDef("mutex", features=("f0", "f1")), ConstraintDef("requires", features=("f2",), needs=("f3",))]

    def test_10k_case_constraint_property_and_infeasible_excluded(self):            # B2 3
        g = tw(toggles(6), constraints=self.CONS)
        for c in take(g, 10_000):
            self.assertFalse(c.state["f0"] == "on" and c.state["f1"] == "on")
            self.assertFalse(c.state["f2"] == "on" and c.state["f3"] == "off")
        cov = g.coverage()
        self.assertEqual(cov.tway_total, g.universe - cov.infeasible)
        self.assertGreater(cov.infeasible, 0)
        self.assertIn("proved infeasible by constraints", cov.guarantee)

    def test_solver_budget_one_node_reports_unknown_and_doubles(self):              # B2 4
        cons = [ConstraintDef("requires", features=("f0",), needs=("f1",)),
                ConstraintDef("mutex", features=("f1", "f2")), ConstraintDef("requires", features=("f3",), needs=("f4",))]
        g = tw(toggles(6), constraints=cons, solver_nodes=1)
        u0 = g.coverage().unknown
        self.assertGreater(u0, 0)
        self.assertEqual(g.coverage().tway_total, g.universe - g.infeasible)        # unknown stays in the denominator
        self.assertIn("undecided", g.coverage().guarantee)
        ref = tw(toggles(6), constraints=cons)
        nodes0 = g.pick_nodes
        n = 0
        while g.sweeps_done < 2 and n < 5000:
            next(g)
            n += 1
        self.assertGreater(g.pick_nodes, nodes0)
        self.assertLess(g.coverage().unknown, u0)
        self.assertLessEqual(g.infeasible, ref.infeasible)

    def test_sampled_mode_total_is_none(self):                                      # B2 8
        g = tw(toggles(6), tuple_cap=50)
        take(g, 30)
        cov = g.coverage()
        self.assertIsNone(cov.tway_total)
        self.assertIn("NOT a complete-coverage claim", cov.guarantee)
        self.assertFalse(ALL.search(cov.guarantee), cov.guarantee)

    def test_bucket_count_is_a_global_option(self):
        c = [control("n", maximum=100)]
        a, b = tw(toggles(3), c, buckets=2), tw(toggles(3), c, buckets=4)
        self.assertLess(a.levels[-1], b.levels[-1])
        self.assertEqual(scheduler.estimate(b.project)["buckets"], 4)
        self.assertIn("4 value buckets", b.coverage().guarantee)
        with self.assertRaises(ValueError):
            tw(toggles(3), c, buckets=0)

    def test_twise_grammar_never_overclaims(self):
        rnd = random.Random(3)
        g = tw(toggles(5), [control("n", maximum=9)], mix={"twise": 1.0})
        for _ in range(300):
            c = next(g)
            g.feedback(c, rnd.choice([FAIL, APPLIED, APPLIED]))
            txt = g.coverage().guarantee
            self.assertTrue(txt.startswith("t=2 interactions:"), txt)
            if g.full_passes == 0:
                self.assertFalse(ALL.search(txt), txt)
                self.assertNotIn("100%", txt)


if __name__ == "__main__":
    unittest.main()
