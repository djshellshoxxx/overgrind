import math
import unittest
from random import Random

from overgrind import domain as dm
from overgrind.types import ControlDef, Domain, Selector

INT = Domain("integer", minimum=1, maximum=16)
INTS = Domain("integer", minimum=0, maximum=100, step=10)
FLT = Domain("float", minimum=0, maximum=100)
FLTS = Domain("float", minimum=0.0, maximum=1.0, step=0.25)
HUGE = Domain("float", minimum=-1e308, maximum=1e308)
NEG = Domain("float", minimum=-5.5, maximum=7.25)
CH = Domain("choice", options=("Off", "2x", "4x"))
BOOL = Domain("boolean")
TXT = Domain("text", max_length=20)
TXTA = Domain("text", max_length=5, alphabet="ab")
ALL = [INT, INTS, FLT, FLTS, HUGE, NEG, CH, BOOL, TXT, TXTA,
       Domain("integer", minimum=3, maximum=3), Domain("text", max_length=0),
       Domain("integer", minimum=-10, maximum=10, step=3)]


class DomainTests(unittest.TestCase):
    def test_sample_always_contained(self):
        for d in ALL:
            rng = Random(7)
            for _ in range(400):
                v = dm.sample(d, rng)
                self.assertTrue(dm.contains(d, v), (d, v))
                if isinstance(v, float):
                    self.assertTrue(math.isfinite(v))

    def test_seeded_repeatability(self):
        for d in ALL:
            a = [dm.sample(d, Random(3)) for _ in range(1)]
            r1, r2 = Random(11), Random(11)
            self.assertEqual([dm.sample(d, r1) for _ in range(50)], [dm.sample(d, r2) for _ in range(50)])
            self.assertEqual(a, [dm.sample(d, Random(3))])

    def test_step_snapping(self):
        rng = Random(1)
        for _ in range(200):
            self.assertEqual(dm.sample(INTS, rng) % 10, 0)
            self.assertIn(dm.sample(FLTS, rng), (0.0, 0.25, 0.5, 0.75, 1.0))

    def test_boundaries(self):
        self.assertEqual(dm.boundary_values(INT), [1, 2, 8, 15, 16])
        self.assertEqual(dm.boundary_values(INTS), [0, 10, 50, 90, 100])
        self.assertEqual(dm.boundary_values(BOOL), [False, True])
        self.assertEqual(dm.boundary_values(CH), ["Off", "2x", "4x"])
        self.assertIn(0.0, dm.boundary_values(NEG))
        t = dm.boundary_values(TXT)
        self.assertIn("", t)
        self.assertIn(20, [len(x) for x in t])
        for d in ALL:
            for v in dm.boundary_values(d):
                self.assertTrue(dm.contains(d, v), (d, v))

    def test_buckets_cover_domain(self):
        for d in ALL:
            for n in (1, 3, 5):
                bs = dm.buckets(d, n)
                self.assertTrue(bs)
                rng = Random(5)
                for _ in range(200):
                    v = dm.sample(d, rng)
                    key = len(v) if d.kind == "text" else v
                    self.assertTrue(any(lo <= key <= hi for _, lo, hi in bs), (d, n, v, bs))
                for b in bs:   # a value can be drawn from every bucket
                    v = dm.sample_bucket(d, b, rng)
                    self.assertTrue(dm.contains(d, v), (d, b, v))
                    key = len(v) if d.kind == "text" else v
                    self.assertTrue(b[1] <= key <= b[2], (d, b, v))

    def test_bucket_shapes(self):
        self.assertEqual([b[0] for b in dm.buckets(CH, 2)], ["Off", "2x", "4x"])
        self.assertEqual(len(dm.buckets(BOOL, 9)), 2)
        self.assertEqual([b[0] for b in dm.buckets(TXT, 2)], ["empty", "short", "max"])
        labels = [b[0] for b in dm.buckets(NEG, 4)]
        self.assertIn("min", labels)
        self.assertIn("max", labels)
        self.assertIn("zero", labels)
        self.assertEqual(len([b for b in dm.buckets(FLT, 4) if ".." in b[0]]), 4)
        self.assertEqual(len(dm.buckets(Domain("integer", minimum=1, maximum=2), 10)), 2)
        with self.assertRaises(ValueError):
            dm.buckets(INT, 0)

    def test_contains(self):
        self.assertTrue(dm.contains(INT, 16))
        self.assertFalse(dm.contains(INT, 17))
        self.assertFalse(dm.contains(INT, True))
        self.assertFalse(dm.contains(INT, 2.5))
        self.assertFalse(dm.contains(FLT, float("nan")))
        self.assertFalse(dm.contains(FLT, float("inf")))
        self.assertFalse(dm.contains(INTS, 15))
        self.assertTrue(dm.contains(INTS, 15, strict_step=False))
        self.assertTrue(dm.contains(FLTS, 0.75))
        self.assertFalse(dm.contains(CH, "8x"))
        self.assertFalse(dm.contains(TXT, "x" * 21))
        self.assertFalse(dm.contains(TXTA, "abc"))
        self.assertFalse(dm.contains(BOOL, 1))

    def test_clamp(self):
        self.assertEqual(dm.clamp(INT, 99), 16)
        self.assertEqual(dm.clamp(INT, -4), 1)
        self.assertEqual(dm.clamp(INTS, 47), 50)
        self.assertEqual(dm.clamp(INTS, 1e9), 100)
        self.assertEqual(dm.clamp(FLT, float("nan")), 0.0)
        self.assertEqual(dm.clamp(FLT, float("inf")), 100.0)
        self.assertEqual(dm.clamp(FLTS, 0.6), 0.5)
        self.assertEqual(dm.clamp(CH, "zzz"), "Off")
        self.assertEqual(dm.clamp(TXT, "x" * 50), "x" * 20)
        self.assertEqual(dm.clamp(TXTA, "abcab"), "abab")
        rng = Random(2)
        for d in ALL:
            for v in (-1e9, 1e9, 3.3, "q", None):
                if d.kind in ("integer", "float") and not isinstance(v, (int, float)):
                    continue
                self.assertTrue(dm.contains(d, dm.clamp(d, v)), (d, v))

    def test_subset(self):
        self.assertTrue(dm.subset_of(Domain("integer", minimum=2, maximum=8), INT))
        self.assertFalse(dm.subset_of(Domain("integer", minimum=0, maximum=8), INT))
        self.assertFalse(dm.subset_of(Domain("integer", minimum=2, maximum=20), INT))
        self.assertTrue(dm.subset_of(Domain("integer", minimum=20, maximum=60, step=20), INTS))
        self.assertFalse(dm.subset_of(Domain("integer", minimum=5, maximum=60, step=10), INTS))
        self.assertFalse(dm.subset_of(Domain("float", minimum=0, maximum=1), FLTS))
        self.assertTrue(dm.subset_of(Domain("choice", options=("Off",)), CH))
        self.assertFalse(dm.subset_of(Domain("choice", options=("Off", "9x")), CH))
        self.assertTrue(dm.subset_of(Domain("text", max_length=5), TXT))
        self.assertFalse(dm.subset_of(Domain("text", max_length=50), TXT))
        self.assertFalse(dm.subset_of(Domain("text", max_length=5), Domain("text", max_length=9, alphabet="ab")))
        self.assertTrue(dm.subset_of(Domain("text", max_length=5, alphabet="a"), Domain("text", max_length=9, alphabet="ab")))
        self.assertFalse(dm.subset_of(BOOL, INT))
        self.assertTrue(dm.subset_of(BOOL, BOOL))

    def test_invalid_values(self):
        for d in ALL:
            vals = dm.invalid_values(d, Random(1))
            self.assertEqual(vals, dm.invalid_values(d, Random(1)))
            for v in vals:
                self.assertFalse(dm.contains(d, v), (d, v))
                if isinstance(v, float):
                    self.assertTrue(math.isfinite(v))
        self.assertIn(0, dm.invalid_values(INT, Random(1)))
        self.assertIn(17, dm.invalid_values(INT, Random(1)))
        self.assertIn("a" * 21, dm.invalid_values(TXT, Random(1)))
        self.assertIn("__not_an_option__", dm.invalid_values(CH, Random(1)))
        self.assertEqual(dm.invalid_values(BOOL, Random(1)), [])
        self.assertTrue(dm.invalid_values(INTS, Random(1)))

    def test_count(self):
        self.assertEqual(dm.count(INT), 16)
        self.assertEqual(dm.count(INTS), 11)
        self.assertEqual(dm.count(FLTS), 5)
        self.assertIsNone(dm.count(FLT))
        self.assertEqual(dm.count(CH), 3)
        self.assertEqual(dm.count(BOOL), 2)
        self.assertIsNone(dm.count(TXT))
        self.assertEqual(dm.count(Domain("text", max_length=0)), 1)

    def test_effective(self):
        sub = Domain("integer", minimum=2, maximum=4)
        c = ControlDef("a", "integer", Selector(auto_id="a"), INT)
        self.assertIs(dm.effective(c), INT)
        c2 = ControlDef("a", "integer", Selector(auto_id="a"), INT, fuzz_domain=sub)
        self.assertIs(dm.effective(c2), sub)

    def test_errors(self):
        with self.assertRaises(ValueError):
            dm.sample(Domain("integer"), Random(1))
        with self.assertRaises(ValueError):
            dm.sample(Domain("choice"), Random(1))
        with self.assertRaises(ValueError):
            dm.sample(Domain("blob"), Random(1))


if __name__ == "__main__":
    unittest.main()
