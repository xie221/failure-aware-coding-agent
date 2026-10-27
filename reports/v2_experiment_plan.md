# Failure-Aware Coding Agent v2：冻结实验协议

## 状态与披露

新的 `agent-benchmark-v2` 已构建并完成冻结前行为验证；三组准备、Bubblewrap 执行与 evaluator 接线通过离线验证。正式三组 batch 已完成 60/60 独立评分，但模型没有生成任何可解析 Shell Action，三组 patch correctness 均为 0/20；真实结果与审计细节见 [v2_formal_results.md](v2_formal_results.md)。数据集在本项目开发期间创建，不能称为完全独立于所有开发活动的外部 held-out benchmark。构建/QA 阶段未使用模型输出筛选任务。v1 历史 40 条轨迹属于开发材料，不纳入 v2 结果。

- Benchmark: the development-created sibling dataset `agent-benchmark-v2/` (not an external held-out set).
- Manifest SHA-256：`bdc876aa00cd135082f72b1cc633e9792839aa35005aecdbd03c8df7f7718eff`
- 配置：`config/experiment_v2.yaml`
- 计划实验根目录：`runs/benchmark-v2-three-arm-20261027/`（当前不存在；宿主机脚本拒绝覆盖）
- Host launcher：`scripts/host_run_v2_batch.sh`
- Control / Enhanced / Enhanced v2：各 20 项，共 60 个 task-condition，串行且各运行一次。

## 数据集质量

20 个任务分为五类，每类 4 项：基础逻辑、边界条件、异常处理、数据结构/多步操作、非法输入/特殊情况。难度标注为 easy 7、medium 10、hard 3。每项含公开 `TASK.md`、`buggy.py`、`tests/test_buggy.py`；hidden tests 与 reference fixes 存于 evaluator-only 路径，并在 manifest 中显式记录路径和 SHA-256。

冻结前真实 pytest 验证：原始 buggy 版本 20/20 的公开测试失败且 20/20 的隐藏测试失败；参考修复 20/20 通过公开测试且 20/20 通过隐藏测试。任务工作区只包含三个公开文件，无符号链接。测试按函数行为断言，不以源字符串匹配作为评分。QA 阶段曾扩展 B01/B02/E02/I03/I04 的隐藏边界覆盖，并修正 I02 reference 对 generator iterable 的支持；以上均在冻结和任何模型运行前完成，详见 Benchmark `quality_report.md/json`。

## 冻结的三组条件

所有组共享：`hosted_vllm/Qwen2.5-Coder-7B-Instruct`、`litellm_textbased`、temperature 0、top-p 1、seed 0、同步非流式、SDK/LiteLLM retries 0、每任务最多 12 HTTP attempts、Shell 30 秒、Agent 150 秒、外部 180 秒、相同公共 Prompt、解析器、Bubblewrap wrapper、`PYTHONPATH=/workspace`、工具规则、verified-submit gate 与独立评分。每个 task-condition 从 Benchmark 源创建全新的 public-only 工作区；hidden tests/reference 不会复制到 Agent 工作区。

组间唯一预期差异：

| 组 | 反馈/守卫 |
|---|---|
| Control | 无额外错误反馈或重复失败守卫 |
| Enhanced | 旧版错误反馈与重复失败守卫 |
| Enhanced v2 | 短反馈、相同触发去重、测试已通过且预算较低时的阶段提示；无额外模型请求 |

实验顺序固定为 `control → enhanced → enhanced_v2`，每组按 manifest 顺序处理 20 个任务。此固定顺序可能与服务负载/时间漂移混杂，结果分析需披露。每个 task-condition 最多一次；已经开始的任务不会自动重试。完整性级故障停止批次并保留逐任务状态；普通任务失败仍评分并继续。

## 评分与指标

独立 evaluator 在 Agent 结束后分别运行公开与隐藏测试，使用单独的 evaluator-only 副本。`patch_correct = public_pass AND hidden_pass`。同时记录协议结束、attempt/token、重复失败、反馈触发、耗时和清理；补丁正确率不与正常提交率混为一谈。三组汇总器按共同 Task ID 配对并输出 JSON/CSV，不填补缺失值。

## 离线验证

最后一轮 `scripts/run_offline_tests.py`：**57/57 通过**。包含真实 v2 `prepare` CLI 为三组创建 60 个工作区、检查仅有公开文件及哈希、真实 mini-SWE `BubblewrapEnvironment → pinned wrapper → unshare → bwrap` 执行 `python -m pytest -q tests/test_buggy.py` 并确认导入成功（预期 buggy assertion failure）、独立评分路由、attempt ledger、提交 gate 和三组汇总。没有真实模型请求、vLLM API 请求或正式任务运行。

静态检查包括 Python compile、host launcher `bash -n` 和 CLI help。最终代码指纹以 preparation 生成的 `code_version_sha256` 机制冻结；环境没有 Git metadata，因此不用虚构 commit。正式准备时会把配置、manifest hash、代码指纹、任务/包装器 hash 写入 `prepared_manifest.json`。

## 启动

实验已经在 hj-server 上执行并使用 manifest hash one-shot claim。不要再次运行 `scripts/host_run_v2_batch.sh`：该脚本会拒绝现有实验根目录，且正式 task-condition 已运行一次。首轮零 Action audit 误报后的恢复记录保存在本次实验根目录；Control/L01 未重跑。最终结果与实际结束状态见 [v2_formal_results.md](v2_formal_results.md)。

## 限制

这是项目开发期自行构建并冻结的新评测集，任务数量为 20、每组仅单次运行，不能据此保证统计显著性或普遍泛化。固定组顺序有顺序效应风险；如不改变本轮冻结方案，报告结果时必须明确披露。没有单独消融时，不声称任一单项机制独立导致指标变化。不得根据 v2 跑分修改任务或选择性重跑。
