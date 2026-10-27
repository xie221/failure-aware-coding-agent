"""Evidence-grounded, no-extra-LLM feedback for failed tool observations."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import hashlib
import re
from typing import Callable


SUBMISSION_COMMAND = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


@dataclass(frozen=True)
class Feedback:
    trigger: str
    evidence: str
    next_action: str

    def render(self, remaining_attempts: int, *, public_tests_passed: bool) -> str:
        lines = [
            "<failure_aware_feedback>",
            f"trigger: {self.trigger}",
            f"evidence: {self.evidence}",
            f"next_action: {self.next_action}",
            f"remaining_http_attempts: {remaining_attempts}",
        ]
        if public_tests_passed and remaining_attempts <= 3:
            lines.append(
                "budget_guidance: Public tests just returned success; avoid unnecessary edits. "
                "When ready, follow the verified mini-SWE submission protocol with this standalone "
                f"command: {SUBMISSION_COMMAND}"
            )
        lines.append("</failure_aware_feedback>")
        return "\n".join(lines)


def _evidence_line(output: str, needles: tuple[str, ...], limit: int = 240) -> str:
    for line in output.splitlines():
        if any(needle in line.lower() for needle in needles):
            return line.strip()[:limit]
    return output.strip()[-limit:]


def _normalized(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip())


def classify_failure(command: str, output: str, returncode: int) -> Feedback | None:
    """Map visible command/output evidence to a specific, actionable hint."""
    cmd = command.lower()
    text = output.lower()
    if re.search(r"(^|\s)(nano|vim|vi)(\s|$)", cmd) and any(
        mark in text for mark in ("command not found", "not found", "no such file", "terminal")
    ):
        evidence = _evidence_line(output, ("command not found", "not found", "terminal", "no such file"))
        return Feedback(
            "editor_unavailable", evidence,
            "Do not retry this interactive editor. Make the smallest edit with an available "
            "non-interactive method (Python, sed, or a heredoc), then inspect the changed file.",
        )

    if any(mark in text for mark in ("no such file or directory", "filenotfounderror", "file not found")):
        evidence = _evidence_line(output, ("no such file or directory", "filenotfounderror", "file not found"))
        return Feedback(
            "missing_path", evidence,
            "Verify the current directory and the exact repository-relative path with pwd/ls; "
            "then use the observed path rather than repeating the failed one.",
        )

    if "sudo" in cmd or any(mark in text for mark in ("permission denied", "operation not permitted", "sudo:")):
        evidence = _evidence_line(output, ("permission denied", "operation not permitted", "sudo:"))
        return Feedback(
            "permission_or_sudo", evidence,
            "Do not use sudo or install packages. Check the target's permissions and restrict "
            "changes to files in the task workspace using already available tools.",
        )

    is_pytest = bool(re.search(r"(^|\s)(pytest|python(?:\d(?:\.\d+)?)?\s+-m\s+pytest)(\s|$)", cmd))
    if is_pytest and returncode != 0:
        if any(mark in text for mark in ("error collecting", "collection error", "importerror", "modulenotfounderror")):
            evidence = _evidence_line(output, ("error collecting", "collection error", "importerror", "modulenotfounderror"))
            return Feedback(
                "pytest_collection_error", evidence,
                "Resolve the shown import, collection, syntax, or test-name issue before rerunning pytest. "
                "Inspect the public test/source evidence; do not edit tests to hide the failure.",
            )
        evidence = _evidence_line(output, ("failed", "assert", "error"))
        return Feedback(
            "pytest_assertion_failure", evidence,
            "Use the visible expected-versus-actual assertion to adjust the buggy implementation, "
            "then rerun the same public test command once.",
        )

    if returncode != 0:
        evidence = _evidence_line(output, ("error", "failed", "not found", "traceback"))
        return Feedback("nonzero_command", evidence,
                        "Use this observed error to choose one different, narrowly scoped action; "
                        "do not repeat the command unchanged.")
    return None


class FailureAwareFeedback:
    """Bounded recent-failure memory and feedback audit, with no model calls."""

    def __init__(self, remaining_attempts: Callable[[], int]):
        self.remaining_attempts = remaining_attempts
        self.recent_failures: deque[tuple[str, str]] = deque(maxlen=12)
        self.failure_counts: Counter[str] = Counter()
        self.public_tests_passed = False
        self.records: list[dict] = []

    def observe(self, command: str, result: dict) -> str | None:
        output = str(result.get("output", ""))
        returncode = int(result.get("returncode", -1))
        is_pytest = "pytest" in command.lower()
        if is_pytest and returncode == 0:
            self.public_tests_passed = True

        normalized = _normalized(command)
        command_id = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:16]
        feedback = classify_failure(command, output, returncode)
        previous_same_failure = any(prev_cmd == normalized for prev_cmd, _ in self.recent_failures)
        if feedback is None and returncode != 0:
            feedback = Feedback("nonzero_command", _evidence_line(output, ("error", "failed", "traceback")),
                                "Use the observed failure to choose a different, evidence-backed action.")
        if feedback is None and is_pytest and returncode == 0 and self.public_tests_passed:
            remaining_now = max(0, int(self.remaining_attempts()))
            if remaining_now <= 3:
                feedback = Feedback(
                    "public_tests_passed_low_budget",
                    "The visible public pytest command returned exit code 0.",
                    "Avoid unnecessary edits. Finish by following the verified mini-SWE submission "
                    f"protocol with the standalone command: {SUBMISSION_COMMAND}",
                )
        if feedback is not None and previous_same_failure:
            feedback = Feedback(
                "repeated_failed_command",
                f"This command failed before; latest visible evidence: {feedback.evidence}"[:300],
                "Stop repeating the same failed command/method. Choose a different action supported by "
                "the visible error (for editing, use a non-interactive method).",
            )
        if returncode != 0:
            self.recent_failures.append((normalized, feedback.trigger if feedback else "nonzero"))
            self.failure_counts[command_id] += 1
        if feedback is None:
            return None

        remaining = max(0, int(self.remaining_attempts()))
        rendered = feedback.render(remaining, public_tests_passed=self.public_tests_passed)
        self.records.append({
            "trigger": feedback.trigger,
            "command_fingerprint": command_id,
            "evidence": feedback.evidence[:300],
            "injected_feedback": rendered,
            "remaining_http_attempts": remaining,
        })
        return rendered
