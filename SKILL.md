---
name: qianchuan-meta-skill
description: 将千川素材分析的自然语言意图转为 QuerySpec 与 HighlightSpec，再由直接报表连接器和受控 RPA 流程确定性筛选候选素材、获取诊断数据并选择本地高光帧；不执行 AI 商业分析、报告生成或用户专属技能生成。
---

# 千川素材分析元技能

当前能力覆盖受控素材查询、RPA 诊断、视频临时下载和高光帧策略配置。它采用“模型理解需求，脚本确定性执行”的分层：模型不得直接调用接口或自由生成字段，只能将意图转化为下述两份 JSON；当请求所需信息完整时，CLI 可直接严格校验、取数、筛选、RPA 执行和审计落盘。

## 自然语言到 Spec

面对用户的自然语言需求，先提取以下信息并生成两份 JSON：

- `QuerySpec`：店铺/广告主作用域、日期范围、素材维度筛选、投放指标筛选、排序指标和升降序、目标 Top N。
- `HighlightSpec`：是否保留第 0 秒封面帧，以及六项互动指标各自启用/禁用、选帧策略和帧数。

缺少店铺、广告主、日期、排序、Top N 或任一互动指标的规则时，必须向用户追问，或将未使用指标显式写为 `enabled: false`。信息完整时直接执行查询；仅当用户明确要求预览、草稿或人工确认时，才在执行前回显 Spec 等待确认。

`shopId` 与 `advertiserId` 都必填且属于不同 ID 域：前者只用于报表连接器授权，后者只用于报表业务筛选。字段和操作符只能来自 `config/asset-field-registry-v1.json`；维度和指标条件按 `AND` 处理。

### QuerySpec

```json
{
  "schemaVersion": 1,
  "source": "qianchuan_direct_material_report",
  "scope": {"shopId": "授权店铺 ID", "advertiserId": "广告主 ID"},
  "period": {"startDate": "YYYY-MM-DD", "endDate": "YYYY-MM-DD"},
  "filters": {
    "dimensionFilters": [{"field": "material_type", "operator": "eq", "value": "直播素材"}],
    "metricFilters": [{"field": "net_settlement_roi", "operator": "gte", "value": 1.2}]
  },
  "ranking": {"field": "overall_cost", "direction": "desc"},
  "collection": {"candidateTopN": 27, "targetTopN": 9}
}
```

排序字段必须是注册表中 `sortable` 的原子或派生指标；方向只能是 `asc` 或 `desc`。已支持的素材维度是 `material_id`、`material_name`、`material_type`、`video_type`、`anchor_id`；支持的投放指标及其派生指标以注册表为准。

### HighlightSpec

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
  --output-dir /path/to/new-run
```

`query` 只写入候选查询工件，不触发 RPA、视频下载、抽帧、AI 分析或报告生成。`run` 在候选工件成功落盘后自动解析唯一 RPA 授权账号，按候选排名执行受控波次、失败补位和恢复，临时下载视频并按 `HighlightSpec` 抽帧；长期工件只保留脱敏回执、诊断 CSV、records、JPEG 和 `top_materials_rpa_ready.json`。

## 高光帧策略

独立抽帧仅在已有本地视频与互动曲线时执行，不会发起报表查询；`run` 内部会自动准备这些输入：

```bash
python scripts/run_meta_skill.py select-frames \
  --highlight-spec /path/to/highlight-spec.json \
  --records-json /path/to/interactions.json \
  --video /path/to/material.mp4 \
  --output-dir /path/to/frames
```

过渡期仍可用旧合并式 `--config` 入口，但新请求必须优先使用独立的 `--query-spec` 和 `--highlight-spec`。

阶段三的 AI 商业分析 Prompt 标准化、报告生成和阶段四的独立技能生成尚未实现。
