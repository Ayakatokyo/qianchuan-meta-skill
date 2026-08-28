# 千川 Top3 商业分析事实包设计

## 1. 目标与范围

为 `runs/run_20260828_top3` 增加一个面向后续商业分析的规范化事实层。原始查询、RPA、诊断、互动序列和高光帧工件保持不变；新增事实包只负责分类、结构化、脱敏和来源引用，不产出商业判断、因果解释或策略建议。

本次事实范围仅覆盖本次查询返回的 3 个 Top 素材。原始报表共 1,147 条记录、1,146 条有效候选、212 条通过筛选，最终返回 3 条；这些范围数字作为查询证据保留在事实包清单中，但未入选素材不复制为 Top3 事实。

## 2. 输入与可追溯性

事实包从以下现有工件读取：

- `top_materials.json`：Top3 素材身份、排名和 102 个榜单指标。
- `query-receipt.json`、`resolved-query-spec.json`：查询时间范围、筛选、排序和返回范围。
- `rpa/diagnosis-records/<materialId>.json`：素材信息、日明细、人群、脚本和互动序列。
- `rpa/peak-frames/<materialId>/summary.json`：高光帧选择结果和选择证据。
- `rpa/peak-frames/<materialId>/*.jpg`：已验证的 JPEG 高光帧资产。
- `source-data/field-manifest.json`：报表维度、指标及数据主题清单。

每类事实都带有 `sourceRef`，至少包含运行目录内的相对路径和 JSON 记录定位信息；不写入绝对路径。对文件型来源同时记录 SHA-256 与字节大小，便于离线核验。

## 3. 四类事实边界

### 3.1 素材榜单事实

文件：`material-leaderboard.json`。

粒度为一行一个 Top 素材，包含：

- 排名、素材 ID、素材名称、素材类型、投放视频类型、创建时间和素材 URI 等身份信息；
- `metrics` 中完整保留 102 个 API 原始指标名和值；不改变原始百分比/金额/时间窗口语义，不添加未在源数据中存在的派生指标；
- 素材标签、标题和 `analysisInfo` 中的来源分析元数据；
- `dailyDetails` 中该素材的 11 条投放日明细；
- 每个子块的来源引用和缺失状态。

带签名的 `videoUrl`、`thumbnailUrl`、RPA 账号标识和其他凭证/会话信息不进入事实包。

### 3.2 人群事实

文件：`audience.json`。

粒度为一行一个“素材 × 人群维度 × 展示指标 × 人群值”。保留：

- `dimensionKey`、`dimensionName`、人群值及其原始 `Value`/`ValueStr`；
- `displayMetric`、`displayMetricName`；
- `metricValue`、`metricPercentage`、`dimensionTotal`；
- 日期范围、素材 ID 和来源记录定位。

本次实际维度为年龄、性别、省份、城市、八大人群；展示指标为整体展现次数、整体点击次数、整体成交金额。零值和缺失值必须区分，不能静默删除。

### 3.3 脚本事实

文件：`scripts.json`。

粒度为一行一个素材。将源 `scriptInfo` 的 Python 字面量结构解析为可序列化对象，保留：

- `formula.labels`；
- `detail` 中的标签、标签 ID 和文本片段；
- `text` 逐字稿；
- `data` 原始列表；
- `completeness`，明确公式标签、详情标签、逐字稿和结构化数据是否存在。

解析失败时保留原始字符串并标记 `parseStatus: failed`，不得猜测或补写脚本内容。脚本事实不转化为创意优劣结论。

### 3.4 高光帧事实

文件：`highlight-frames.json`。

粒度为一行一个已选帧。保留：

- 素材 ID、帧序号、视频秒数；
- 运行目录内的相对 JPEG 路径、文件哈希和字节大小；
- `selectionEvidence` 中的指标、选择模式、策略序号、排名和指标值；
- 对应的高光策略版本和不可用指标清单；
- 互动序列的来源引用，便于回看 CLICK/LOSE/LIKE/COMMENT/SHARE/NEW_FOLLOWER 六类曲线。

本次高光规则包含封面帧、点击局部峰值（最多 2 帧）和转发全局最大值（最多 1 帧）；同一秒命中多个指标时合并为一行并完整保留所有证据。

## 4. 事实包索引与脱敏

文件：`manifest.json`。

索引包含：

- `schemaVersion`、`runId`、生成时间；
- 查询范围、排序字段/方向、筛选条件的脱敏副本；
- Top3 素材 ID 与各分类的事实计数；
- 原始输入文件清单、哈希、记录数和处理状态；
- `redactions`，列出已移除的签名 URL、绝对路径、账号/会话信息；
- `quality`，记录每类事实的完整度、解析失败、缺失字段和校验结果。

文件路径全部使用相对于 `runs/run_20260828_top3` 的 POSIX 路径。事实包不复制大体量原始 CSV/JSON；原始工件继续作为唯一证据源。

## 5. 验证要求

生成后必须执行离线校验：

1. 四个分类文件均为合法 JSON，且只能包含预期的 Top3 素材 ID。
2. `material-leaderboard.json` 有 3 条素材、每条 102 个榜单指标和 11 条日明细。
3. `audience.json` 的记录总数等于源 `crowd_analysis` 记录数（本次为 3,903），且维度/指标枚举与源一致。
4. `scripts.json` 有 3 条素材，解析状态和缺失状态与源内容一致。
5. `highlight-frames.json` 的帧数、秒数、证据与三个 `summary.json` 一致；所有引用路径均为相对路径且 JPEG 可读。
6. 所有 `sourceRef` 可定位到现有工件；哈希、大小和记录数一致。
7. 事实包中不存在 `videoUrl`、`thumbnailUrl`、绝对路径、RPA 账号、Authorization、Cookie 或 session 字段。

## 6. 非目标

- 不在本次任务中生成商业分析报告、洞察、策略或自然语言结论。
- 不修改 `SKILL.md`、查询/RPA 流程或原始运行工件。
- 不对金额、百分比、ROI、完播率、人群占比做重新计算或跨素材汇总；后续分析阶段再基于事实包明确声明计算口径。
