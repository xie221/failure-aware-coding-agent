# Failure-Aware Coding Agent

基于 mini-SWE-agent 2.4.6 的研究型二次开发：把已发生的工具失败转成简洁、证据约束的后续 Observation，并在工作区未变化时抑制相同失败命令重放。反馈由程序确定性生成，不增加独立 Reflection 模型调用。

## 最新结果：v2.1 三组探索性对照

Control、Enhanced、Enhanced v2 在同一开发期间构建的 20 项 Python 修复任务上各运行一次，60/60 项均完成公开与隐藏测试独立评分。

| 指标 | Control | Enhanced | Enhanced v2 |
|---|---:|---:|---:|
| Patch correctness（公开与隐藏均通过） | 3/20 (15%) | 4/20 (20%) | 4/20 (20%) |
| 公开测试通过 | 4/20 | 4/20 | 4/20 |
| 隐藏测试通过 | 4/20 | 4/20 | 4/20 |
| 正常提交 | 3/20 | 2/20 | 3/20 |
| 平均 HTTP attempts | 11.05 | 11.35 | 11.30 |
| 平均输入 / 输出 / 总 Token | 10,612 / 384 / 10,996 | 12,158 / 317 / 12,475 | 11,057 / 330 / 11,387 |
| 重复失败命令 | 71 | 89 | 87 |
| 反馈触发 / 重试拦截 | 0 / 0 | 132 / 72 | 63 / 62 |

Enhanced v2 与 Enhanced 的正确率相同：B04 转为正确，E01 转为错误；平均输入 Token 减少 9.1%，但重复失败命令仅从 89 降到 87。**本实验没有证明 Failure-Aware v2 提高补丁正确率。** 每题每组只有一次运行，且评测集在项目开发期间构建；结果为探索性描述，不是外部盲测或统计显著性证据。完整指标、逐题变化与限制见 [v2.1 结果报告](reports/v2_1_formal_results.md)。

## 项目演进与历史结果

- **v1 配对实验：**旧 20 项任务的 Control/Enhanced 正确补丁分别为 0/20 和 3/20；重复失败命令 86→62，Enhanced 多消耗约 14.2% Token。单次小样本观察，反馈与重试守卫未单独消融，不能解释为已证明的普遍提升。详见 [Experiments](experiments.md)。
- **v2 三组实验：**Control、Enhanced、Enhanced v2 均为 0/20 正确；60/60 任务因模型输出格式不符合解析器约定而没有执行 Shell Action。该负结果促成了协议问题定位与共同 Prompt 修复，详见 [v2 报告](reports/v2_formal_results.md)。
- **协议修复开发 smoke：**三组都实际解析并执行 Shell Action、运行公开测试，但没有产生正确补丁；它验证执行链路，不纳入正式对照统计。
- **v2.1：**在单独数据副本中修正 D02 测试的顺序非确定性后完成新的三组探索比较。v1、v2、v2.1 的任务/协议条件不同，禁止合并正确率。

## 架构与源码入口

```text
mini-SWE Agent.query()
        │
        ├── LiteLLM completion guard ── HTTPHandler ── BudgetedTransport ── vLLM
        │                                (共享、逐 HTTP attempt 预算与脱敏账本)
        │
        └── Action → verified-submit gate → FailureAware Observation
                   → unchanged-workspace retry guard
                   → workspace-pinned Bubblewrap wrapper → Shell
                                                        │
                                Agent 退出后 evaluator-only public/hidden tests
```

主要实现入口：

- `src/failure_aware_agent/runtime.py`：组装 model/environment/Agent；注入 HTTP handler；为增强条件包装 Observation 与 retry guard。
- `src/failure_aware_agent/model_adapter.py`、`transport_budget.py`：fail-closed LiteLLM 调用适配、共享线程安全 HTTP attempt 预算、每次发送前检查与元数据账本。
- `src/failure_aware_agent/feedback.py`：旧版错误分类及具体可执行提示。
- `src/failure_aware_agent/retry_guard.py`：按规范化命令和工作区文件指纹检测重复失败；工作区改变后允许重试。
- `src/failure_aware_agent/enhanced_v2.py`：短反馈去重、剩余预算提示及 verified-submit gate。提交仅在固定公开测试命令通过且源码哈希仍匹配时允许。
- `src/failure_aware_agent/action_wrapper.py`：生成每任务绑定的 Shell wrapper；Action 通过 Bubblewrap/unshare 运行，Agent 工作区只含公开任务文件。
- `scripts/launch_experiment.py`、`scripts/run_task.py`：准备、预检、串行任务启动、持久化与批次控制。
- `scripts/score_finished_runs.py`、`scripts/summarize_v2_results.py`：Agent 结束后的隔离公开/隐藏评分及逐任务配对统计。

更详细的调用与数据边界见 [architecture.md](architecture.md)；机制可支持的结论与消融限制见 [ablation.md](ablation.md)。

## 离线验证与运行边界

离线测试使用 HTTPX MockTransport、预导入 socket/DNS 拦截和合成响应，不调用真实模型。需在兼容的 mini-SWE Python 环境中运行：

```bash
python scripts/run_offline_tests.py
```

本项目依赖 mini-SWE-agent 2.4.6、LiteLLM 1.105.1、HTTPX 0.28.1；正式任务数据集和上游仓库作为外部依赖，不随本项目公开副本分发。服务器专用的 `host_run_*.sh` 已列入 `.gitignore`，其中包含本机路径；请勿将这些脚本直接发布。要复现实验，需先在目标环境配置外部 Benchmark、mini-SWE 源码、模型服务和本机运行参数，并按报告中冻结的 manifest/config 版本执行。

## 发布与证据

建议发布源码、测试、配置模板、架构/实验/面试文档和脱敏的汇总 CSV/JSON；不要发布 `runs/` 下的原始模型轨迹、attempt 明细、evaluator-only 隐藏材料或大日志，也不要发布模型权重、凭据及宿主机专用脚本。v2.1 原始记录保存在本地忽略目录，报告不替代原始证据。

简历草稿见 [resume_project.md](resume_project.md)，面试问答见 [docs/interview_qa.md](docs/interview_qa.md)。
