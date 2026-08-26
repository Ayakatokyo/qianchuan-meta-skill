from pathlib import Path
import unittest

import yaml


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"


def load_skill_document():
    text = SKILL_PATH.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---\n", 2)
    return yaml.safe_load(frontmatter), body


class SkillDocumentTests(unittest.TestCase):
    def test_pre_input_orders_scope_metrics_and_highlights(self):
        metadata, _ = load_skill_document()
        pre_input = metadata.get("metadata", {}).get("pre_input", "")

        self.assertIn("查询范围", pre_input)
        self.assertIn("指标筛选", pre_input)
        self.assertIn("高光帧规则", pre_input)
        self.assertLess(pre_input.index("查询范围"), pre_input.index("指标筛选"))
        self.assertLess(pre_input.index("指标筛选"), pre_input.index("高光帧规则"))
        self.assertIn("排序", pre_input)
        self.assertIn("Top N", pre_input)
        self.assertNotIn("素材类型", pre_input)
        self.assertNotIn("candidateTopN", pre_input)

    def test_skill_body_preserves_api_field_and_recovery_contract(self):
        _, body = load_skill_document()

        self.assertIn("## 输入", body)
        self.assertIn("## 执行", body)
        self.assertIn("## 产物与恢复", body)
        self.assertIn("API 原始字段名", body)
        self.assertIn("102 个指标", body)
        self.assertIn("metricFilters", body)
        self.assertIn("ranking.field", body)
