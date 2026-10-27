from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "failure_aware_score_finished_runs", ROOT / "scripts/score_finished_runs.py"
)
assert SPEC is not None and SPEC.loader is not None
SCORER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCORER)


class ScoringSemanticsTests(unittest.TestCase):
    def test_patch_correct_requires_public_and_hidden_tests(self):
        self.assertTrue(SCORER.patch_is_correct(True, True))
        self.assertFalse(SCORER.patch_is_correct(False, True))
        self.assertFalse(SCORER.patch_is_correct(True, False))
        self.assertFalse(SCORER.patch_is_correct(False, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
