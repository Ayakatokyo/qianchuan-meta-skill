# Direct Material Report Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the asset-table SQL query with the direct Qianchuan material report connector.

**Architecture:** A standalone report client owns the authenticated gateway request, response decoding, and redacted source artifacts. The query layer receives normalized object records, then applies the existing registry-constrained aggregation, filtering, ranking, and AIGC exclusion.

**Tech Stack:** Python 3.9, requests, unittest.

---

### Task 1: Add the direct report client

**Files:**
- Create: `scripts/qianchuan_report_client.py`
- Create: `tests/test_qianchuan_report_client.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Write a failing request-contract test**

```python
records = fetch_material_report(
    "shop-1", "1770096415226951", "2026-08-01", "2026-08-07",
    output_dir=root, environ=valid_env(), session=session,
)
self.assertEqual(records, [material_record()])
self.assertEqual(session.post_call["json"]["shop_id"], "shop-1")
self.assertEqual(session.post_call["json"]["business_params"]["advertiserId"], 1770096415226951)
```

- [ ] **Step 2: Run the failing test**

Run: `python -m unittest tests.test_qianchuan_report_client.QianchuanReportClientTests.test_fetches_payload_with_shop_and_advertiser_ids -v`

Expected: FAIL because `qianchuan_report_client` is absent.

- [ ] **Step 3: Implement the client**

Implement `fetch_material_report(shop_id, advertiser_id, start_date, end_date, *, output_dir, environ=None, session=None)`. Call `/adg/v1/agent/invoke` with `OCEANENGINE_UNI_PROMOTION_PLAN_DATA_REQUESTS_ZDY`, `OVERALL_ROI_LIVE_MATERIAL_VIDEO`, the confirmed dimensions and metrics, and the two fixed source filters. Validate non-empty object records. Redact credentials and signed URL query strings before writing `gateway-response.json`.

- [ ] **Step 4: Verify the client**

Run: `python -m unittest tests.test_qianchuan_report_client -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add requirements.txt scripts/qianchuan_report_client.py tests/test_qianchuan_report_client.py && git commit -m "feat: add direct material report client"`

### Task 2: Decode connector file results safely

**Files:**
- Modify: `scripts/qianchuan_report_client.py`
- Modify: `tests/test_qianchuan_report_client.py`

- [ ] **Step 1: Write a failing file-result test**

```python
records = fetch_material_report(..., output_dir=root, session=session)
self.assertEqual(records, [material_record()])
self.assertTrue((root / "report.csv.structured.json").is_file())
self.assertNotIn("signed-secret", (root / "gateway-response.json").read_text())
```

- [ ] **Step 2: Run the failing test**

Run: `python -m unittest tests.test_qianchuan_report_client.QianchuanReportClientTests.test_files_response_writes_structured_records_and_redacts_url -v`

Expected: FAIL because files responses are not decoded.

- [ ] **Step 3: Implement CSV decoding**

Accept only a `COMPLETED` files result with zero failures. Download the first file into `source-data/`, parse each single-column CSV row as a JSON object, atomically write `<filename>.structured.json`, and raise `ReportClientError` on any download, CSV, JSON, or record-shape failure.

- [ ] **Step 4: Verify the client suite**

Run: `python -m unittest tests.test_qianchuan_report_client -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add scripts/qianchuan_report_client.py tests/test_qianchuan_report_client.py && git commit -m "feat: persist direct report source artifacts"`

### Task 3: Switch query configuration and aggregation input

**Files:**
- Modify: `scripts/material_query.py`
- Modify: `scripts/run_meta_skill.py`
- Modify: `config/example-config.json`
- Modify: `config/query-preview-config.json`
- Modify: `tests/test_material_query.py`
- Modify: `tests/test_run_meta_skill.py`

- [ ] **Step 1: Write a failing configuration-and-record test**

```python
with self.assertRaisesRegex(MaterialQueryError, "shopId"):
    parse_query_config(config_without_shop_id, registry)

result = execute_material_query([json.loads(origin_row(...))], valid_query, registry)
self.assertEqual(result["materials"][0]["materialId"], "m-1")
```

- [ ] **Step 2: Run the failing test**

Run: `python -m unittest tests.test_material_query.MaterialQueryTests.test_requires_shop_id_and_accepts_direct_report_records -v`

Expected: FAIL because `shopId` is not required and query rows are strings.

- [ ] **Step 3: Implement the adapter**

Require a non-empty `query.shopId`. Make `execute_material_query()` load direct dict records. Make `query` call the report client using `shopId`, `advertiserId`, and the selected dates, with `output-dir/source-data` as its artifact directory. Keep metric aggregation, registered-field filtering, sort behavior, AIGC exclusion, and high-frame policy unchanged. Add `source.type = "direct_material_report"` to the receipt without credentials.

- [ ] **Step 4: Verify query behavior**

Run: `python -m unittest tests.test_material_query tests.test_run_meta_skill -v`

Expected: PASS, with only source and candidate artifacts written by `query`.

- [ ] **Step 5: Commit**

Run: `git add scripts/material_query.py scripts/run_meta_skill.py config/example-config.json config/query-preview-config.json tests/test_material_query.py tests/test_run_meta_skill.py && git commit -m "feat: query materials from direct report connector"`

### Task 4: Remove the asset-table path and update skill guidance

**Files:**
- Delete: `scripts/asset_table_client.py`
- Modify: `SKILL.md`
- Modify: `tests/test_run_meta_skill.py`

- [ ] **Step 1: Write a failing no-legacy-dependency test**

```python
self.assertNotIn("asset_table_client", (ROOT / "scripts" / "run_meta_skill.py").read_text())
self.assertFalse((ROOT / "scripts" / "asset_table_client.py").exists())
```

- [ ] **Step 2: Run the failing test**

Run: `python -m unittest tests.test_run_meta_skill.MetaSkillCliTests.test_query_has_no_asset_table_dependency -v`

Expected: FAIL because the legacy client exists.

- [ ] **Step 3: Delete and document**

Delete the asset-table client. In `SKILL.md`, require `query.shopId`, describe direct-report source artifacts, and state explicitly that query stops before RPA, video download, extraction, analysis, and reporting.

- [ ] **Step 4: Run full verification**

Run: `python -m unittest discover -s tests -v && python -m py_compile scripts/*.py && python3 /Users/shuzida/.codex/skills/.system/skill-creator/scripts/quick_validate.py .`

Expected: all unit tests pass, scripts compile, and the skill validator prints `Skill is valid!`.

- [ ] **Step 5: Commit and push**

Run: `git add -A && git commit -m "refactor: remove asset table material query" && git push origin main`
