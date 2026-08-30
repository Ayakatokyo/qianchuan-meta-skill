# 千川元技能模型二次分析闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通事实包就绪后的对话模型二次分析闭环，并保持阶段一采集后暂停的行为。

**Architecture:** 运行脚本继续提供确定性事实切片、提示词编译和结果验收；技能文档定义模型交接协议；对话编排在阶段二把 `analysis-input.json` 交给当前模型并接收 `analysis-result.json`。所有交接绑定 `runId` 与 manifest SHA256。

**Tech Stack:** Python 3、现有 CLI、JSON Schema/契约、pytest、Codex 对话模型交接。

**Spec:** `docs/superpowers/specs/2026-08-30-qianchuan-model-analysis-handoff-design.md`

## Global Constraints

- 阶段一只使用现有 `metadata.pre_input`，完成事实包后必须停止。
- 不修改原始工件，不引入新的模型凭证或外部网络依赖。
- 模型只能使用选定事实包切片，结论必须引用 `sourceRefs`。
- 保留当前 `prepare-analysis`、`verify-analysis` 和已有未提交用户改动的兼容性。

---

### Task 1: 固化模型交接输入契约

**Files:**
- Modify: `scripts/analysis.py`
- Test: `tests/test_analysis.py`

**Interfaces:**
- `compile_model_input(...)` 输出模型可直接消费的 `analysis-input.json`，增加用户原始需求、模型约束、结果 Schema 摘要和 manifest 哈希。

- [ ] 写测试覆盖四类分区、来源引用、外部数据禁止和 manifest 哈希。
- [ ] 运行测试确认失败。
- [ ] 实现契约字段并保持旧 fixture 向后兼容。
- [ ] 运行分析测试确认通过。

### Task 2: 增加模型结果接收与结构化落盘入口

**Files:**
- Modify: `scripts/run_meta_skill.py`
- Modify: `scripts/analysis.py`
- Test: `tests/test_run_meta_skill.py`, `tests/test_analysis.py`

**Interfaces:**
- 新增从阶段二交接中接收模型 JSON 结果的入口；写入 `analysis-result.json` 后调用现有 `verify_analysis`。
- 缺失或非 JSON 结果保持 `awaiting_model`，不生成成功报告。

- [ ] 写成功、缺失、非法 JSON 和证据不匹配测试。
- [ ] 运行测试确认失败。
- [ ] 实现接收、状态迁移和错误回执。
- [ ] 运行测试确认通过。

### Task 3: 更新技能编排提示，触发当前对话模型分析

**Files:**
- Modify: `SKILL.md`
- Test: `tests/test_skill_document.py`

- [ ] 写文档测试：阶段一停止、阶段二用户输入、模型交接字段和禁止外部补数均有明确说明。
- [ ] 更新 SKILL.md，要求模型解析 AnalysisSpec、读取 `analysis-input.json`、执行二次分析并返回结构化结果。
- [ ] 增加用户可见的阶段二提示模板和模型交接输出格式。
- [ ] 运行文档测试确认通过。

### Task 4: 端到端验证与回归

**Files:**
- Modify: `tests/test_run_meta_skill.py`
- Modify: `tests/test_analysis.py`

- [ ] 用现有事实包 fixture 验证“阶段一停止 → 阶段二分析 → 结果校验”。
- [ ] 验证单分区、多分区和缺失分区场景。
- [ ] 运行完整测试集：`pytest -q`。
- [ ] 检查 analysis receipt、analysis-result 和 report 均包含来源引用及阶段状态。
