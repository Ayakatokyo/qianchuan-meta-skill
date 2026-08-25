---
name: qianchuan-meta-skill
description: 使用已确认资产字段筛选千川素材，并配置多互动指标高光帧规则；当前阶段只输出候选素材与规则回执，不执行 RPA、视频下载、AI 分析或报告生成。
---

# 千川素材分析元技能

当前能力只覆盖自定义素材查询与高光帧策略配置。

## 配置

配置必须包含 `query` 和 `highlightPolicy`。字段 ID 必须来自 `config/asset-field-registry-v1.json`，条件之间按 `AND` 处理。`validate` 不访问外部服务；`query` 使用 `ENV_BACKEND_HOST` 与 `YCSESSIONID` 或 `YUCE_AUTHORIZATION` 读取资产表，并在候选素材输出后停止。

```bash
python scripts/run_meta_skill.py validate --config /path/to/config.json
python scripts/run_meta_skill.py query --config /path/to/config.json --output-dir /path/to/new-run
```

`query` 只生成 `top_materials.json`、`query-receipt.json` 和 `highlight-policy.json`。不得将会话凭证写入配置或产物。

## 高光帧策略

可同时选择 `CLICK_COUNT`、`LOSE_COUNT`、`LIKE_COUNT`、`COMMENT_COUNT`、`SHARE_COUNT`、`NEW_FOLLOWER_COUNT`。每项使用 `local_peaks` 或 `global_maximum`；同一秒的多项命中必须在后续抽帧阶段合并为一张 JPEG，并保留所有命中证据。

阶段三的商业分析 Prompt 标准化和阶段四的独立技能生成尚未实现。
