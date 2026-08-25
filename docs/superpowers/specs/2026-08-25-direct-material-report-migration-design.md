# 直接素材报表取数迁移设计

## 目标

以 `codex/migrate-direct-material-data` 在 `a02575e2f8459dafbab2bdd7b74a530bd5886285` 之后的报表连接器取数方式，替代元技能现有的资产表发现、SQL 和分页查询。元技能继续只完成阶段一、二：候选素材查询与高光帧策略配置；查询完成后不得发起 RPA、视频下载、抽帧、AI 分析或报告生成。

## 输入与边界

`query.shopId` 成为必填字符串，和 `query.advertiserId` 并列。两者是不同 ID 域：`shopId` 仅供报表连接器授权调用，`advertiserId` 仅供报表业务参数筛选；不得互相替代或转换。

运行环境要求 `ENV_BACKEND_HOST`、`YUCE_AUTHORIZATION`，以及 `YUCE_SESSION_ID` 或 `YCSESSIONID`。凭证只在进程环境中读取，不进入配置、日志、回执或工件。

## 取数适配层

新增独立的 `qianchuan_report_client.py`，实现与参考提交一致的连接器契约：

- 请求 `/adg/v1/agent/invoke`，使用 `OCEANENGINE_UNI_PROMOTION_PLAN_DATA_REQUESTS_ZDY`、`OVERALL_ROI_LIVE_MATERIAL_VIDEO` 与固定的已确认维度、指标和业务过滤器。
- 支持直接记录列表、`result_type=payload`、`result_type=files` 三种网关结果。
- 文件结果下载单列 JSON CSV，并写出结构化 JSON；响应元数据与文件 URL 均脱敏。
- 仅在取得非空、对象形式的标准素材记录后返回；网关、认证、下载或解析失败时不得写出部分候选。

客户端返回的每条记录保持 `dimensions` 和 `metrics` 结构。`material_query.py` 直接读取这些对象记录，不再接收资产表 `origin_data` JSON 字符串。原有字段注册表继续作为唯一允许筛选、排序与聚合的字段来源。

## 产物与审计

`query` 运行目录只允许出现以下工件：

- `source-data/gateway-response.json`：脱敏网关响应。
- 当连接器返回文件时，原始 CSV 与对应的 `.structured.json`。
- `top_materials.json`：按受控条件筛选后的候选素材。
- `query-receipt.json`：规范化查询、候选统计和排除原因。
- `highlight-policy.json`：后续高光帧策略。

回执增加不含凭证的 `source` 摘要，标识 `direct_material_report` 和网关结果类型。不可映射的 AIGC 动态创意集合仍在聚合前排除，并以素材级记录和源记录数审计。

## 兼容与移除

配置版本保持 `schemaVersion: 1`，但缺少 `query.shopId` 的旧配置必须校验失败。删除外部元技能中的 `asset_table_client.py`，`query` 不得包含资产表名称、动态 SQL、表发现或分页逻辑。

高光帧策略、抽帧命令与六项互动指标的选择规则不变。

## 测试

先增加失败测试，覆盖：

1. 配置缺少 `shopId` 时校验失败。
2. 查询把 `shopId`、`advertiserId` 与日期传给连接器，并将对象记录交给既有筛选器。
3. payload 和 files 返回可生成候选；网关或文件失败时不发布候选文件。
4. 网关元数据、工件和错误信息不泄露会话或签名 URL。
5. AIGC 集合排除、多指标高光策略及“查询后停止”的既有回归测试持续通过。
