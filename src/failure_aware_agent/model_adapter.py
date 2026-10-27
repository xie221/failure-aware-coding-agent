"""Fail-closed Stage 6F adapter for mini-SWE-agent model and Reflection calls."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from typing import Iterator

import litellm
from minisweagent.exceptions import LimitsExceeded

from .transport_budget import AttemptBudget, AttemptBudgetExceeded


class AgentAttemptBudgetExceeded(LimitsExceeded):
    """Stable mini-SWE-agent interrupt carrying an explicit budget exit reason."""

    def __init__(self):
        super().__init__({
            "role": "exit",
            "content": "Stage6F: HTTP attempt budget exhausted",
            "extra": {
                "exit_status": "LimitsExceeded",
                "limit_kind": "http_attempt_budget",
                "submission": "",
            },
        })


class UnsafeModelCall(RuntimeError):
    """Raised rather than permitting an unbudgeted client/mode fallback."""


_call_kind: ContextVar[str] = ContextVar("stage6f_call_kind", default="agent_decision")


def _contains_budget_exhaustion(exc: BaseException) -> bool:
    pending = [exc]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, AttemptBudgetExceeded):
            return True
        for attr in ("__cause__", "__context__", "original_exception", "cause"):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


class MiniSWEAttemptAdapter:
    """Inject one Stage 6E HTTPHandler into real model.query and reflection calls.

    `install()` wraps the LiteLLM public completion entry used by both paths. It
    validates identity/mode/retries on every call and tags the transport scope.
    """

    def __init__(self, budget: AttemptBudget, handler, *, expected_model: str):
        self.budget = budget
        self.handler = handler
        self.expected_model = expected_model
        self.terminations: list[dict] = []
        self._original_completion = None
        self._previous_retries = None
        self._model = None
        self._request_kind_provider = None

    def install(self, model) -> None:
        if self._original_completion is not None:
            raise RuntimeError("adapter already installed")
        if os.environ.get("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT") != "1":
            raise UnsafeModelCall("mini-SWE-agent retry count must be set to one attempt")
        if litellm.num_retries not in (None, 0):
            raise UnsafeModelCall("LiteLLM global retries must be disabled")
        if model.config.model_name != self.expected_model:
            raise UnsafeModelCall("unexpected model route")
        model.config.model_kwargs.update({
            "client": self.handler,
            "max_retries": 0,
            "num_retries": 0,
            "stream": False,
            "acompletion": False,
        })
        self._original_completion = litellm.completion
        self._previous_retries = litellm.num_retries
        self._model = model
        adapter = self

        def guarded_completion(*args, **kwargs):
            if kwargs.get("client") is not adapter.handler:
                raise UnsafeModelCall("controlled HTTPHandler missing; refusing default-client fallback")
            route = kwargs.get("model")
            if route != adapter.expected_model or not route.startswith("hosted_vllm/"):
                raise UnsafeModelCall("unexpected model route")
            if kwargs.get("stream", False) or kwargs.get("acompletion", False):
                raise UnsafeModelCall("only synchronous non-streaming calls are validated")
            if kwargs.get("max_retries", 0) != 0 or kwargs.get("num_retries", 0) != 0:
                raise UnsafeModelCall("LiteLLM retries must be disabled")
            kind = (adapter._request_kind_provider()
                    if adapter._request_kind_provider is not None else _call_kind.get())
            if kind not in {"agent_decision", "reflection"}:
                raise UnsafeModelCall("request kind is not in the audit allowlist")
            try:
                with adapter.budget.logical_call(kind):
                    return adapter._original_completion(*args, **kwargs)
            except Exception as exc:
                category = "budget_exhausted" if _contains_budget_exhaustion(exc) else "model_transport_error"
                adapter.terminations.append({
                    "request_kind": kind,
                    "category": category,
                    "exception_class": type(exc).__name__,
                })
                if category == "budget_exhausted":
                    raise AgentAttemptBudgetExceeded() from exc
                raise

        litellm.num_retries = 0
        litellm.completion = guarded_completion
        abort = list(getattr(model, "abort_exceptions", []))
        if AgentAttemptBudgetExceeded not in abort:
            abort.append(AgentAttemptBudgetExceeded)
        model.abort_exceptions = abort

    def restore(self) -> None:
        if self._original_completion is not None:
            litellm.completion = self._original_completion
            litellm.num_retries = self._previous_retries
            self._original_completion = None
            self._model = None
            self._request_kind_provider = None

    def bind_reflection_agent(self, agent) -> None:
        """Use the Stage 6B ReflectionAgent's actual kind marker for transport labels."""
        if getattr(agent, "model", None) is not self._model:
            raise UnsafeModelCall("ReflectionAgent is not using the adapter model")
        request_budget = getattr(agent, "request_budget", None)
        if request_budget is None or not hasattr(request_budget, "kind"):
            raise UnsafeModelCall("ReflectionAgent request-kind state is unavailable")
        self._request_kind_provider = lambda: request_budget.kind

    @contextmanager
    def reflection_scope(self) -> Iterator[None]:
        token = _call_kind.set("reflection")
        try:
            yield
        finally:
            _call_kind.reset(token)

    def reflection_completion(self, model) :
        """Stage 6F isolated equivalent of Stage 6B's direct LiteLLM reflection call."""
        if model is not self._model:
            raise UnsafeModelCall("Reflection model differs from the installed model")
        with self.reflection_scope():
            return litellm.completion(
                model=model.config.model_name,
                messages=[
                    {"role": "system", "content": "Return one short synthetic reflection."},
                    {"role": "user", "content": "Synthetic evidence only; suggest a next step."},
                ],
                **model.config.model_kwargs,
            )
