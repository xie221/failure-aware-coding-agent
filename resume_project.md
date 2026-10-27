# 简历项目描述

项目：**Failure-Aware Coding Agent｜mini-SWE-agent 二次开发与评测**

技术：Python、mini-SWE-agent 2.4.6、LiteLLM、HTTPX、Qwen2.5-Coder-7B-Instruct、pytest

以下按岗位方向二选一，不建议把两组内容拼成超过一段的项目经历。

## Agent 算法 / Agent 研发

- 基于 mini-SWE-agent 2.4.6 实现 Failure-Aware Observation 反馈：从 Shell 与测试结果识别常见错误，并将短小、可执行的提示注入后续上下文；增强路径不额外调用 Reflection 模型。
- 实现基于命令、失败结果与工作区状态的重复失败守卫，并加入反馈压缩和预算阶段提示；将 Control、Enhanced、Enhanced v2 固定在同一模型、工具协议和每任务最多 12 次 HTTP attempt 下比较。
- 在内部构建并冻结的 20 项 Python 修复任务上完成三组共 60 次运行和 60/60 独立评分；正确补丁分别为 Control 3/20、Enhanced 4/20、Enhanced v2 4/20。
- Enhanced v2 相对 Enhanced 的正确率持平（4/20），输入 Token 均值下降 9.1%、总 Token 下降 8.7%，重复失败命令仅由 89 降至 87；结果是单次小样本探索，未证明普遍或统计显著增益。

## Agent 后端 / LLM 应用研发

- 为同步、非流式 `hosted_vllm` 调用接入共享 HTTPX attempt 预算与审计账本；三组共记录 674 次 HTTP attempt，单任务不超过 12 次，账本中的响应均为 HTTP 200。
- 构建串行三组评测执行链，逐任务保存 trajectory、Action、Token、HTTP ledger、diff、评分和清理状态；60/60 个任务均产生独立评分记录。
- 实现 verified-submit gate 与 evaluator-only 评分流程：只有源码发生有效变化且公开测试通过后才允许正式提交；补丁正确性按公开和隐藏测试均通过计算，隐藏材料不进入 Agent 工作区。
- 通过真实轨迹定位并修复工具协议兼容问题：早期 v2 的 60/60 次运行均未解析出 Shell Action；协议修复后的 v2.1 共解析 634 个 Action、执行 500 次包装器调用，另有 134 次重复失败重试被守卫拦截。

## 面试时的结果边界

v2.1 是项目开发期间构建的内部探索集，不是外部盲测基准；每个任务每组仅运行一次。v1、v2、v2.1 条件不同，不能合并正确率。三组 v2.1 结果表明工程链路可审计，但 Enhanced v2 没有比旧 Enhanced 多修复任务；不要把 Token 降低或单个任务变化描述成已验证的因果算法收益。

详细逐任务结果见 [v2.1 实验报告](reports/v2_1_formal_results.md)，项目架构与面试问答见 [architecture.md](architecture.md) 和 [docs/interview_qa.md](docs/interview_qa.md)。
