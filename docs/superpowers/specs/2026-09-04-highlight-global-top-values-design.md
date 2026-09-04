# 高光帧“全局高值”规则设计

## 背景

当前高光帧规则使用 `global_maximum` 表示全局最大值，并强制 `maxFrames=1`。这会把“按指标数值排序取前几帧”的需求误解成“只取一个最大值”，导致用户指定的全局最大值 2 帧无法执行。

## 目标

- 使用易懂且准确的规则名称表达“在完整互动曲线上按数值排序取前 K 帧”。
- 允许 `maxFrames` 为任意正整数，并在可用点不足时返回全部可用点。
- 保持同一秒多指标合并、封面帧开关和选帧证据结构不变。
- 不破坏已有使用 `global_maximum` 的配置。

## 规则名称与兼容策略

- 新规范名称：`global_top_values`。
- 中文展示名称：**全局高值（按指标数值排序）**。
- `global_maximum` 作为兼容输入别名接受；归一化结果、生成的策略文件和新示例统一输出 `global_top_values`。
- 两个名称不同时出现在同一指标配置中；旧名称不承担“只能一帧”的语义。

## 选帧算法

对每个启用指标：

1. 收集该指标所有合法的 `(durationSec, metricValue)` 点；同一秒出现不同值仍视为数据错误。
2. 按 `metricValue` 从高到低排序；指标值相同按 `durationSec` 从早到晚排序。
3. 取排序结果前 `maxFrames` 个不同时间点；可用点少于 K 时全部返回。
4. 将命中点写入现有 `selectionEvidence`，`selectionRank` 从 1 开始。
5. 不同指标命中相同秒数时合并为一帧；`includeZeroSecond` 仍按原规则处理。

示例：数值为 `(1s, 5), (3s, 9), (5s, 9), (7s, 2)`，K=2 时选择 3s、5s。

## 影响范围

- `scripts/highlight_policy.py`：策略名称归一化、maxFrames 校验、全局排序取 K。
- `scripts/config_specs.py`：HighlightSpec 接受新名称和旧别名，统一输出新名称。
- `SKILL.md`：规则说明、自然语言提示、示例和迁移说明。
- `config/example-config.json`、`config/query-preview-config.json`：示例改用新名称并展示多帧。
- `tests/test_highlight_policy.py`、`tests/test_config_specs.py`：新增排序、并列、少量数据、别名兼容和回归测试。

## 错误与边界

- `maxFrames` 必须是大于 0 的整数；新旧全局策略均允许任意正整数。
- 指标值必须是有限、非负数；原有校验保持不变。
- 没有互动曲线时继续记录 `unavailableMetrics`，不推断为零。
- 若最终没有任何帧，继续抛出 `no highlight frames were selected`。

## 验收标准

1. `global_top_values` K=2 能返回数值最高的两个时间点。
2. 并列值按时间升序稳定选择。
3. K 大于可用点数量时不报错且返回全部点。
4. `global_maximum` 输入仍可校验并归一化为 `global_top_values`。
5. 既有局部峰值、封面帧、同秒合并和无效配置测试继续通过。
