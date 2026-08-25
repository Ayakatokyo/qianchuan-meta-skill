# Execution Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Qianchuan RPA runs portable and offline-verifiable, require explicit RPA authorization, and persist phase-level lifecycle evidence.

**Architecture:** The manifest retains absolute paths for existing resume behavior, while ready payloads and outward receipts contain only portable relative artifact metadata. A focused verifier validates completed runs offline, and the CLI owns RPA authorization plus lifecycle stage transitions.

**Tech Stack:** Python 3.13, standard library, unittest, existing adapters.

---

### Task 1: Portable RPA Artifact Metadata

**Files:**

- Modify: `scripts/rpa_pipeline.py:76-307`
- Modify: `tests/test_rpa_pipeline.py`

- [ ] **Step 1: Write a failing portable-metadata test**

Add a successful-pipeline test asserting ready materials contain `diagnosisCsvArtifact`, `diagnosisRecordsArtifact`, `highlightSummaryArtifact`, and each frame's `artifact`. Every artifact must use this contract:

```python
artifact = {"artifactPath": "rpa/diagnosis-csv/123.csv", "sha256": "a" * 64, "sizeBytes": 1}
self.assertFalse(Path(artifact["artifactPath"]).is_absolute())
self.assertNotIn("..", Path(artifact["artifactPath"]).parts)
self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
self.assertGreater(artifact["sizeBytes"], 0)
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_rpa_pipeline.RpaPipelineTests.test_ready_artifacts_use_relative_paths_and_integrity_metadata -v`

Expected: FAIL because the public portable fields do not exist.

- [ ] **Step 3: Implement minimal metadata helpers**

In `scripts/rpa_pipeline.py`, add a helper that verifies a regular file is inside the run root and returns:

```python
return {"artifactPath": path.relative_to(run_root).as_posix(), "sha256": _sha256(path), "sizeBytes": path.stat().st_size}
```

Use it in `_retain_records`, `build_postprocessor`, `_ready_payload`, and final RPA receipts. Retain existing absolute path fields only in `batch-manifest.json`; omit them from ready payload and outward artifact references.

- [ ] **Step 4: Verify GREEN**

Run: `python -m unittest tests.test_rpa_pipeline -v`

Expected: all pipeline tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/rpa_pipeline.py tests/test_rpa_pipeline.py
git commit -m "feat: make RPA artifacts portable"
```

### Task 2: Offline Run Verification

**Files:**

- Create: `scripts/run_verifier.py`
- Modify: `scripts/run_meta_skill.py:173-205,208-284`
- Create: `tests/test_run_verifier.py`

- [ ] **Step 1: Write failing verifier tests**

Create a fixture run with one successful material and test a valid result then a changed frame:

```python
result = verify_completed_run(root)
self.assertTrue(result["valid"])
self.assertEqual(result["verifiedMaterialCount"], 1)
(root / "rpa" / "peak-frames" / "123" / "01_0s.jpg").write_bytes(b"changed")
with self.assertRaisesRegex(RunVerificationError, "sha256"):
    verify_completed_run(root)
```

Also assert absolute paths, invalid records JSON, and a JPEG without SOI/EOI are rejected.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_run_verifier -v`

Expected: FAIL because `run_verifier` does not exist.

- [ ] **Step 3: Implement verifier and CLI command**

Create `scripts/run_verifier.py` with `RunVerificationError` and `verify_completed_run(output_dir)`. Validate batch success, matching fingerprints/counts, normalized relative artifact paths, SHA-256/size, JSON records containing the material ID, summary success, and JPEG bytes. Add `verify-run --output-dir` to the parser; it must bypass spec loading, credentials, network, report, and RPA code.

- [ ] **Step 4: Verify GREEN**

Run: `python -m unittest tests.test_run_verifier -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_verifier.py scripts/run_meta_skill.py tests/test_run_verifier.py
git commit -m "feat: verify portable RPA runs offline"
```

### Task 3: Explicit Authorization and Lifecycle Receipt

**Files:**

- Create: `scripts/run_receipt.py`
- Modify: `scripts/run_meta_skill.py:173-284`
- Modify: `tests/test_run_meta_skill.py`
- Modify: `SKILL.md:59-79`
- Modify: `docs/2026-08-25-handoff.md:101-126`

- [ ] **Step 1: Write failing authorization tests**

Test that `run` without `--execute-rpa` never resolves an RPA account, writes a query-only success receipt, and that the flag retains RPA execution:

```python
with patch("run_meta_skill.list_rpa_accounts", side_effect=AssertionError("unexpected")):
    exit_code = run_meta_skill.main(["run", "--query-spec", query_path, "--highlight-spec", highlight_path, "--output-dir", output_dir])
receipt = json.loads((Path(output_dir) / "run-receipt.json").read_text())
self.assertEqual(exit_code, 0)
self.assertFalse(receipt["executionAuthorized"])
self.assertEqual(receipt["stages"]["query"]["status"], "succeeded")
self.assertEqual(receipt["stages"]["rpa"]["status"], "not_requested")
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_run_meta_skill.MetaSkillCliTests.test_run_requires_execute_rpa_before_resolving_rpa_account -v`

Expected: FAIL because current `run` resolves an RPA account unconditionally.

- [ ] **Step 3: Implement receipt state and RPA gate**

Create `RunReceipt` in `scripts/run_receipt.py` with atomic persistence. It initializes query/RPA/batch stages and records fingerprint, command, authorization, epoch timestamps, duration, status, error type, and redacted message. Add `--execute-rpa`; without it, `run` only queries/reuses candidates, writes a succeeded receipt and a next-command hint. With it, retain current RPA execution and persist RPA/batch success or failure before returning.

- [ ] **Step 4: Update usage documentation**

Change `SKILL.md` and `docs/2026-08-25-handoff.md` so only `run --execute-rpa` triggers external RPA; document `verify-run` as offline and credential-free.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
python -m unittest tests.test_run_meta_skill -v
python -m unittest discover -s tests -v
python -m py_compile scripts/*.py
```

Expected: all tests pass and compilation succeeds.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_receipt.py scripts/run_meta_skill.py tests/test_run_meta_skill.py SKILL.md docs/2026-08-25-handoff.md
git commit -m "feat: require RPA execution authorization"
```

### Task 4: Portable Copied-Run End-to-End Coverage

**Files:**

- Modify: `tests/test_run_meta_skill.py`
- Modify: `tests/test_run_verifier.py`

- [ ] **Step 1: Write failing copied-run test**

Use the authorized mocked pipeline, copy output to a new temporary directory, then verify the copied run:

```python
source = self.run_authorized_mocked_pipeline()
with tempfile.TemporaryDirectory() as copied_directory:
    copied = Path(copied_directory) / "copied-run"
    shutil.copytree(source, copied)
    self.assertTrue(verify_completed_run(copied)["valid"])
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_run_meta_skill.MetaSkillCliTests.test_authorized_run_is_verifiable_after_directory_copy -v`

Expected: FAIL until every ready-payload reference is portable.

- [ ] **Step 3: Correct only fixtures or contracts needed for GREEN**

Update mocked postprocessor fixtures to write records, summary, and valid JPEG artifacts compatible with the portable contract. Do not add export packaging or scheduler changes.

- [ ] **Step 4: Final verification**

Run:

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/*.py
git diff --check
```

Expected: all tests pass, compilation succeeds, and no whitespace errors are reported.

- [ ] **Step 5: Commit**

```bash
git add tests/test_run_meta_skill.py tests/test_run_verifier.py
git commit -m "test: cover portable authorized run verification"
```

