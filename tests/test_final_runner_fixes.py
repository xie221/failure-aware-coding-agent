from __future__ import annotations

import importlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from failure_aware_agent.action_wrapper import render_action_wrapper
from failure_aware_agent.persistence import atomic_write_json
from failure_aware_agent.runtime import install_atomic_agent_saver
from minisweagent.agents.interactive import InteractiveAgent
from minisweagent.exceptions import LimitsExceeded

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
launcher = importlib.import_module("launch_experiment")
run_task = importlib.import_module("run_task")
scorer = importlib.import_module("score_finished_runs")
summarizer = importlib.import_module("summarize_results")


class FinalRunnerFixTests(unittest.TestCase):
    def test_action_audit_counts_wrapper_runs_and_guard_skips(self):
        self.assertTrue(launcher.action_wrapper_audit_matches(11, 2, 9))
        self.assertFalse(launcher.action_wrapper_audit_matches(11, 2, 8))
        self.assertFalse(launcher.action_wrapper_audit_matches(11, None, 9))

    def test_devnull_eof_limit_path_raises_clean_limits_exceeded_without_prompt(self):
        agent = InteractiveAgent.__new__(InteractiveAgent)
        agent.config = SimpleNamespace(mode="yolo", step_limit=1, cost_limit=999,
                                       wall_time_limit_seconds=0)
        agent.n_calls = 1
        agent.cost = 0.0
        started = time.monotonic()
        with open(os.devnull, "r") as eof_stream, patch("sys.stdin", eof_stream), \
                patch("builtins.input", side_effect=AssertionError("must not prompt")):
            with self.assertRaises(LimitsExceeded):
                InteractiveAgent.query(agent)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(run_task.classify_termination({"exit_status": "LimitsExceeded"}),
                         "step_or_cost_limit_exceeded")

    def test_task_subprocess_stdin_is_always_devnull(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            with patch.object(launcher.subprocess, "Popen") as popen:
                launcher.spawn_unattended_task(["synthetic-child"], run_dir, {})
            self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_http_budget_exhaustion_has_explicit_termination_category(self):
        adapter = SimpleNamespace(terminations=[{"category": "budget_exhausted"}])
        self.assertEqual(run_task.classify_termination({"exit_status": "LimitsExceeded"}, adapter),
                         "http_attempt_budget_exhausted")

    def test_task_ledger_validator_accepts_budget_and_rejects_overspend(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            attempts = [{"event": "http_attempt", "attempt_id": f"http-{i:06d}",
                         "status_code": 200} for i in range(1, 13)]
            attempts.append({"event": "budget_rejected_before_send", "attempt_id": None})
            (run_dir / "http_attempts.json").write_text(json.dumps(attempts))
            task_result = {"http_attempts": 12, "budget_rejections": 1}
            self.assertIsNone(launcher.http_ledger_integrity_error(run_dir, task_result))
            attempts.insert(12, {"event": "http_attempt", "attempt_id": "http-000013",
                                 "status_code": 200})
            (run_dir / "http_attempts.json").write_text(json.dumps(attempts))
            task_result["http_attempts"] = 13
            self.assertIn("12-attempt cap", launcher.http_ledger_integrity_error(run_dir, task_result))

    def test_editor_wrapper_returns_immediately_and_records_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            workspace = base / "workspace"
            workspace.mkdir()
            audit = base / "audit.count"
            wrapper = base / "wrapper.py"
            generated = render_action_wrapper(workspace, audit, base / "mini-env", base / "mini-src")
            wrapper.write_text(generated, encoding="utf-8")
            namespace = {"__name__": "test_generated_wrapper"}
            exec(compile(generated, str(wrapper), "exec"), namespace)
            for command in ("nano buggy.py", "cd /workspace && vim x", "/usr/bin/vi file",
                            "sudo nano file", "env EDITOR=x vim file"):
                self.assertTrue(namespace["is_interactive_editor_command"](command), command)
            self.assertFalse(namespace["is_interactive_editor_command"]("echo nano is unavailable"))
            started = time.monotonic()
            proc = subprocess.run([
                sys.executable, str(wrapper), "--bind", str(workspace), str(workspace),
                "--chdir", str(workspace), "--setenv", "LANG", "C.UTF-8",
                "bash", "-c", "nano buggy.py",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
            self.assertEqual(proc.returncode, 125)
            self.assertIn("use Python file operations, sed, or shell redirection", proc.stderr)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(audit.read_text(encoding="utf-8"), "1\n")
            self.assertNotIn("/usr/bin/unshare", proc.stderr)

    def test_failed_agent_outcome_is_scored_before_any_batch_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_dir = root / "control" / "A01"
            workspace = task_dir / "workspace"
            workspace.mkdir(parents=True)
            (root / "results.jsonl").touch()
            (task_dir / "http_attempts.json").write_text("[]\n")
            (task_dir / "task_result.json").write_text(json.dumps({
                "task_id": "A01", "exit_reason": "LimitsExceeded",
                "http_attempts": 0, "budget_rejections": 0,
            }))
            record = {"task_id": "A01", "split": "control", "workspace": str(workspace)}
            launcher.record_progress(root, {"event": "batch_started"})
            launch = {"exit_code": 1, "termination": "normal",
                      "cleanup_status": "verified_no_process_group_members",
                      "action_wrapper_audit": "matched"}
            called = []

            def scorer(experiment_root, run_dir):
                called.append((experiment_root, run_dir))
                return 0, {"status": "scored", "patch_correct": False}

            _, score, stop = launcher.persist_and_score_task(root, record, launch, score_fn=scorer)
            self.assertEqual(len(called), 1)
            self.assertFalse(score["patch_correct"])
            self.assertIsNone(stop)
            self.assertEqual(len((root / "results.jsonl").read_text().splitlines()), 1)

    def test_all_terminal_outcomes_without_patch_still_run_both_evaluators(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hidden = root / "benchmark/evaluation/hidden_tests/A01"
            hidden.mkdir(parents=True)
            (hidden / "test_hidden.py").write_text("# hidden fixture kept outside Agent workspace\n")

            calls = []
            class FailedPytest:
                returncode = 1
                def __init__(self, *args, **kwargs): calls.append((args, kwargs))
                def communicate(self, timeout=None): return ("synthetic assertion failure\n", None)

            with patch.object(scorer, "BENCHMARK", root / "benchmark"), \
                    patch.object(scorer, "PYTHON", Path("/synthetic/python")), \
                    patch.object(scorer.subprocess, "Popen", FailedPytest):
                for index, reason in enumerate(("Submitted", "LimitsExceeded", "TimeExceeded",
                                                "external_timeout", "http_attempt_budget_exhausted")):
                    run_dir = root / "control" / f"A01-{index}"
                    (run_dir / "workspace").mkdir(parents=True)
                    (run_dir / "evaluator_snapshot/tests").mkdir(parents=True)
                    (run_dir / "workspace/buggy.py").write_text("def f(): return 1\n")
                    (run_dir / "evaluator_snapshot/tests/test_buggy.py").write_text("# public fixture\n")
                    (run_dir / "task.json").write_text(json.dumps({"task_id": "A01", "category": "synthetic"}))
                    (run_dir / "task_result.json").write_text(json.dumps({
                        "task_id": "A01", "condition": "control", "exit_reason": reason,
                        "http_attempts": 12, "total_tokens": 10, "elapsed_seconds": 1.0,
                        "changed_files": [],
                    }))
                    result = scorer.score_one(run_dir)
                    self.assertFalse(result["public_tests_passed"])
                    self.assertFalse(result["hidden_tests_passed"])
                    self.assertFalse(result["patch_correct"])
                    self.assertEqual(result["exit_reason"], reason)
                    self.assertTrue((run_dir / "score.json").is_file())
            self.assertEqual(len(calls), 10)

    def test_deleted_agent_file_is_scored_using_verified_original_public_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            benchmark = root / "benchmark"
            hidden = benchmark / "evaluation/hidden_tests/C04/test_hidden.py"
            hidden.parent.mkdir(parents=True)
            hidden.write_text("# evaluator-only hidden fixture\n")
            run_dir = root / "control/C04"
            (run_dir / "workspace").mkdir(parents=True)
            snapshot = run_dir / "evaluator_snapshot"
            (snapshot / "tests").mkdir(parents=True)
            original = snapshot / "buggy.py"
            original.write_text("def f(): return None\n")
            source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
            (snapshot / "tests/test_buggy.py").write_text("# public fixture\n")
            (run_dir / "task.json").write_text(json.dumps({
                "task_id": "C04", "category": "exception_handling",
                "source_hashes": {"buggy.py": source_hash},
            }))
            (run_dir / "task_result.json").write_text(json.dumps({
                "task_id": "C04", "condition": "control", "exit_reason": "LimitsExceeded",
                "http_attempts": 12, "total_tokens": 100, "elapsed_seconds": 7,
            }))

            class FailedPytest:
                returncode = 1
                def __init__(self, *args, **kwargs): pass
                def communicate(self, timeout=None): return ("synthetic no-patch failure\n", None)

            with patch.object(scorer, "BENCHMARK", benchmark), \
                    patch.object(scorer, "PYTHON", Path("/synthetic/python")), \
                    patch.object(scorer.subprocess, "Popen", FailedPytest):
                score = scorer.score_one(run_dir)
            self.assertNotEqual(score.get("status"), "unavailable")
            self.assertEqual(score["candidate_source"],
                             "original_public_buggy_due_to_missing_agent_file")
            self.assertFalse(score["patch_correct"])
            self.assertEqual(len(list((run_dir / "evaluator_only").glob("*/buggy.py"))), 2)

    def test_resume_rescores_completed_task_and_returns_only_never_started_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            done = root / "control/A01"
            fresh = root / "control/A02"
            done.mkdir(parents=True)
            fresh.mkdir(parents=True)
            (done / "task_result.json").write_text("{}\n")
            (done / "launcher_status.json").write_text(json.dumps({
                "cleanup_status": "verified_no_process_group_members",
                "action_wrapper_audit": "matched",
            }))
            (done / "score.json").write_text(json.dumps({"status": "unavailable"}))
            records = [
                {"task_id": "A01", "split": "control", "workspace": str(done / "workspace")},
                {"task_id": "A02", "split": "control", "workspace": str(fresh / "workspace")},
            ]
            def fake_score(experiment_root, run_dir):
                score = {"status": "scored", "patch_correct": False}
                (run_dir / "score.json").write_text(json.dumps(score))
                return 0, score
            pending, recovered, error = launcher.resume_unfinished_records(root, records, fake_score)
            self.assertIsNone(error)
            self.assertEqual(recovered, ["A01"])
            self.assertEqual([r["task_id"] for r in pending], ["A02"])
            self.assertEqual(len((root / "results.jsonl").read_text().splitlines()), 1)

    def test_resume_refuses_to_rerun_execution_artifact_without_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "control/A01"
            (run_dir / "runtime").mkdir(parents=True)
            (run_dir / "console.log").write_text("agent started\n")
            records = [{"task_id": "A01", "split": "control",
                        "workspace": str(run_dir / "workspace")}]
            pending, recovered, error = launcher.resume_unfinished_records(root, records)
            self.assertEqual(pending, [])
            self.assertEqual(recovered, [])
            self.assertIn("refusing rerun", error)

    def test_task_failure_with_verified_cleanup_continues_to_next_task(self):
        seen = []

        def execute(record):
            seen.append(record["task_id"])
            return "task_failed" if record["task_id"] == "A01" else "task_ok"

        status = launcher.run_serial_tasks([{"task_id": "A01"}, {"task_id": "A02"}], execute)
        self.assertEqual(seen, ["A01", "A02"])
        self.assertEqual(status, 1)
        safe_failed_launch = {"cleanup_status": "verified_no_process_group_members",
                              "action_wrapper_audit": "matched"}
        failed_score = {"status": "scored", "patch_correct": False}
        self.assertFalse(launcher.should_stop_after_task(safe_failed_launch, failed_score, []))

    def test_severe_integrity_failure_stops_sequence_and_preserves_prior_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atomic_write_json(root / "control/A01/task_result.json", {"task_id": "A01"})
            seen = []

            def execute(record):
                seen.append(record["task_id"])
                if record["task_id"] == "A01":
                    return "infrastructure_stop"
                return "task_ok"

            status = launcher.run_serial_tasks([{"task_id": "A01"}, {"task_id": "A02"}], execute)
            self.assertEqual(status, 2)
            self.assertEqual(seen, ["A01"])
            self.assertEqual(json.loads((root / "control/A01/task_result.json").read_text()),
                             {"task_id": "A01"})
        severe = {"cleanup_status": "FAILED_residual_process", "action_wrapper_audit": "matched"}
        self.assertTrue(launcher.should_stop_after_task(severe, {"status": "scored"}, []))

    def test_partial_summary_preserves_unscored_task_without_crashing(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "control/A01"
            run_dir.mkdir(parents=True)
            (run_dir / "task_result.json").write_text(json.dumps({
                "task_id": "A01", "exit_reason": "external_timeout"
            }))
            summary, details = summarizer.summarize_condition(Path(temp), "control")
            self.assertEqual(summary["started"], 1)
            self.assertEqual(summary["scored"], 0)
            self.assertEqual(summary["unscored_or_unavailable"], 1)
            self.assertEqual(details[0]["status"], "unscored")

    def test_agent_saves_are_atomic_json(self):
        class FakeAgent:
            def serialize(self, *extra):
                return {"messages": [1], "extra": list(extra)}

        agent = FakeAgent()
        install_atomic_agent_saver(agent)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "trajectory.json"
            returned = agent.save(target, {"synthetic": True})
            self.assertEqual(json.loads(target.read_text()), returned)
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
