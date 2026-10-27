# 面试问答：Failure-Aware Coding Agent

以下回答严格按仓库实现和已完成实验表述。v2.1 是开发期间形成的数据集上的单次探索性比较，不是外部盲测，也没有证明普遍性能提升。

## Agent 与工具协议

### 1. 这个 Agent 的主循环是什么？

上游 mini-SWE-agent `DefaultAgent` 维护消息历史，在每个 step 调用 `model.query()`，将 assistant 返回的 action 交给 Environment 执行，再把结果格式化为 user-role Observation 加回历史。运行由提交、步数/成本限制、内部时间或异常终止。项目通过 `runtime.assemble_task()` 组装模型、环境与 Agent，没有修改上游 Agent loop。

### 2. Action 和 Observation 如何关联？

解析出的命令存于 assistant 消息 `extra.actions`；Environment 返回 output、return code 和异常信息。mini-SWE 的 `format_observation_messages()` 将它们转为后续上下文可见的 Observation。增强环境在底层执行后，仅基于这条命令及其已返回结果追加反馈，不额外读取 Agent 不可见的任务信息。

### 3. 为什么模型必须输出 `mswea_bash_command`？

当前安装版 `LitellmTextbasedModelConfig.action_regex` 用正则匹配该 label，并要求恰好解析出一个动作。普通 `bash` fence、多个代码块或零个匹配都会触发 `FormatError`。v2 的真实运行暴露了 60/60 次响应未遵循该协议、因此没有执行 Shell；修复是恢复三组共用的解析器一致 Prompt，而不是放宽解析器。

### 4. LiteLLM 的调用点在哪里？

上游 `LitellmTextbasedModel._query()` 调用 `litellm.completion(model, messages, **model_kwargs)`。本项目通过 `MiniSWEAttemptAdapter` 包装该入口，验证路由、HTTPHandler 身份、同步非流式模式和 retry 配置；不满足时 fail closed，不允许无预算的默认客户端回退。

### 5. HTTPX 预算与逻辑模型调用数有什么区别？

逻辑调用是 Agent 或其他调用者请求一次模型生成；HTTP attempt 是一次进入受控 HTTPX transport 的发送。`BudgetedTransport` 在 delegate sender 之前为每次发送原子预留预算并落账。若底层要重试，每次重发也必须重新过同一个 transport guard。账本只保存请求种类、ID、时间、状态/异常类别等元数据，不保存 prompt、URL、headers 或凭据。

### 6. 为什么用同一个共享预算？

若 Agent 决策和额外反思各自有预算，实际总发送数会超过实验上限。此项目的预算实例按任务初始化；所有经适配器的调用共享该实例，额度耗尽时在发送前拒绝。Failure-Aware 反馈本身是本地确定性逻辑，不发起额外模型请求。

### 7. 模型错误与工具错误如何区分？

模型/API 路径记录 LiteLLM/transport 错误或预算拒绝；工具路径记录解析后的 Shell Action 及 Environment Observation。HTTP 200 只表示收到了模型服务响应，不代表动作被解析、更不代表代码正确。v2 的 HTTP 200 与 0 Shell Action 正是这个区别的实例。

## 失败反馈与状态机制

### 8. Failure-Aware 反馈检测哪些失败？

`feedback.classify_failure()` 基于可见 command/output/return code 识别交互编辑器不可用、路径缺失、权限或 sudo、pytest collection/import 错误、pytest 断言失败和一般非零命令。反馈包含短证据与可执行的下一步，例如先核对实际路径或按 traceback 检查源码；不生成任务答案。

### 9. 重复失败守卫如何判断重试无效？

`RepeatFailureGuard` 对命令做保守规范化并计算 workspace fingerprint，覆盖相对路径、常规文件内容和权限位。只有完全相同的失败命令且 fingerprint 未变化才抑制重放，并把可见的 skip Observation 返回给 Agent；工作区变化后允许重试。遇到符号链接或读取错误时指纹不确定，策略 fail open，而不是错误拦截。

### 10. 它会不会在命令成功后仍然阻止后续操作？

成功执行会从失败状态中清除对应命令记录；workspace 发生变化也会让重试重新放行。测试覆盖这些状态转换。它不会替模型编辑代码，只能对完全相同且此前失败、文件状态未变的命令做拦截。

### 11. Enhanced v2 相对旧 Enhanced 的变化是什么？

`FailureAwareFeedbackV2` 将相同触发/证据的重复反馈去重，限制提示长度，并根据剩余 HTTP attempts 在确切公开测试通过后给出提交阶段提示。旧 Enhanced 使用旧版提示与触发记忆；两版都配有 unchanged-workspace retry guard。两组仅在本实验配置指定的反馈 profile 上不同。

### 12. verified-submit gate 怎样防止过早提交？

`VerifiedSubmissionGate` 固定公开测试命令以及初始源码/测试哈希。提交标记出现时，只有测试文件未被改动、`buggy.py` 确实改变，且当前源码哈希与最近一次精确公开测试命令成功时记录的哈希一致，才允许提交。测试通过后若源码再改，旧验证失效。此 gate 在 v2.1 三组共享，不是 Enhanced 独有功能。

## 执行与评测

### 13. Shell Action 如何执行？

`BubblewrapEnvironment` 使用每任务固定的 executable wrapper。wrapper 校验工作区 bind、cwd、环境变量和 bash argv，再通过 unshare 网络命名空间与 Bubblewrap 启动命令；任务工作区是可写持久任务目录，其余只读暴露必要运行环境。每条命令有 30 秒 timeout。公开仓库不会携带真实工作区、隐藏答案或宿主机启动参数。

### 14. 隐藏测试怎样与 Agent 隔离？

准备阶段只将 manifest 允许的公开文件复制到 Agent workspace。Agent 完全退出后，评分器在 evaluator-only 的独立目录分别复制候选源码和公开或隐藏测试运行 pytest；隐藏测试和参考修复不装入 Agent workspace。评分结果同时保留两个测试 exit code 和候选文件来源。

### 15. “Patch correctness” 如何定义？

必须同时通过公开和隐藏测试。正常提交、公开测试通过或 Agent 自述完成，都不能单独算正确补丁。v2.1 的 summary 和 score 文件都采用双套测试交集定义。

### 16. v2.1 的真实结果是什么？

Control、Enhanced、Enhanced v2 各 20 项，全部 60 项完成独立评分；正确补丁分别 3/20、4/20、4/20。Enhanced v2 对旧 Enhanced 是 B04 转正确、E01 转错误，净变化为零。每题每组一次运行，因此只能报告观察差异。

### 17. v2.1 的成本权衡是什么？

Enhanced v2 相比 Enhanced 平均输入 Token 下降 9.1%、总 Token 下降 8.7%，但相较 Control 总 Token 仍高 3.55%。重复失败命令为 71、89、87；v2 对旧 Enhanced 只减少两次。减少的反馈/重试并未在该结果中体现为更高 patch correctness。

### 18. B04 与 E01 的逐任务变化说明什么？

B04 在 Enhanced 未修复；Enhanced v2 先有一条无效多行 sed，随后改为追加编辑，公开测试通过并正常提交，独立公开/隐藏评分正确。E01 在旧 Enhanced 公开/隐藏评分正确但以 LimitsExceeded 结束；v2 未通过评分。它们显示同一机制版本也可能有得有失，不能仅用整体均值掩盖配对变化。

### 19. Verified-submit gate 是否提高了提交率？

本实验不能证明因果效果。三组都启用了 gate，没有无 gate 对照；日志证明它记录并阻止了一些未满足源码变化/公开测试验证条件的提交尝试。实际正常提交率分别为 3/20、2/20、3/20，因此不能声称它提高提交率。

### 20. 目前最重要的限制与下一步是什么？

评测集在开发期间构建、每题仅单次模型运行、没有反馈与守卫的单独消融，样本只有 20 个配对任务。v1/v2/v2.1 的协议或数据条件也不同，不能合并正确率。下一步应预注册并使用未参与调参的外部任务、多随机种子，做机制消融；公开结果前排除原始 trajectory、attempt logs 和 evaluator-only 文件。

## 源码索引

- mini-SWE 上游 loop/parser：`mini-swe-agent/src/minisweagent/agents/default.py`、`models/litellm_model.py`、`models/litellm_textbased_model.py`、`models/utils/actions_text.py`
- 本项目接线：`src/failure_aware_agent/runtime.py`、`model_adapter.py`、`transport_budget.py`
- 失败反馈/守卫/提交协议：`feedback.py`、`retry_guard.py`、`enhanced_v2.py`
- Shell 与评测：`action_wrapper.py`、`scripts/launch_experiment.py`、`scripts/score_finished_runs.py`
- 冻结结果：`reports/v2_1_formal_results.md`
