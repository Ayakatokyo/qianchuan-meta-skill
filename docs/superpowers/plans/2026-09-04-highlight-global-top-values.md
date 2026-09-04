# 高光帧全局高值规则 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将全局最大值规则改为可按指标数值排序提取任意数量的全局高值帧。

**Architecture:** 在高光策略归一化层引入规范名 `global_top_values`，兼容读取 `global_maximum`；在选帧层复用现有时间点聚合与证据结构，仅替换全局单点选择为稳定的 Top-K 排序。文档、示例和测试同步更新。

**Tech Stack:** Python 3、unittest、JSON 配置、现有高光帧提取管线。

**Spec:** `docs/superpowers/specs/2026-09-04-highlight-global-top-values-design.md`

## Global Constraints

- `maxFrames` 必须为正整数。
- 指标值按降序选择；同值按时间升序。
- 同秒多指标只抽取一张 JPEG并保留全部证据。
- `global_maximum` 仅作为兼容输入，规范输出使用 `global_top_values`。

---

### Task 1: 高光策略核心算法

**Files:**
- Modify: `scripts/highlight_policy.py`
- Test: `tests/test_highlight_policy.py`

**Interfaces:**
- Consumes: `select_highlight_frames(records, raw_policy)` 与现有 records/policy 结构。
- Produces: 归一化 policy 中使用 `global_top_values`，并返回按规则排序的 `frames` 与 `selectionEvidence`。

- [ ] **Step 1: Write the failing tests**

```python
    def test_global_top_values_selects_top_k_by_value(self):
        records = [
            interaction("LIKE_COUNT", 1, 5),
            interaction("LIKE_COUNT", 3, 9),
            interaction("LIKE_COUNT", 5, 7),
            interaction("LIKE_COUNT", 7, 8),
        ]
        selection = select_highlight_frames(
            records,
            {"schemaVersion": 1, "includeZeroSecond": False,
             "policies": [{"metric": "LIKE_COUNT", "mode": "global_top_values", "maxFrames": 2}]},
        )
        self.assertEqual([f["durationSec"] for f in selection["frames"]], [3, 7])

    def test_global_top_values_breaks_ties_by_earliest_timestamp(self):
        selection = select_highlight_frames(
            [interaction("LIKE_COUNT", 5, 9), interaction("LIKE_COUNT", 3, 9), interaction("LIKE_COUNT", 7, 8)],
            {"schemaVersion": 1, "includeZeroSecond": False,
             "policies": [{"metric": "LIKE_COUNT", "mode": "global_top_values", "maxFrames": 2}]},
        )
        self.assertEqual([f["durationSec"] for f in selection["frames"]], [3, 5])

    def test_global_top_values_returns_all_points_when_k_is_larger(self):
        selection = select_highlight_frames(
            [interaction("LIKE_COUNT", 1, 2), interaction("LIKE_COUNT", 2, 1)],
            {"schemaVersion": 1, "includeZeroSecond": False,
             "policies": [{"metric": "LIKE_COUNT", "mode": "global_top_values", "maxFrames": 5}]},
        )
        self.assertEqual([f["durationSec"] for f in selection["frames"]], [1, 2])

    def test_global_maximum_is_accepted_as_legacy_alias(self):
        selection = select_highlight_frames(
            [interaction("LIKE_COUNT", 1, 2)],
            {"schemaVersion": 1, "includeZeroSecond": False,
             "policies": [{"metric": "LIKE_COUNT", "mode": "global_maximum", "maxFrames": 2}]},
        )
        self.assertEqual(selection["policy"]["policies"][0]["mode"], "global_top_values")

```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests/test_highlight_policy.py -v`

Expected: FAIL because `global_top_values` is unsupported and legacy `global_maximum` rejects `maxFrames=2`.

- [ ] **Step 3: Implement minimal algorithm and normalization**

Update `scripts/highlight_policy.py` as follows:

```python
_MODE_ALIASES = {"global_maximum": "global_top_values"}
_SUPPORTED_MODES = {"local_peaks", "global_top_values"}

# inside normalize_highlight_policy
mode = _MODE_ALIASES.get(mode, mode)
if mode not in _SUPPORTED_MODES:
    raise HighlightPolicyError("unsupported highlight selection mode")
if type(max_frames) is not int or max_frames <= 0:
    raise HighlightPolicyError("maxFrames is invalid for highlight selection mode")

# inside select_highlight_frames
if item["mode"] == "local_peaks":
    selected = sorted(_local_peaks(points), key=lambda point: (-point[1], point[0]))[:item["maxFrames"]]
else:
    selected = sorted(points, key=lambda point: (-point[1], point[0]))[:item["maxFrames"]]
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m unittest tests/test_highlight_policy.py -v`

Expected: all highlight policy tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/highlight_policy.py tests/test_highlight_policy.py
git commit -m "feat: support global top values highlight frames"
```

### Task 2: 配置契约与示例文档

**Files:**
- Modify: `scripts/config_specs.py`
- Modify: `SKILL.md`
- Modify: `config/example-config.json`
- Modify: `config/query-preview-config.json`
- Test: `tests/test_config_specs.py`

**Interfaces:**
- Consumes: `resolve_highlight_spec(payload)` and `highlight_spec_to_policy(spec)`.
- Produces: canonical strategy `global_top_values` with arbitrary positive `maxFrames`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_highlight_spec_accepts_global_top_values_with_multiple_frames(self):
        spec = highlight_spec()
        spec["metrics"]["SHARE_COUNT"] = {"enabled": True, "strategy": "global_top_values", "maxFrames": 2}
        resolved = resolve_highlight_spec(spec)
        self.assertEqual(resolved["metrics"]["SHARE_COUNT"]["maxFrames"], 2)
        self.assertEqual(highlight_spec_to_policy(resolved)["policies"][1]["mode"], "global_top_values")

    def test_highlight_spec_normalizes_legacy_global_maximum(self):
        spec = highlight_spec()
        spec["metrics"]["SHARE_COUNT"] = {"enabled": True, "strategy": "global_maximum", "maxFrames": 2}
        resolved = resolve_highlight_spec(spec)
        self.assertEqual(resolved["metrics"]["SHARE_COUNT"]["strategy"], "global_top_values")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests/test_config_specs.py -v`

Expected: FAIL because the config layer rejects the new strategy and enforces `maxFrames=1`.

- [ ] **Step 3: Implement config normalization and update docs/examples**

In `scripts/config_specs.py`, map `global_maximum` to `global_top_values`, allow both as input, remove the equality-to-one check, and retain positive-integer validation. Update `SKILL.md` prompt text, rule table/example, and migration note. Update both JSON examples to use `global_top_values` and a multi-frame value.

- [ ] **Step 4: Run focused tests**

Run: `python -m unittest tests/test_config_specs.py tests/test_skill_document.py -v`

Expected: all focused tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/config_specs.py SKILL.md config/example-config.json config/query-preview-config.json tests/test_config_specs.py tests/test_skill_document.py
git commit -m "docs: rename global highlight strategy and allow top k"
```

### Task 3: 全量验证与执行交接

**Files:**
- Verify: `tests/` (no source changes expected)
- Read: `docs/superpowers/specs/2026-09-04-highlight-global-top-values-design.md`
- Read: `docs/superpowers/plans/2026-09-04-highlight-global-top-values.md`

- [ ] **Step 1: Run the complete test suite**

Run: `python -m unittest discover -s tests -v`

Expected: exit code 0 with zero failures.

- [ ] **Step 2: Inspect the diff and validate representative configs**

Run: `git diff --check && python scripts/run_meta_skill.py validate --query-spec runs/run-20260825-20260830/query-spec-input.json --highlight-spec runs/run-20260825-20260830/highlight-spec-input.json` (if the existing run inputs are present).

Expected: no whitespace errors; validation either succeeds or reports only missing local run inputs.

- [ ] **Step 3: Send execution handoff to the requested Codex task**

Send a follow-up message to task `01a06b26-29ea-7742-ad4c-ca5f2f113ed1` containing the design path, plan path, canonical name, compatibility behavior, and request to execute the plan from the shared workspace.

- [ ] **Step 4: Record verification evidence**

Capture test command, exit code, and changed-file summary in the final response and in the target task's execution context.
