from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import httpx
import yaml
from failure_aware_agent.feedback import FailureAwareFeedback
from failure_aware_agent.retry_guard import RepeatFailureGuard
from failure_aware_agent.runtime import FeedbackEnvironment, assemble_task


class FakeEnvironment:
    config = object()

    def __init__(self, returncode=1):
        self.returncode = returncode
        self.calls = 0

    def execute(self, action, cwd="", *, timeout=None):
        self.calls += 1
        return {"output": "synthetic command failed", "returncode": self.returncode,
                "exception_info": ""}

    def serialize(self):
        return {"info": {"config": {}}}

    def get_template_vars(self, **kwargs):
        return {}


class RepeatFailureGuardTests(unittest.TestCase):
    def test_exact_failed_retry_is_skipped_and_reported_without_second_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "buggy.py").write_text("def f(): return 1\n")
            base = FakeEnvironment()
            guard = RepeatFailureGuard(workspace)
            feedback = FailureAwareFeedback(lambda: 10)
            env = FeedbackEnvironment(base, feedback, guard)
            action = {"command": "python -m pytest -q"}

            first = env.execute(action)
            second = env.execute(action)

            self.assertEqual(base.calls, 1)
            self.assertEqual(first["returncode"], 1)
            self.assertEqual(second["returncode"], 125)
            self.assertIn("workspace files are unchanged", second["output"])
            self.assertEqual(guard.suppressed_count, 1)
            self.assertEqual(guard.records[-1]["event"], "retry_suppressed_unchanged_workspace")

    def test_ten_identical_failures_execute_once_and_skip_nine_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "buggy.py").write_text("def f(): return 1\n")
            base = FakeEnvironment()
            guard = RepeatFailureGuard(workspace)
            env = FeedbackEnvironment(base, FailureAwareFeedback(lambda: 10), guard)
            for _ in range(10):
                env.execute({"command": "nano buggy.py"})
            self.assertEqual(base.calls, 1)
            self.assertEqual(guard.suppressed_count, 9)

    def test_changed_workspace_allows_same_command_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "buggy.py"
            source.write_text("def f(): return 1\n")
            guard = RepeatFailureGuard(workspace)
            command = "pytest -q"
            guard.observe(command, {"returncode": 1})
            source.write_text("def f(): return 2\n")
            self.assertFalse(guard.should_suppress(command))
            self.assertEqual(guard.records[-1]["event"], "retry_allowed_workspace_changed")

    def test_new_empty_directory_counts_as_workspace_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "source.py").write_text("x = 1\n")
            guard = RepeatFailureGuard(workspace)
            guard.observe("pytest -q", {"returncode": 1})
            (workspace / "new_dir").mkdir()
            self.assertFalse(guard.should_suppress("pytest -q"))

    def test_success_does_not_leave_a_suppressible_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "buggy.py").write_text("x = 1\n")
            guard = RepeatFailureGuard(workspace)
            guard.observe("pytest -q", {"returncode": 1})
            guard.observe("pytest -q", {"returncode": 0})
            self.assertFalse(guard.should_suppress("pytest -q"))
            self.assertEqual(guard.suppressed_count, 0)

    def test_quoted_command_whitespace_is_not_normalized_away(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "source.py").write_text("x = 1\n")
            guard = RepeatFailureGuard(workspace)
            guard.observe("printf 'a  b'", {"returncode": 1})
            self.assertFalse(guard.should_suppress("printf 'a b'"))

    def test_audit_contains_hashes_not_raw_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "source.py").write_text("x = 1\n")
            guard = RepeatFailureGuard(workspace)
            command = "pytest --secret-marker -q"
            guard.observe(command, {"returncode": 2})
            serialized = json.dumps(guard.records)
            self.assertNotIn(command, serialized)
            self.assertNotIn("secret-marker", serialized)

    def test_guard_is_only_attached_to_enhanced_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "source.py").write_text("x = 1\n")
            wrapper = root / "wrapper.py"
            wrapper.write_text("# not executed\n")
            config_path = Path(__file__).resolve().parents[1] / "config/experiment.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            mock = httpx.MockTransport(lambda request: httpx.Response(500, request=request))

            control = assemble_task(config, workspace, wrapper, condition="control", mock_transport=mock)
            self.assertIsNone(control["retry_guard"])
            control["adapter"].restore()
            control["http_client"].close()

            enhanced = assemble_task(config, workspace, wrapper, condition="enhanced", mock_transport=mock)
            self.assertIsInstance(enhanced["retry_guard"], RepeatFailureGuard)
            enhanced["adapter"].restore()
            enhanced["http_client"].close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
