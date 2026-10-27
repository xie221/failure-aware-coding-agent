"""Evidence-bounded v2 feedback and verified-submit gate.

The gate is a shared execution-protocol layer for all arms of a future v2
comparison. It prevents the mini-SWE submit sentinel from ending a task until
the unchanged public test suite has passed against the current buggy.py.
"""
from __future__ import annotations

from collections import deque
import hashlib
from pathlib import Path
import re
import shlex
from typing import Callable

from .feedback import classify_failure


SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def _sha(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


class VerifiedSubmissionGate:
    """Delegate actions, but reject submission until a verified test pass."""

    def __init__(self, base, workspace: Path, *, public_test_command: str,
                 initial_hashes: dict[str, str]):
        self.base = base
        self.config = base.config
        self.workspace = Path(workspace).resolve(strict=True)
        self.public_test_command = _norm(public_test_command)
        self.initial_buggy_hash = initial_hashes.get("buggy.py")
        self.initial_test_hash = initial_hashes.get("tests/test_buggy.py")
        if not self.initial_buggy_hash or not self.initial_test_hash or not self.public_test_command:
            raise ValueError("verified submission gate requires pinned source/test hashes and test command")
        self.verified_buggy_hash: str | None = None
        self.records: list[dict] = []

    def _block_reason(self) -> str | None:
        current_buggy = _sha(self.workspace / "buggy.py")
        current_tests = _sha(self.workspace / "tests/test_buggy.py")
        if current_tests != self.initial_test_hash:
            return "public test file differs from its pinned original"
        if current_buggy is None or current_buggy == self.initial_buggy_hash:
            return "buggy.py has no verified source change"
        if current_buggy != self.verified_buggy_hash:
            return "public tests have not passed against the current buggy.py"
        return None

    def execute(self, action, cwd="", *, timeout=None):
        command = str(action.get("command", ""))
        if SUBMIT_MARKER in command:
            reason = self._block_reason()
            if reason:
                message = ("Submission gate blocked early submission: " + reason + ". "
                           "Inspect buggy.py, make the required implementation change, then run "
                           "the exact public test command: " + self.public_test_command)
                # Keep the attempt inside the ordinary isolated Shell path so
                # action-wrapper auditing remains complete; never execute the
                # model's submit command while it is blocked.
                safe_command = "printf '%s\\n' " + shlex.quote(message)
                self.base.execute({"command": safe_command}, cwd=cwd, timeout=timeout)
                self.records.append({"event": "submission_blocked", "reason": reason})
                return {"output": message, "returncode": 125, "exception_info": ""}
            self.records.append({"event": "submission_allowed_after_verified_tests"})
            return self.base.execute(action, cwd=cwd, timeout=timeout)

        result = self.base.execute(action, cwd=cwd, timeout=timeout)
        # Only the exact manifest-pinned command can authorize submission.
        # Accepting arbitrary pytest-looking shell strings (e.g. collect-only,
        # a chained command, or a different target) would weaken this protocol.
        if _norm(command) == self.public_test_command:
            current_tests = _sha(self.workspace / "tests/test_buggy.py")
            current_buggy = _sha(self.workspace / "buggy.py")
            if (result.get("returncode") == 0 and current_tests == self.initial_test_hash
                    and current_buggy is not None and current_buggy != self.initial_buggy_hash):
                self.verified_buggy_hash = current_buggy
                self.records.append({"event": "public_tests_verified_for_source",
                                     "buggy_sha256": current_buggy})
            else:
                self.verified_buggy_hash = None
                self.records.append({"event": "public_tests_not_verified",
                                     "returncode": result.get("returncode")})
        return result

    def get_template_vars(self, **kwargs):
        return self.base.get_template_vars(**kwargs)

    def serialize(self):
        result = self.base.serialize()
        result.setdefault("info", {}).setdefault("config", {})["verified_submission_gate"] = True
        return result

    def cleanup(self):
        return self.base.cleanup()


class FailureAwareFeedbackV2:
    """Short, deduplicated feedback from visible tool evidence; no model call."""

    def __init__(self, remaining_attempts: Callable[[], int], *, public_test_command: str):
        self.remaining_attempts = remaining_attempts
        self.public_test_command = _norm(public_test_command)
        self.public_tests_passed = False
        self._seen: set[tuple[str, str]] = set()
        self.recent_failures: deque[str] = deque(maxlen=8)
        self.records: list[dict] = []
        self.suppressed_duplicate_feedback = 0

    def observe(self, command: str, result: dict) -> str | None:
        output = str(result.get("output", ""))
        rc = int(result.get("returncode", -1))
        normalized = _norm(command)
        is_public_test = normalized == self.public_test_command
        if is_public_test and rc == 0:
            self.public_tests_passed = True

        if "Submission gate blocked early submission:" in output:
            trigger = "early_submission_blocked"
            evidence = output.splitlines()[0][:180]
            advice = ("Do not submit yet. Make or inspect the buggy.py change, run the exact public "
                      "test command shown above, and submit only if it passes without a later edit.")
        else:
            failure = classify_failure(command, output, rc)
            if failure is None and rc == 0:
                if (is_public_test and self.public_tests_passed
                        and int(self.remaining_attempts()) <= 3):
                    trigger = "verified_tests_low_budget"
                    evidence = "The exact public test command returned exit code 0."
                    advice = ("Do not edit the tested source. Submit now using the task's required "
                              "mini-SWE submission protocol.")
                else:
                    return None
            elif failure is not None:
                trigger, evidence = failure.trigger, failure.evidence.strip().splitlines()[0][:180]
                advice = failure.next_action
            else:
                trigger, evidence = "nonzero_command", output.strip().splitlines()[-1:]
                evidence = evidence[0][:180] if evidence else "command returned nonzero"
                advice = "Use the visible error to choose one different, narrowly scoped action."

        key = (trigger, hashlib.sha256(evidence.encode("utf-8", "replace")).hexdigest()[:16])
        if key in self._seen:
            self.suppressed_duplicate_feedback += 1
            return None
        self._seen.add(key)
        if rc != 0:
            self.recent_failures.append(hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:12])
        remaining = max(0, int(self.remaining_attempts()))
        rendered = ("<failure_feedback_v2>\n"
                    f"type: {trigger}\nevidence: {evidence}\n"
                    f"next: {advice}\nattempts_left: {remaining}\n"
                    "</failure_feedback_v2>")
        self.records.append({"trigger": trigger, "evidence": evidence,
                             "feedback_chars": len(rendered),
                             "remaining_http_attempts": remaining})
        return rendered
