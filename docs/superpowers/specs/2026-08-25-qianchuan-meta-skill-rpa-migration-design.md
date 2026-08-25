# 千川元技能 RPA 分层迁移设计

## 1. 文档状态

- 状态：已确认，作为下一次实现对话的设计输入。
- 方案：方案 2，分层迁移。
- 范围：把原千川视频高光帧技能中已经验证的 RPA 可靠性能力迁入元技能，并与现有 `QuerySpec`、`HighlightSpec` 串成自动链路。
- 本轮不实现代码、不执行真实 RPA、不修改原技能仓库。

原技能实现参考 `codex/migrate-direct-material-data` 分支（RPA 适配器、波次调度、运行时隔离、CSV 解析、工件回执和批量验证）。迁移时复用其行为契约，不复制合并式 CLI 的全部业务逻辑。

## 2. 目标与非目标

### 2.1 目标

一次 `run` 执行应形成以下稳定链路：

```text
QuerySpec + HighlightSpec
        -> 直接千川报表取数
        -> 候选素材池落盘
        -> 自动解析唯一 RPA 授权账号
        -> 按素材 ID 直接提交 RPA 诊断
        -> 波次调度、失败补位、恢复和回执持久化
        -> 解析诊断 CSV 为规范化 records
        -> 临时下载视频
        -> 按 HighlightSpec 抽取六指标高光帧
        -> 验证并生成 rpa-ready 素材清单
```

用户不需要在 API 素材列表落盘后再次确认；素材 ID 从候选工件传入 RPA 后立即执行。查询条件和高光帧标准仍由模型转换为受控 Spec，脚本只做确定性校验和执行。

### 2.2 非目标

- 本阶段不实现商业分析口语到标准 Prompt 的转换。
- 本阶段不生成商业分析报告。
- 本阶段不根据元技能自动生成用户专属新技能。
- 不把 RPA 账号选择、重试、并发和超时交给自然语言自由控制。
- 不把原技能的固定“点击峰值”规则迁入；所有抽帧选择都必须经过现有六指标 `HighlightSpec`。

## 3. 现有契约的扩展

### 3.1 QuerySpec

保留当前 `schemaVersion: 1` 和现有字段。`collection` 增加可选的 `candidateTopN`：

```json
{
  "schemaVersion": 1,
  "source": "qianchuan_direct_material_report",
  "scope": {"shopId": "report-shop-id", "advertiserId": "advertiser-id"},
  "period": {"startDate": "2026-08-01", "endDate": "2026-08-24"},
  "filters": {
    "dimensionFilters": [],
    "metricFilters": []
  },
  "ranking": {"field": "overall_cost", "direction": "desc"},
  "collection": {"candidateTopN": 27, "targetTopN": 9}
}
```

规则：

1. `targetTopN` 是最终需要成功完成 RPA 和后处理的素材数。
2. `candidateTopN` 是本次 RPA 候选池大小，必须大于等于 `targetTopN`；缺省时取 `targetTopN`，保持旧配置兼容。
3. 直接报表查询只能返回候选池，不得在查询阶段下载视频或触发 RPA。
4. 候选素材必须包含可验证的 `materialId`；缺少、重复或不符合路径安全规则的 ID 在进入 RPA 前失败。
5. `shopId` 只属于直接报表连接器的 ID 域，不得作为 RPA 账号 ID 使用。

`resolved-query-spec.json`、`top_materials.json` 和 `query-receipt.json` 继续保存解析后的 Spec 和配置指纹；`top_materials.json` 的 `materialCount` 代表候选池数量，并记录 `candidateTopN`、`targetTopN`。

### 3.2 HighlightSpec

沿用当前契约，六项指标必须全部显式声明：

- `CLICK_COUNT`
- `LOSE_COUNT`
- `LIKE_COUNT`
- `COMMENT_COUNT`
- `SHARE_COUNT`
- `NEW_FOLLOWER_COUNT`

每项可禁用，或使用 `local_peaks` / `global_maximum`，并设置 `maxFrames`。多指标命中同一秒时只产生一张 JPEG，但证据中保留全部命中指标。RPA 只负责提供诊断 records；抽帧、波峰与绝对值最大值的判定全部由现有高光帧选择器根据 `HighlightSpec` 完成。

### 3.3 RPA 执行配置

RPA 运行参数是运行控制项，不属于商业查询意图，不放入用户自由编写的 QuerySpec。实现可通过受控 CLI 参数或内部 `RpaRunOptions` 提供，至少包括：

- RPA 并发数；
- 单素材隔离超时；
- 延迟 CSV 工件等待窗口；
- 失败结果恢复次数与退避时间；
- CSV、视频最大大小及下载总截止时间。

实现应提供稳定默认值，并把实际采用的非敏感参数写入 `resolved-rpa-options.json`，不得写入任何凭证。

## 4. CLI 与执行语义

### 4.1 `query`：纯取数入口

```bash
python scripts/run_meta_skill.py query \
  --query-spec /path/query-spec.json \
  --highlight-spec /path/highlight-spec.json \
  --output-dir /path/run
```

`query` 的职责到 `top_materials.json`、`query-receipt.json` 和源数据落盘为止。它不能触发 RPA、视频下载、抽帧、AI 分析或报告生成，便于调试和只取数场景。

### 4.2 `run`：自动 RPA 入口

```bash
python scripts/run_meta_skill.py run \
  --query-spec /path/query-spec.json \
  --highlight-spec /path/highlight-spec.json \
  --output-dir /path/run
```

`run` 先执行与 `query` 完全相同的 Spec 校验和报表取数。候选工件成功落盘后，不请求人工确认，直接读取其中的素材 ID 并执行 RPA。`run` 必须支持从已有运行目录恢复：若查询工件和配置指纹一致，可跳过已完成的查询阶段；若指纹不一致，必须拒绝复用旧工件。

### 4.3 `select-frames`：本地独立入口

保留当前入口，用于已有本地 records 和视频的抽帧测试或补处理；它不查询报表，也不调用 RPA。`run` 内部的后处理复用同一抽帧实现。

## 5. RPA 适配层

### 5.1 网关契约

迁入原技能 `rpa_adapter.py` 的网关行为：

- function code：`rpa.conn.juliang.qc.material.analysis.video.live.diagnosis`；
- platform：`RPA_JULIANG_QC_ACCOUNT_PASSWORD`；
- data source type：`rpa`；
- 运行环境：`ENV_BACKEND_HOST`、`YUCE_AUTHORIZATION`、`YUCE_SESSION_ID`，并兼容现有 `YCSESSIONID` 会话变量映射；
- 网关请求、CSV 下载和视频下载均使用超时、大小上限、临时文件原子替换和响应脱敏。

### 5.2 授权账号解析

`run` 在提交第一波素材前调用 RPA 授权账户列表接口，并按以下规则解析：

1. 0 个匹配账号：失败，错误类型 `rpa_account_unavailable`。
2. 多个匹配账号：失败，错误类型 `rpa_account_ambiguous`，禁止静默选择。
3. 恰好 1 个账号：自动使用该账号继续执行，不需要用户确认。

报表 `shopId` 和 RPA 授权账号 ID 必须分别记录在不同字段中，避免跨 ID 域误用。账号列表响应只保留脱敏摘要和所选账号的非敏感标识。

### 5.3 日期映射

RPA 诊断日期固定复用 `QuerySpec.period`：

- `date_range_type = CUSTOM`；
- `custom_start_date = period.startDate`；
- `custom_end_date = period.endDate`。

不得使用 RPA 默认“昨日/近 7 日”等相对日期，否则会造成报表候选与诊断曲线周期不一致。恢复时也必须校验 manifest 中的日期和当前 Spec 一致。

## 6. 波次调度与失败补位

迁入原技能 `rpa_batch_runner.py` 的波次模型，单个素材的 RPA 请求在独立子进程中运行：

1. 按候选池排名建立 manifest 条目，初始状态为 `pending`。
2. 每波最多提交受控并发数的素材，波次结束后统一进入屏障。
3. RPA 成功且 CSV 工件可验证的素材才进入后处理。
4. RPA 请求失败、超时、CSV 不可用或 records 无法解析的素材标记失败；从候选池后续素材补位，直到达到 `targetTopN` 或候选耗尽。
5. 后处理失败也会释放名额并触发补位，但原失败条目保留完整错误和尝试记录。
6. 已达到 `targetTopN` 后，不再提交额外候选；未达到时输出部分成功状态并保留可恢复 manifest。

每个素材至少需要以下状态字段：`rpaStatus`、`postprocessStatus`、`status`、`failureReason`、`errorType`、`diagnosisCsvPath`、`diagnosisRecordsPath`、`recordsPersisted`、`postprocessAttemptCount` 和各阶段耗时。状态迁移必须单调且可由 manifest 重建。

## 7. 工件、回执与恢复

一次运行的 RPA 工件统一放在运行目录下：

```text
run/
  resolved-query-spec.json
  resolved-highlight-spec.json
  resolved-rpa-options.json
  configuration-fingerprint.json
  source-data/
  top_materials.json
  query-receipt.json
  highlight-policy.json
  rpa/
    batch-manifest.json
    diagnosis-csv/<material-id>.csv
    diagnosis-records/<material-id>.json
    rpa-receipts/<material-id>.json
    peak-frames/<material-id>/
    top_materials_rpa_ready.json
    batch-verification.json
```

视频只允许在素材处理期间临时下载到受控临时目录，抽帧完成或失败后删除；不得把视频副本写入上述长期工件目录。

每个素材的 RPA 回执至少经过：

1. `submitted`：已提交请求，记录 client attempt ID；
2. `artifact_downloaded`：CSV 已下载，记录字节数、SHA-256 和下载时间；
3. `records_persisted`：CSV 已按 `\x01` 分隔格式解析并写入 records，记录记录数和文件完整性。

CSV、records 和回执都采用临时文件加原子替换。恢复时按配置指纹、material ID、日期和 SHA-256 校验；只有完整且可验证的工件才可跳过 RPA 请求。处于 `submitted` 但没有完整回执的条目进入有限恢复流程，不得无限重试。收到中断信号时先写 checkpoint，再退出，并保留 `batch-manifest.json`。

## 8. CSV 解析与六指标后处理

RPA CSV 使用原技能解析器的字段分隔、结构化字段、数值校验和空行处理规则，输出规范化 records。解析失败属于素材级失败，不得把未经解析的原始文本直接传给抽帧器。

后处理顺序固定为：

1. 校验 records 的 material ID 与候选条目一致。
2. 从 records 提取六项互动指标的时间序列。
3. 使用解析后的 `HighlightSpec` 分别计算局部波峰或全局绝对值最大点。
4. 合并相同秒数命中，按既有规则抽取 JPEG 并写入证据摘要。
5. 生成每素材摘要和 `top_materials_rpa_ready.json`。

`top_materials_rpa_ready.json` 只包含 RPA 成功、records 已持久化、抽帧验证通过的素材，且不改变原候选排名。它应记录命中的指标、秒数、帧路径、records 路径、诊断 CSV 回执引用和配置指纹。

## 9. 验证、错误与退出行为

`batch-verification.json` 对最终结果执行批量验证，至少检查：

- 候选池和目标数量关系；
- manifest 中所有选中素材都有可验证 CSV、records 和抽帧摘要；
- JPEG 存在、非空且可解码；
- material ID、日期、配置指纹和工件路径相互一致；
- 凭证、授权 token 和完整网关响应未进入日志或长期工件。

建议错误分类：

- 配置类：`invalid_query_spec`、`invalid_highlight_spec`、`candidate_pool_invalid`；
- 授权类：`rpa_account_unavailable`、`rpa_account_ambiguous`；
- 请求类：`rpa_gateway_client_error`、`rpa_gateway_server_error`、`rpa_rate_limited`、`rpa_request_timeout`；
- 工件类：`rpa_csv_artifact_unavailable`、`rpa_csv_invalid`、`rpa_records_invalid`；
- 后处理类：`video_download_failed`、`frame_extraction_failed`、`frame_verification_failed`。

行为约定：

- 配置、账号解析或查询失败：不提交任何 RPA 素材，命令非零退出。
- 候选池耗尽但成功数少于 `targetTopN`：写完整 manifest、回执和验证结果，命令非零退出，并明确报告部分成功数量。
- 达到 `targetTopN` 且所有选中素材验证通过：命令零退出。
- 单素材失败不覆盖其他素材结果，也不丢弃失败证据。

## 10. 测试验收

实现必须先以测试驱动方式完成，至少覆盖：

### 配置与入口

- `candidateTopN` 缺省、等于和大于 `targetTopN` 的规范化行为；小于目标时在执行前失败。
- `query` 只生成候选工件，绝不调用 RPA；`run` 在候选落盘后自动调用 RPA，且不等待人工确认。
- 旧 `--config` 入口仍可完成兼容查询。

### 账号、周期与安全

- 0/1/多 RPA 授权账号分别产生失败、自动选择、歧义失败。
- 报表 `shopId` 不会作为 RPA 账号 ID；QuerySpec 周期准确映射到 `CUSTOM`。
- 缺失环境变量、超大下载、非法 material ID 和敏感信息脱敏均有测试。

### 波次、恢复与补位

- RPA 超时能终止隔离子进程并写终态。
- CSV 延迟到达可在有限窗口内恢复；超过重试上限后终态失败。
- RPA 失败和后处理失败都会按排名顺序补位，波次屏障不会提前计入未验证素材。
- 中断后可从 manifest 恢复，已验证工件不重复请求，未完成条目可继续执行。

### 解析、抽帧与验证

- `\x01` CSV 解析、结构化字段和无效数值处理符合既有契约。
- 六项指标可同时启用；局部波峰、全局最大值、同秒合并和第 0 秒策略均按 `HighlightSpec` 生效。
- `top_materials_rpa_ready.json` 只包含完整成功素材；批量验证可识别缺失或篡改工件。

## 11. 实现顺序（下一对话执行）

1. 扩展配置解析和 CLI：`candidateTopN`、受控 RPA 运行选项及 `run` 入口，先补测试。
2. 迁移 RPA 适配器、账号解析、日期映射、隔离运行时和 CSV 解析。
3. 接入波次调度、补位、回执、manifest checkpoint 和恢复。
4. 接入临时视频下载及 `HighlightSpec` 后处理，生成 rpa-ready 工件。
5. 接入批量验证、错误退出语义和完整回归测试。

每一步都不得修改原技能仓库；元技能与原技能之间通过明确的 Python 模块契约和运行工件契约交互。
