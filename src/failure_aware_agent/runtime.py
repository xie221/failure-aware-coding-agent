"""Construct mini-SWE components with the shared HTTP attempt budget."""
from __future__ import annotations

from pathlib import Path
from types import MethodType

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from minisweagent.agents import get_agent
from minisweagent.environments.extra.bubblewrap import BubblewrapEnvironment
from minisweagent.models import get_model

from .feedback import FailureAwareFeedback
from .enhanced_v2 import FailureAwareFeedbackV2, VerifiedSubmissionGate
from .model_adapter import MiniSWEAttemptAdapter
from .persistence import atomic_write_json
from .retry_guard import RepeatFailureGuard
from .transport_budget import AttemptBudget, BudgetedTransport


SAFE_MODEL_KWARGS = frozenset({
    "temperature", "top_p", "max_tokens", "drop_params", "stream", "acompletion",
    "max_retries", "num_retries", "timeout",
})


class FeedbackEnvironment:
    """Delegate all actions to Bubblewrap, injecting visible feedback in output."""

    def __init__(self, base, controller: FailureAwareFeedback, retry_guard=None):
        self.base = base
        self.controller = controller
        self.retry_guard = retry_guard
        self.config = base.config

    def execute(self, action, cwd="", *, timeout=None):
        command = action.get("command", "")
        if self.retry_guard is not None and self.retry_guard.should_suppress(command):
            result = {
                "output": ("Failure-aware repeat guard: skipped this exact command because it "
                           "already failed and task workspace files are unchanged. Change the "
                           "source or use a meaningfully different command before retrying."),
                "returncode": 125,
                "exception_info": "",
            }
        else:
            result = self.base.execute(action, cwd=cwd, timeout=timeout)
            if self.retry_guard is not None:
                self.retry_guard.observe(command, result)
        hint = self.controller.observe(action.get("command", ""), result)
        if hint:
            result = dict(result)
            result["output"] = (str(result.get("output", "")) + "\n\n" + hint + "\n")
        return result

    def get_template_vars(self, **kwargs):
        return self.base.get_template_vars(**kwargs)

    def serialize(self):
        data = self.base.serialize()
        info = data.setdefault("info", {}).setdefault("config", {})
        info["failure_aware_feedback"] = True
        info["unchanged_workspace_repeat_guard"] = self.retry_guard is not None
        return data

    def cleanup(self):
        return self.base.cleanup()


def install_safe_model_serializer(model) -> None:
    """Avoid serializing the client object, endpoint, or credential into trajectories."""
    def safe_serialize(self):
        config = self.config
        kwargs = {k: v for k, v in config.model_kwargs.items()
                  if k in SAFE_MODEL_KWARGS and isinstance(v, (str, int, float, bool, type(None)))}
        safe_config = {
            "model_name": config.model_name,
            "model_kwargs": kwargs,
            "litellm_model_registry": None,
            "set_cache_control": config.set_cache_control,
            "cost_tracking": config.cost_tracking,
            "format_error_template": config.format_error_template,
            "observation_template": config.observation_template,
            "multimodal_regex": config.multimodal_regex,
        }
        if hasattr(config, "action_regex"):
            safe_config["action_regex"] = config.action_regex
        return {"info": {"config": {
            "model": safe_config,
            "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
        }}}
    model.serialize = MethodType(safe_serialize, model)


def install_atomic_agent_saver(agent) -> None:
    """Make mini-SWE's per-step trajectory saves atomic without editing upstream."""
    def save(self, path=None, *extra_dicts):
        data = self.serialize(*extra_dicts)
        if path:
            atomic_write_json(path, data)
        return data
    agent.save = MethodType(save, agent)


def make_budgeted_handler(budget: AttemptBudget, *, mock_transport=None):
    delegate = mock_transport if mock_transport is not None else httpx.HTTPTransport(retries=0)
    transport = BudgetedTransport(budget, delegate)
    client = httpx.Client(transport=transport, trust_env=False, timeout=httpx.Timeout(20.0))
    return HTTPHandler(client=client), client


def assemble_task(config: dict, workspace: Path, wrapper: Path, *, condition: str,
                  mock_transport=None, protocol_guard: bool = False,
                  initial_hashes: dict[str, str] | None = None,
                  public_test_command: str | None = None):
    if condition not in {"control", "enhanced", "enhanced_v2"}:
        raise ValueError("condition must be control, enhanced, or enhanced_v2")
    if condition == "enhanced_v2" and not protocol_guard:
        raise ValueError("enhanced_v2 requires the shared verified-submission protocol guard")
    if protocol_guard and (not initial_hashes or not public_test_command):
        raise ValueError("v2 protocol guard requires pinned workspace hashes and test command")
    agent_output_path = config.get("agent", {}).get("output_path")
    audit_path = Path(agent_output_path).parent / "http_attempts.json" if agent_output_path else None
    budget = AttemptBudget(12, audit_path=audit_path)
    litellm.num_retries = 0
    handler, http_client = make_budgeted_handler(budget, mock_transport=mock_transport)
    model_config = dict(config["model"])
    model_config["model_kwargs"] = dict(model_config.get("model_kwargs", {}))
    model_config["model_kwargs"].update({
        "api_base": model_config["model_kwargs"].get("api_base", "http://127.0.0.1:8000/v1"),
        "api_key": "local-placeholder-no-secret",
        "max_retries": 0,
        "num_retries": 0,
        "stream": False,
        "acompletion": False,
    })
    model = get_model(config=model_config)
    if model.config.model_name.split("/", 1)[0] != "hosted_vllm":
        http_client.close()
        raise RuntimeError("Only hosted_vllm is enabled in this runner")
    adapter = MiniSWEAttemptAdapter(budget, handler, expected_model=model.config.model_name)
    adapter.install(model)
    install_safe_model_serializer(model)

    environment = BubblewrapEnvironment(
        cwd=str(workspace.resolve()), executable=str(wrapper.resolve()), wrapper_args=[], timeout=30,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
    )
    submission_gate = None
    if protocol_guard:
        submission_gate = VerifiedSubmissionGate(
            environment, workspace, public_test_command=public_test_command,
            initial_hashes=initial_hashes,
        )
        environment = submission_gate
    if condition in {"enhanced", "enhanced_v2"}:
        if condition == "enhanced_v2":
            controller = FailureAwareFeedbackV2(
                lambda: budget.limit - budget.attempts,
                public_test_command=public_test_command or "python -m pytest -q tests/test_buggy.py",
            )
        else:
            controller = FailureAwareFeedback(lambda: budget.limit - budget.attempts)
        retry_guard = RepeatFailureGuard(Path(workspace))
        environment = FeedbackEnvironment(environment, controller, retry_guard)
    else:
        controller = None
        retry_guard = None

    agent_config = dict(config["agent"])
    # Exactly one condition difference: tool-observation feedback injection.
    agent_config["agent_class"] = "interactive"
    agent_config["step_limit"] = 12
    agent_config["wall_time_limit_seconds"] = 150
    agent_config["output_path"] = agent_config.get("output_path")
    agent_config["mode"] = "yolo"
    agent_config["confirm_exit"] = False
    agent_config["cost_limit"] = 9999.0
    agent = get_agent(model, environment, agent_config, default_type="interactive")
    install_atomic_agent_saver(agent)
    return {
        "agent": agent, "environment": environment, "budget": budget, "adapter": adapter,
        "http_client": http_client, "feedback": controller,
        "retry_guard": retry_guard, "submission_gate": submission_gate,
    }
