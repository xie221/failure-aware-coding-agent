import unittest

from failure_aware_agent.feedback import FailureAwareFeedback, classify_failure


class FeedbackTests(unittest.TestCase):
    def test_unavailable_editor_suggests_noninteractive_edit(self):
        hint = classify_failure("nano buggy.py", "bash: nano: command not found", 127)
        self.assertEqual(hint.trigger, "editor_unavailable")
        self.assertIn("non-interactive", hint.next_action)

    def test_missing_path_uses_observed_error(self):
        hint = classify_failure("cat src/buggy.py", "cat: src/buggy.py: No such file or directory", 1)
        self.assertEqual(hint.trigger, "missing_path")
        self.assertIn("pwd/ls", hint.next_action)

    def test_sudo_and_permission_failure_disallowed(self):
        hint = classify_failure("sudo apt-get install nano", "sudo: not allowed", 1)
        self.assertEqual(hint.trigger, "permission_or_sudo")
        self.assertIn("Do not use sudo", hint.next_action)

    def test_pytest_collection_and_assertion_are_distinguished(self):
        collection = classify_failure("pytest -q", "ImportError while importing test module", 2)
        assertion = classify_failure("python -m pytest -q", "FAILED test_buggy.py::test_x AssertionError", 1)
        self.assertEqual(collection.trigger, "pytest_collection_error")
        self.assertEqual(assertion.trigger, "pytest_assertion_failure")

    def test_repeated_failure_emits_nonrepeat_guidance(self):
        controller = FailureAwareFeedback(lambda: 8)
        first = controller.observe("nano buggy.py", {"output": "nano: not found", "returncode": 127})
        second = controller.observe("nano buggy.py", {"output": "nano: not found", "returncode": 127})
        self.assertIn("editor_unavailable", first)
        self.assertIn("repeated_failed_command", second)
        self.assertIn("Stop repeating", second)
        self.assertEqual(len(controller.records), 2)

    def test_public_test_passed_with_low_budget_uses_verified_submit_protocol(self):
        controller = FailureAwareFeedback(lambda: 2)
        hint = controller.observe("python -m pytest -q", {"output": "2 passed", "returncode": 0})
        self.assertIn("public_tests_passed_low_budget", hint)
        self.assertIn("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", hint)

    def test_success_without_low_budget_does_not_add_feedback(self):
        controller = FailureAwareFeedback(lambda: 8)
        self.assertIsNone(controller.observe("python -m pytest -q", {"output": "2 passed", "returncode": 0}))

    def test_same_command_after_success_is_not_misclassified_as_failed_repeat(self):
        controller = FailureAwareFeedback(lambda: 8)
        controller.observe("python -m pytest -q", {"output": "2 passed", "returncode": 0})
        self.assertEqual(len(controller.recent_failures), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
