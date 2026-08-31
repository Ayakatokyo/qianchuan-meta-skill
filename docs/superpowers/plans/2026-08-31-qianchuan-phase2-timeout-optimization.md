# 千川阶段二超时问题优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `continue-analysis` 在大事实包场景下不再因重复哈希卡住，并具备可复用、可恢复、可观测的阶段二执行能力。

**Architecture:** 保持 fact-pack、analysis 和 CLI 的现有边界。以共享文件哈希缓存和事实包校验回执消除重复 I/O；以阶段 receipt 和结构化产物决定恢复与完成；以 sourceRef 聚合降低模型输入；入口完整性检查提前暴露安装问题。

**Tech Stack:** Python 3、JSON、pytest、现有 CLI。

**Spec:** `docs/superpowers/specs/2026-08-31-qianchuan-phase2-timeout-optimization-design.md`

## Global Constraints

- 直接在 `main` 工作区实施，不创建隔离工作树。
- 保留现有用户改动，不使用破坏性 git 操作。
- 原始工件只读，不新增模型凭证或外部网络依赖。
- 兼容旧 sourceRef、既有事实包和现有 CLI 命令。

---

### Task 1: 修复校验路径的重复哈希

**Files:**
- Modify: `scripts/fact_pack.py`
- Test: `tests/test_fact_pack.py`

- [ ] 写测试：对同一文件的多个 sourceRef，`_sha256` 只被调用一次；大小/mtime 变化后重新计算。
- [ ] 运行定向测试确认失败。
- [ ] 将 `_verify_file_ref` 和相关调用统一切换到 `_cached_file_hash`，必要时按文件聚合验证。
- [ ] 运行 `pytest tests/test_fact_pack.py -v` 确认通过。

### Task 2: 增加事实包校验回执复用

**Files:**
- Modify: `scripts/fact_pack.py`
- Modify: `scripts/analysis.py`
- Test: `tests/test_fact_pack.py`, `tests/test_analysis.py`

- [ ] 写测试：首次校验写回执；manifest 未变化时 `prepare_analysis` 不重复全量校验；文件变化时强制复验。
- [ ] 运行测试确认失败。
- [ ] 实现校验回执、指纹比较和失效策略，保证失败回执不可复用。
- [ ] 运行定向测试确认通过。

### Task 3: 压缩 sourceRef 和分析输入

**Files:**
- Modify: `scripts/fact_pack.py`
- Modify: `scripts/analysis.py`
- Test: `tests/test_fact_pack.py`, `tests/test_analysis.py`

- [ ] 写测试：聚合后的引用保留所有 recordIndexes，旧单 recordIndex 输入仍可读取。
- [ ] 实现按文件去重和必要字段投影，不改变证据可追溯性。
- [ ] 用真实产物比较 `analysis-input.json` 大小和 sourceRef 数量。
- [ ] 运行测试确认通过。

### Task 4: 完善阶段状态、恢复和幂等完成判定

**Files:**
- Modify: `scripts/run_meta_skill.py`
- Modify: `scripts/analysis.py`
- Test: `tests/test_run_meta_skill.py`, `tests/test_analysis.py`

- [ ] 写测试：阶段进度回执、超时后读取已完成产物、结果哈希不变时幂等复用。
- [ ] 实现结构化状态优先、进度字段、原子 receipt 更新和日志文本降级为辅助诊断。
- [ ] 运行 CLI 回归测试。

### Task 5: 技能包完整性与文档提示

**Files:**
- Modify: `scripts/run_meta_skill.py`
- Modify: `SKILL.md`
- Test: `tests/test_run_meta_skill.py`, `tests/test_skill_document.py`

- [ ] 写测试：缺少 `config/analysis-registry-v1.json` 时阶段二入口快速失败并给出路径。
- [ ] 实现必需文件检查，补充长任务和恢复操作提示。
- [ ] 运行文档与 CLI 测试。

### Task 6: 真实产物全量验证

**Files:**
- Test: `tests/`

- [ ] 运行 `pytest -q`。
- [ ] 对真实 `run-20260815-20260825` 执行 `verify-fact-pack`、`continue-analysis` 离线回归并记录耗时。
- [ ] 检查分析输入、receipt、结果和报告状态，汇报剩余边界。
