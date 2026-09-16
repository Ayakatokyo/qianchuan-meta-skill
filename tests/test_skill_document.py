from pathlib import Path
import unittest

import yaml


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
METADATA_PATH = Path(__file__).resolve().parents[1] / "metadata.yaml"


def load_skill_document():
    text = SKILL_PATH.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---\n", 2)
    return yaml.safe_load(frontmatter), body


def load_skill_form():
    return yaml.safe_load(METADATA_PATH.read_text(encoding="utf-8"))["skill_form"]


class SkillDocumentTests(unittest.TestCase):
    def test_pre_input_documents_conditional_highlight_rules_and_metrics(self):
        metadata, _ = load_skill_document()
        pre_input = metadata.get("metadata", {}).get("pre_input", "")

        self.assertTrue(pre_input.startswith("请采集千川素材及相关诊断数据"))
        self.assertGreaterEqual(pre_input.count("\n"), 12)
        self.assertNotIn("<p>", pre_input)
        self.assertNotIn("data-pre-input-blank", pre_input)
        self.assertNotIn("____", pre_input)
        self.assertIn("__field:shop_id__", pre_input)
        self.assertIn("__field:advertiser_id__", pre_input)
        self.assertIn("时间范围", pre_input)
        self.assertIn("素材筛选与排序", pre_input)
        self.assertIn("筛选“整体消耗”>=10000，再按“净成交ROI”降序", pre_input)
        self.assertIn("__field:detail_top_n__", pre_input)
        self.assertIn("是否同时生成高光帧图片", pre_input)
        self.assertIn("__field:highlight_mode__", pre_input)
        self.assertIn("封面是否保留", pre_input)
        for metric in (
            "整体点击次数", "整体流失数", "整体点赞次数", "整体评论次数", "整体转发次数", "整体新增粉丝数",
        ):
            self.assertIn(metric, pre_input)
        self.assertNotIn("局部峰值/最高值", pre_input)
        self.assertIn("帧数", pre_input)
        self.assertLess(pre_input.index("授权店铺 ID"), pre_input.index("素材筛选与排序"))
        self.assertLess(pre_input.index("素材筛选与排序"), pre_input.index("是否同时生成高光帧图片"))
        self.assertNotIn("candidateTopN", pre_input)

    def test_pre_input_references_every_typed_form_field_once(self):
        metadata, _ = load_skill_document()
        pre_input = metadata["metadata"]["pre_input"]
        form = load_skill_form()
        keys = [field["key"] for field in form]
        references = __import__("re").findall(r"__field:([a-z0-9_]+)__", pre_input)

        self.assertEqual(len(keys), 25)
        self.assertEqual(set(references), set(keys))
        self.assertEqual(len(references), len(keys))

    def test_skill_body_preserves_api_field_and_recovery_contract(self):
        _, body = load_skill_document()

        self.assertIn("## 输入", body)
        self.assertIn("## 执行", body)
        self.assertIn("## 产物与恢复", body)
        self.assertIn("API 原始字段名", body)
        self.assertIn("102 个指标", body)
        self.assertIn("metricFilters", body)
        self.assertIn("ranking.field", body)
        self.assertIn("未提供排序条件时按连接器返回的原始顺序输出", body)

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

        self.assertIn("阶段一素材总览报告", body)
        self.assertIn("render-overview", body)
        self.assertIn("overview/overview-data.json", body)
        self.assertIn("overview/index.html", body)
        self.assertIn("不重新查询、不重跑 RPA、不调用模型", body)
        self.assertIn("awaiting_analysis_input", body)
        self.assertIn("factPackManifestSha256", body)
        self.assertIn("materialScope", body)
        self.assertIn("continue-analysis", body)
        self.assertIn("不自动生成商业结论", body)

    def test_skill_body_specifies_model_handoff_contract_and_pause_resume_protocol(self):
        _, body = load_skill_document()

        self.assertIn("阶段一成功后必须停止", body)
        self.assertIn("用户继续输入", body)
        self.assertIn("AnalysisSpec", body)
        self.assertIn("requestText", body)
        self.assertIn("modelConstraints", body)
        self.assertIn("doNotUseExternalData", body)
        self.assertIn("doNotFillMissingData", body)
        self.assertIn("resultSchema", body)
        self.assertIn("receive-analysis-result", body)
        self.assertIn("awaiting_model", body)
        self.assertIn("不得伪造", body)

    def test_skill_body_requires_natural_language_to_fact_retrieval_only(self):
        _, body = load_skill_document()
        self.assertIn("FactRetrievalSpec", body)
        self.assertIn("只读取已落盘事实包", body)
        self.assertIn("不重新查询", body)
        self.assertIn("retrievalTrace", body)

    def test_skill_body_documents_sectioned_business_report_contract(self):
        _, body = load_skill_document()

        self.assertIn("contractVersion=2", body)
        self.assertIn("section", body)
        self.assertIn("priority", body)
        self.assertIn("businessImplication", body)
        self.assertIn("附录 A：证据索引", body)
        self.assertIn("附录 B：结论审计", body)
        self.assertIn("历史结果缺少 section/priority", body)
