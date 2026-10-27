# GitHub 发布前检查

检查范围：项目源码、公开文档、配置、测试与忽略规则。未运行模型、Agent、Benchmark 或评分；未删除或改写 `runs/` 历史数据。

## 当前状态

- 初次审查时项目目录没有 `.git`。按用户随后要求，现已初始化本地 `main` 分支并暂存精选源码/文档；**尚未创建 commit 或推送**。当前机器未配置此仓库的 Git 作者身份，且没有 `gh` CLI，因此没有伪造作者身份或尝试密码认证。
- 已整理 README、架构/实验/消融说明、面试问答及岗位定制简历草稿。公开结果以 v2.1 三组实验为主；v1 与 v2 作为条件不同的历史记录，未合并指标。
- 现有离线套件最终运行 **65/65 通过**。测试使用 mock 响应和 socket/DNS 拦截；这不等价于重新验证真实 Bubblewrap/模型运行。

## 敏感材料审查

- 对公开源码、配置、测试和文档扫描了常见密钥格式及服务器绝对路径：没有发现真实 API key、私钥或 `/home/<用户名>`、`/data2/<用户名>` 路径。测试中的 `offline-placeholder` 与 `offline.invalid` 是明确的合成值。
- 原始轨迹、HTTP 记录、evaluator 副本（包括隐藏测试/答案）、运行日志及运行时文件位于历史运行材料中；**不得发布**。没有删除这些文件。
- `.gitignore` 已忽略 `runs/`、`runtime/`、日志、JSONL、离线运行输出、结果状态文件、凭据扩展名、模型权重扩展名，以及服务器专用 `host_run_*.sh` / `host_finish_batch.sh`。忽略规则不能替代提交前的实际 index 检查。
- Benchmark v2.1 是开发期间构建的内部探索集，不应以“外部独立 benchmark”名义发布；该数据集及隐藏评测材料不属于推荐提交清单。

## 代码与复现限制

- 将 Shell wrapper 生成逻辑中硬编码的宿主机父目录改为按目标路径构造空挂载父目录；增加 argv 级单元测试。此变更通过离线测试，但本轮没有运行 Bubblewrap，因此应在目标部署环境单独验证后再宣称新路径已实机验证。
- `tests/test_enhanced_v2.py` 改为从 PATH 发现 pytest，避免固定 Conda 路径。
- 项目依赖外部 mini-SWE-agent 源码、Benchmark、模型服务和服务器执行环境；公开源码不是开箱即运行的独立 benchmark 包。README 已披露该限制。
- v2.1 单次 20 题/组结果是探索性描述。Enhanced v2 与 Enhanced 正确率均为 4/20；不能声称已证明算法增益。

## 推荐纳入首次提交

- `README.md`、`architecture.md`、`experiments.md`、`ablation.md`、`resume_project.md`
- `docs/interview_qa.md`
- `reports/` 中经过逐项复核、仅含汇总信息的报告
- `src/failure_aware_agent/`、`tests/`、`pyproject.toml`
- 可移植的离线测试、评分/汇总源码与不含本机路径的配置模板
- `.gitignore`

适合纳入的 Python 入口限于 `launch_experiment.py`、`run_task.py`、`run_offline_tests.py`、评分器和汇总器。不要纳入 `scripts/build_benchmark_v2.py`：该生成器在源码中内嵌了隐藏测试和参考实现；也不需要公开数据集验证运行脚本。

不要纳入：`runs/`、evaluator-only 数据、原始 trajectories、HTTP 请求记录、运行日志、`offline_validation.json` 等机器运行产物、服务器 host launcher、模型/环境文件、私密或外部 Benchmark 数据。若准备分享结果表，使用报告中的聚合数据并再次核对，不从运行目录复制隐藏材料。

## 发布结论

**内容整理和本地暂存已完成，但暂不能称为已完成 GitHub 发布或推送。**提交前需由项目所有者设置正确的 `user.name`/`user.email`，复核 `git status` 与 `git diff --cached --name-only`，再提交。随后通过 GitHub CLI 浏览器授权或 SSH 配置远端；不要使用账户密码进行 Git 认证。远端仓库应先保持 Private，复核网页文件后再决定是否公开。本轮没有创建远端或推送。

推荐仓库描述：

> A mini-SWE-agent extension for evidence-grounded failure feedback, workspace-aware retry suppression, and auditable paired evaluation under a shared HTTP-attempt budget.
