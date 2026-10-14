import unittest

from overgrind.adapters import resolve as R
from overgrind.types import ElementInfo, Selector


def el(name, ctype, aid="", cls="", path=()):
    return ElementInfo(Selector(auto_id=aid, title=name, control_type=ctype, class_name=cls, path=path),
                       name, ctype)


TREE = [
    el("Main", "Window", "main", "Win"),
    el("Enable A", "CheckBox", "cb_a", "CheckBox", ("Main",)),
    el("Enable B", "CheckBox", "cb_b", "CheckBox", ("Main",)),
    el("OK", "Button", "", "Button", ("Main", "Left")),
    el("OK", "Button", "", "Button", ("Main", "Right")),
    el("Name", "Edit", "", "Edit", ("Main",)),
    el("Other", "Edit", "", "Edit", ("Main",)),
]


class ResolveTests(unittest.TestCase):
    def test_auto_id_first_tier(self):
        r = R.resolve(Selector(auto_id="cb_b", control_type="CheckBox"), TREE)
        self.assertEqual((r.index, r.tier, r.ambiguous), (2, "auto_id", False))

    def test_heals_when_auto_id_changed(self):
        r = R.resolve(Selector(auto_id="renamed", title="Enable A", control_type="CheckBox"), TREE)
        self.assertEqual((r.index, r.tier), (1, "title+type"))
        self.assertIn("drifting", r.note)
        self.assertEqual(r.tried[0], ("auto_id", 0))

    def test_ambiguous_title_resolved_by_path(self):
        sel = Selector(title="OK", control_type="Button", path=("Main", "Right"))
        r = R.resolve(sel, TREE)
        self.assertEqual((r.index, r.tier), (4, "path+type"))

    def test_ambiguous_without_disambiguation_is_unresolved(self):
        r = R.resolve(Selector(title="OK", control_type="Button"), TREE)
        self.assertIsNone(r.index)
        self.assertTrue(r.ambiguous)
        self.assertEqual(set(r.candidates), {3, 4})

    def test_class_index_last_resort(self):
        r = R.resolve(Selector(class_name="Edit", control_type="Edit", index=1), TREE)
        self.assertEqual((r.index, r.tier, r.ambiguous), (6, "class+index", True))

    def test_not_found_and_empty(self):
        self.assertIsNone(R.resolve(Selector(auto_id="nope"), TREE).index)
        self.assertIsNone(R.resolve(Selector(), TREE).index)

    def test_explain_and_score(self):
        s, _ = R.stability_score(Selector(auto_id="cb_a"), TREE)
        w, why = R.stability_score(Selector(title="OK", control_type="Button"), TREE)
        self.assertGreater(s, w)
        self.assertIn("matches several", why)
        self.assertIn("auto_id", R.explain(Selector(auto_id="cb_a"), TREE))

    def test_suggest_selector_is_unique(self):
        for i, e in enumerate(TREE):
            sug = R.suggest_selector(e, TREE)
            r = R.resolve(sug, TREE)
            self.assertEqual(r.index, i, (e.name, sug))


if __name__ == "__main__":
    unittest.main()
