# 千川元技能执行可靠性设计

日期：2026-08-26

## 目标

在不扩展商业分析、报告生成或独立技能生成范围的前提下，完成阶段一、二的三项 P1 可靠性改造：

1. 运行工件可在解压或迁移后被定位和独立校验。
2. RPA、视频下载和抽帧必须有明确的命令级执行授权。
3. 每次运行产生可审计的阶段级生命周期回执。

## 非目标

- 不改变 QuerySpec、HighlightSpec、字段注册表或高光帧算法。
- 不改变候选排序、RPA 波次、失败补位和恢复策略。
- 不实现后处理并发调度、真实网关故障演练或商业报告。

## 方案选择

采用兼容式收紧：运行时 manifest 保留绝对路径，以维持既有断点恢复；所有面向交付或下游消费的工件新增相对 `artifactPath` 和完整性哈希。RPA 执行改为显式 opt-in，避免仅依赖 Skill 文档中的自然语言确认约定。

## CLI 契约

### `query`

`query` 的语义保持不变：只查询并写出候选与查询工件，绝不调用 RPA。

### `run`

`run` 新增布尔参数 `--execute-rpa`。

- 参数存在：执行当前完整链路，即查询或复用候选、RPA、视频临时下载、抽帧与批次校验。
- 参数缺失：查询或复用候选后停止，退出码为 0；不解析 RPA 账号、不写 RPA manifest、不发起 RPA 或下载。标准输出给出可复制的下一步命令。
- 已有同指纹候选可复用；没有候选时仍只执行查询。

该行为明确覆盖旧 `--config` 与新双 Spec 入口。`SKILL.md` 和交接文档必须与此契约一致。

## 可迁移工件契约

为每一个长期保留的 RPA 工件定义相对于运行根目录的 POSIX `artifactPath`：

- `diagnosisCsvArtifact`：`artifactPath`、`sha256`、`sizeBytes`。
- `diagnosisRecordsArtifact`：`artifactPath`、`sha256`、`sizeBytes`。
- 每个高光帧：`artifactPath`、`sha256`、`sizeBytes`，并保留已有选帧证据。
- 每个选帧摘要：作为 `highlightSummaryArtifact` 记录相同三字段。

`top_materials_rpa_ready.json` 只输出这些可迁移引用，不输出执行环境绝对路径。RPA 回执使用相同结构。运行 manifest 可以保留原有绝对路径字段用于兼容恢复，但必须同时输出新引用字段。

哈希使用 SHA-256，尺寸使用字节数。路径必须：非空、相对运行根目录、使用 POSIX 分隔符，且不能包含 `..`、绝对根、盘符或符号链接逃逸。

## `verify-run` 命令

新增离线命令：

```bash
python scripts/run_meta_skill.py verify-run --output-dir RUN
```

它不读取凭据、不访问网络、不启动 RPA。命令校验：

1. `batch-verification.json` 成功状态、目标数和配置指纹一致。
2. `top_materials_rpa_ready.json` 的每项引用在运行根目录内存在，尺寸与 SHA-256 一致。
3. records 是 JSON 列表且包含对应素材 ID；帧是有效 JPEG；摘要 JSON 状态为 `succeeded` 且与素材 ID 目录一致。
4. 不允许遗留绝对路径作为 ready 清单中的可消费引用。

成功时输出简短 JSON，包含 `valid: true`、配置指纹、已验证素材数和文件数；失败时输出明确原因并以退出码 2 结束。

## 生命周期回执

在运行根目录产生 `run-receipt.json`，以原子替换写入。字段如下：

```json
{
  "schemaVersion": 1,
  "configurationFingerprint": "...",
  "command": "query | run",
  "executionAuthorized": false,
  "status": "running | succeeded | failed",
  "startedAtEpochSec": 0,
  "finishedAtEpochSec": 0,
  "stages": {
    "query": {"status": "not_started | running | succeeded | failed", "startedAtEpochSec": 0, "finishedAtEpochSec": 0, "durationSec": 0, "error": null},
    "rpa": {"status": "not_requested | running | succeeded | failed", "startedAtEpochSec": 0, "finishedAtEpochSec": 0, "durationSec": 0, "error": null},
    "batchVerification": {"status": "not_requested | running | succeeded | failed", "startedAtEpochSec": 0, "finishedAtEpochSec": 0, "durationSec": 0, "error": null}
  }
}
```

仅记录错误类型与已脱敏错误摘要，不记录 token、cookie、会话值、下载 URL 查询参数或业务响应体。候选复用写为 query 阶段 `succeeded` 并带 `reused: true`。RPA 内部的下载与抽帧已由 manifest 的每素材状态承载，不增加一个会误导为独立后台阶段的外部状态。

## 错误与恢复

若查询失败，回执标记 query 和整体为 `failed`，不创建 RPA 工件。若执行已授权且 RPA 批次失败，回执保留 batch verification 路径与脱敏错误摘要，CLI 仍以非零退出。`verify-run` 只验证已成功批次，不作为修复或重跑命令。

## 验收与测试

按测试驱动开发新增或调整以下测试：

1. `run` 没有 `--execute-rpa` 时不调用账号解析或 RPA，但输出候选和成功回执。
2. 有 `--execute-rpa` 时保留当前完整 RPA 链路。
3. 运行回执写入查询、RPA、批次校验阶段状态、指纹与授权标志，失败时不泄露敏感信息。
4. RPA ready 清单和回执仅含相对引用及校验元数据。
5. 从新目录运行 `verify-run` 可验证真实样式工件；缺文件、篡改哈希、绝对路径、无效 JSON 或非 JPEG 都失败。
6. 现有恢复、失败补位、候选上限、高光帧和脱敏测试保持通过。

