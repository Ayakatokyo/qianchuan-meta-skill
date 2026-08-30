# 千川元技能两阶段采集与商业分析设计

## 目标

将技能拆分为“数据采集/事实包生成”和“用户选数/商业分析”两个阶段。保留现有 `pre_input` 的筛选语义，不要求用户在首次调用时提供分析问题；事实包完成并校验后暂停，等待用户选择数据分区并用自然语言描述分析意图。

## 范围与非目标

- 范围：运行状态、阶段交接、第二阶段输入、事实包绑定、恢复执行和报告链路。
- 非目标：本阶段不重构现有采集字段、不改变原始工件、不改变四类事实包字段定义。

## 交互状态

```text
awaiting_query_input → querying → rpa_processing → fact_pack_ready
→ awaiting_analysis_input → analysis_prepared → analysis_verified → report_ready
```

失败状态保留 `query_failed`、`fact_pack_failed`、`analysis_failed`，均携带可重试原因和最近成功阶段。

## 阶段一：采集

首次调用仅读取现有 `metadata.pre_input`：店铺/广告主、时间范围、筛选条件、排序、TopN 和高光帧规则。执行查询、RPA、落盘、四类分类、事实包构建与校验。校验通过后生成 `fact_pack_ready` 交接消息，包含 `runId`、事实包路径、manifest 摘要、四类数据条数、缺失项和来源引用；不自动生成商业结论。

## 阶段二：分析

用户在交接消息后选择一个或多个分区：`material_leaderboard`、`audience`、`script`、`highlight_frames`；指定全部/TopN/素材 ID 范围；输入自然语言分析目标、关注指标、比较维度、输出形式和深度。模型将这些内容解析为现有 `AnalysisSpec`，再由确定性校验、切片、提示词编译和 AI 二次分析流程生成报告。

阶段二请求契约：

```json
{
  "stage": "analysis",
  "runId": "run-20260810-20260820",
  "factPackManifestSha256": "...",
  "sections": ["material_leaderboard", "audience"],
  "materialScope": {"type": "top_n", "value": 3},
  "requestText": "分析高消耗素材的共同卖点，并给出创作建议",
  "analysisSpec": null
}
```

## 绑定与安全

- 阶段二必须绑定 `runId` 和事实包 manifest 的 SHA256；事实包不完整、校验失败或发生变化时拒绝分析。
- 只分析用户选择的分区；缺失分区明确标记为不可用，不推断为零。
- 所有结论保留事实包来源引用，原始工件只读。
- 支持从 `fact_pack_ready` 重新进入阶段二，不重复采集。

## 推荐实现形态

默认采用对话式交接：事实包就绪后暂停并等待用户回复。保留 CLI 续接入口（`continue-analysis --run-dir ... --analysis-request ...`）用于自动化；一次性采集并分析作为后续可选模式，不作为默认流程。

## 验收标准

1. 仅提供阶段一输入时，流程在事实包校验成功后停在 `awaiting_analysis_input`。
2. 阶段二可选择任一分区或多分区，并生成合法 `AnalysisSpec`。
3. 无事实包、分区缺失、素材范围非法或 manifest 变化时，分析被阻断并给出可操作错误。
4. 同一 `runId` 可稍后恢复分析，且不重新执行采集。
5. 现有采集和事实包测试全部保持通过。
