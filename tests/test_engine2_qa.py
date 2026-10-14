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
