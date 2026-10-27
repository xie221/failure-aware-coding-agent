# Experiments

## Formal paired comparison (completed)

Run root: `runs/formal-control-enhanced-final-20261023-v2/`

Condition order: Control (20 tasks), then Enhanced (the same 20 task IDs), serially.
Status: batch exit code `0`; all 40 tasks have an independent public/hidden score. The prior interrupted debug batch and the A01 smoke are preserved but excluded.

Both conditions used the frozen `experiment_config.yaml`: Qwen2.5-Coder-7B-Instruct via synchronous, non-streaming `hosted_vllm`, temperature 0, top-p 1, seed 0, retry limits 0, up to 12 HTTP attempts, 30-second Shell actions, 150-second Agent limit, and 180-second external limit. Enhanced adds deterministic failure-aware feedback and the unchanged-workspace repeated-failure guard; Control does not. Each task used a fresh public-only workspace. Scoring ran afterward in evaluator-only copies.

| Metric | Control | Enhanced | Enhanced − Control |
|---|---:|---:|---:|
| Tasks started / independently scored | 20 / 20 | 20 / 20 | — |
| Public tests passed | 0/20 (0%) | 4/20 (20%) | +4 tasks; +20 pp |
| Hidden tests passed | 14/20 (70%) | 14/20 (70%) | 0 pp |
| Patch correct (public **and** hidden pass) | 0/20 (0%) | 3/20 (15%) | +3 tasks; +15 pp |
| Protocol completion (`Submitted`) | 6/20 (30%) | 5/20 (25%) | −1 task; −5 pp |
| `LimitsExceeded` termination | 14/20 (70%) | 15/20 (75%) | +1 task; +5 pp |
| Mean HTTP attempts | 9.40 | 9.60 | +0.20 (+2.1%) |
| Mean total tokens | 8,883.5 | 10,146.7 | +1,263.25 (+14.2%) |
| Mean Agent runtime | 6.075 s | 5.158 s | −0.918 s (−15.1%) |
| Repeated failed commands | 86 | 62 | −24 (−27.9%) |
| Enhanced feedback events | 0 | 93 | +93 |
| Unchanged-workspace retries suppressed | 0 | 39 | +39 |
| External timeouts / cleanup failures | 0 / 0 | 0 / 0 | — |

Enhanced-only patch-correct tasks were A01, A02, and C05; there were no Control-only wins and no task was correct in both conditions. A simple exact paired test over the three discordant outcomes gives two-sided p=0.25; this small one-run-per-task study does **not** establish a statistically reliable general improvement. The observed +15 percentage-point patch-correct difference is worth reporting as exploratory evidence, not as proof. Token cost increased, protocol completion declined slightly, and hidden-test pass rate was unchanged.

Category patch correctness (Control → Enhanced): basic logic 0/5 → 2/5; boundary conditions 0/5 → 0/5; exception handling 0/5 → 1/5; data structures/multistep 0/5 → 0/5. Each category has only five tasks.

## Execution integrity and artifacts

- Every task has a result and independently recorded public and hidden test outcome; no score is unavailable.
- HTTP-attempt ledgers match recorded transport attempts and remain within the per-task limit of 12. All task process-group cleanup checks passed.
- Benchmark manifest SHA-256 before and after is `672a075e89522d6fd329c47c113863c21cdc178c95fa904f4bce386564c10d7e`.
- Control C04 initially produced an unavailable score because the Agent removed the expected `buggy.py`. It was scored without rerunning the Agent: the evaluator used the hash-verified original public starter as the no-change candidate. The run records this source explicitly.
- The batch was resumed only in its already claimed root. Previously executed tasks were skipped; only never-started tasks ran. The initial interrupted debug batch and the separate A01 smoke remain excluded.

Machine-readable results: `runs/formal-control-enhanced-final-20261023-v2/baseline_vs_enhanced_summary.json`, `per_task_results.csv`, and `independent_scores.json`. Per-task trajectories and ledgers are retained under ignored `runs/` and are not intended for direct public upload.

## Interpretation

The data supports two descriptive observations: Enhanced had three correct patches where Control had none, and repeated failed commands fell by 27.9% while the guard suppressed 39 unchanged-workspace retries. It does not isolate the individual contribution of feedback versus repeat suppression, prove causality, or establish a general patch-correctness gain. There was one model run per task, only 20 paired tasks, and outcomes remain subject to model variability despite fixed generation parameters. Do not selectively rerun failures or pool the earlier Stage 6A/6B experiments into this comparison.

## v2 three-arm comparison (completed; no measured patch gain)

Run root: `runs/benchmark-v2-three-arm-20261027/`
Benchmark: development-created `agent-benchmark-v2`, manifest SHA-256 `bdc876aa00cd135082f72b1cc633e9792839aa35005aecdbd03c8df7f7718eff`. This dataset is newly frozen but not independent of all project development activity.

| Metric | Control | Enhanced | Enhanced v2 |
|---|---:|---:|---:|
| Started / independently scored | 20 / 20 | 20 / 20 | 20 / 20 |
| Public pass | 0/20 | 1/20* | 0/20 |
| Hidden pass | 0/20 | 0/20 | 0/20 |
| Patch correct (both suites) | 0/20 | 0/20 | 0/20 |
| Submitted | 0/20 | 0/20 | 0/20 |
| Mean HTTP attempts | 3.00 | 3.00 | 3.00 |
| Mean input / output / total tokens | 1126.50 / 247.55 / 1374.05 | 1126.50 / 249.60 / 1376.10 | 1126.50 / 249.60 / 1376.10 |
| Mean Agent runtime | 4.243 s | 4.267 s | 4.249 s |
| Repeated failed commands / feedback triggers | 0 / 0 | 0 / 0 | 0 / 0 |

All 60 runs ended with `RepeatedFormatError` after three HTTP-200 model responses; zero Shell actions were parsed or executed. Thus no arm actually exercised failure feedback, retry suppression, or verified-submit behavior. These results show no repair improvement and cannot compare algorithm efficacy. The observed token/runtime differences are descriptive only.

*Enhanced D02's public tests passed once while the workspace source hash remained identical to the unmodified `list(set(...))` implementation; its hidden tests failed. Set iteration order is hash-randomized, making this public result nondeterministic. The frozen dataset was not altered or rerun; treat the 1/20 public pass as a test-quality caveat, not an Agent success.

The first batch invocation stopped after Control/L01 because the runner required at least one parsed action to accept a wrapper audit. L01's zero actions and zero wrapper calls were consistent; process cleanup and evaluator results were valid. The original launcher status, prepared manifest, partial summary, and initial audit status were preserved. A narrowly scoped runner amendment corrected zero-action audit handling, was covered by the offline suite, and the already-claimed batch resumed without rerunning L01. Final resumed batch exit code was 0. Details: [reports/v2_formal_results.md](reports/v2_formal_results.md), `runs/benchmark-v2-three-arm-20261027/v2_detailed_metrics.json`.

## Protocol compatibility follow-up and v2.1 preparation

Trajectory inspection established that mini-SWE-agent 2.4.6's parser accepts only one `mswea_bash_command` fenced block. In the failed v2 run, all 60 raw responses had zero matching blocks and were preserved unchanged into `FormatError`; the model used ordinary `bash` fences, prose, and often multiple blocks. The successful v1 prompt and correction message included the literal parser-compatible fence. The shared v2 prompt/correction have now restored that v1 contract; parser strictness and all three arm implementations are unchanged. See [reports/v2_tool_protocol_diagnosis.md](reports/v2_tool_protocol_diagnosis.md).

One fresh v1 A01 development smoke was run per arm after the prompt correction. All three produced 12 parsed and wrapper-audited Shell Actions and ran public pytest, confirming protocol/transport/tool execution. All three exhausted 12 HTTP attempts, made speculative edits without first reading source, and failed public tests; hidden tests happened to pass, so all three overall patch scores remain false. These are development diagnostics only, not comparative efficacy results. Full artifacts are in `runs/protocol-smoke-v1-develop-20261027T184855/`.

The D02 set-order issue was corrected only in a separate `agent-benchmark-v2.1` copy. It is explicitly derived from the development-created v2 set, not a blind or external test set. Quality validation confirmed buggy public/hidden failures 20/20 and reference public/hidden passes 20/20. The old v2 benchmark, its 60 scored results, and its one-shot claim remain unchanged.

The v2.1 three-arm experiment completed at `runs/protocolfix-v2.1-three-arm-20261027/`: 60/60 tasks were independently scored, all 60 process cleanup records were normal, and no tool-format error occurred. Manifest SHA-256 is `815668d97cb88c2c12017ae829637dc5ba0c3826bd7459d8f22caefeaf274071`; frozen config SHA-256 is `112d052f4d7f68ac19a2decc7a2a62587471e00a7cda08aa157a45ffda638c00`. Do not rerun this one-shot batch or the old v2 root. See [reports/v2_1_formal_results.md](reports/v2_1_formal_results.md) for metrics, paired outcomes, and limitations.

### v2.1 measured results

| Metric | Control | Enhanced | Enhanced v2 |
|---|---:|---:|---:|
| Patch correct | 3/20 (15%) | 4/20 (20%) | 4/20 (20%) |
| Public / hidden passes | 4/20 / 4/20 | 4/20 / 4/20 | 4/20 / 4/20 |
| Submitted | 3/20 | 2/20 | 3/20 |
| Mean HTTP attempts | 11.05 | 11.35 | 11.30 |
| Mean input / output / total tokens | 10,612 / 384 / 10,996 | 12,158 / 317 / 12,475 | 11,057 / 330 / 11,387 |
| Repeated failed commands | 71 | 89 | 87 |
| Feedback triggers / retry skips | 0 / 0 | 132 / 72 | 63 / 62 |
| Mean Agent runtime | 8.107 s | 6.251 s | 6.475 s |
| Parsed / actually executed Shell commands | 207 / 207 | 213 / 141 | 214 / 152 |

Enhanced v2 had the same 4/20 correct patches as Enhanced, with B04 newly correct and E01 no longer correct. It lowered average input tokens by 9.1% and total tokens by 8.7% versus Enhanced, but did not reduce repeated failed commands materially (89→87); relative to Control the repeated-failure count was higher. This is a one-run, n=20 exploratory comparison on a development-created dataset; no general or causal performance claim is justified. See the full report for exact task transitions, gate-event counts, and evidence limitations.
