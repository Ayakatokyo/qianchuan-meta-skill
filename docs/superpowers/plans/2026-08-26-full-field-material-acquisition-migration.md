# Full-Field Material Acquisition Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect and retain the verified 8-dimension/102-metric material aggregate dataset while keeping QuerySpec restricted to the existing 18-field business registry and preserving the RPA input contract.

**Architecture:** The report client owns the static raw request contract, response-key validation, and sanitized manifest. Candidate selection consumes only registry fields but records incomplete source data as structured exclusions. The CLI keeps selected `materialId` and resolved period as the RPA boundary.

**Tech Stack:** Python 3, `requests`, standard-library `json`/`csv`/`decimal`, `unittest`.

---

## File Map

- `scripts/qianchuan_report_client.py`: replace 5/8 request fields with the 8/102 static contract; validate returned metric keys; persist the field manifest.
- `tests/test_qianchuan_report_client.py`: contract, response validation, files/payload manifest and redaction coverage.
- `scripts/material_query.py`: tolerate incomplete source rows and ensure missing numeric facts are never accumulated as zero.
- `tests/test_material_query.py`: source-ID, active dimension, incomplete-metric and receipt tests.
- `tests/test_run_meta_skill.py`: receipt propagation and material/date RPA handoff regression.
- `SKILL.md`: public documentation of the full-field transport versus controlled QuerySpec boundary.

### Task 1: Report Contract and Field Manifest

**Files:**

- Modify: `scripts/qianchuan_report_client.py:14-79,237-276`
- Modify: `tests/test_qianchuan_report_client.py:16-200`

- [ ] **Step 1: Write tests that define the 8/102 contract.**

Make the test fixture create all requested dimensions and metrics, with optional omitted keys:

```python
def material_record(*, omit_metrics=(), omit_dimensions=()):
    return {
        "dimensions": {
            field: {"Value": field, "ValueStr": field}
            for field in client.DEFAULT_DIMENSIONS if field not in omit_dimensions
        },
        "metrics": {
            field: {"Value": 1, "ValueStr": "1"}
            for field in client.DEFAULT_METRICS if field not in omit_metrics
        },
    }
```

Add a test requiring the exact ordered dimensions below, 102 unique metrics, and all four extensions. Require that the dimensions exclude `roi2_material_advice`, `roi2_material_advice_reason`, `stat_time_day`, and `range_stat_time_hour`.

```python
self.assertEqual(client.DEFAULT_DIMENSIONS, [
    "ecp_app_id", "roi2_material_type_v3", "roi2_material_video_play_info",
    "roi2_material_video_name", "roi2_material_video_type", "material_create_time_v2",
    "material_id", "anchor_id",
])
self.assertEqual(len(client.DEFAULT_METRICS), 102)
self.assertEqual(len(client.DEFAULT_METRICS), len(set(client.DEFAULT_METRICS)))
self.assertTrue({
    "stat_cost_for_overall_roi2", "total_prepay_and_pay_settle_overall_roi2_1h",
    "total_cost_per_pay_order_settle_for_overall_roi2_1h", "total_prepay_and_pay_settle_roi2_1h",
}.issubset(client.DEFAULT_METRICS))
```

Add a payload test omitting `video_play_finish_rate_for_roi2_v2`, expecting `ReportClientError` that names the key and no `field-manifest.json`. Extend successful payload and files tests to require a `field-manifest.json` with 8 dimensions, 102 metrics, record count, and no authorization/session/signed URL values.

- [ ] **Step 2: Run the new tests to verify current failure.**

```bash
python -m unittest tests.test_qianchuan_report_client.QianchuanReportClientTests.test_default_contract_is_an_aggregate_eight_dimension_102_metric_request -v
python -m unittest tests.test_qianchuan_report_client.QianchuanReportClientTests.test_rejects_nonempty_record_missing_a_requested_metric_key -v
```

Expected: failure because the current client has 5 dimensions, 8 metrics, and no response-key validation or manifest.

- [ ] **Step 3: Implement the static aggregate contract.**

Rename the request constants to `DEFAULT_DIMENSIONS` and `DEFAULT_METRICS`. Copy their exact ordered 8/102 values from `qianchuan-video-peak-frames/scripts/qianchuan_report_client.py` at commit `1c821cc`; do not call dynamic config. Keep the existing `order_by` argument used by `run_meta_skill._connector_order_by`.

Add pre-request validation:

```python
def _validate_field_contract(order_by):
    if len(DEFAULT_DIMENSIONS) > 10:
        raise ReportClientError("material aggregate query exceeds 10 dimension limit")
    if len(DEFAULT_DIMENSIONS) != len(set(DEFAULT_DIMENSIONS)):
        raise ReportClientError("material aggregate dimensions contain duplicates")
    if len(DEFAULT_METRICS) != len(set(DEFAULT_METRICS)):
        raise ReportClientError("material aggregate metrics contain duplicates")
    if {"stat_time_day", "range_stat_time_hour"}.intersection(DEFAULT_DIMENSIONS):
        raise ReportClientError("material aggregate query includes temporal dimensions")
    if not order_by or order_by[0].get("field") not in DEFAULT_METRICS:
        raise ReportClientError("material aggregate sort field is not a metric")
```

After payload/files parsing produces a nonempty record list, validate metric key presence and only then write the manifest:

```python
def _validate_record_fields(records):
    missing = set()
    for record in records:
        metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
        missing.update(field for field in DEFAULT_METRICS if field not in metrics)
    if missing:
        raise ReportClientError("material report missing expected metrics: " + ", ".join(sorted(missing)))

def _write_field_manifest(source_dir, advertiser_id, start_date, end_date, order_by, records):
    params = _business_params(advertiser_id, start_date, end_date, order_by=order_by)
    _write_json(Path(source_dir) / "field-manifest.json", {
        "dataTopic": params["dataTopic"], "dimensions": params["dimensions"],
        "metrics": params["metrics"], "filters": params["filters"], "orderBy": params["orderBy"],
        "startTime": params["startTime"], "endTime": params["endTime"], "recordCount": len(records),
    })
```

Validate keys only: `0`, `""`, and `null` are values, not missing keys. Do not validate dimensions in the client because a real response can have incomplete metadata.

- [ ] **Step 4: Verify and commit report acquisition.**

```bash
python -m unittest tests.test_qianchuan_report_client -v
git add scripts/qianchuan_report_client.py tests/test_qianchuan_report_client.py
git commit -m "feat: acquire complete material aggregate fields"
```

Expected: all report-client tests pass.

### Task 2: Candidate Data-Quality Tolerance

**Files:**

- Modify: `scripts/material_query.py:169-259`
- Modify: `tests/test_material_query.py:13-124`

- [ ] **Step 1: Write failing material-query tests.**

Add the following cases:

```python
def test_skips_missing_material_id_without_aborting_other_candidates(self):
    broken = origin_row("m-broken", "缺失ID", "直播素材", 99, 99)
    del broken["dimensions"]["material_id"]
    result = execute_material_query([broken, origin_row("m-1", "可用素材", "直播素材", 10, 20)], self.query, self.registry)
    self.assertEqual([item["materialId"] for item in result["materials"]], ["m-1"])
    self.assertEqual(result["receipt"]["sourceRecordCount"], 2)
    self.assertEqual(result["receipt"]["skippedSourceRecordCount"], 1)
    self.assertIn({"sourceRowIndex": 1, "reason": "missing_material_id"}, result["receipt"]["excluded"])
```

Add `test_excludes_missing_active_dimension_with_a_data_quality_reason`: use a `material_type` filter with a record missing `roi2_material_type_v3`, then assert `reason == "unavailable_dimension_value"` and `field == "material_type"`.

Add `test_does_not_sum_partial_metric_rows_as_complete_material`: two rows share an ID, one lacks `stat_cost_for_overall_roi2`; rank by `overall_cost` and assert `unavailable_sort_value`, never a partial sum.

- [ ] **Step 2: Run the three tests and confirm current failure.**

```bash
python -m unittest tests.test_material_query.MaterialQueryTests.test_skips_missing_material_id_without_aborting_other_candidates -v
python -m unittest tests.test_material_query.MaterialQueryTests.test_excludes_missing_active_dimension_with_a_data_quality_reason -v
python -m unittest tests.test_material_query.MaterialQueryTests.test_does_not_sum_partial_metric_rows_as_complete_material -v
```

Expected: missing ID is batch-fatal; missing dimensions become ordinary filter mismatches; partial metric data can be summed.

- [ ] **Step 3: Implement record-level skip and unavailable-field propagation.**

Enumerate source rows from one and replace the batch error:

```python
source_record_count = len(raw_rows)
skipped_source_record_count = 0
for source_row_index, row in enumerate(raw_rows, start=1):
    material_id = _text(_value_at(row, material_id_source))
    if not material_id:
        skipped_source_record_count += 1
        excluded.append({"sourceRowIndex": source_row_index, "reason": "missing_material_id"})
        continue
```

Before testing active dimension filters, exclude the first no-value dependency distinctly:

```python
unavailable = next((item for item in query["dimensionFilters"] if not dimensions.get(item["field"])), None)
if unavailable is not None:
    excluded.append({"materialId": material_id, "reason": "unavailable_dimension_value", "field": unavailable["field"]})
    continue
```

Change `_metrics_for_row` to return `(values, unavailable_ids)`. `None` and empty strings enter `unavailable_ids`; valid numeric values enter `values`; nonempty nonnumeric values retain the existing `MaterialQueryError`. Store a union of unavailable atomic metrics in every aggregate. Before derived formulas are evaluated, force unavailable atomic IDs to `None`, so a value missing on any source row cannot be silently replaced by another row's value.

Add these receipt keys while preserving `top_materials.json` schema:

```python
"sourceRecordCount": source_record_count,
"skippedSourceRecordCount": skipped_source_record_count,
"exclusionSummary": {
    reason: sum(1 for item in excluded if item["reason"] == reason)
    for reason in sorted({item["reason"] for item in excluded})
},
```

Keep AIGC handling as-is. Missing-ID source exclusions have `sourceRowIndex` only; material-level exclusions retain `materialId`.

- [ ] **Step 4: Verify and commit candidate-layer behavior.**

```bash
python -m unittest tests.test_material_query -v
git add scripts/material_query.py tests/test_material_query.py
git commit -m "fix: tolerate incomplete material source records"
```

Expected: all material-query tests pass.

### Task 3: CLI Receipt and RPA Handoff Regression

**Files:**

- Modify: `tests/test_run_meta_skill.py:1-360`
- Modify only if tests expose a loss: `scripts/run_meta_skill.py:102-141,300-350`

- [ ] **Step 1: Add failing or regression CLI tests.**

Patch `fetch_material_report` to return a valid row and a row missing `material_id`. Execute `query`, then assert:

```python
self.assertEqual(receipt["source"], {"type": "direct_material_report"})
self.assertEqual(receipt["sourceRecordCount"], 2)
self.assertEqual(receipt["skippedSourceRecordCount"], 1)
self.assertEqual(receipt["exclusionSummary"]["missing_material_id"], 1)
```

For `run --execute-rpa`, arrange the pipeline mock to invoke its supplied `fetch_diagnosis` callback once with `{"materialId": "m-1"}` and `{"csv": root / "csv"}`. Patch `run_isolated`; assert its input has the selected material ID and exact resolved period:

```python
self.assertEqual(invocation["materialId"], "m-1")
self.assertEqual(invocation["startDate"], "2026-08-01")
self.assertEqual(invocation["endDate"], "2026-08-07")
```

- [ ] **Step 2: Run the focused CLI tests.**

```bash
python -m unittest tests.test_run_meta_skill.MetaSkillCliTests.test_query_receipt_preserves_source_data_quality_counts -v
python -m unittest tests.test_run_meta_skill.MetaSkillCliTests.test_authorized_run_passes_selected_material_and_resolved_period_to_rpa -v
```

Expected: source-count test fails before Task 2 receipt exists. The RPA input regression may pass without production changes; that is the intended compatibility proof.

- [ ] **Step 3: Keep orchestration changes minimal.**

`_write_query_artifacts` must persist the result receipt unchanged except for existing source and fingerprint additions:

```python
result["receipt"]["source"] = {"type": "direct_material_report"}
result["receipt"]["configurationFingerprint"] = fingerprint
_write_json(output_dir / "query-receipt.json", result["receipt"])
```

Do not change `top_materials.json` date fields, candidate keys, `--execute-rpa` behavior, or RPA worker schema. If the date/material assertion fails, modify only the nested fetch callback to preserve `entry["materialId"]` and `query["period"]`.

- [ ] **Step 4: Verify and commit.**

```bash
python -m unittest tests.test_run_meta_skill -v
git add tests/test_run_meta_skill.py
git add scripts/run_meta_skill.py  # only when production code changed
git commit -m "test: preserve full-field query audit through RPA handoff"
```

Expected: all CLI tests pass.

### Task 4: Skill Documentation and Full Verification

**Files:**

- Modify: `SKILL.md:8-39,59-83`

- [ ] **Step 1: Document the two-field-contract boundary.**

Add after the QuerySpec description:

```markdown
### 原始采集字段

每次日期范围素材汇总查询固定请求 8 个非时间维度和 102 个已验证原始指标，并在 `source-data/field-manifest.json` 写入本次请求的脱敏字段清单。原始字段用于完整落盘与审计；它们不会自动成为 QuerySpec 字段。

QuerySpec 仍只能使用 `config/asset-field-registry-v1.json` 的 18 个受控业务字段：5 个素材维度、8 个原子指标、5 个派生指标。未注册的原始指标不得用于筛选或排序。
```

- [ ] **Step 2: Run final verification.**

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/*.py
python3 /Users/shuzida/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
git diff --check
```

Expected: all tests pass, Python compiles, skill validation passes, and no whitespace error is reported.

- [ ] **Step 3: Run an internal acceptance query only when credentials are intentionally available.**

```bash
python scripts/run_meta_skill.py query \
  --query-spec /absolute/path/query-spec.json \
  --highlight-spec /absolute/path/highlight-spec.json \
  --output-dir /absolute/path/new-meta-skill-run
```

Do not pass `--execute-rpa`. Confirm `source-data/field-manifest.json` has 8 dimensions and 102 metrics, `top_materials.json` has aggregate candidates with `actualStartDate`, `actualEndDate`, `materialId`, and `query-receipt.json` contains quality counters.

- [ ] **Step 4: Commit the documentation.**

```bash
git add SKILL.md
git commit -m "docs: clarify full-field material acquisition boundary"
```

## Completion Checklist

- [ ] Raw request uses the exact 8 aggregate dimensions and 102 unique verified metrics.
- [ ] The report client validates metric keys and writes a sanitized manifest only after validation.
- [ ] QuerySpec remains restricted to the unchanged 18-field registry.
- [ ] Missing source IDs and active dependencies are auditable exclusions, never batch failures or zero-filled facts.
- [ ] Selected material ID and date range remain unchanged at the RPA handoff.
- [ ] Unit suite, compilation, skill validation, and whitespace checks have fresh passing evidence.
