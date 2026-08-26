# 千川素材全字段采集迁移设计

日期：2026-08-26

## 目标

将当前素材分析技能已验证的日期范围素材汇总采集契约迁入元技能，使元技能每次素材列表查询请求 8 个非时间维度和 102 个指标，并将完整原始记录和请求字段清单落盘。

本次只扩展原始数据采集和审计能力。`QuerySpec` 继续只开放现有字段注册表中的受控业务字段；不将全部 API 原始指标暴露为自然语言筛选或排序字段。

## 背景与事实

接口主题固定为 `OVERALL_ROI_LIVE_MATERIAL_VIDEO`。动态 config 可确认 98 个指标；下列 4 个指标虽不在动态 config 中，但已通过直接报表请求验证可返回：

```text
stat_cost_for_overall_roi2
total_prepay_and_pay_settle_overall_roi2_1h
total_cost_per_pay_order_settle_for_overall_roi2_1h
total_prepay_and_pay_settle_roi2_1h
```

因此本设计定义的“全字段”为 98 个 config 指标加上述 4 个扩展指标，共 102 个已知、已验证的指标；不承诺未公开的服务端理论字段全集。

真实响应证明：个别记录可能缺失非关键素材元数据。因此字段采集的完整性和可用于候选/RPA 的完整性必须分层处理。

## 非目标

- 不通过运行时 `get-qianchuan-report-config` 动态决定请求字段。
- 不按日或小时取数，也不对不同时间粒度结果做合并。
- 不改动 `QuerySpec` 或 `config/asset-field-registry-v1.json` 的公开字段集合。
- 不引入商业分析、报告生成、独立技能生成或新的 RPA 参数。
- 不把缺失指标当作数值零。

## 固定采集契约

在 `scripts/qianchuan_report_client.py` 以静态、有序、无重复常量维护下列字段集合。

### 维度

```text
ecp_app_id
roi2_material_type_v3
roi2_material_video_play_info
roi2_material_video_name
roi2_material_video_type
material_create_time_v2
material_id
anchor_id
```

维度数为 8，低于接口的 10 个维度限制。明确不请求：

```text
roi2_material_advice
roi2_material_advice_reason
stat_time_day
range_stat_time_hour
```

后两个时间维度会将结果拆成素材乘日期或素材乘小时，不符合目标的日期范围素材汇总粒度。`roi2_material_advice_reason` 在真实 1,147 条响应中均未返回，且当前不被下游消费。

### 指标

`DEFAULT_METRICS` 使用当前技能已经验证的完整有序 102 项集合。该集合包括现有元技能使用的 8 个原子指标，因而现有受控业务查询继续具备全部数据来源。

请求仍包含既有业务过滤器：

```json
[
  {"field": "ecp_app_id", "operator": 7, "values": ["1", "2"]},
  {"field": "roi2_material_type_v3", "operator": 7, "values": ["3"]}
]
```

网关排序由 QuerySpec 排序字段映射得到：原子指标采用其 API 字段，派生指标保留 `stat_cost_for_overall_roi2` 作为稳定的网关回退排序；最终候选排序始终在本地聚合后执行。

## 两层字段边界

原始采集契约和 QuerySpec 注册表是独立契约：

```text
QuerySpec（18 个受控业务字段）
  -> 报表连接器（8 个维度 + 102 个原始指标）
  -> source-data 原始完整记录与字段清单
  -> material_query（仅消费注册表字段）
  -> top_materials.json
  -> RPA（materialId、startDate、endDate）
```

注册表保持 18 个字段：5 个素材维度、8 个原子指标、5 个派生指标。完整定义保留在 `config/asset-field-registry-v1.json`，本次不新增可供 QuerySpec 使用的字段。

这样可同时保证全量原始事实可审计，并防止缺少中文业务语义、操作符、排序资格和派生口径定义的 API 字段被意外开放。

## 模块改造

### `scripts/qianchuan_report_client.py`

1. 将现有的 5 个维度、8 个指标替换为静态 8/102 字段常量，并沿用现有直接网关、payload、files/CSV 结构化、认证脱敏和阶段状态文件机制。
2. 请求构造前校验：维度最多 10 个、维度与指标分别无重复、不包含时间拆分维度、网关排序字段属于指标集合。
3. 成功取得非空对象记录后校验每条记录的 `metrics` 都含 102 个预期键。`0`、空字符串和 `null` 是返回值而不是缺键，不能被视为字段缺失。
4. 不将维度键作为整批失败条件。维度值缺失须完整保留到原始数据，交由候选层按实际需要处置。
5. 仅当字段校验成功时写入 `source-data/field-manifest.json`。

### `scripts/material_query.py`

1. 保持按 `material_id` 聚合、派生指标计算、筛选和排序的现有职责。
2. 源记录缺少 `material_id` 时跳过该记录，而不是让整批查询失败；回执写入 `missing_material_id` 和源记录计数。
3. 活跃维度筛选依赖的字段无值时，该记录以 `unavailable_dimension_value` 排除，不能与普通 `dimension_filter` 混淆。
4. 活跃的指标筛选或排序所需原子/派生指标无值时，继续使用 `unavailable_metric_value` 或 `unavailable_sort_value` 排除对应素材。
5. 仅累加真实存在的原子指标；不得以零补齐缺失值。素材输出格式和 `top_materials.json` 保持兼容。

### `scripts/run_meta_skill.py`

仅补充查询回执的采集摘要。`collect_report_records`、`_top_materials_payload`、QuerySpec 解析和 RPA 调用边界不变。

执行 RPA 时继续从候选条目读取 `materialId`，并从解析后的 QuerySpec 读取：

```python
startDate = query["period"]["startDate"]
endDate = query["period"]["endDate"]
```

因此无需修改 RPA worker 的入参协议。

## 运行工件与审计

成功的查询运行应包含既有工件，并增加完整字段证据：

- `source-data/gateway-response.json`：脱敏网关回执。
- `source-data/*.csv` 与 `.structured.json`：当网关返回 files 时保留。
- `source-data/field-manifest.json`：主题、8 个维度、102 个指标、过滤条件、网关排序、日期范围、记录数；不得含凭证、cookie、签名 URL 或授权头。
- `query-receipt.json`：既有规范化查询和候选统计，增加 `sourceRecordCount`、`skippedSourceRecordCount` 及按原因汇总的跳过/排除统计。
- `top_materials.json`：保持现有候选和日期范围 schema，供 RPA 无变化消费。

字段清单只证明请求与响应键集合，不将缺失的维度值伪造为有效数据。候选回执明确区分源数据问题与业务规则筛除。

## 错误处理

- 网关、认证、下载、CSV 解析、空结果或任一预期指标键缺失：查询失败，不发布 `top_materials.json`。
- 单条缺失 `material_id`：跳过记录，剩余有效记录继续查询。
- 单条缺少不参与活跃筛选/排序的维度：保留其余数据，候选输出中的对应显示字段可为空。
- 缺少当前筛选、排序或派生计算必需指标：只排除该素材，写入结构化回执；不将其作为零值参与计算。

所有错误和回执继续沿用脱敏要求，不记录环境变量、会话值、cookie、authorization 或签名 URL 查询参数。

## 测试与验收

### 报表客户端

1. 默认维度精确为 8 项，包含所列维度且不含四个排除字段。
2. 默认指标精确为 102 项、无重复，包含四个直接验证扩展字段及既有注册表所依赖的 8 个原子 API 指标。
3. 请求保持日期范围、业务过滤器、授权协议和原子指标排序映射。
4. payload 和 files/CSV 两种结果都通过指标键校验并写出字段清单。
5. 非空记录缺少任一预期指标键时失败，错误和工件不泄露敏感信息。
6. 维度值缺失不会被采集客户端当作整批失败。

### 候选查询与 CLI

1. 缺少 `material_id` 的源记录被跳过，其他素材仍可进入候选和 RPA。
2. 缺失活跃维度筛选字段、筛选指标或排序指标时生成正确、可审计的排除原因。
3. 缺失字段不会被累计成 `0`，派生公式也不会在输入不完整时产生数值。
4. 当前 18 个 QuerySpec 字段仍可验证、筛选、聚合和排序；不接受未注册的 102 原始字段。
5. `top_materials.json` 继续保留 `actualStartDate`、`actualEndDate` 与 `materialId`；`run --execute-rpa` 回归确认其原样传递给 RPA。
6. 全量测试套件通过，且不依赖真实内网接口；真实内网同条件查询作为人工验收，不纳入单元测试。

## 实施顺序

1. 为 8/102 请求契约、字段清单和指标完整性新增失败测试。
2. 改造报表客户端并通过客户端测试。
3. 为素材 ID 缺失和活跃字段缺失新增候选容错测试。
4. 改造 `material_query.py` 和查询回执，再验证 CLI/RPA 参数回归。
5. 运行完整测试套件；具备授权时以真实内网查询确认字段清单和候选工件。
