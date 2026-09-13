---
name: 千川素材全景诊断
description: 适用于按店铺、广告主和日期范围筛选千川素材并执行受控 RPA 诊断；阶段一取数和事实包校验完成后生成离线素材总览报告，高光帧只在第一阶段明确请求时生成，商业分析只读取已落盘证据。
metadata:
  argument_hint: 输入查询范围、可选的素材筛选/排序条件和详情数量；高光帧默认不生成，按下方规则选择是否保留封面及提取指标。
  pre_input: |
    请采集千川素材及相关诊断数据。
    授权店铺ID是 ____（必填）。
    广告主ID是 ____（必填）。
    时间范围是 ____（填写起止日期）。
    素材筛选及排序条件是 ____（可选；例如筛选“整体消耗”>=10000，再按“净成交ROI”降序；排序指标和排序方向均可留空）。
    需要获取详情的素材数是 ____（包含互动时序、人群、脚本和高光帧数据，默认10）。

    是否同时生成高光帧图片：____（是/否，可选，默认否）。
    高光帧设置（选择否时，以下全部留空；选择是时，按需逐项填写）：
    - 封面帧是否保留：____（是/否）。
    - 整体点击次数：是否选择 ____；提取规则 ____（局部峰值/最高值）；帧数 ____（正整数）。
    - 整体流失数：是否选择 ____；提取规则 ____（局部峰值/最高值）；帧数 ____（正整数）。
    - 整体点赞次数：是否选择 ____；提取规则 ____（局部峰值/最高值）；帧数 ____（正整数）。
    - 整体评论次数：是否选择 ____；提取规则 ____（局部峰值/最高值）；帧数 ____（正整数）。
    - 整体转发次数：是否选择 ____；提取规则 ____（局部峰值/最高值）；帧数 ____（正整数）。
    - 整体新增粉丝数：是否选择 ____；提取规则 ____（局部峰值/最高值）；帧数 ____（正整数）。
---

# 千川素材全景诊断

该技能负责受控素材查询、RPA 诊断、视频与高光帧资产、阶段一离线素材总览报告，以及在完整事实包上的可审计商业分析准备与验收。流程严格分两阶段：第一阶段做外部取数、事实包落盘与校验，再从已完成运行目录生成素材总览报告；第二阶段才接收用户自然语言商业分析思路，将其转换为 `FactRetrievalSpec`，只读取已落盘事实包，再交给模型分析。脚本负责确定性校验、事实切片、脱敏、结果验收与报告落盘；当前项目没有外部模型调用通道，不会擅自增加凭证或网络依赖。

## 输入

自然语言入口从同一请求解析 `QuerySpec` 和 `HighlightSpec`；文件入口直接读取已保存的两份 JSON。解析成功后立即落盘并执行，缺少必填项或字段不合法时反馈具体错误。除用户明确要求预览、草稿或人工确认外，不等待二次确认。

### 查询范围

`shopId` 与 `advertiserId` 都必填且属于不同 ID 域：前者只用于报表连接器授权，后者只用于报表业务筛选。日期范围为闭区间，起止日期均使用 `YYYY-MM-DD`。字段和操作符只能来自 `config/asset-field-registry-v1.json`；维度和指标条件按 `AND` 处理。

### 指标筛选

客户以中文指标名表达阈值、排序和目标 Top N，例如“净成交 ROI >= 1.2，按净成交金额降序取 Top 5”。未给出阈值时 `metricFilters` 为空；未提供排序条件时按连接器返回的原始顺序输出；客户不需要输入候选补位数。

### QuerySpec

```json
{
  "schemaVersion": 1,
  "source": "qianchuan_direct_material_report",
  "scope": {"shopId": "授权店铺 ID", "advertiserId": "广告主 ID"},
  "period": {"startDate": "YYYY-MM-DD", "endDate": "YYYY-MM-DD"},
  "filters": {
    "dimensionFilters": [],
    "metricFilters": [{"field": "total_prepay_and_pay_settle_roi2_1h", "operator": "gte", "value": 1.2}]
  },
  "ranking": {"field": "total_order_settle_amount_for_roi2_1h", "direction": "desc"},
  "collection": {"targetTopN": 5}
}
```

用户以中文业务名表达筛选意图；模型查阅注册表的中文 `label` 后，必须在 QuerySpec 的 `field` 中写入对应 API 原始字段名。固定报表契约的 102 个指标均可用于 `metricFilters`；如填写排序条件，指标可用于 `ranking.field` 且方向只能是 `asc` 或 `desc`。未填写 `ranking` 时不做本地排序，也不因排序指标缺失而排除素材。API 百分比字段使用其原始百分点，例如 `66.67` 表示 66.67%，而非 0.6667。

### 原始采集字段

每次日期范围素材汇总查询固定请求 8 个非时间维度和 102 个已验证原始指标，并在 `source-data/field-manifest.json` 写入本次请求的脱敏字段清单。每个已请求字段也已注册为 QuerySpec 字段：维度可用于筛选，102 个指标均可用于筛选和排序。

QuerySpec 的 `field` 必须来自 `config/asset-field-registry-v1.json`。注册表的 `label` 仅用于将中文意图映射到 API 字段；已确认的前端中文名称会被保留，其他新开放字段暂使用 API 原始名作为技术标签。候选从单条素材汇总结果直接读取，不做本地累加或派生计算；`material_id = -2` 的 AIGC 动态创意集合会排除，其他重复素材 ID 会作为数据质量错误拒绝查询。

### 高光帧规则

无论“是否同时生成高光帧图片”选择什么，都按以下规则理解：

| 设置 | 选择“否”时 | 选择“是”时 |
| --- | --- | --- |
| 封面帧 | 不填写、不校验，不生成 0 秒封面帧 | 必填“是”或“否”，对应 `includeZeroSecond` |
| 高光指标 | 不填写、不校验 | 可从整体点击次数、整体流失数、整体点赞次数、整体评论次数、整体转发次数、整体新增粉丝数中选择一项或多项 |
| 提取规则 | 不填写、不校验 | 为每个已选指标选择一种：局部峰值（`local_peaks`）或最高值（`global_top_values`） |
| 帧数 | 不填写、不校验 | 为每个已选指标填写正整数 `maxFrames`，表示该指标最多提取的帧数，不是所有指标合计帧数 |

选择“否”时只保留视频，不生成高光帧图片；如果同时提交封面或指标规则，会在取数前报错。选择“是”时至少选择一个高光指标；不同指标命中同一秒时合并为一帧，因此实际图片总数可能少于各指标帧数之和。

```json
{
  "schemaVersion": 1,
  "includeZeroSecond": true,
  "metrics": {
    "CLICK_COUNT": {"enabled": true, "strategy": "local_peaks", "maxFrames": 3},
    "LOSE_COUNT": {"enabled": false},
    "LIKE_COUNT": {"enabled": false},
    "COMMENT_COUNT": {"enabled": false},
    "SHARE_COUNT": {"enabled": true, "strategy": "global_top_values", "maxFrames": 2},
    "NEW_FOLLOWER_COUNT": {"enabled": false}
  }
}
```

六项指标必须全部显式声明。启用指标只能使用 `local_peaks` 或 `global_top_values`；`global_top_values` 会在完整互动曲线上按指标值降序、同值按时间升序取前 `maxFrames` 个不同时间点，`maxFrames` 为任意正整数。相同秒数的多指标命中只抽取一张 JPEG，并在选帧证据中保留全部命中。旧配置中的 `global_maximum` 仍可读取，但会归一化为 `global_top_values`。

## 执行

`validate` 不访问外部服务；`query` 使用 `ENV_BACKEND_HOST`、`YUCE_AUTHORIZATION`，以及 `YUCE_SESSION_ID` 或 `YCSESSIONID` 调用千川报表连接器。RPA 与报表连接器采用相同会话优先级：`YUCE_SESSION_ID` → `YCSESSIONID` → `YUCE_AUTHORIZATION`。凭证不得进入 Spec、Git、日志或运行工件。

在未预装依赖的本地 Codex 运行面，统一从技能根目录使用 `uv run --with-requirements requirements.txt python`；它会按本技能清单解析依赖，避免 `requests` 缺失被误判为鉴权故障。不要把凭证写入命令行。

```bash
uv run --with-requirements requirements.txt python scripts/run_meta_skill.py validate \
  --query-spec /path/to/query-spec.json \
  --highlight-spec /path/to/highlight-spec.json

uv run --with-requirements requirements.txt python scripts/run_meta_skill.py query \
  --query-spec /path/to/query-spec.json \
  --highlight-spec /path/to/highlight-spec.json \
  --output-dir /path/to/new-run

uv run --with-requirements requirements.txt python scripts/run_meta_skill.py run \
  --query-spec /path/to/query-spec.json \
  --highlight-spec /path/to/highlight-spec.json \
  --execute-rpa \
  --output-dir /path/to/new-run

# 单 skill 完整工作流入口（阶段一完成后暂停，等待第二次分析输入）
uv run --with-requirements requirements.txt python scripts/run_meta_skill.py workflow \
  --query-spec /path/to/query-spec.json \
  --highlight-spec /path/to/highlight-spec.json \
  --execute-rpa \
  --output-dir /path/to/new-run

# 阶段一取数、事实包构建和校验成功后，生成阶段一离线素材总览报告
uv run --with-requirements requirements.txt python scripts/run_meta_skill.py render-overview \
  --run-dir /path/to/completed-run \
  --output-dir /path/to/completed-run/overview

uv run --with-requirements requirements.txt python scripts/run_meta_skill.py verify-run \
  --output-dir /path/to/completed-run

uv run --with-requirements requirements.txt python scripts/run_meta_skill.py build-fact-pack \
  --run-dir /path/to/completed-run

uv run --with-requirements requirements.txt python scripts/run_meta_skill.py verify-fact-pack \
  --run-dir /path/to/completed-run \
  --fact-pack-dir /path/to/completed-run/fact-pack

uv run --with-requirements requirements.txt python scripts/run_meta_skill.py prepare-analysis \
  --run-dir /path/to/completed-run \
  --analysis-request /path/to/analysis-request.json \
  --output-dir /path/to/completed-run/analysis

uv run --with-requirements requirements.txt python scripts/run_meta_skill.py verify-analysis \
  --run-dir /path/to/completed-run \
  --analysis-dir /path/to/completed-run/analysis
```

`query` 只写入候选查询工件，不触发 RPA、视频下载、抽帧、AI 分析或报告生成。`run` 默认同样只查询或复用候选工件，并写入 `run-receipt.json`；只有显式传入 `--execute-rpa` 才会解析唯一 RPA 授权账号，按候选排名执行受控波次、失败补位和恢复。每个成功详情素材的视频下载至运行目录的 `rpa/videos/`，并以相对路径、大小和 SHA-256 纳入 RPA 回执与事实包清单；高光帧仍只在提供 `HighlightSpec` 时抽取。

只有 `run --execute-rpa` 在 RPA、records、抽帧和批量验证均成功后，才会自动生成 `fact-pack/`；成功回执的 `factPack` 阶段会记录 manifest 路径、`targetTopN` 和分类计数。仅 `query` 或未授权的 `run` 不会创建或声称完成事实包。也可以对已有完成目录显式执行 `build-fact-pack`，但 query-only、RPA 部分失败或工件不完整时命令会非零退出。

阶段一取数成功并通过事实包校验后，必须继续执行 `render-overview` 生成阶段一素材总览报告。该命令只读取当前运行目录中的已校验素材汇总、事实包和可用详情证据，不重新查询、不重跑 RPA、不调用模型；默认输出 `overview/overview-data.json` 和 `overview/index.html`。报告是事实投影工作台，包含素材榜单、查询上下文、可选字段，以及在证据可用时展示的人群、脚本、高光帧、9:16 单素材视频预览和单素材详情；报告会把已验证视频复制到相对 `assets/single-material/videos/`。不可用数据必须保留明确的 unavailable/gap 状态，不得补零或生成商业结论。命令返回 `status=succeeded` 且两个文件均存在后，阶段一报告才算完成；来源文件缺失、记录数不一致、字段未注册或写入失败时必须阻断阶段一交接。

事实包按 `targetTopN` 生成以下文件：`material-leaderboard.json`（素材榜单与平台维度字段）、`audience.json`（人群事实）、`scripts.json`（脚本事实）、`highlight-frames.json`（高光帧事实）以及 `manifest.json`/`README.md`。候选池大于目标时，只纳入 RPA 成功选中的目标素材；`scripts.json` 使用 `parseStatus=parsed|missing|failed` 和对应错误码，`highlight-frames.json` 在没有选中帧时使用 `status=no_highlight_frames`，不会把缺失推断为事实。

事实包只保留可追溯事实，不生成商业结论。所有 `sourceRef`/`artifact` 都是运行目录内的相对 POSIX 路径并带 SHA-256 与大小；视频以 `manifest.json.videoArtifacts` 按素材 ID 绑定，只有已在 `sourceFiles` 中声明且通过哈希校验的 MP4/WebM 才可进入报告。文件哈希在一次进程运行内按规范化绝对路径缓存，避免对大 records 文件重复计算。签名媒体 URL、绝对路径、账号、会话和凭证字段会被移除。`verify-fact-pack` 完全离线校验 TopN、分类计数、来源哈希、JPEG、已声明视频及敏感字段。

## 商业分析交接

采集、阶段一报告和商业分析分为两个阶段。阶段一完成查询、RPA、四类事实包和离线校验后，先执行 `render-overview` 生成素材总览报告；总览报告生成成功即代表阶段一成功，阶段一成功后必须停止并提示用户进入阶段二，运行状态按交接协议进入 `awaiting_analysis_input`。阶段一报告只呈现已落盘事实，不自动生成商业结论。状态链路为：`awaiting_query_input → querying → rpa_processing → fact_pack_building → fact_pack_verifying → fact_pack_ready → [render-overview] → awaiting_analysis_input → analysis_preparing → awaiting_model → analysis_verifying → analysis_verified → report_ready`；`render-overview` 是离线报告产物步骤，不改变事实包或分析交接文件。长阶段回执持续记录开始/结束时间、进度和当前素材，失败时保留 `query_failed`、`fact_pack_failed` 或 `analysis_failed` 及可重试原因。

阶段一的交接文件为 `analysis-handoff.json`，至少包含 `runId`、`factPackPath`、`factPackManifestSha256`、四类计数、`missingItems` 和 `sourceRefs`。阶段二由用户选择一个或多个 `material_leaderboard`、`audience`、`scripts`、`highlight_frames` 分区，指定全部、TopN 或素材 ID 范围，并用自然语言描述分析目标、关注指标、比较维度、输出形式和深度。

阶段二请求契约是一个 JSON 根对象；外层 `schemaVersion` 位于请求根，且固定为 `1`。`analysisSpec` 与 `factRetrievalSpec` 是二选一的嵌套对象，不能把两者混在一起，也不能把检索字段平铺到外层。`analysisSpec` 可先为 `null`，本地恢复入口会根据交接内容生成最小合法规范，或直接使用上游解析出的 AnalysisSpec：

```json
{
  "schemaVersion": 1,
  "stage": "analysis",
  "runId": "run-20260810-20260820",
  "factPackManifestSha256": "...",
  "sections": ["material_leaderboard", "audience"],
  "materialScope": {"type": "top_n", "value": 3},
  "requestText": "分析高消耗素材的共同卖点，并给出创作建议",
  "analysisSpec": null
}
```

`FactRetrievalSpec` 自身也是根对象，内部另有自己的 `schemaVersion: 1`；它只表达自然语言目标到事实包白名单字段的中性检索计划。`period`、`scope`、`ranking`、`candidateTopN`、店铺/广告主、QuerySpec、HighlightSpec 和 RPA 字段都不能出现在其中。对象/数组误用、未知字段、外层与嵌套形态混用时，脚本返回包含 `code`、`path`、`expected`、`actual` 和 `repair` 的结构化错误，供当前对话模型自动修正。

继续分析时必须绑定同一 `runId` 和 manifest SHA-256；事实包变化、分区文件缺失/不可用、素材范围越界或请求文本为空都会被阻断，缺失数据不会被当作零：

```bash
uv run --with-requirements requirements.txt python scripts/run_meta_skill.py continue-analysis \
  --run-dir /path/to/completed-run \
  --analysis-request /path/to/analysis-request.json
```

阶段二长任务可安全重试：入口会先检查分析配置，`prepare-analysis` 会复用同一事实包且未变化时的成功校验回执；事实包、manifest 或来源文件变化时会自动失效并重新校验。超时后优先读取 `run-receipt.json`、`analysis/analysis-receipt.json` 和已生成的 `analysis-input.json`，不要重新执行查询或 RPA。

分析请求文件使用上述外层对象；阶段二由用户继续输入触发，当前对话模型先把自然语言标准化为 `FactRetrievalSpec` 或 `AnalysisSpec`，本地脚本只接受并校验对应嵌套对象。AnalysisSpec 必须声明一个或多个 `sections`（`material_leaderboard`、`audience`、`scripts`、`highlight_frames`）、素材 ID/rank 选择、目标/问题、字段与聚合、分组比较、显式派生计算、证据策略和输出格式。

阶段二补充约束：用户继续输入的自然语言先转成中性的 `FactRetrievalSpec`，只读取已落盘事实包；它不得携带阶段一字段。`analysis-input.json` 中的 `retrievalTrace` 记录意图、分区、字段、事实文件/集合路径和选定素材，非法字段、越界素材或缺失分区只返回缺口并保持等待，不重新查询、不重跑 RPA；补问完成后只重新生成检索计划。

阶段一停止时向用户提示：`阶段一采集和事实包校验已完成，当前暂停在 awaiting_analysis_input。请描述要分析的目标、分区、素材范围和关注指标；阶段二将由当前对话模型继续。` 不得在阶段一自动生成商业结论。用户继续输入后，模型必须先生成合法 `FactRetrievalSpec` 或 `AnalysisSpec`，再调用 `continue-analysis`/`prepare-analysis` 生成交接文件；不能把自然语言直接当作已校验的分析结果。

`analysis-input.json` 是当前对话模型的唯一事实边界，至少包含 `runId`、`factPackManifestSha256`、`factPack.manifestRef`、标准化 `analysisSpec`、用户原始 `requestText`、选定分区的事实切片和 `sourceRefs`。其中 `inputContract.rootType=object`，`factPackRootType=object`，集合行路径固定为 `sections.<section>.evidence.rows`，每个分区声明事实文件、集合路径、字段注册表路径、选定字段和 sourceRef 路径；`sliceSummary` 与 `missingItems` 明确请求行数、返回行数、截断、缺失和证据可用性。`modelConstraints.onlyUseProvidedEvidence=true`、`doNotUseExternalData=true`、`doNotFillMissingData=true`。模型只能使用选定分区、素材 ID、字段和 input 中的 sourceRefs；不得访问未选分区、补充外部事实、把缺失推断为零或伪造模型调用。

模型完成二次分析后必须只返回可解析的 `analysis-result.json`，至少包含 `schemaVersion`、`contractVersion=2`、`analysisSpecFingerprint` 和非空 `claims`；每条 claim 必须包含 `claimType`、`statement`、`section`、`priority`、`evidenceRefs`、`confidence`，其中 `section` 只能是本次选定的事实包分区或 `synthesis`，`priority` 为 `high|medium|low`，可选 `businessImplication` 用于表达业务含义。建议必须带证据引用，计算还必须声明 `calculationId`、`formula` 和 `inputs`。结果必须符合输入中的 `resultSchema`，并通过：

```bash
uv run --with-requirements requirements.txt python scripts/run_meta_skill.py receive-analysis-result \
  --run-dir /path/to/completed-run --analysis-result /path/to/analysis-result.json
```

当前运行时没有本地模型调用通道；`receive-analysis-result` 只负责接收对话模型交回的 JSON、执行离线证据/哈希校验并在成功后落盘报告，不会伪造模型调用，模型不得伪造调用已发生。缺失或非法 JSON 保持 `awaiting_model`，不生成成功报告；校验失败保持 `analysis_failed`，也不生成成功报告。

`prepare-analysis` 只允许读取已通过 `verify-fact-pack` 的完整事实包，并写入 `analysis-request.json`、`analysis-spec.json`、`analysis-input.json`、`analysis-receipt.json`。输入只包含声明字段和选择范围的切片；每个来源保留 manifest/sourceRef/哈希。`analysis-input.json` 是模型交接边界，模型应返回带 `contractVersion=2` 的 `analysis-result.json`；每条 claim 必须带 `claimType`、`section`、`priority`、`evidenceRefs`、`confidence`，可选 businessImplication，计算另带 formula/inputs。事实包正文按素材榜单、人群、脚本、高光帧固定分区；人群结论只能表达相关性，脚本必须引用逐字稿原文，高光帧视觉观察必须标记为 AI 推断并引用 JPEG artifact。历史结果缺少 section/priority 时按来源路径兼容归类，无法唯一归类的进入 synthesis。

`verify-analysis` 完全离线校验结果契约、事实包、证据路径与哈希，并生成 `report.json`、`report.md`。报告固定按“核心商业结论、优先行动建议、素材榜单分析、人群分析、脚本分析、高光帧分析、跨分区综合洞察、数据限制与下一步、附录 A：证据索引、附录 B：结论审计”输出；正文只展示 `[E1]` 等短证据标记，confidence、完整来源路径、公式和归类方式仅保留在 JSON 与审计附录，并保留 `claimsByType` 兼容字段。缺少模型结果、query-only、事实包不完整、请求字段未知、素材越权、缺失类别或未声明计算时，只能保持 `blocked`/`awaiting_model`/`failed`/`incomplete`，禁止声称商业分析完成。任何证据缺失、脚本 `missing|failed` 或无高光帧都不会被推断为零或成功事实。当前 Markdown 为无外部依赖的唯一渲染格式；HTML/PDF 导出属于后续可选扩展，本次链路不依赖也不声称已生成。

独立抽帧仅在已有本地视频与互动曲线时执行，不会发起报表查询；`run` 内部会自动准备这些输入：

```bash
uv run --with-requirements requirements.txt python scripts/run_meta_skill.py select-frames \
  --highlight-spec /path/to/highlight-spec.json \
  --records-json /path/to/interactions.json \
  --video /path/to/material.mp4 \
  --output-dir /path/to/frames
```

过渡期仍可用旧合并式 `--config` 入口，但新请求必须优先使用独立的 `--query-spec` 和 `--highlight-spec`。

## 产物与恢复

运行目录保存解析后的配置、`source-data/`、候选素材、脱敏查询回执和 `run-receipt.json`；RPA 运行另保存诊断 CSV、records、`rpa/videos/` 中的原视频、可选 JPEG 和 `top_materials_rpa_ready.json`。配置、查询范围或高光帧规则变化时必须使用新的输出目录。

`query` 成功不代表 RPA 已完成。RPA 仅在目标数量的素材均成功处理并保存视频后才完整；失败素材会按保存的排名顺序由后续候选补位，仍不会超过 `targetTopN`。当前保留经下载上限校验的原视频，不做隐式压缩；如需压缩，后续必须显式声明目标编码、码率/尺寸上限、保留源文件还是替换源文件，并为压缩产物另写入路径、大小和 SHA-256。长期工件不保存凭证。`verify-run` 完全离线，可校验已完成目录的相对路径、哈希、JSON、视频与 JPEG 工件，不读取凭证也不调用外部服务。
