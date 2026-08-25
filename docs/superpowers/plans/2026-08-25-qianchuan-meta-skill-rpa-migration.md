# Qianchuan Meta-Skill RPA Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the deterministic `run` pipeline that queries candidates, runs bounded Qianchuan RPA diagnosis, extracts configured highlight frames, and persists resumable verified artifacts.

**Architecture:** Keep `query` as a pure report-collection command. Add focused RPA gateway, CSV, and pipeline modules; the pipeline owns manifests, recovery, backfill, and temporary video cleanup while the existing frame selector remains the only highlight-policy implementation.

**Tech Stack:** Python 3, standard library multiprocessing/concurrency, `requests`, FFmpeg through existing `frame_extractor`.

---

### Task 1: Candidate-Pool Contract

**Files:**
- Modify: `scripts/config_specs.py`, `scripts/run_meta_skill.py`
- Modify: `tests/test_config_specs.py`, `tests/test_run_meta_skill.py`

- [x] Add failing tests for default, equal, greater-than, and invalid `candidateTopN`.
- [x] Run the targeted tests and confirm they fail because the resolved collection omits `candidateTopN`.
- [x] Normalize `candidateTopN >= targetTopN`, preserve old configs by defaulting it to target, and persist both counts in candidate artifacts.
- [x] Re-run targeted tests and commit the green change.

### Task 2: Safe RPA Gateway and CSV Artifacts

**Files:**
- Create: `scripts/path_safety.py`, `scripts/rpa_csv_parser.py`, `scripts/rpa_adapter.py`
- Create: `tests/test_rpa_csv_parser.py`, `tests/test_rpa_adapter.py`

- [x] Add failing parser tests for `\x01` CSV, structured cells, and invalid values.
- [x] Add failing gateway tests for one-account selection, ambiguous/unavailable accounts, custom dates, size limits, and redaction.
- [x] Implement atomic downloads, material-ID safety, CSV parsing, account resolution, and RPA submission using the specified gateway contract.
- [x] Re-run the module tests and compile the green change.

### Task 3: Resumable RPA Pipeline

**Files:**
- Create: `scripts/rpa_pipeline.py`
- Create: `tests/test_rpa_pipeline.py`

- [x] Add failing tests showing ranking-order backfill after RPA and postprocess failure, manifest checkpoint recovery, and rejected fingerprint reuse.
- [x] Implement per-material state, receipt/records persistence, bounded concurrent waves, target stop conditions, and fingerprint/date validation.
- [x] Re-run pipeline tests and compile the green change.

### Task 4: Video Postprocessing and Verification

**Files:**
- Modify: `scripts/rpa_pipeline.py`
- Create: `tests/test_rpa_pipeline.py`

- [x] Add failing tests that require temporary-video deletion, `HighlightSpec`-derived frame output, and missing-frame verification failure.
- [x] Implement video download, existing frame extraction reuse, JPEG checks, `top_materials_rpa_ready.json`, and `batch-verification.json`.
- [x] Re-run pipeline tests and compile the green change.

### Task 5: CLI Integration and Skill Boundary

**Files:**
- Modify: `scripts/run_meta_skill.py`, `SKILL.md`
- Modify: `tests/test_run_meta_skill.py`

- [x] Add failing tests that prove `query` never calls RPA, `run` calls query then RPA without confirmation, partial batches exit nonzero, and resolved RPA options are non-sensitive.
- [x] Add the `run` command and controlled RPA options; reuse existing query artifacts only when the fingerprint matches.
- [x] Update the skill description to advertise automatic RPA while retaining the exclusions for AI analysis and reports.
- [x] Run the full suite, compile all scripts, inspect the diff, and complete the migration review.
