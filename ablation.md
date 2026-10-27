# Mechanism evidence and ablation limits

## Implemented mechanisms

The enhanced path contains several deterministic components:

1. `FailureAwareFeedback` classifies visible command/test failures and appends short actionable guidance to the returned Observation.
2. `RepeatFailureGuard` suppresses an exact failed command only if the public task workspace fingerprint is unchanged; the suppressed action is auditable and visible to the Agent.
3. `FailureAwareFeedbackV2` deduplicates feedback evidence and shortens hints; when the exact public suite passes with few attempts remaining, it gives a phase-specific submission reminder.
4. `VerifiedSubmissionGate` is shared execution protocol in v2/v2.1: it allows the submit sentinel only after the pinned public test command passes against the exact current changed source, with the public tests unchanged.

No component makes an extra LLM call. Control disables the Failure-Aware feedback and retry guard; the v2 comparison also applies its shared submit protocol as frozen in the experiment configuration.

## What the experiments support

- The earlier v1 Control/Enhanced run observed 3/20 versus 0/20 correct patches and fewer repeated failed commands in Enhanced (86→62), with higher mean token use. This is a single 20-task paired sample and did not isolate the feedback from the retry guard.
- The first v2 run produced 0/20 correct patches in all arms because all 60 responses failed the action-format parser before any Shell Action. It is evidence of a protocol-compatibility failure, not evidence for or against the feedback algorithm.
- After restoring a parser-compatible shared prompt and making the D02 test deterministic in a separate v2.1 dataset copy, the three-arm exploratory run scored 60/60 tasks. Correct patches were 3/20 Control, 4/20 Enhanced, and 4/20 Enhanced v2. Enhanced v2 tied Enhanced on overall correctness (B04 gain, E01 loss). Mean input tokens fell 9.1% versus Enhanced, while repeated failed commands changed only 89→87 and remained above Control's 71.
- The verified-submit gate logged blocked attempts and verified source/test hashes, but was enabled across arms; there is no no-gate condition to estimate a causal change in submission or correctness.

These results do not show that feedback or any individual component caused a repair-rate improvement. The v2.1 dataset was created during project development, each task was run once per arm, and multiple mechanisms differ together between Control and Enhanced. v1, v2, and v2.1 use different task/protocol conditions and must not be pooled.

## Follow-up needed for causal claims

A credible ablation would pre-register an untouched task set and compare: (a) feedback classifier only, (b) repeat guard only, (c) both together, and (d) control. Keep model parameters, prompts, attempt ceilings, execution, and evaluator fixed; use paired task IDs and multiple seeds. Publish per-task outcomes, token/attempt cost, skip counts, protocol completion, and uncertainty. Until then, report the v2.1 token reduction as a measured descriptive difference and do not claim a component-specific or general correctness gain.
