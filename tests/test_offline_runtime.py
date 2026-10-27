from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import litellm
from litellm.llms.custom_httpx import llm_http_handler

from failure_aware_agent.runtime import assemble_task
from failure_aware_agent.model_adapter import AgentAttemptBudgetExceeded, UnsafeModelCall
from failure_aware_agent.runtime import FeedbackEnvironment
from failure_aware_agent.feedback import FailureAwareFeedback
from failure_aware_agent.action_wrapper import render_action_wrapper
from failure_aware_agent.transport_budget import AttemptBudget, BudgetedTransport
import failure_aware_offline_state as network_state


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/experiment.yaml"
MODEL = "hosted_vllm/Qwen2.5-Coder-7B-Instruct"
SENT = []


def mock_response(request):
    SENT.append(request)
    return httpx.Response(200, json={
        "id": "offline-completion", "object": "chat.completion", "created": 1,
        "model": "Qwen2.5-Coder-7B-Instruct",
        "choices": [{"index": 0, "message": {"role": "assistant", "content":
                      "Synthetic result.\n```mswea_bash_command\nprintf synthetic\n```"},
                      "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }, request=request)


def config_dict():
    import yaml
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


class OfflineRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if network_state.blocked_attempts != 0:
            raise AssertionError("unexpected network/DNS attempt during imports")
        os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        cls.previous_retries = litellm.num_retries
        litellm.num_retries = 0

    @classmethod
    def tearDownClass(cls):
        litellm.num_retries = cls.previous_retries

    def setUp(self):
        SENT.clear()
        self.temp = tempfile.TemporaryDirectory(prefix="failure-aware-offline-")
        base = Path(self.temp.name)
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.wrapper = base / "wrapper.py"
        self.wrapper.write_text("# deliberately never executed in offline model test\\n")
        self.bundle = assemble_task(config_dict(), self.workspace, self.wrapper,
                                    condition="control", mock_transport=httpx.MockTransport(mock_response))

    def tearDown(self):
        self.bundle["adapter"].restore()
        self.bundle["http_client"].close()
        self.temp.cleanup()
        self.assertEqual(network_state.blocked_attempts, 0,
                         "network/DNS operation attempted during offline test")

    def test_real_mini_model_query_uses_only_injected_mock_and_safe_trajectory(self):
        from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel
        self.assertIsInstance(self.bundle["agent"].model, LitellmTextbasedModel)
        with patch.object(llm_http_handler, "_get_httpx_client",
                          side_effect=AssertionError("default HTTP client fallback")):
            message = self.bundle["agent"].model.query([
                {"role": "user", "content": "synthetic prompt marker"}
            ])
        self.assertIn("printf synthetic", message["content"])
        self.assertEqual((len(SENT), self.bundle["budget"].attempts), (1, 1))
        self.bundle["agent"].add_messages(message)
        saved = self.bundle["agent"].save(None)
        serialized = json.dumps(saved)
        for secretish in ("local-placeholder-no-secret", "127.0.0.1:8000", "synthetic prompt marker"):
            self.assertNotIn(secretish, serialized)
        self.assertEqual(network_state.blocked_attempts, 0)

    def test_attempt_budget_stops_thirteenth_request_before_mock_sender(self):
        # The Agent step budget is not involved here: exercise the actual model
        # entry directly to establish the transport-level ceiling.
        model = self.bundle["agent"].model
        for index in range(12):
            model.query([{"role": "user", "content": f"synthetic {index}"}])
        self.assertEqual((len(SENT), self.bundle["budget"].attempts), (12, 12))
        with self.assertRaises(AgentAttemptBudgetExceeded):
            model.query([{"role": "user", "content": "synthetic 13"}])
        self.assertEqual((len(SENT), self.bundle["budget"].attempts), (12, 12))
        self.assertEqual(self.bundle["adapter"].terminations[-1]["category"], "budget_exhausted")

    def test_missing_or_replaced_client_fails_closed_before_any_send(self):
        model = self.bundle["agent"].model
        model.config.model_kwargs["client"] = object()
        with self.assertRaises(UnsafeModelCall):
            model.query([{"role": "user", "content": "synthetic fail closed"}])
        self.assertEqual((len(SENT), self.bundle["budget"].attempts), (0, 0))

    def test_enhanced_mode_injects_feedback_into_real_environment_observation_shape(self):
        class FakeBase:
            config = object()

            def execute(self, action, cwd="", *, timeout=None):
                return {"output": "nano: command not found", "returncode": 127, "exception_info": ""}

            def serialize(self):
                return {"info": {"config": {}}}

            def get_template_vars(self, **kwargs):
                return {}

        feedback = FailureAwareFeedback(lambda: 7)
        env = FeedbackEnvironment(FakeBase(), feedback)
        observation = env.execute({"command": "nano buggy.py"})
        self.assertEqual(observation["returncode"], 127)
        self.assertIn("editor_unavailable", observation["output"])
        self.assertIn("non-interactive", observation["output"])
        self.assertEqual(len(feedback.records), 1)

    def test_workspace_wrapper_is_pinned_and_has_no_host_home_or_network_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = render_action_wrapper(base / "task", base / "audit.count",
                                           Path("/readonly/mini-env"), Path("/readonly/mini-src"))
            compile(source, "generated-wrapper.py", "exec")
            self.assertIn('"--net"', source)
            self.assertIn('"--bind", WORKSPACE, "/workspace"', source)
            self.assertIn('"--clearenv"', source)
            self.assertNotIn('"/home"', source)
            self.assertNotIn('"/home/', source)
            self.assertIn("add_empty_parent_dirs", source)
            self.assertNotIn('"/var/run/docker.sock"', source)
            self.assertEqual((len(SENT), self.bundle["budget"].attempts), (0, 0))

    def test_wrapper_builds_runtime_mount_parents_without_host_specific_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = render_action_wrapper(root / "task", root / "audit.count",
                                           Path("/readonly/mini-env"), Path("/readonly/project/mini-src"))
            namespace = {}
            exec(compile(source, "generated-wrapper.py", "exec"), namespace)
            captured = []
            previous_argv = namespace["sys"].argv
            workspace = str((root / "task").resolve())
            namespace["sys"].argv = ["wrapper.py", "--bind", workspace, workspace,
                                     "--chdir", workspace, "bash", "-c", "pwd"]
            try:
                with patch("os.execv", side_effect=lambda executable, argv: captured.extend(argv)):
                    namespace["main"]()
            finally:
                namespace["sys"].argv = previous_argv
            for expected in ("/readonly", "/readonly/project", "/readonly/mini-env",
                             "/readonly/project/mini-src"):
                self.assertIn(expected, captured)
            self.assertNotIn("/home", captured)

    def test_attempt_ledger_is_durable_and_records_only_safe_transport_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "attempts.json"
            budget = AttemptBudget(1, audit_path=audit)
            transport = BudgetedTransport(
                budget, httpx.MockTransport(lambda request: httpx.Response(200, request=request)))
            client = httpx.Client(transport=transport, trust_env=False)
            with budget.logical_call("agent_decision"):
                response = client.get("https://synthetic.invalid/path?marker=do-not-log")
            client.close()
            events = json.loads(audit.read_text(encoding="utf-8"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["result"], "http_response")
            serialized = json.dumps(events)
            for forbidden in ("synthetic.invalid", "marker", "do-not-log", "Authorization"):
                self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
