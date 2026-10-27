from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUMMARY_SCRIPT = ROOT / "scripts/summarize_v2_results.py"


class SummarizeV2Tests(unittest.TestCase):
    def test_three_arm_summary_pairs_only_scored_common_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prepared_manifest.json").write_text(
                json.dumps({"benchmark_manifest_sha256": "a" * 64}), encoding="utf-8")
            for condition, correct in (("control", False), ("enhanced", True),
                                       ("enhanced_v2", True)):
                run = root / condition / "A01"
                run.mkdir(parents=True)
                (run / "task_result.json").write_text(json.dumps({
                    "task_id": "A01", "exit_reason": "Submitted", "http_attempts": 4,
                    "total_tokens": 123, "elapsed_seconds": 2.5, "budget_rejections": 0,
                    "changed_files": ["buggy.py"],
                }), encoding="utf-8")
                (run / "score.json").write_text(json.dumps({
                    "task_id": "A01", "category": "basic_logic",
                    "public_tests_passed": correct, "hidden_tests_passed": correct,
                    "patch_correct": correct, "protocol_completion": True,
                    "feedback_events": 1 if condition == "enhanced_v2" else 0,
                    "retry_guard_skips": 0,
                }), encoding="utf-8")
                (run / "trajectory.json").write_text(json.dumps({"messages": []}), encoding="utf-8")

            result = subprocess.run([sys.executable, str(SUMMARY_SCRIPT), str(root)],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)
            summary = json.loads((root / "comparison_v2_summary.json").read_text())
            self.assertEqual(summary["paired_task_count"], 1)
            self.assertFalse(summary["paired"][0]["control_patch_correct"])
            self.assertTrue(summary["paired"][0]["enhanced_v2_patch_correct"])
            self.assertEqual(summary["conditions"]["enhanced_v2"]["patch_correct"], 1)
            self.assertTrue((root / "comparison_v2_per_task.csv").is_file())

            # Frozen summaries are append-once artifacts: a second run must fail closed.
            repeated = subprocess.run([sys.executable, str(SUMMARY_SCRIPT), str(root)],
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      timeout=10, check=False)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("refusing to overwrite", repeated.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
