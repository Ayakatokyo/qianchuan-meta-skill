# 千川元技能模型二次分析闭环设计

## 目标

在保留两阶段交互的前提下，打通第二阶段的模型闭环：用户输入分析思路后，模型先将其标准化为 `AnalysisSpec`，技能依据该规范切片对应事实包并编译标准化提示词，再由当前对话模型使用“提示词 + 事实包”完成 AI 二次分析，最后由技能校验并落盘结构化结果。

## 阶段边界

阶段一仅负责 `pre_input`、取数、RPA、分类、事实包构建与校验；成功后状态为 `awaiting_analysis_input` 并停止。阶段二由用户继续输入触发，不重复采集。

## 阶段二数据流

```text
analysisRequest(requestText, sections, selection)
→ 当前模型解析 AnalysisSpec
→ 技能 validate/resolve AnalysisSpec
→ 技能切片事实包并保留 sourceRefs
→ 编译 category-specific 标准提示词
→ 当前对话模型执行 AI 二次分析
→ analysis-result.json
→ verify-analysis
→ analysis result / report artifacts
```

## 模型交接协议

技能向模型提供 `analysis-input.json`，至少包含：`runId`、事实包 manifest 引用及 SHA256、标准化 `analysisSpec`、用户原始需求、选定分区的事实切片、来源引用、禁止外部补数规则和结果 JSON Schema。模型必须返回 `analysis-result.json`，每条 claim 包含 `claimType`、`statement`、`evidenceRefs`、`confidence`；计算必须声明公式和输入。模型不得访问未选分区或补充外部事实。

## 编排责任

- 对话技能：控制阶段暂停/恢复，并把标准化输入交给当前模型。
- 本地脚本：确定性校验、事实切片、提示词编译、结果校验和落盘；不自行创建新的模型凭证或网络依赖。
- 当前对话模型：自然语言结构化和 AI 二次分析。

## 失败与恢复

事实包未校验、分区不存在、素材越权、模型结果缺失或证据不匹配时，保持 `blocked`/`awaiting_model`/`analysis_failed`，不得声称分析完成。`runId + manifest SHA256` 绑定失效时必须重新准备输入；可从 `awaiting_analysis_input` 恢复，不重跑阶段一。

## 验收标准

1. 阶段一完成后自动停止并提示进入阶段二。
2. 阶段二能将自然语言需求转换为合法 `AnalysisSpec`。
3. 模型收到标准提示词和对应事实切片，并返回可校验的结构化结果。
4. 结果校验失败时不生成成功报告；成功时保留证据引用并落盘。
5. 现有采集、事实包和离线分析测试不回归。
