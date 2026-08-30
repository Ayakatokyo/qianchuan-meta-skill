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

        self.assertTrue(pre_input.startswith("请采集千川素材列表和高光帧数据"))
        self.assertIn("授权店铺 ID", pre_input)
        self.assertIn("广告主 ID", pre_input)
        self.assertIn("时间周期", pre_input)
        self.assertIn("筛选条件", pre_input)
        self.assertIn("排序指标", pre_input)
        self.assertIn("排序方向", pre_input)
        self.assertIn("需要执行详情拆解的素材数", pre_input)
        self.assertIn("高光帧筛选", pre_input)
        self.assertIn("高光指标至少选择一项", pre_input)
        self.assertIn("点击、流失、点赞、评论、分享、新增粉丝", pre_input)
        self.assertIn("波峰", pre_input)
        self.assertIn("指标大小", pre_input)
        self.assertLess(pre_input.index("授权店铺 ID"), pre_input.index("筛选条件"))
        self.assertLess(pre_input.index("筛选条件"), pre_input.index("高光帧筛选"))
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

    def test_skill_body_describes_analysis_handoff_and_completion_gate(self):
        _, body = load_skill_document()

        self.assertIn("prepare-analysis", body)
        self.assertIn("verify-analysis", body)
        self.assertIn("analysis-input.json", body)
        self.assertIn("analysis-result.json", body)
        self.assertIn("query-only", body)
        self.assertIn("不完整", body)

    def test_skill_body_describes_two_stage_analysis_handoff(self):
        _, body = load_skill_document()

        self.assertIn("awaiting_analysis_input", body)
        self.assertIn("factPackManifestSha256", body)
        self.assertIn("materialScope", body)
        self.assertIn("continue-analysis", body)
        self.assertIn("不自动生成商业结论", body)
