# 千川分区商业分析报告设计

## 目标

将阶段二 `report.md` 从按 claim 类型重复罗列的技术产物，升级为面向业务决策的报告：核心结论与行动建议前置，随后严格按四类事实包（素材榜单、人群、脚本、高光帧）分章输出结论，审计信息后置且完整保留。

## 现状问题

真实报告虽然已有四个分区标题，但 claim 没有稳定分区归属，建议会混入分区正文；同一内容又被“事实/计算/AI 推断/建议”章节完整重复；正文展示 confidence 和长 evidence 路径，导致重点分散、篇幅冗余、业务可读性差。

## 结果契约

对新生成的 `analysis-result.json`，每条 claim 新增并校验：

```json
{
  "section": "material_leaderboard | audience | scripts | highlight_frames | synthesis",
  "priority": "high | medium | low",
  "businessImplication": "可选的业务含义或行动说明"
}
```

`section` 是正文分类依据：前四项对应事实包章节，`synthesis` 只允许跨分区洞察。`claimType` 继续区分 fact/calculation/inference/recommendation，且保留 evidenceRefs/confidence/计算声明。兼容旧结果：没有 `section` 的 claim 按来源引用路径归类，无法唯一归类的进入 `synthesis` 并在审计附录标明历史推断归类。

## 报告结构

```text
一、核心商业结论
二、优先行动建议
三、素材榜单分析
四、人群分析
五、脚本分析
六、高光帧分析
七、跨分区综合洞察
八、数据限制与下一步
附录 A：证据索引
附录 B：结论审计
```

正文只展示短证据标记（如 `[E1]`），每个分区包含“关键发现—业务含义—分区建议”。正文不重复技术型 claim 列表，不直接展示 confidence、完整文件路径、哈希或 `fact_refs`。附录 A 保存短证据标记到完整来源引用的映射；附录 B 保存 claim 类型、优先级、置信度、计算公式和归类方式。

## 模型交接

标准化提示词要求模型先输出四类事实包的分区结论，再输出 `synthesis`；建议必须关联一个或多个事实结论并标注业务含义。模型不得使用事实包外数据；人群只能写相关性；高光帧观察需标记 AI 推断。

## 验收标准

1. 新结果的每条 claim 都有有效 `section` 和 `priority`。
2. 报告正文四类事实包章节顺序固定，结论只出现一次。
3. 核心商业结论和优先行动建议置顶，行动建议按优先级排序。
4. 审计字段仅在 JSON 与附录出现，短证据标记可追溯至完整来源。
5. 历史结果可兼容渲染，真实产物回放通过。
