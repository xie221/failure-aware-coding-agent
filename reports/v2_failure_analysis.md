# Failure-Aware Coding Agent v2：冻结实验失败轨迹分析

## 范围与数据完整性

本报告只读取正式批次 `runs/formal-control-enhanced-final-20261023-v2/`、相应轨迹/HTTP ledger/差异/独立评分，以及原 Benchmark 任务与参考修复。正式批次原件不修改。历史结果仍是一次小样本开发观察，不将本轮新算法结果倒填进正式结果。

两组均有 20 个任务结果和评分。Control 与 Enhanced 各 20 个任务、每任务最多 12 次 HTTP attempt。独立评分定义为公开测试与隐藏测试都通过才算 patch correct。原 manifest SHA-256 为 `672a075e89522d6fd329c47c113863c21cdc178c95fa904f4bce386564c10d7e`；分析前后保持一致。

## 关键总体数据

| 指标 | Control | Enhanced | 差值（Enhanced−Control）|
|---|---:|---:|---:|
| Patch correct | 0/20 | 3/20 | +3 项（+15 个百分点）|
| 公开测试通过 | 0/20 | 4/20 | +4 项（+20 个百分点）|
| 隐藏测试通过 | 14/20 | 14/20 | 0 |
| 正常提交 | 6/20 | 5/20 | −1 项 |
| 重复失败命令 | 86 | 62 | −24（−27.9%）|
| 未改变工作区时被重试守卫拦截 | — | 39 次 | Enhanced 机制记录 |
| 平均 HTTP attempt | 9.4 | 9.6 | +0.2 |
| 平均总 Token | 8,883.45 | 10,146.7 | +1,263.25（+14.2%）|
| 平均输入 Token | 8,652.25 | 9,910.85 | +1,258.6（+14.5%）|
| 平均输出 Token | 231.2 | 235.85 | +4.65（+2.0%）|
| 反馈事件 | 0 | 93 | — |

Token 增量几乎全部出现在输入侧。反馈加入后的上下文/轨迹更长是合理解释，但仅凭记录不能把全部输入 Token 差异因果归因于反馈；任务动作序列和上下文也不同。不能宣称 v1 Enhanced 已经证明普遍提升：这里只有 20 个配对任务、每项单次运行，且两组的 patch-correct 绝对结果都很低。

## 系统性首要问题：公开测试执行环境不一致

40 份 trajectory 中，27 份公开 pytest 观察到 collection/import 错误：Control 13 份、Enhanced 14 份，错误为 `ModuleNotFoundError: No module named 'buggy'`。例如 A01 的 Agent 工作区测试调用在 Bubblewrap 中不能导入同目录 `buggy.py`；对应 evaluator-only 评分命令显式设置了任务目录 `PYTHONPATH`，因此 Agent 与评分器的导入环境不一致。生成的旧 Action wrapper 清理了环境变量，却没有为任务 Shell 设置 `PYTHONPATH=/workspace`。

这属于运行器/环境缺陷，不是模型算法失败。27/40 是出现该 collection 错误的 trajectory 数，不等于 27 项的全部测试尝试都只发生此错误；部分任务还出现 assertion 等其他输出。Benchmark 参考修复通过评分不能修复 Agent 执行环境不一致。

v2 的 `src/failure_aware_agent/action_wrapper.py` 为 Bubblewrap 任务命令显式设置 `PYTHONPATH=/workspace`。离线回归使用真实项目 pytest entrypoint：无此变量时合成任务报同样的 import error，有此变量时测试通过。历史 wrapper、轨迹和成绩均保持不变。

## D01–D05：异常少的调用与过早提交

下表中的调用数来自每任务 `task_result.json` 的 `http_attempts`，动作顺序与命令来自实际 trajectory；所有 D 任务都没有 pytest Action、独立分数均为错误，最终 `changed_files` 为空，Agent 以 `Submitted` 结束，而非请求预算耗尽或框架异常。

| 任务/组 | 请求数 | 第一次 Action | 观察与第二次 Action | 结束/文件状态 |
|---|---:|---|---|---|
| D01 Control / Enhanced | 2 / 2 | `sed` 尝试匹配 `def remove_duplicates(sequence):` 并注入实现 | 输出空、return code 0；随后 submit sentinel | `Submitted`；无文件变化 |
| D02 Control / Enhanced | 2 / 2 | `sed` 尝试匹配 `def count_words(text):` | 输出空、return code 0；随后 submit sentinel | `Submitted`；无文件变化 |
| D03 Control | 2 | `sed` 在假设的 `return merged_dict` 处注入逻辑 | 输出空、return code 0；随后 submit sentinel | `Submitted`；无文件变化 |
| D03 Enhanced | 4 | `nano buggy.py`；随后对假设的 `old_function/new_function` 做 `sed` | 编辑器错误、sed 无实际改动，之后 submit | `Submitted`；无文件变化 |
| D04 Control / Enhanced | 2 / 2 | `sed` 尝试匹配 `def rotate_matrix(matrix):` | 空观察、return code 0；随后 submit | `Submitted`；无文件变化 |
| D05 Control / Enhanced | 2 / 2 | `sed` 尝试匹配 `def apply_deltas(entries):` | 空观察、return code 0；随后 submit | `Submitted`；无文件变化 |

10 个 D 任务中，9 个恰好 2 次请求，Enhanced D03 为 4 次；总计 Control 10 次、Enhanced 12 次。共同模式不是模型或 HTTP 出错：首个模型回答生成了基于猜测函数签名/锚点的编辑命令，命令虽然 shell 返回 0，但锚点未匹配、文件未改；mini-SWE 把空观察交回后，下一模型响应直接输出协议 submit sentinel。框架按协议正常提交，结果 JSON `error=null`。因此这是规划/提交协议过早结束，而不是预算造成的终止。Enhanced D03 多出的两次请求对应交互编辑器失败和无效替换，不改变早提交结果。

两个请求之前模型可见的内容是任务描述和要求执行的公开测试命令；第一次 Action 后的模型上下文包含该命令退出码 0 及空输出。它没有从空观察推导编辑无效，也没有检查文件/运行公开测试。不能从轨迹推断其内部推理，只能确认采取了上述动作。

## 失败类型与轨迹支持

下列类别可重叠，除明确给出数量外不把它们误作互斥任务计数：

1. **工具环境/测试导入失败：** 27/40 轨迹有 `ModuleNotFoundError` collection 观察，根因是缺少 workspace import path。
2. **过早提交：** D01–D05 两组共 10 项，提交前没有测试；此外 Control A02 在未通过公开测试后仍提交。结果记录显示这 11 项均未 patch-correct。
3. **无效编辑与空结果误读：** D 组首个 `sed` 多数是对函数签名/文本锚点的猜测替换；return code 0 不是“文件已被修改”的证据，Git/file hash 最终显示无改动。
4. **交互式/不适配工具：** Enhanced 中有 20 次编辑器调用被反馈/执行层拒绝，Control 有 7 次；D03 Enhanced 明确先调用 `nano buggy.py`。拒绝降低了工具卡住风险，但未自动转化为有效补丁。
5. **重复失败命令：** 从 trajectory 汇总为 Control 86、Enhanced 62；Enhanced 重复失败守卫记录拦截 39 次工作区未变化时的复试。该机制减少了重复尝试，不保证修复质量。
6. **格式错误：** 每组各有 2 次需格式纠正的模型响应，分别见 Control A02/A05 和 Enhanced A05/D03；不是 D01–D05 仅两次调用的原因。
7. **修改代码但未通过测试：** 真实存在于若干任务轨迹和最终 diff；此外 pytest import 故障使一部分模型无法获得可信的测试反馈。具体补丁正确性以 evaluator 评分，不以 Agent 自述判定。
8. **请求预算耗尽：** 29/40 以 `LimitsExceeded`/step-or-cost 类别结束；HTTP ledger 的 `budget_rejections` 汇总为 0。它不是 D 组早提交的原因，也不能把 mini-SWE step/cost limit 和 12 次 transport attempt 上限混为一谈。
9. **协议提交但补丁错误：** Control 有 6 项、Enhanced 5 项提交；其中多数/全部提交并不代表测试通过。协议结束与 patch correctness 必须分开报告。

### Enhanced 成功案例

- **A01：** Enhanced 的文件操作最终生成可通过独立公开与隐藏测试的补丁；Control 的编辑序列最终错误。两边都受到 pytest import 环境缺陷影响，Agent 测试观察不可信。只能说本次 Enhanced 最终补丁正确，不能确认反馈是唯一原因。
- **A02：** Enhanced 的实现通过独立公开与隐藏测试；Control 最终没有有效源文件变化，并在测试未通过后提交。这个配对同时显示了“提交协议不能替代评分”和“最终补丁正确性”差异，但不能排除模型随机性。
- **C05：** Enhanced 最终补丁通过公开与隐藏测试；Control 重复尝试 `cp buggy.py /workspace/`（同路径复制）并遭遇 pytest import error，未形成正确补丁。轨迹支持 Enhanced 采取了更有效的实现路径，但反馈的独立因果贡献尚未证明。

## Benchmark 与评分质量

只读审查任务源码、公开/隐藏测试、参考修复，并在隔离的临时 evaluator-only 目录对源 buggy 版本和参考修复分别运行评分，没有把隐藏文件放进 Agent 工作区，也未修改源 Benchmark：

- 20/20 原始 buggy 任务的公开测试失败，说明每题公开集都能检测该任务预设缺陷。
- 原始 buggy 版本隐藏测试仅 14/20 失败；其余 6 个任务的隐藏集单独看会漏检原始错误。因此隐藏通过不能单独作为 patch correctness。
- 20/20 参考修复均通过公开与隐藏测试。
- 当前 `patch_correct` 要求 public **and** hidden 都通过，定义适当；结果中的 14/20 hidden pass 不代表有 14 个错误原程序已正确修复。
- 未发现参考修复/公开测试整体质量缺陷。发现的是隐藏集的基线辨别力有限，以及 Agent 沙箱和 evaluator 的 import 环境不一致。保持正式 Benchmark 不变。

## 优化优先级（证据→实现）

1. **先修真实运行器缺陷：** 给 Bubblewrap 下的任务命令显式 `PYTHONPATH=/workspace`，避免已证实的 collection error。该环境设置对三组必须相同。
2. **统一验证后提交协议：** D 组 10/10 未运行公开测试即提交且均无变化；新协议在 Control、旧 Enhanced、enhanced_v2 中一致地阻止未经验证的早交，并要求改动后的当前 `buggy.py` 有公开测试通过记录。它属于三组共用的实验协议，不计为 Enhanced 的独有算法收益。该 gate 是实验流程护栏，不是对恶意 Shell 命令的安全边界。
3. **enhanced_v2 轻量反馈：** 压缩错误提示、按 trigger+证据去重、在公开测试通过且预算 ≤3 时提醒按既有 mini-SWE 提交协议结束；无额外模型调用。v1 有 93 条反馈且输入 Token 增加，故减少反馈冗余有证据动机；是否降 Token 或提高正确率尚未验证。

不得把以上实现效果预先写成性能提升。现有 20 项已用于归因，应视为开发数据；新算法最终验证需要冻结且未参与调参的新任务集。
