# 千川元技能 RPA 核心功能设计压缩指令

你正在实现千川素材分析元技能的 RPA 迁移。必须保留“模型理解需求，脚本确定性执行”的边界：模型只生成并校验 `QuerySpec`、`HighlightSpec`，不得自由拼接 API、SQL、账号或指标字段；脚本负责查询、RPA、工件、恢复和抽帧。

## 目标链路

`QuerySpec + HighlightSpec -> 直接千川报表取数 -> 候选素材池 -> 唯一 RPA 授权账号 -> 按 materialId 执行诊断 -> 波次/并发/失败补位/恢复 -> 诊断 CSV/records -> 临时下载视频 -> 六指标高光帧 -> rpa-ready 清单`。

## CLI 契约

保留三个入口：

```bash
python scripts/run_meta_skill.py validate --query-spec QUERY.json --highlight-spec HIGHLIGHT.json
python scripts/run_meta_skill.py query --query-spec QUERY.json --highlight-spec HIGHLIGHT.json --output-dir RUN
python scripts/run_meta_skill.py run --query-spec QUERY.json --highlight-spec HIGHLIGHT.json --execute-rpa --output-dir RUN
python scripts/run_meta_skill.py verify-run --output-dir RUN
```

`query` 只能取数和落盘候选工件，绝不能启动 RPA、视频下载、抽帧、AI 分析或报告生成。`run` 默认只查询或复用候选并写入生命周期回执；只有携带 `--execute-rpa` 才会启动 RPA、视频下载和抽帧。已有运行目录只有在配置指纹和日期完全一致时才可恢复。`verify-run` 不访问网络，可离线校验完成运行的便携工件。

## Spec 约束

`QuerySpec` 保留 `schemaVersion: 1`、`source: qianchuan_direct_material_report`、`scope.shopId`、`scope.advertiserId`、日期范围、白名单过滤器和排序；`collection` 必须包含 `targetTopN`，可选 `candidateTopN`，缺省时等于目标数，且 `candidateTopN >= targetTopN`。`targetTopN` 是最终成功数，候选池可更大用于失败补位。报表 `shopId` 和 RPA 授权账号/店铺 ID 属于不同 ID 域，禁止混用。

`HighlightSpec` 必须显式配置六个指标：`CLICK_COUNT`、`LOSE_COUNT`、`LIKE_COUNT`、`COMMENT_COUNT`、`SHARE_COUNT`、`NEW_FOLLOWER_COUNT`。启用规则只能是 `local_peaks` 或 `global_maximum`；后者 `maxFrames` 必须为 1；同秒多指标只抽一张 JPEG 但保留全部证据；`includeZeroSecond` 控制第 0 秒封面帧。

## RPA 网关

使用环境变量 `ENV_BACKEND_HOST`、`YUCE_AUTHORIZATION`、`YUCE_SESSION_ID`，兼容 `YCSESSIONID`。RPA function code 为 `rpa.conn.juliang.qc.material.analysis.video.live.diagnosis`，platform 为 `RPA_JULIANG_QC_ACCOUNT_PASSWORD`，data source type 为 `rpa`。账号列表必须严格遵循：0 个匹配账号报 `rpa_account_unavailable`，多个报 `rpa_account_ambiguous`，恰好 1 个自动选用；选中的 RPA shop/account ID 必须单独记录，不能用报表 `scope.shopId` 替代。

诊断日期固定映射为 `date_range_type=CUSTOM`、`custom_start_date=period.startDate`、`custom_end_date=period.endDate`。必须校验网关成功状态、文件任务 `COMPLETED`、`failedInstances == 0`；同时支持 payload records 和 files CSV 两类响应。HTTP 请求、CSV 和视频下载均有超时、大小上限、截止时间、临时文件和原子替换；敏感字段、token、cookie、完整网关响应不得进入日志或长期工件。

## 调度与恢复不变量

按候选排名建立 manifest，单素材请求在隔离子进程执行；每波最多提交受控并发数，且不得提交超过剩余 `targetTopN` 名额。RPA 失败、超时、CSV 无效、records 无效、视频/抽帧/验证失败都必须保留失败证据，并从后续候选按排名补位。达到 `targetTopN` 后立即停止新提交，ready 清单最多包含目标数。

每个素材至少记录：`rpaStatus`、`postprocessStatus`、`status`、`failureReason`、`errorType`、`diagnosisCsvPath`、`diagnosisRecordsPath`、`recordsPersisted`、`postprocessAttemptCount`、各阶段耗时和恢复尝试。回执状态单调推进：`submitted -> artifact_downloaded -> records_persisted`；CSV/records/回执使用临时文件加原子替换。恢复时校验配置指纹、日期、materialId、路径在运行目录内、CSV SHA-256、records 身份、JPEG/summary 完整性；不完整工件不得跳过 RPA。

## 工件布局

```text
RUN/
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

视频只能存在于素材处理期间的受控临时目录，抽帧后必须删除。`top_materials_rpa_ready.json` 只能包含 RPA 成功、records 已持久化、抽帧和 JPEG 验证通过的素材，且保持原候选排名。

## 错误与退出

配置/账号/查询失败：不提交 RPA，非零退出。候选耗尽但成功数低于 `targetTopN`：保留 manifest、失败证据和批量验证，非零退出。达到目标且所有选中素材验证通过：零退出。错误类型至少区分：`invalid_query_spec`、`candidate_pool_invalid`、`rpa_account_unavailable`、`rpa_account_ambiguous`、`rpa_request_timeout`、`rpa_csv_artifact_unavailable`、`rpa_csv_invalid`、`rpa_records_invalid`、`video_download_failed`、`frame_extraction_failed`、`frame_verification_failed`。

## 测试门禁

必须测试：`candidateTopN` 兼容和边界；`query` 不触发 RPA；未携带 `--execute-rpa` 的 `run` 不解析账号或触发 RPA，携带该参数的 `run` 才继续；0/1/多账号；报表/RPA ID 隔离；CUSTOM 日期；CSV `\x01`、结构化字段、SHOW_COUNT 和无效数值；超时隔离；延迟 CSV 有限恢复；波次屏障、失败补位、目标上限；中断 checkpoint；指纹/日期/工件篡改恢复；临时视频清理；六指标局部峰、全局最大、同秒合并、第 0 秒；JPEG 和批量验证；敏感信息脱敏。

## 非目标

本阶段不实现 AI 商业分析 Prompt 标准化、商业分析报告生成，也不根据元技能自动生成用户专属技能。不要把这些能力塞进通用 RPA/调度层。
