from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace

import httpx
import yaml
from minisweagent.exceptions import Submitted

from failure_aware_agent.action_wrapper import render_action_wrapper
from failure_aware_agent.enhanced_v2 import FailureAwareFeedbackV2, VerifiedSubmissionGate
from failure_aware_agent.runtime import assemble_task


TEST_COMMAND = "python -m pytest -q tests/test_buggy.py"
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeEnvironment:
    config = SimpleNamespace(model_dump=lambda **kwargs: {})

    def __init__(self):
        self.commands = []

    def execute(self, action, cwd="", *, timeout=None):
        command = action["command"]
        self.commands.append(command)
        if command == SUBMIT:
            raise Submitted({"role": "exit", "content": "", "extra": {"exit_status": "Submitted"}})
        return {"output": "2 passed", "returncode": 0, "exception_info": ""}

    def serialize(self):
        return {"info": {"config": {}}}

    def get_template_vars(self, **kwargs):
        return {}

    def cleanup(self):
        pass


class EnhancedV2Tests(unittest.TestCase):
    def make_workspace(self, root: Path):
        workspace = root / "workspace"
        (workspace / "tests").mkdir(parents=True)
        (workspace / "buggy.py").write_text("def f(): return 1\n", encoding="utf-8")
        (workspace / "tests/test_buggy.py").write_text("def test_f(): assert True\n", encoding="utf-8")
        hashes = {"buggy.py": sha(workspace / "buggy.py"),
                  "tests/test_buggy.py": sha(workspace / "tests/test_buggy.py")}
        return workspace, hashes

    def test_submit_without_changed_source_or_verified_test_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, hashes = self.make_workspace(Path(tmp))
            base = FakeEnvironment()
            gate = VerifiedSubmissionGate(base, workspace, public_test_command=TEST_COMMAND,
                                          initial_hashes=hashes)
            result = gate.execute({"command": SUBMIT})
            self.assertEqual(result["returncode"], 125)
            self.assertIn("no verified source change", result["output"])
            self.assertEqual(gate.records[0]["event"], "submission_blocked")
            self.assertNotIn(SUBMIT, base.commands[0])

    def test_noop_sed_exit_zero_is_not_mistaken_for_source_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, hashes = self.make_workspace(Path(tmp))
            source = workspace / "buggy.py"
            before = sha(source)
            result = subprocess.run(["sed", "-i", "s/absent_anchor/replacement/", str(source)],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=3, check=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(sha(source), before)
            gate = VerifiedSubmissionGate(FakeEnvironment(), workspace,
                                         public_test_command=TEST_COMMAND, initial_hashes=hashes)
            blocked = gate.execute({"command": SUBMIT})
            self.assertEqual(blocked["returncode"], 125)
            self.assertIn("no verified source change", blocked["output"])

    def test_submit_allowed_only_after_public_pass_for_current_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, hashes = self.make_workspace(Path(tmp))
            (workspace / "buggy.py").write_text("def f(): return 2\n", encoding="utf-8")
            base = FakeEnvironment()
            gate = VerifiedSubmissionGate(base, workspace, public_test_command=TEST_COMMAND,
                                          initial_hashes=hashes)
            test = gate.execute({"command": TEST_COMMAND})
            self.assertEqual(test["returncode"], 0)
            with self.assertRaises(Submitted):
                gate.execute({"command": SUBMIT})
            self.assertEqual(gate.records[-1]["event"], "submission_allowed_after_verified_tests")

    def test_edit_after_test_pass_invalidates_submission_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, hashes = self.make_workspace(Path(tmp))
            source = workspace / "buggy.py"
            source.write_text("def f(): return 2\n", encoding="utf-8")
            gate = VerifiedSubmissionGate(FakeEnvironment(), workspace,
                                         public_test_command=TEST_COMMAND, initial_hashes=hashes)
            gate.execute({"command": TEST_COMMAND})
            source.write_text("def f(): return 3\n", encoding="utf-8")
            result = gate.execute({"command": SUBMIT})
            self.assertEqual(result["returncode"], 125)
            self.assertIn("current buggy.py", result["output"])

    def test_modified_public_tests_never_authorize_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, hashes = self.make_workspace(Path(tmp))
            (workspace / "buggy.py").write_text("def f(): return 2\n", encoding="utf-8")
            tests = workspace / "tests/test_buggy.py"
            tests.write_text("def test_f(): assert True\n# changed\n", encoding="utf-8")
            gate = VerifiedSubmissionGate(FakeEnvironment(), workspace,
                                         public_test_command=TEST_COMMAND, initial_hashes=hashes)
            gate.execute({"command": TEST_COMMAND})
            result = gate.execute({"command": SUBMIT})
            self.assertEqual(result["returncode"], 125)
            self.assertIn("test file differs", result["output"])

    def test_pytest_lookalike_or_collect_only_command_cannot_authorize_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, hashes = self.make_workspace(Path(tmp))
            (workspace / "buggy.py").write_text("def f(): return 2\n", encoding="utf-8")
            gate = VerifiedSubmissionGate(FakeEnvironment(), workspace,
                                         public_test_command=TEST_COMMAND, initial_hashes=hashes)
            gate.execute({"command": "python -m pytest --collect-only -q tests/test_buggy.py"})
            blocked = gate.execute({"command": SUBMIT})
            self.assertEqual(blocked["returncode"], 125)
            self.assertIn("public tests have not passed", blocked["output"])

    def test_feedback_is_short_actionable_and_duplicate_is_suppressed(self):
        feedback = FailureAwareFeedbackV2(lambda: 8, public_test_command=TEST_COMMAND)
        result = {"output": "ModuleNotFoundError: No module named 'buggy'", "returncode": 2}
        first = feedback.observe("pytest -q tests/test_buggy.py", result)
        second = feedback.observe("pytest -q tests/test_buggy.py", result)
        self.assertIn("pytest_collection_error", first)
        self.assertLess(len(first), 600)
        self.assertIsNone(second)
        self.assertEqual(feedback.suppressed_duplicate_feedback, 1)

    def test_successful_non_test_action_does_not_trigger_feedback(self):
        feedback = FailureAwareFeedbackV2(lambda: 8, public_test_command=TEST_COMMAND)
        self.assertIsNone(feedback.observe("pwd", {"output": "/workspace", "returncode": 0}))

    def test_budget_guidance_only_after_verified_public_test_success(self):
        feedback = FailureAwareFeedbackV2(lambda: 2, public_test_command=TEST_COMMAND)
        hint = feedback.observe(TEST_COMMAND, {"output": "2 passed", "returncode": 0})
        self.assertIn("verified_tests_low_budget", hint)
        self.assertIn("Do not edit the tested source", hint)

    def test_generated_wrapper_exposes_only_task_root_for_pytest_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrapper = render_action_wrapper(root / "workspace", root / "audit", root / "env", root / "mini")
            self.assertIn('"--setenv", "PYTHONPATH", "/workspace"', wrapper)

    def test_pytest_console_entrypoint_imports_task_module_with_workspace_path(self):
        pytest = shutil.which("pytest")
        if pytest is None:
            self.skipTest("project pytest entrypoint unavailable")
        pytest_path = Path(pytest).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "buggy.py").write_text("value = 7\n", encoding="utf-8")
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_buggy.py").write_text(
                "from buggy import value\ndef test_value(): assert value == 7\n", encoding="utf-8")
            clean_env = {"PATH": os.pathsep.join((str(pytest_path.parent), "/usr/bin", "/bin")),
                         "HOME": tmp, "TMPDIR": tmp, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                         "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
            broken = subprocess.run([str(pytest_path), "-q", "tests/test_buggy.py"], cwd=workspace,
                                    env=clean_env, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=10)
            fixed_env = {**clean_env, "PYTHONPATH": str(workspace)}
            fixed = subprocess.run([str(pytest_path), "-q", "tests/test_buggy.py"], cwd=workspace,
                                   env=fixed_env, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=10)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("No module named 'buggy'", broken.stdout)
            self.assertEqual(fixed.returncode, 0, fixed.stdout)

    def test_v2_assembly_requires_shared_gate_and_old_modes_remain_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace, hashes = self.make_workspace(root)
            wrapper = root / "wrapper.py"
            wrapper.write_text("# test only\n", encoding="utf-8")
            config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/experiment_v2.yaml").read_text())
            mock = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []}, request=request))
            control = assemble_task(config, workspace, wrapper, condition="control", protocol_guard=True,
                                    initial_hashes=hashes, public_test_command=TEST_COMMAND,
                                    mock_transport=mock)
            self.assertIsNotNone(control["submission_gate"])
            self.assertIsNone(control["feedback"])
            control["adapter"].restore()
            control["http_client"].close()

            with self.assertRaises(ValueError):
                assemble_task(config, workspace, wrapper, condition="enhanced_v2", protocol_guard=False,
                              mock_transport=mock)
            v2 = assemble_task(config, workspace, wrapper, condition="enhanced_v2", protocol_guard=True,
                               initial_hashes=hashes, public_test_command=TEST_COMMAND,
                               mock_transport=mock)
            self.assertIsNotNone(v2["submission_gate"])
            self.assertIsInstance(v2["feedback"], FailureAwareFeedbackV2)
            self.assertEqual(v2["budget"].limit, 12)
            v2["adapter"].restore()
            v2["http_client"].close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
