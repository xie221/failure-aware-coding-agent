from __future__ import annotations

import json
import sys
import subprocess
import tempfile
from pathlib import Path
import unittest

import yaml
from minisweagent.environments.extra.bubblewrap import BubblewrapEnvironment

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT.parent
BENCHMARK = PROJECTS / "agent-benchmark-v2"
CONFIG = ROOT / "config/experiment_v2.yaml"
sys.path.insert(0, str(ROOT / "scripts"))
from score_finished_runs import score_one  # noqa: E402


class BenchmarkV2IntegrationTests(unittest.TestCase):
    def test_prepare_three_public_only_arms_score_and_real_bubblewrap_pytest_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "experiment"
            preparation = subprocess.run([
                sys.executable, str(ROOT / "scripts/launch_experiment.py"), "prepare",
                "--experiment-root", str(run_root), "--config", str(CONFIG),
                "--benchmark-root", str(BENCHMARK), "--conditions",
                "control", "enhanced", "enhanced_v2", "--no-smoke",
            ], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
            self.assertEqual(preparation.returncode, 0, preparation.stdout[-3000:])
            frozen = json.loads((run_root / "prepared_manifest.json").read_text())
            self.assertEqual(frozen["experiment_protocol_version"], 2)
            self.assertEqual(frozen["conditions"], ["control", "enhanced", "enhanced_v2"])
            self.assertEqual(frozen["condition_order"], ["control", "enhanced", "enhanced_v2"])
            self.assertEqual(len(frozen["runs"]), 60)
            self.assertEqual(len(frozen["code_version_sha256"]), 64)

            for record in frozen["runs"]:
                run_dir = Path(record["workspace"]).parent
                workspace = Path(record["workspace"])
                task_meta = json.loads((run_dir / "task.json").read_text())
                visible = {path.relative_to(workspace).as_posix() for path in workspace.rglob("*")
                           if path.is_file()}
                self.assertEqual(visible, {"TASK.md", "buggy.py", "tests/test_buggy.py"})
                self.assertFalse(any(path.is_symlink() for path in workspace.rglob("*")))
                self.assertNotIn("test_hidden.py", visible)
                self.assertNotIn("reference_fixes", visible)
                self.assertEqual(task_meta["benchmark_manifest_sha256"], frozen["benchmark_manifest_sha256"])

            # Exercise the actual mini-SWE BubblewrapEnvironment -> pinned wrapper ->
            # unshare -> bwrap chain with the real pytest module command. The original
            # task is intentionally buggy, so pytest should collect and fail assertions,
            # not fail to import `buggy`.
            selected = next(row for row in frozen["runs"] if row["task_id"] == "L01"
                            and row["split"] == "control")
            workspace = Path(selected["workspace"])
            wrapper = Path(selected["wrapper"])
            env_config = yaml.safe_load(CONFIG.read_text())["environment"]["env"]
            env = BubblewrapEnvironment(cwd=str(workspace), executable=str(wrapper),
                                        wrapper_args=[], timeout=30, env=env_config)
            try:
                outcome = env.execute({"command": "python -m pytest -q tests/test_buggy.py"},
                                      cwd=str(workspace), timeout=30)
            finally:
                env.cleanup()
            self.assertEqual(outcome["returncode"], 1, outcome)
            self.assertNotIn("ModuleNotFoundError", outcome["output"])
            self.assertIn("FAILED tests/test_buggy.py", outcome["output"])

            # Exercise evaluator routing against the prepared task metadata, without
            # any model output and with hidden tests remaining evaluator-only.
            score_dir = Path(next(row["workspace"] for row in frozen["runs"]
                                  if row["task_id"] == "L01" and row["split"] == "enhanced_v2")).parent
            (score_dir / "task_result.json").write_text(json.dumps({
                "task_id": "L01", "condition": "enhanced_v2", "exit_reason": "LimitsExceeded",
                "http_attempts": 0, "total_tokens": 0, "elapsed_seconds": 0.01,
            }), encoding="utf-8")
            scored = score_one(score_dir)
            self.assertFalse(scored["patch_correct"])
            self.assertFalse(scored["public_tests_passed"])
            self.assertFalse(scored["hidden_tests_passed"])
            self.assertTrue((score_dir / "evaluator_only/hidden/pytest.log").is_file())
            self.assertFalse((Path(scored["test_details"]["hidden"]["log"]).is_relative_to(
                Path(task_meta["workspace"]))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
