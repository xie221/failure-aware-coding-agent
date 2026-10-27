from __future__ import annotations

from pathlib import Path
import unittest

import yaml

from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_textbased_model import LitellmTextbasedModelConfig
from minisweagent.models.utils.actions_text import parse_regex_actions


ROOT = Path(__file__).resolve().parents[1]
ACTION_REGEX = r"```mswea_bash_command\s*\n(.*?)\n```"


class ToolProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v1 = yaml.safe_load((ROOT / "config/experiment.yaml").read_text())
        cls.v2 = yaml.safe_load((ROOT / "config/experiment_v2.yaml").read_text())
        cls.v2_1 = yaml.safe_load((ROOT / "config/experiment_v2_1.yaml").read_text())

    def test_v2_restores_v1_system_and_correction_protocol(self):
        self.assertEqual(self.v2["agent"]["system_template"], self.v1["agent"]["system_template"])
        self.assertEqual(self.v2["model"]["format_error_template"],
                         self.v1["model"]["format_error_template"])
        self.assertIn("```mswea_bash_command\n<one shell command>\n```",
                      self.v2["agent"]["system_template"])
        self.assertIn("```mswea_bash_command\n<one shell command>\n```",
                      self.v2["model"]["format_error_template"])

    def test_exact_mini_swe_action_block_is_parsed(self):
        result = parse_regex_actions(
            "```mswea_bash_command\nprintf 'probe'\n```",
            action_regex=ACTION_REGEX,
            format_error_template="expected one labeled action",
        )
        self.assertEqual(result, [{"command": "printf 'probe'"}])

    def test_single_plain_bash_block_is_rejected_fail_closed(self):
        with self.assertRaises(FormatError):
            parse_regex_actions(
                "```bash\nprintf 'probe'\n```",
                action_regex=ACTION_REGEX,
                format_error_template="expected one labeled action",
            )

    def test_multiple_blocks_are_rejected_fail_closed(self):
        with self.assertRaises(FormatError):
            parse_regex_actions(
                "```bash\nprintf 'one'\n```\n```bash\nprintf 'two'\n```",
                action_regex=ACTION_REGEX,
                format_error_template="expected one labeled action",
            )

    def test_three_conditions_share_one_protocol_config(self):
        # Conditions are runtime behavior switches; there is only one shared
        # v2 model/parser prompt, so no arm-specific format adaptation exists.
        self.assertEqual(self.v2["runner"]["condition_order"],
                         ["control", "enhanced", "enhanced_v2"])
        self.assertEqual(self.v2["model"]["model_class"], self.v1["model"]["model_class"])
        self.assertEqual(LitellmTextbasedModelConfig.model_fields["action_regex"].default,
                         ACTION_REGEX)

    def test_v2_1_formal_config_uses_same_protocol_and_versioned_benchmark(self):
        self.assertEqual(self.v2_1["agent"]["system_template"],
                         self.v1["agent"]["system_template"])
        self.assertEqual(self.v2_1["model"]["format_error_template"],
                         self.v1["model"]["format_error_template"])
        self.assertEqual(self.v2_1["runner"]["benchmark_path"], "../agent-benchmark-v2.1")
        self.assertEqual(self.v2_1["runner"]["condition_order"],
                         ["control", "enhanced", "enhanced_v2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
