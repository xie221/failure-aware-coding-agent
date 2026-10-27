# v2 tool-protocol diagnosis

## Finding

The v2 batch failure was at the text-action parser boundary, not at LiteLLM/vLLM transport or Shell isolation. mini-SWE-agent 2.4.6 uses ````mswea_bash_command\\s*\\n(.*?)\\n````, then requires exactly one match (`mini-swe-agent/src/minisweagent/models/litellm_textbased_model.py` and `models/utils/actions_text.py`). The local Bubblewrap environment only receives an action after this parser succeeds.

The successful v1 trajectory `runs/formal-control-enhanced-20261023T154155/control/A01/trajectory.json` stores a real response that is byte-for-byte the content passed to the parser and contains the exact labeled fence. Its saved system prompt and format-error template both show the complete fence and explicitly reject `bash`, `sh`, and unlabeled fences. By contrast, v2 `runs/benchmark-v2-three-arm-20261027/control/L01/trajectory.json` stores three HTTP-200 responses; each parser input equals the corresponding LiteLLM response content, each has ordinary `bash` fences / explanatory prose / multiple code blocks, and each yields zero regex matches. No response rewrite or normalization is evident between LiteLLM content and parser input.

Across v2, all 60 trajectories ended `RepeatedFormatError` after three HTTP attempts each (180 attempts total); no Shell Action was parsed, so Failure-Aware feedback and verified-submit were never reached. HTTP 200 established transport success only, not tool compatibility.

## Most likely cause and minimum correction

The v2 system prompt described the label in prose but omitted the literal fenced example used by the parser. Its correction message likewise said “one block labeled” without showing the exact syntax. The previously successful v1 prompt had both. This is the strongest directly evidenced difference relevant to the failure. Model, model class, action regex, sync/non-streaming settings, and HTTP retry configuration match. The v2 prompt also added substantially more procedural text; instruction dilution is plausible but not independently established.

The v2 shared prompt and correction template now restore the v1 format contract and literal example. No parser, model adapter, upstream source, budget, or arm-specific behavior was changed. A strict parser remains in place; ordinary or multiple code blocks are still rejected rather than guessed.

## Evidence boundary

The v1 trajectory demonstrates valid parsed actions, including shell edits and pytest commands. The v2 raw response is preserved in `FormatError.extra.model_response`; it exactly matches `response.choices[0].message.content`, while mini-SWE records zero parsed actions. The new three-arm real-model smoke in `runs/protocol-smoke-v1-develop-20261027T184855/` confirms the restored shared prompt now produces executable actions, but it does not establish repair quality: each A01 run exhausted its 12-attempt budget with public tests failing.

## D02 evaluator reliability

The frozen v2 D02 buggy implementation uses `list(set(items))`, while its public test asserts first-seen order. Eight read-only runs with different `PYTHONHASHSEED` values produced eight different orderings; one happened to match the expected output. The test was therefore nondeterministic as a detector. The original v2 dataset is unchanged. A separately versioned v2.1 copy adds a second permutation of the same set of values to both public and hidden tests; unordered set iteration cannot satisfy both required encounter orders. Full 20-task quality validation is recorded in `agent-benchmark-v2.1/benchmark_v21_validation_report.md`.

## Formal follow-up

The protocol smoke is sufficient to proceed to a new exploratory comparison, not to claim improved patch quality. Use the new v2.1 manifest and identical prompt/config across Control, Enhanced, and Enhanced v2. The v2.1 set is derived from a development-created dataset and remains non-independent of all development activity. Preserve the 2026-10-27 v2 batch and its one-shot claim unchanged.
