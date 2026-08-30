# 千川素材分析执行链路优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除真实运行中的哈希性能瓶颈和误判收尾问题，补齐进度/恢复、模型交接和标准报告章节。

**Architecture:** 在现有 fact-pack、analysis 和 CLI 边界内做增量改造。文件哈希缓存与 sourceRef 聚合属于共享基础能力；状态/幂等属于运行编排；模型交接和报告属于分析输出层，各自保持可测试接口。

**Tech Stack:** Python 3、JSON、pytest、现有 CLI。

**Spec:** `docs/superpowers/specs/2026-08-30-qianchuan-execution-optimization-design.md`

## Global Constraints

- 直接在 `main` 工作区实施，不创建隔离工作树。
- 保留所有现有用户改动，不使用破坏性 git 操作。
- 不修改原始工件，不新增模型凭证或外部网络依赖。
- 兼容旧版单 `recordIndex` sourceRef 和既有 CLI。

---

### Task 1: 文件哈希缓存与 sourceRef 聚合

**Files:**
- Modify: `scripts/fact_pack.py`
- Modify: `scripts/analysis.py`
- Test: `tests/test_fact_pack.py`, `tests/test_analysis.py`

- [ ] 增加失败测试：同一 records 文件多次引用只调用一次哈希；聚合引用仍能定位多个记录。
- [ ] 运行定向测试确认失败。
- [ ] 实现共享文件级缓存、聚合 sourceRef 及旧格式读取兼容。
- [ ] 运行定向测试确认通过，并用真实运行目录做离线基准。

### Task 2: 阶段进度、恢复与幂等收尾

**Files:**
- Modify: `scripts/run_meta_skill.py`
- Modify: `scripts/fact_pack.py`
- Test: `tests/test_run_meta_skill.py`, `tests/test_fact_pack.py`

- [ ] 增加阶段状态、时间戳、进度和 lastSuccessfulPhase 测试。
- [ ] 增加已成功产物复用和结果哈希不变的幂等测试。
- [ ] 实现结构化状态优先的完成判定、阶段进度输出和原子 receipt 更新。
- [ ] 运行 CLI 回归测试。

### Task 3: 模型二次分析交接强化

**Files:**
- Modify: `scripts/analysis.py`
- Modify: `scripts/run_meta_skill.py`
- Modify: `SKILL.md`
- Test: `tests/test_analysis.py`, `tests/test_run_meta_skill.py`, `tests/test_skill_document.py`

- [ ] 测试自然语言请求到 AnalysisSpec 的交接字段、选定事实切片和外部补数约束。
- [ ] 测试模型结果缺失、非法 JSON、证据不匹配和成功接收。
- [ ] 实现明确的 `awaiting_model`/`analysis_verified` 状态和交接提示，不伪造模型调用。
- [ ] 运行分析和文档测试。

### Task 4: 标准章节报告渲染

**Files:**
- Modify: `scripts/analysis.py`
- Test: `tests/test_analysis.py`

- [ ] 测试 report.json 的章节元数据和 Markdown 固定章节顺序。
- [ ] 实现执行摘要、范围质量、四类分区、综合洞察、建议、证据限制章节；保留 claims 分层。
- [ ] 运行渲染测试并检查真实 fixture 输出。

### Task 5: 全量验证与交付

**Files:**
- Test: `tests/`

- [ ] 运行 `pytest -q`。
- [ ] 对 `runs/run-20260815-20260825` 执行离线 fact-pack/analysis 验证和耗时对比。
- [ ] 汇报改动、测试结果、性能变化和仍未实现的 HTML/PDF 扩展。
