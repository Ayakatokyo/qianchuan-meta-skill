# 元技能全字段素材采集交接摘要

日期：2026-08-26

## 本次范围

本次仅改造元技能的“素材列表直接报表采集 -> 受控候选筛选 -> RPA 交接”链路：将素材列表采集扩展为已验证的 8 个维度、102 个指标，并修复不完整源记录导致候选/RPA 链路中断的风险。

不包括商业分析、报告生成、动态 config 探查，或将全部原始 API 字段开放给 QuerySpec。

## 已实现

### 1. 固定全字段素材汇总采集

`scripts/qianchuan_report_client.py` 使用静态字段契约，不依赖每次运行的动态 config：

- 数据主题：`OVERALL_ROI_LIVE_MATERIAL_VIDEO`
- 8 个非时间维度：
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
- 102 个已验证指标：98 个 config 已知指标，加上 4 个直接请求验证指标：
  ```text
  stat_cost_for_overall_roi2
  total_prepay_and_pay_settle_overall_roi2_1h
  total_cost_per_pay_order_settle_for_overall_roi2_1h
  total_prepay_and_pay_settle_roi2_1h
  ```

明确不请求 `roi2_material_advice`、`roi2_material_advice_reason`、`stat_time_day`、`range_stat_time_hour`。因此返回的是查询日期范围内的素材汇总数据，而不是逐日或逐小时拆分数据。

请求前会校验维度数量不超过 10、字段无重复、无时间拆分维度，且网关排序字段属于请求指标集合。

### 2. 字段完整性与审计产物

成功解析 payload 或 files/CSV 后，客户端验证每条非空记录都包含 102 个预期的指标键。键存在时，`0`、空字符串、`null` 都属于服务端返回值，不会被误判为缺字段。

校验成功后写入：

```text
source-data/field-manifest.json
```

该清单包含数据主题、实际请求的 8 个维度、102 个指标、过滤条件、网关排序、日期范围和记录数。它不包含 authorization、cookie、session 或签名下载 URL。

若任一预期指标键缺失，查询失败，不生成字段清单，也不发布候选素材产物。

### 3. 素材候选的源数据容错

`scripts/material_query.py` 不再因一条记录缺少 `material_id` 而中止整批查询：

- 缺少 `material_id`：跳过该源记录，回执记录 `missing_material_id` 和 `sourceRowIndex`。
- 活跃的维度筛选字段缺值：以 `unavailable_dimension_value` 和字段 ID 排除，区别于正常不匹配的 `dimension_filter`。
- 某素材的任一源行缺少注册表中的原子指标：该指标对整个素材汇总视为不可用，绝不以其他行值或 `0` 补齐。
- 筛选或排序依赖不可用指标：沿用 `unavailable_metric_value` 或 `unavailable_sort_value` 排除该素材。
- AIGC 动态创意集合 `material_id = -2` 的既有排除逻辑未改变。

`query-receipt.json` 现新增：

```json
{
  "sourceRecordCount": 0,
  "skippedSourceRecordCount": 0,
  "exclusionSummary": {"reason": 0}
}
```

`top_materials.json` 的既有 schema 不变。

### 4. QuerySpec 与 RPA 边界

原始采集字段和 QuerySpec 字段是两层独立契约：

```text
QuerySpec（现有 18 个受控业务字段）
  -> 直接报表连接器（8 维度 + 102 原始指标）
  -> source-data 原始记录 + field-manifest.json
  -> material_query（只消费注册表字段）
  -> top_materials.json
  -> RPA
```

`config/asset-field-registry-v1.json` 未修改，QuerySpec 仍只支持：

- 5 个维度：`material_id`、`material_name`、`material_type`、`video_type`、`anchor_id`
- 8 个原子指标：`overall_cost`、`base_cost`、`overall_order_count`、`net_settlement_order_count`、`net_settlement_amount`、`overall_payment_amount`、`refund_order_count_1h`、`refund_amount_1h`
- 5 个派生指标：`net_settlement_roi`、`overall_payment_roi`、`net_settlement_rate`、`overall_order_cost`、`net_settlement_order_cost`

RPA 入参协议未改。授权运行时仍从候选条目传入 `materialId`，并从已解析 QuerySpec 传入 `startDate`、`endDate`。已增加回归测试验证该事实。

## 涉及文件

- `scripts/qianchuan_report_client.py`
- `scripts/material_query.py`
- `tests/test_qianchuan_report_client.py`
- `tests/test_material_query.py`
- `tests/test_run_meta_skill.py`
- `SKILL.md`
- `docs/superpowers/specs/2026-08-26-full-field-material-acquisition-migration-design.md`
- `docs/superpowers/plans/2026-08-26-full-field-material-acquisition-migration.md`

## Git 状态

当前仓库：`/Users/shuzida/Desktop/FDE客户/小仙炖/qianchuan-meta-skill`

当前分支：`codex/rpa-migration`，已跟踪 `origin/codex/rpa-migration`。

本次功能相关提交：

```text
a73a509 获取完整的素材汇总字段
18ac440 兼容不完整的素材源记录
43fc4be 保留 RPA 交接中的全字段查询审计
a6ee630 澄清全字段素材获取边界
```

本次新增的设计和计划提交：

```text
3c719fd 说明全字段素材获取迁移方案
ca115fc 规划全字段素材获取迁移
```

仓库仍有两个非跟踪项，均非本次改造产生，未修改：

```text
docs/skill-test-rounds/
docs/superpowers/specs/2026-08-25-spec-driven-meta-skill-design.md
```

## 验证证据

已执行并通过：

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/*.py
python3 /Users/shuzida/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
git diff --check
```

结果：65 个单元测试通过，Python 编译通过，技能校验输出 `Skill is valid!`，无 diff whitespace 错误。

未执行真实内网查询，也未执行真实 RPA。

## 新对话建议

1. 先阅读本文件及全字段迁移设计、实施计划。
2. 若要做真实验收，准备合法的 `QuerySpec`、`HighlightSpec` 与内网环境变量，在新的输出目录执行 `query`，不要带 `--execute-rpa`：
   ```bash
   python scripts/run_meta_skill.py query \
     --query-spec /absolute/path/query-spec.json \
     --highlight-spec /absolute/path/highlight-spec.json \
     --output-dir /absolute/path/new-meta-skill-run
   ```
3. 验收重点：`source-data/field-manifest.json` 是否为 8/102，`query-receipt.json` 是否有源数据质量统计，`top_materials.json` 是否保留 `actualStartDate`、`actualEndDate`、`materialId`。
4. 除非用户明确要求，不要扩展 `asset-field-registry-v1.json` 或将 102 个原始字段直接开放给 QuerySpec；这需要另行定义中文业务语义、操作符、排序资格和派生口径。
