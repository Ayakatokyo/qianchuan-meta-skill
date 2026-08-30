# 千川元技能两阶段分析交互实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有 `pre_input` 语义的前提下，实现事实包就绪后暂停、用户选数并输入分析思路、再恢复商业分析的两阶段链路。

**Architecture:** 以显式阶段状态和 `runId + manifest SHA256` 交接契约连接采集与分析。SKILL 文档负责对话提示，运行脚本负责状态/恢复/校验，现有 `prepare-analysis` 继续负责确定性切片与提示词编译。

**Tech Stack:** Python 3、现有 CLI、JSON manifest、pytest。

**Spec:** `docs/superpowers/specs/2026-08-30-qianchuan-two-stage-analysis-design.md`

## Global Constraints

- 不修改现有 `metadata.pre_input` 字段语义。
- 不改写原始工件；事实包和来源引用保持只读可追溯。
- 阶段二必须绑定已校验的 `runId` 和 manifest SHA256。
- 不引入外部网络数据作为分析事实。

---

### Task 1: 定义阶段状态与交接数据结构

**Files:**
- Modify: `scripts/run_meta_skill.py`
- Test: `tests/test_run_meta_skill.py`

**Interfaces:**
- 新增阶段状态常量和交接摘要函数；返回值包含 `stage`、`runId`、`factPackManifestSha256`、分区计数和缺失项。

- [ ] 写测试：事实包校验成功后状态为 `fact_pack_ready`，摘要包含四类计数和 manifest 哈希。
- [ ] 运行对应 pytest，确认测试失败。
- [ ] 实现状态/摘要与 JSON 持久化，兼容已有 receipt。
- [ ] 运行测试并确认通过。

### Task 2: 增加阶段二请求解析与事实包绑定校验

**Files:**
- Modify: `scripts/analysis.py`
- Test: `tests/test_analysis.py`

**Interfaces:**
- 新增 `validate_analysis_handoff(request, fact_pack_dir) -> dict`。
- 接受 `sections`、`materialScope`、`requestText` 和可选 `analysisSpec`；拒绝缺失分区、非法范围、哈希不匹配。

- [ ] 写覆盖单分区、多分区、缺失分区、哈希变化的失败测试。
- [ ] 运行测试确认失败。
- [ ] 实现校验和规范化请求，复用现有 `resolve_analysis_spec`。
- [ ] 运行 `pytest tests/test_analysis.py -v`。

### Task 3: 接入可恢复的 `awaiting_analysis_input` 流程

**Files:**
- Modify: `scripts/run_meta_skill.py`
- Modify: `SKILL.md`
- Test: `tests/test_run_meta_skill.py`, `tests/test_skill_document.py`

**Interfaces:**
- `run` 在事实包就绪后输出等待分析输入的交接消息，不自动执行分析。
- 新增 `continue-analysis --run-dir RUN_DIR --analysis-request FILE`，读取交接状态后进入 `prepare-analysis`。

- [ ] 写测试：阶段一停止、阶段二恢复、重复阶段一不会被触发。
- [ ] 运行测试确认失败。
- [ ] 实现 CLI 分支、状态迁移和错误提示。
- [ ] 在 SKILL.md 增加阶段二提示模板及字段说明，不改 `pre_input`。
- [ ] 运行文档与 CLI 测试。

### Task 4: 端到端回归与报告链路验证

**Files:**
- Modify: `tests/test_analysis.py`
- Modify: `tests/test_run_meta_skill.py`

- [ ] 使用现有 fixture 验证四类分区组合分析和报告输出。
- [ ] 验证缺失脚本/高光帧时只阻断对应分区，不影响其他分区。
- [ ] 运行完整测试集：`pytest -q`。
- [ ] 检查生成的 `analysis-input.json`、验证结果和报告均保留来源引用。
