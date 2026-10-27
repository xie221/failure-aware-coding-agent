from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
launch_experiment = importlib.import_module("launch_experiment")


class BatchSmokeGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="batch-gate-")
        self.base = Path(self.temp.name)
        self.benchmark = self.base / "benchmark"
        self.benchmark.mkdir()
        (self.benchmark / "benchmark_manifest.json").write_text('{"synthetic": true}\n')
        self.manifest_hash = hashlib.sha256(
            (self.benchmark / "benchmark_manifest.json").read_bytes()).hexdigest()
        self.batch_root = self.base / "new-batch"
        self.batch_root.mkdir()
        self.pilot = self.base / "pilot/smoke/A01"
        self.pilot.mkdir(parents=True)
        (self.pilot.parents[1] / "experiment_config.yaml").write_text("synthetic config\n")
        (self.pilot.parents[1] / "prepared_manifest.json").write_text(json.dumps({
            "benchmark_manifest_sha256": self.manifest_hash
        }))
        self.workspace = self.pilot / "workspace"
        (self.workspace / "tests").mkdir(parents=True)
        for name in ("TASK.md", "buggy.py", "tests/test_buggy.py"):
            (self.workspace / name).write_text("synthetic public fixture\n")
        self.wrapper = self.pilot / "action-wrapper.py"
        self.wrapper.write_text("# synthetic pinned wrapper\n")
        self.audit = self.pilot / "wrapper_invocations.count"
        self.audit.write_text("1\n")
        self.task = {
            "task_id": "A01",
            "wrapper": str(self.wrapper),
            "wrapper_sha256": hashlib.sha256(self.wrapper.read_bytes()).hexdigest(),
            "audit_file": str(self.audit), "workspace": str(self.workspace),
            "public_files_only": ["TASK.md", "buggy.py", "tests/test_buggy.py"],
            "source_hashes": {
                name: hashlib.sha256((self.workspace / name).read_bytes()).hexdigest()
                for name in ("TASK.md", "buggy.py", "tests/test_buggy.py")
            },
        }
        (self.pilot / "task.json").write_text(json.dumps(self.task))
        (self.pilot.parents[1] / "prepared_manifest.json").write_text(json.dumps({
            "benchmark_manifest_sha256": self.manifest_hash,
            "runs": [{
                "split": "smoke", "task_id": "A01", "files": self.task["source_hashes"],
                "wrapper_sha256": self.task["wrapper_sha256"],
            }],
        }))
        self.events = [{
            "event": "http_attempt", "attempt_id": "http-000001", "status_code": 200
        }]
        self._write("http_attempts.json", self.events)
        self._write("task_result.json", {
            "task_id": "A01", "http_attempts": 1, "wrapper_invocations": 1, "changed_files": [],
            "exit_reason": "LimitsExceeded"
        })
        self._write("trajectory.json", {"messages": [{
            "role": "assistant", "extra": {"actions": [{"command": "synthetic"}]}
        }]})
        self._write("launcher_status.json", {
            "cleanup_status": "verified_no_process_group_members",
            "action_wrapper_audit": "matched"
        })
        for group in ("public", "hidden"):
            evaluator = self.pilot / "evaluator_only" / group
            evaluator.mkdir(parents=True)
            (evaluator / "buggy.py").write_text("synthetic isolated copy\n")
            log = evaluator / "pytest.log"
            log.write_text("synthetic failed test result\n")
            self.score_details[group] = {
                "passed": False, "exit_code": 1, "log": str(log)
            }
        self._write("score.json", {
            "task_id": "A01",
            "public_tests_passed": False, "hidden_tests_passed": False,
            "patch_correct": False, "protocol_completion": False,
            "test_details": self.score_details,
        })
        self.benchmark_patch = patch.object(launch_experiment, "BENCHMARK", self.benchmark)
        self.benchmark_patch.start()

    @property
    def score_details(self):
        if not hasattr(self, "_score_details"):
            self._score_details = {}
        return self._score_details

    def tearDown(self):
        self.benchmark_patch.stop()
        self.temp.cleanup()

    def _write(self, name, value):
        (self.pilot / name).write_text(json.dumps(value) + "\n")

    def test_zero_parsed_actions_and_zero_wrapper_calls_are_consistent(self):
        self.assertTrue(launch_experiment.action_wrapper_audit_matches(0, 0, 0))
        self.assertFalse(launch_experiment.action_wrapper_audit_matches(0, 1, 0))
        self.assertFalse(launch_experiment.action_wrapper_audit_matches(1, 0, 0))

    def test_failed_patch_and_limits_exceeded_do_not_block_valid_pilot(self):
        okay, reason = launch_experiment.smoke_gate_passes(self.batch_root, self.pilot)
        self.assertTrue(okay, reason)

    def test_over_budget_or_duplicate_attempt_ids_block(self):
        self.events = [{"event": "http_attempt", "attempt_id": f"http-{i:06d}",
                        "status_code": 200} for i in range(1, 14)]
        self._write("http_attempts.json", self.events)
        self._write("task_result.json", {
            "task_id": "A01", "http_attempts": 13, "wrapper_invocations": 1, "changed_files": []
        })
        okay, _ = launch_experiment.smoke_gate_passes(self.batch_root, self.pilot)
        self.assertFalse(okay)

    def test_unverified_process_cleanup_blocks(self):
        self._write("launcher_status.json", {
            "cleanup_status": "FAILED_residual_process", "action_wrapper_audit": "matched"
        })
        okay, reason = launch_experiment.smoke_gate_passes(self.batch_root, self.pilot)
        self.assertFalse(okay)
        self.assertIn("cleanup", reason)

    def test_mismatched_wrapper_audit_blocks(self):
        self.audit.write_text("1\n2\n")
        okay, reason = launch_experiment.smoke_gate_passes(self.batch_root, self.pilot)
        self.assertFalse(okay)
        self.assertIn("wrapper audit", reason)

    def test_formal_batch_claim_is_one_shot_and_never_overwrites(self):
        marker = self.base / "formal_batch_claim.json"
        with patch.object(launch_experiment, "FORMAL_BATCH_CLAIM", marker):
            self.assertTrue(launch_experiment.claim_formal_batch(self.batch_root))
            original = marker.read_text()
            self.assertFalse(launch_experiment.claim_formal_batch(self.base / "second-root"))
            self.assertEqual(marker.read_text(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
