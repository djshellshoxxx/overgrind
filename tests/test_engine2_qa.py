"""QA-BACKEND: determinism across PYTHONHASHSEED and resolver type-safety."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = r'''
import hashlib, json
from overgrind.types import *
from overgrind import scheduler
feats=tuple(FeatureDef(id=n, kind=("multistate" if i%3==0 else "toggle"), states=(("off","lo","hi") if i%3==0 else ("off","on"))) for i,n in enumerate(["alpha","beta","gamma","delta","eps","zeta","eta"]))
ctrls=(ControlDef("w","integer",Selector(auto_id="w"),Domain("integer",0,100)),
 ControlDef("t","text",Selector(auto_id="t"),Domain("text",max_length=8)),
 ControlDef("m","choice",Selector(auto_id="m"),Domain("choice",options=("a","b","c")),scope=("alpha",)))
cons=(ConstraintDef("mutex",features=("beta","gamma","delta")),ConstraintDef("requires",features=("eps",),needs=("zeta",)))
out=[]
for s in ["state_cover","ordered_pairs","twise","boundary","adaptive","chaos"]:
    p=Project("x",TargetRef(),feats,ctrls,cons,RunConfig(strategy=s,seed=5))
    cs=scheduler.preview(p,200)
    out.append(hashlib.sha256(json.dumps([[c.seq,sorted(c.state.items()),sorted(c.values.items()),c.order,c.tags] for c in cs],default=str).encode()).hexdigest()[:10])
print(",".join(out))
'''


class HashSeedIndependence(unittest.TestCase):
    def test_streams_do_not_depend_on_pythonhashseed(self):
        res = set()
        for seed in ("0", "1", "4242"):
            r = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, cwd=ROOT,
                               env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(ROOT)), timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            res.add(r.stdout.strip())
        self.assertEqual(len(res), 1, res)


class ResolverTypeSafety(unittest.TestCase):
    def test_same_auto_id_different_control_type_is_not_bound(self):
        from overgrind.adapters import resolve as R
        from overgrind.types import ElementInfo, Selector
        els = [ElementInfo(Selector(auto_id="btnApply", control_type="CheckBox"), "Apply", "CheckBox")]
        self.assertFalse(R.resolve(Selector(auto_id="btnApply", control_type="Button"), els).found)
        self.assertTrue(R.resolve(Selector(auto_id="btnApply", control_type="CheckBox"), els).found)
        self.assertTrue(R.resolve(Selector(auto_id="btnApply"), els).found)


if __name__ == "__main__":
    unittest.main()


class TwentyToggles(unittest.TestCase):
    def test_2_pow_20_states_are_enumerated_without_repeats(self):
        from test_engine2_common import key, project, take, toggles
        from overgrind import scheduler
        g = scheduler.make_generator(project(toggles(20), seed=1))
        cv0 = g.coverage()
        self.assertEqual(cv0.states_total, 2 ** 20)           # was 'not enumerable' (budget 1,000,000 < 2^20)
        cs = take(g, 10_000)
        self.assertEqual(len({key(c) for c in cs}), 10_000)


class TwiseSpecAcceptance(unittest.TestCase):
    """B2 acceptance 1, 2, 11, 13 (the numeric ones), asserted rather than eyeballed."""

    @staticmethod
    def gen(n, v, t, seed):
        from test_engine2_common import multistate, project, toggles
        from overgrind import scheduler
        feats = [multistate(f"f{i}", [f"s{k}" for k in range(v)]) for i in range(n)] if v > 2 else toggles(n)
        return scheduler.make_generator(project(feats, strategy="twise", seed=seed, strategy_options={"t": t}))

    def cases_to_full(self, n, v, t, seed, total):
        g, k = self.gen(n, v, t, seed), 0
        while g.progress()["covered"] < total and k < 1000:
            next(g)
            k += 1
        return k, g.progress()

    def test_pairwise_10x3_within_greedy_bound(self):
        for seed in range(8):
            k, pr = self.cases_to_full(10, 3, 2, seed, 405)
            self.assertEqual(pr["universe"], 405)
            self.assertLessEqual(k, 52, seed)

    def test_threeway_8x2_within_greedy_bound(self):
        k, pr = self.cases_to_full(8, 2, 3, 1, 448)
        self.assertEqual(pr["universe"], 448)
        self.assertLessEqual(k, 47)

    def test_every_prefix_matches_brute_force_coverage(self):
        import itertools
        g, seen = self.gen(4, 2, 2, 3), set()
        for _ in range(30):
            c = next(g)
            st = [c.state[f"f{j}"] for j in range(4)]
            for a, b in itertools.combinations(range(4), 2):
                seen.add((a, b, st[a], st[b]))
            pr = g.progress()
            if pr["sweeps_done"] == 0:
                self.assertEqual(pr["covered"], len(seen))
