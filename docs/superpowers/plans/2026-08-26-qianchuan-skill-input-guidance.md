# Qianchuan Skill Input Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the Qianchuan skill entry around customer-facing query scope, metric selection, and highlight-frame guidance while retaining the API-field QuerySpec contract.

**Architecture:** Keep the implementation as a documentation-only change in root `SKILL.md`. Add a focused unittest that parses the YAML frontmatter and guards the required entry groups and operational sections, so the customer prompt cannot accidentally expose material type or candidate pool controls.

**Tech Stack:** Markdown, YAML frontmatter, Python 3 `unittest`, PyYAML.

---

### Task 1: Add a skill-document contract test

**Files:**
- Create: `tests/test_skill_document.py`
- Test: `tests/test_skill_document.py`

- [ ] **Step 1: Write the failing test**

```python
class SkillDocumentTests(unittest.TestCase):
    def test_pre_input_orders_scope_metrics_and_highlights(self):
        metadata, _ = load_skill_document()
        pre_input = metadata["metadata"]["pre_input"]

        self.assertIn("查询范围", pre_input)
        self.assertIn("指标筛选", pre_input)
        self.assertIn("高光帧规则", pre_input)
        self.assertLess(pre_input.index("查询范围"), pre_input.index("指标筛选"))
        self.assertLess(pre_input.index("指标筛选"), pre_input.index("高光帧规则"))
        self.assertIn("排序", pre_input)
        self.assertIn("Top N", pre_input)
        self.assertNotIn("素材类型", pre_input)
        self.assertNotIn("candidateTopN", pre_input)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_skill_document -v`

Expected: failure because current `SKILL.md` has no `metadata.pre_input`.

- [ ] **Step 3: Add the document shape test**

```python
    def test_skill_body_preserves_api_field_and_recovery_contract(self):
        _, body = load_skill_document()

        self.assertIn("## 输入", body)
        self.assertIn("## 执行", body)
        self.assertIn("## 产物与恢复", body)
        self.assertIn("API 原始字段名", body)
        self.assertIn("102 个指标", body)
        self.assertIn("metricFilters", body)
        self.assertIn("ranking.field", body)
```

- [ ] **Step 4: Run the test again**

Run: `python -m unittest tests.test_skill_document -v`

Expected: failure because the current body lacks the required `## 输入` and `## 产物与恢复` sections.

### Task 2: Rewrite the skill entry and workflow description

**Files:**
- Modify: `SKILL.md:1-118`
- Test: `tests/test_skill_document.py`

- [ ] **Step 1: Replace the YAML frontmatter**

Set a concise trigger description, an `argument_hint`, and `metadata.pre_input`. In `pre_input`, use the ordered labels `查询范围`、`指标筛选`、`高光帧规则`; include scope IDs and date range first; include Chinese metric examples, filter thresholds, ranking direction, and target Top N second; include cover-frame and six interaction metrics third. Do not include `素材类型` or `candidateTopN` in this string.

- [ ] **Step 2: Rewrite the body into the four required sections**

Keep a concise purpose paragraph, then `## 输入`, `## 执行`, and `## 产物与恢复`. State that natural-language Chinese labels resolve through `config/asset-field-registry-v1.json`, but generated `QuerySpec.field` values are API raw field names. State that all 102 metrics are usable by `metricFilters` and `ranking.field`; preserve percentage-point behavior, the QuerySpec and HighlightSpec examples, RPA opt-in behavior, AIGC duplicate protection, and offline verification.

- [ ] **Step 3: Run the document test to verify it passes**

Run: `python -m unittest tests.test_skill_document -v`

Expected: 2 tests pass.

### Task 3: Verify the full repository contract

**Files:**
- Modify: `SKILL.md`
- Modify: `tests/test_skill_document.py`

- [ ] **Step 1: Run the full test suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass.

- [ ] **Step 2: Check document syntax and patch whitespace**

Run:

```bash
python - <<'PY'
from pathlib import Path
import yaml

text = Path("SKILL.md").read_text(encoding="utf-8")
assert text.startswith("---\\n")
frontmatter = text.split("---\\n", 2)[1]
yaml.safe_load(frontmatter)
PY
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 3: Commit the implementation**

```bash
git add SKILL.md tests/test_skill_document.py docs/superpowers/plans/2026-08-26-qianchuan-skill-input-guidance.md
git commit -m "docs: refine qianchuan skill input guidance"
```
