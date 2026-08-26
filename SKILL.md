---
name: qianchuan-meta-skill
description: 适用于按店铺、广告主和日期范围筛选千川素材，按投放指标排序取 Top N，并执行受控 RPA 诊断和本地高光帧提取的场景。
argument_hint: 依次输入查询范围、指标筛选和高光帧规则；指标可用中文表达，系统会生成使用 API 原始字段名的 QuerySpec 和 HighlightSpec。
metadata:
  pre_input: 请采集千川素材列表和高光帧数据，授权店铺 ID 是 ____（必填）；广告主 ID 是 ____（必填）；时间周期是 ____（自定义起止日期），筛选条件是 ____（可选，例如综合消耗 >= 1000、净成交 ROI >= 1.2、视频完播率 >= 20）；排序指标是 ____（必填，例如综合消耗、净成交金额、净成交 ROI、净成交订单成本）；排序方向是 ____（升序或降序，必填）；需要执行详情拆解的素材数为 ____（正整数，必填）。高光帧筛选：是否保留封面帧（是/否）；高光指标至少选择一项，可从点击、流失、点赞、评论、分享、新增粉丝六项中选择；每个已选指标可按波峰或指标大小截取，并指定帧数。（如：保留封面帧，点赞次数取局部峰值/波峰 2 帧，分享取指标值最高的 1 帧。）
---

# 千川素材分析元技能

该技能只负责受控素材查询、RPA 诊断、临时视频下载和高光帧资产；不生成 AI 商业分析、报告或用户专属技能。模型将自然语言解析为 Spec，脚本负责校验、取数、筛选、RPA、抽帧与审计落盘。

## 输入

自然语言入口从同一请求解析 `QuerySpec` 和 `HighlightSpec`；文件入口直接读取已保存的两份 JSON。解析成功后立即落盘并执行，缺少必填项或字段不合法时反馈具体错误。除用户明确要求预览、草稿或人工确认外，不等待二次确认。

### 查询范围

`shopId` 与 `advertiserId` 都必填且属于不同 ID 域：前者只用于报表连接器授权，后者只用于报表业务筛选。日期范围为闭区间，起止日期均使用 `YYYY-MM-DD`。字段和操作符只能来自 `config/asset-field-registry-v1.json`；维度和指标条件按 `AND` 处理。

### 指标筛选

客户以中文指标名表达阈值、排序和目标 Top N，例如“净成交 ROI >= 1.2，按净成交金额降序取 Top 5”。未给出阈值时 `metricFilters` 为空；客户不需要输入候选补位数。

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

用户以中文业务名表达筛选意图；模型查阅注册表的中文 `label` 后，必须在 QuerySpec 的 `field` 中写入对应 API 原始字段名。固定报表契约的 102 个指标均可用于 `metricFilters` 和 `ranking.field`，方向只能是 `asc` 或 `desc`。API 百分比字段使用其原始百分点，例如 `66.67` 表示 66.67%，而非 0.6667。

### 原始采集字段

每次日期范围素材汇总查询固定请求 8 个非时间维度和 102 个已验证原始指标，并在 `source-data/field-manifest.json` 写入本次请求的脱敏字段清单。每个已请求字段也已注册为 QuerySpec 字段：维度可用于筛选，102 个指标均可用于筛选和排序。

QuerySpec 的 `field` 必须来自 `config/asset-field-registry-v1.json`。注册表的 `label` 仅用于将中文意图映射到 API 字段；已确认的前端中文名称会被保留，其他新开放字段暂使用 API 原始名作为技术标签。候选从单条素材汇总结果直接读取，不做本地累加或派生计算；`material_id = -2` 的 AIGC 动态创意集合会排除，其他重复素材 ID 会作为数据质量错误拒绝查询。

### 高光帧规则

```json
{
  "schemaVersion": 1,
  "includeZeroSecond": true,
  "metrics": {
    "CLICK_COUNT": {"enabled": true, "strategy": "local_peaks", "maxFrames": 3},
    "LOSE_COUNT": {"enabled": false},
    "LIKE_COUNT": {"enabled": false},
    "COMMENT_COUNT": {"enabled": false},
    "SHARE_COUNT": {"enabled": true, "strategy": "global_maximum", "maxFrames": 1},
    "NEW_FOLLOWER_COUNT": {"enabled": false}
  }
}
```

六项指标必须全部显式声明。启用指标只能使用 `local_peaks` 或 `global_maximum`；后者的 `maxFrames` 必须为 1。相同秒数的多指标命中只抽取一张 JPEG，并在选帧证据中保留全部命中。

## 执行

`validate` 不访问外部服务；`query` 使用 `ENV_BACKEND_HOST`、`YUCE_AUTHORIZATION`，以及 `YUCE_SESSION_ID` 或 `YCSESSIONID` 调用千川报表连接器。凭证不得进入 Spec、Git、日志或运行工件。

```bash
python scripts/run_meta_skill.py validate \
  --query-spec /path/to/query-spec.json \
  --highlight-spec /path/to/highlight-spec.json

python scripts/run_meta_skill.py query \
  --query-spec /path/to/query-spec.json \
  --highlight-spec /path/to/highlight-spec.json \
  --output-dir /path/to/new-run

python scripts/run_meta_skill.py run \
  --query-spec /path/to/query-spec.json \
  --highlight-spec /path/to/highlight-spec.json \
  --execute-rpa \
  --output-dir /path/to/new-run

python scripts/run_meta_skill.py verify-run \
  --output-dir /path/to/completed-run
```

`query` 只写入候选查询工件，不触发 RPA、视频下载、抽帧、AI 分析或报告生成。`run` 默认同样只查询或复用候选工件，并写入 `run-receipt.json`；只有显式传入 `--execute-rpa` 才会解析唯一 RPA 授权账号，按候选排名执行受控波次、失败补位和恢复，临时下载视频并按 `HighlightSpec` 抽帧。

独立抽帧仅在已有本地视频与互动曲线时执行，不会发起报表查询；`run` 内部会自动准备这些输入：

```bash
python scripts/run_meta_skill.py select-frames \
  --highlight-spec /path/to/highlight-spec.json \
  --records-json /path/to/interactions.json \
  --video /path/to/material.mp4 \
  --output-dir /path/to/frames
```

过渡期仍可用旧合并式 `--config` 入口，但新请求必须优先使用独立的 `--query-spec` 和 `--highlight-spec`。

## 产物与恢复

运行目录保存解析后的配置、`source-data/`、候选素材、脱敏查询回执和 `run-receipt.json`；RPA 运行另保存诊断 CSV、records、JPEG 和 `top_materials_rpa_ready.json`。配置、查询范围或高光帧规则变化时必须使用新的输出目录。

`query` 成功不代表 RPA 已完成。RPA 仅在目标数量的素材均成功处理后才完整；失败素材会按保存的排名顺序由后续候选补位，仍不会超过 `targetTopN`。临时视频在高光帧验证后删除，长期工件不保存凭证。`verify-run` 完全离线，可校验已完成目录的相对路径、哈希、JSON 与 JPEG 工件，不读取凭证也不调用外部服务。
