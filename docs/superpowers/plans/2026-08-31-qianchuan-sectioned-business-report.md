# 千川分区商业分析报告实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让阶段二报告以商业结论为重点，严格按四类事实包分章输出，且保持完整证据审计与历史结果兼容。

**Architecture:** 扩展模型结果 claim 契约提供稳定的 section/priority/业务含义；验证器负责约束与旧结果归类；单一渲染器生成正文、证据索引和审计附录，不再生成重复的按 claim 类型正文。

**Tech Stack:** Python 3、JSON、Markdown、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-qianchuan-sectioned-business-report-design.md`

## Global Constraints

- 直接在 `main` 工作区实施，不创建隔离工作树。
- 原始事实包、来源引用和 report.json 保持可追溯，不能为展示而删除审计数据。
- 模型不得补充外部事实；人群仅相关性、高光帧观察明确为 AI 推断。
- 兼容历史 `analysis-result.json` 中缺少 `section`/`priority` 的 claim。

---

### Task 1: 扩展 claim 契约与模型提示词

**Files:**
- Modify: `scripts/analysis.py`
- Modify: `SKILL.md`
- Test: `tests/test_analysis.py`, `tests/test_skill_document.py`

**Interfaces:**
- `validate_analysis_result(...)` 接受并规范化 claim 的 `section`、`priority`、可选 `businessImplication`。
- `compile_model_input(...)` 的提示要求输出上述字段。

- [ ] 写失败测试：新结果缺少 section 或 priority、section 不在选定分区/`synthesis`、无 evidenceRefs 的建议均被拒绝。
- [ ] 运行 `pytest tests/test_analysis.py -k claim -v`，确认失败。
- [ ] 实现字段校验和提示词约束，保留旧结果的兼容路径。
- [ ] 运行定向测试和文档测试，确认通过。

### Task 2: 实现历史 claim 归类与证据索引

**Files:**
- Modify: `scripts/analysis.py`
- Test: `tests/test_analysis.py`

**Interfaces:**
- 新增私有归类函数：以事实包文件路径映射历史 claim 到四类 section；不唯一时归为 `synthesis` 并记录 `classificationMode=legacy_inferred`。
- 生成按稳定编号的证据索引 `E1...En`。

- [ ] 写测试：material/audience/scripts/highlight 来源可正确归类；多分区引用归 synthesis；历史结果可渲染。
- [ ] 运行测试确认失败。
- [ ] 实现归类和证据编号。
- [ ] 运行定向测试确认通过。

### Task 3: 重构 Markdown 与 JSON 报告模板

**Files:**
- Modify: `scripts/analysis.py`
- Test: `tests/test_analysis.py`

**Interfaces:**
- `render_reports(...)` 输出固定章节、`evidenceIndex` 与 `claimAudit`；正文只出现短证据标记。

- [ ] 写测试：核心结论/行动建议在前、四分区顺序固定、建议按 high/medium/low 排序、正文不存在 confidence/evidence 路径和重复 claim 类型章节。
- [ ] 运行渲染测试确认失败。
- [ ] 实现单一正文渲染、附录 A 证据索引、附录 B 结论审计。
- [ ] 运行渲染测试确认通过。

### Task 4: 真实产物回放与全量回归

**Files:**
- Modify: `tests/test_analysis.py`
- Test: `tests/`

- [ ] 将真实 `run-20260810-20260820` 产物复制到临时目录，重渲染 report.md，不改原始 runs。
- [ ] 断言四类事实包章节、短证据标记、附录完整性和历史 claim 兼容归类。
- [ ] 运行 `pytest -q`、`python -m compileall -q scripts tests`、`git diff --check`。
- [ ] 汇报变更、测试与未包含的 HTML/PDF 图表扩展。
