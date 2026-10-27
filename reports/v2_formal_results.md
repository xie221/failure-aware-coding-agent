# Failure-Aware Coding Agent v2：正式三组实验结果

## 执行状态

- 实验根目录：`runs/benchmark-v2-three-arm-20261027/`
- v2 manifest SHA-256：`bdc876aa00cd135082f72b1cc633e9792839aa35005aecdbd03c8df7f7718eff`
- Control / Enhanced / Enhanced v2：各启动 20 项、各独立评分 20 项；共 60/60 评分。
- 恢复后的项目 batch 退出码：0。首次启动在 Control/L01 后退出码 2，原因是运行器把“0 parsed actions == 0 wrapper invocations”错误判为审计失败。该任务已评分且**没有重跑**；旧状态另存，修正后的只读一致性门控代码指纹及原因记入 `runtime_code_amendments.jsonl` 和 `prepared_manifest.json`。除这一项仅影响零 Action 审计/续跑的修正外，模型、提示、任务、预算、评分和 Agent 执行逻辑未变。
- 三组所有任务的模型终止原因均为 `RepeatedFormatError`；每任务 3 次模型请求，全部收到 HTTP 200，均未解析出 Shell Action。三组总计 180 HTTP attempts，低于每项 12 次上限。

## 三组实测结果

| 指标 | Control | Enhanced | Enhanced v2 |
|---|---:|---:|---:|
| 启动 / 独立评分 | 20 / 20 | 20 / 20 | 20 / 20 |
| 公开测试通过 | 0/20 (0%) | 1/20 (5%) | 0/20 (0%) |
| 隐藏测试通过 | 0/20 (0%) | 0/20 (0%) | 0/20 (0%) |
| 补丁正确（公开与隐藏均通过） | 0/20 (0%) | 0/20 (0%) | 0/20 (0%) |
| 正常提交 | 0/20 (0%) | 0/20 (0%) | 0/20 (0%) |
| 平均 HTTP attempts | 3.00 | 3.00 | 3.00 |
| 平均输入 Token | 1,126.50 | 1,126.50 | 1,126.50 |
| 平均输出 Token | 247.55 | 249.60 | 249.60 |
| 平均总 Token | 1,374.05 | 1,376.10 | 1,376.10 |
| 平均 Agent 运行时间 | 4.243 s | 4.267 s | 4.249 s |
| 重复失败命令 | 0 | 0 | 0 |
| Failure-Aware 反馈触发 | 0 | 0 | 0 |
| 工作区未变化重试拦截 | 0 | 0 | 0 |
| 解析出的 Shell Actions | 0 | 0 | 0 |

Enhanced 唯一的公开测试通过来自 D02，但其 workspace `buggy.py` 哈希与未修改源完全相同，且隐藏测试失败，因此不是 Agent 补丁成功。D02 使用 `list(set(...))`，结果顺序受 Python hash 随机化影响；一次公开 pytest 偶然通过，另两组不通过。此为数据集公开测试非确定性风险，原冻结 Benchmark 未在本轮修改；不应把 Enhanced 的 1/20 公开通过解释为性能提升。三组补丁正确率均为 0，全部类别均为 0/4。

## 轨迹归因

轨迹显示模型没有遵循共同 system prompt 所要求的单个 `mswea_bash_command` 代码块。它返回普通 `bash` 标签、解释文字和多个代码块；mini-SWE-agent 解析器因此将每个 response 判为格式错误。每个任务经过 3 次 HTTP 200 模型响应后以 `RepeatedFormatError` 结束。没有调用 Shell wrapper、没有修改源码、没有 Agent 侧运行 pytest、没有提交，也没有触发 Enhanced/Enhanced v2 的观察反馈或重复命令守卫。公开/隐藏测试均是在 Agent 结束后由 evaluator 独立运行。

因此本轮无法检验 Failure-Aware 的“错误反馈是否减少重试”或“反馈压缩是否节省输入 Token”：反馈路径没有被触发，重复命令数为零。输入 Token 三组均值相同；Enhanced 两组输出 Token 均值比 Control 高 2.05 tokens，属于本样本实际差异，但不是性能收益。verified-submit gate 因没有 Agent submit Action 而未被触发，不能据本轮判断它减少了无效提交。

最明确的问题是当前模型与 mini-SWE-Agent 工具动作格式之间的兼容性/遵循率不足，而非 API、GPU、attempt budget、Bubblewrap 或 evaluator 故障。由于三组共用同一格式协议且都零 Action，组间算法效果不可识别。不能把旧 v1 的分数混入本次 v2 结果。

## 完整性与风险

- 60/60 任务有 trajectory、HTTP ledger、task result、launcher status、score 与公开/隐藏 evaluator 日志。
- 180/180 ledger attempts 为 HTTP 200；每任务 3/12 attempts；没有预算耗尽、API 错误或外部超时。
- 60/60 workspace 只含 manifest 允许的三个公开文件；包装器审计均为 0 Action/0 wrapper 的一致记录，进程组清理均已核实。
- evaluator 独立保存公开和隐藏日志；Agent workspace 未含隐藏测试或参考修复。
- v2 manifest hash 在运行前后匹配。旧 v1 实验未修改。
- `results.jsonl` 保留首次误报的 infrastructure-stop 行；后续恢复事件及原始 launcher/prepared manifest 状态均另有记录。以最终 `batch_exit_status.txt=0`、progress 事件、60 项 score 和本报告为恢复后的最终状态；`host_batch_exit_status.txt=2` 是首次 host 脚本退出状态，刻意保留。
- 数据集由项目开发期间构建，不是完全独立的外部 held-out benchmark。D02 测试随机性降低了公开通过率的可重复性；不能在本轮结果上改测试或补跑。

## 结果文件

- 总表：`comparison_v2_summary.json`、`comparison_v2_per_task.csv`
- 扩展 Token/attempt/完整性逐任务指标：`v2_detailed_metrics.json`
- 原始数据：各组任务目录中的 `trajectory.json`、`http_attempts.json`、`task_result.json`、`score.json`、workspace 与 `evaluator_only/`。由于 60 项均无文件修改，没有生成非空 patch diff；workspace 与 source 的 SHA-256 一致可验证这一点。
- 首次停止和恢复审计：`batch_progress.json`、`runtime_code_amendments.jsonl`、`prepared_manifest.before-runtime-amendment.json`、`control/L01/launcher_status.initial-audit-false-positive.json`

## 求职表述边界

本轮不支持“Failure-Aware 提高了修复率”或“降低了无效操作”的结论。可准确表述为：完成 20 任务、三组、60 次的统一预算评测管线；全部 60 项完成独立评分、请求 ledger 与进程清理审计；实验暴露出模型工具格式不兼容导致 0/60 Shell Action，并发现 D02 公开测试的 hash-order 非确定性。若将其作为项目经历，应强调诊断、可审计实验工程与负结果，不包装为算法性能提升。
