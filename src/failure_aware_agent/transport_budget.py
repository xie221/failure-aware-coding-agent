"""Stage 6E copy of the Stage 6D HTTPX budget transport, with audit fixes."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Iterator

import httpx


class AttemptBudgetExceeded(RuntimeError):
    """Raised before a delegate send when the global HTTP-attempt budget is spent."""


class InvalidRequestKind(ValueError):
    """A request kind outside the fixed audit vocabulary was supplied."""


REQUEST_KINDS = frozenset({"agent_decision", "reflection"})
_scope: ContextVar[tuple[str, str] | None] = ContextVar("stage6e_http_attempt_scope", default=None)


class AttemptBudget:
    """Thread-safe, process-local budget shared across logical calls and retries."""

    def __init__(self, limit: int, audit_path: str | Path | None = None):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        self.limit = limit
        self._lock = threading.Lock()
        self._attempts = 0
        self._logical_calls = 0
        self._dispatch_indexes: dict[str, int] = {}
        self.events: list[dict] = []
        self.audit_path = Path(audit_path) if audit_path is not None else None

    def _persist_locked(self) -> None:
        """Atomically persist metadata-only events, including in-flight attempts."""
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [{k: v for k, v in event.items() if not k.startswith("_")} for event in self.events],
            indent=2,
        ) + "\n"
        fd, temporary = tempfile.mkstemp(prefix="." + self.audit_path.name + ".", dir=self.audit_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.audit_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    @property
    def logical_calls(self) -> int:
        with self._lock:
            return self._logical_calls

    @contextmanager
    def logical_call(self, kind: str = "agent_decision") -> Iterator[str]:
        if kind not in REQUEST_KINDS:
            raise InvalidRequestKind("request kind is not in the audit allowlist")
        with self._lock:
            self._logical_calls += 1
            call_id = f"call-{self._logical_calls:06d}"
        token = _scope.set((call_id, kind))
        try:
            yield call_id
        finally:
            _scope.reset(token)

    def _begin(self, method: str) -> dict:
        scope = _scope.get()
        call_id, kind = scope if scope is not None else ("unscoped", "unspecified")
        with self._lock:
            # Dispatch index also advances for a rejected dispatch, so repeated
            # rejections remain distinguishable without inflating HTTP attempts.
            call_attempt = self._dispatch_indexes.get(call_id, 0) + 1
            self._dispatch_indexes[call_id] = call_attempt
            if self._attempts >= self.limit:
                self.events.append({
                    "event": "budget_rejected_before_send",
                    "logical_call_id": call_id,
                    "request_kind": kind,
                    "attempt_id": None,
                    "attempt_index_for_call": call_attempt,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "method": method,
                    "result": "not_sent",
                    "exception_class": "AttemptBudgetExceeded",
                })
                self._persist_locked()
                raise AttemptBudgetExceeded(f"HTTP attempt budget {self.limit} exhausted")
            self._attempts += 1
            event = {
                "event": "http_attempt",
                "logical_call_id": call_id,
                "request_kind": kind,
                "attempt_id": f"http-{self._attempts:06d}",
                "attempt_index_for_call": call_attempt,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "result": "in_flight",
                "status_code": None,
                "exception_class": None,
                "elapsed_ms": None,
            }
            self.events.append(event)
            self._persist_locked()
        event["_started_monotonic"] = time.monotonic()
        return event

    def _finish_response(self, event: dict, status_code: int) -> None:
        with self._lock:
            started = event.pop("_started_monotonic")
            event["status_code"] = status_code
            event["result"] = (
                "http_response" if status_code < 400 else
                "http_4xx_response" if status_code < 500 else "http_5xx_response"
            )
            event["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            self._persist_locked()

    def _finish_error(self, event: dict, exc: BaseException) -> None:
        with self._lock:
            started = event.pop("_started_monotonic")
            event["result"] = "transport_error_delivery_unknown"
            event["exception_class"] = type(exc).__name__
            event["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            self._persist_locked()

    def safe_events(self) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in event.items() if not k.startswith("_")}
                    for event in self.events]

    def write_jsonl(self, path: str | Path) -> None:
        Path(path).write_text("".join(json.dumps(e) + "\n" for e in self.safe_events()),
                              encoding="utf-8")


class BudgetedTransport(httpx.BaseTransport):
    """One atomic budget reservation for each HTTPX transport dispatch."""

    def __init__(self, budget: AttemptBudget, delegate: httpx.BaseTransport):
        self.budget, self.delegate = budget, delegate

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        event = self.budget._begin(request.method)
        try:
            response = self.delegate.handle_request(request)
        except BaseException as exc:
            self.budget._finish_error(event, exc)
            raise
        self.budget._finish_response(event, response.status_code)
        return response

    def close(self) -> None:
        self.delegate.close()


class RetryingTransport(httpx.BaseTransport):
    """Test-only retry shim positioned outside BudgetedTransport."""

    def __init__(self, delegate: httpx.BaseTransport, retries: int):
        self.delegate, self.retries = delegate, retries

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for index in range(self.retries + 1):
            try:
                return self.delegate.handle_request(request)
            except AttemptBudgetExceeded:
                raise
            except httpx.TransportError:
                if index >= self.retries:
                    raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.delegate.close()


def make_openai_client(budget: AttemptBudget, transport: httpx.BaseTransport,
                       *, api_key: str = "offline-placeholder"):
    """Stage 6D prototype helper; SDK retries are explicitly disabled."""
    from openai import OpenAI
    http_client = httpx.Client(transport=BudgetedTransport(budget, transport),
                               trust_env=False, timeout=2.0)
    return OpenAI(api_key=api_key, base_url="http://offline.invalid/v1",
                  http_client=http_client, max_retries=0, timeout=2.0)
