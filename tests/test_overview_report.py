import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from overview_report import (  # noqa: E402
    OverviewReportError,
    build_overview_data,
    render_overview_html,
    write_overview,
)


REGISTRY_PATH = ROOT / "config" / "asset-field-registry-v1.json"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _source_row(material_id, title, cost, created, duration, source=45):
    play_info = {
        "VideoDuration": duration,
        "LegoSource": source,
        "CoverImage": {"WebUrl": "https://signed.example/cover?sign=secret"},
        "PlayResource": {"UrlList": ["https://signed.example/video?token=secret"]},
    }
    return {
        "dimensions": {
            "material_id": {"Value": material_id},
            "roi2_material_video_name": {"ValueStr": title},
            "material_create_time_v2": {"ValueStr": created},
            "roi2_material_video_play_info": {"ValueStr": json.dumps(play_info)},
        },
        "metrics": {
            "stat_cost_for_overall_roi2": {"Value": str(cost) if cost is not None else ""},
            "total_prepay_and_pay_settle_roi2_1h": {"Value": "1.2"},
            "total_order_settle_amount_for_roi2_1h": {"Value": "88.5"},
            "live_show_count_for_roi2_v2": {"Value": "7"},
        },
    }


def _make_run(root, record_count=3):
    run_dir = root / "run-20260907"
    source_dir = run_dir / "source-data"
    filename = "materials.csv"
    rows = [
        _source_row("m-1", "普通素材", 20, "2026-09-01 10:00:00", 12),
        _source_row("m-2", "亚健康早餐", 80, "2026-09-02 10:00:00", 28, source=46),
        _source_row("m-3", "亚健康晚餐", None, "", 35),
    ]
    _write_json(source_dir / "result-parse-status.json", {
        "stage": "result-parse",
        "status": "completed",
        "filename": filename,
        "recordCount": record_count,
    })
    (source_dir / filename).write_text("material_id\n", encoding="utf-8")
    _write_json(source_dir / "field-manifest.json", {
        "dataTopic": "OVERALL_ROI_LIVE_MATERIAL_VIDEO",
        "dimensions": [
            "material_id",
            "roi2_material_video_name",
            "roi2_material_video_play_info",
            "material_create_time_v2",
        ],
        "metrics": [
            "stat_cost_for_overall_roi2",
            "total_prepay_and_pay_settle_roi2_1h",
            "total_order_settle_amount_for_roi2_1h",
            "live_show_count_for_roi2_v2",
        ],
        "startTime": "2026-09-01 00:00:00",
        "endTime": "2026-09-07 23:59:59",
        "recordCount": 3,
        "orderBy": [{"field": "stat_cost_for_overall_roi2", "type": 2}],
    })
    _write_json(run_dir / "query-receipt.json", {
        "schemaVersion": 1,
        "registryVersion": "2026-09-07-qianchuan-export-aligned",
        "query": {
            "period": {"startDate": "2026-09-01", "endDate": "2026-09-07"},
            "shopId": "shop-secret",
            "advertiserId": "advertiser-secret",
            "dimensionFilters": [
                {"field": "material_id", "operator": "eq", "value": "m-1"},
                {"field": "anchor_id", "operator": "eq", "value": "anchor-secret"},
            ],
            "metricFilters": [],
            "sort": {"field": "stat_cost_for_overall_roi2", "direction": "desc"},
        },
        "sort": {"field": "stat_cost_for_overall_roi2", "direction": "desc"},
        "sourceRecordCount": 3,
        "candidateCount": 3,
        "passedCount": 3,
        "returnedCount": 3,
    })
    _write_json(source_dir / (filename + ".structured.json"), rows)
    return run_dir


def _field(model, field_id):
    return next(item for item in model["fields"] if item["id"] == field_id)


class OverviewProjectionTests(unittest.TestCase):
    def test_build_overview_projects_all_source_rows_with_verified_field_labels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            model = build_overview_data(run_dir, REGISTRY_PATH)

        self.assertEqual(model["source"]["recordCount"], 3)
        self.assertEqual(len(model["rows"]), 3)
        self.assertNotIn("material_id", [field["id"] for field in model["fields"]])
        self.assertEqual(_field(model, "live_show_count_for_roi2_v2")["label"], "整体展现次数")
        self.assertEqual(model["rows"][1]["materialId"], "m-2")
        self.assertEqual(model["rows"][1]["values"]["material_duration_sec"], 28)
        self.assertEqual(model["rows"][1]["values"]["material_source"], 46)
        self.assertEqual(
            model["defaultFieldIds"],
            [
                "roi2_material_video_name",
                "material_duration_sec",
                "material_create_time_v2",
                "material_source",
                "stat_cost_for_overall_roi2",
                "total_prepay_and_pay_settle_roi2_1h",
                "total_order_settle_amount_for_roi2_1h",
            ],
        )

        serialized = json.dumps(model, ensure_ascii=False)
        self.assertNotIn("shop-secret", serialized)
        self.assertNotIn("advertiser-secret", serialized)
        self.assertNotIn("anchor-secret", serialized)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("roi2_material_video_play_info\":", serialized)

    def test_build_overview_rejects_parse_status_count_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir), record_count=2)
            with self.assertRaisesRegex(OverviewReportError, "recordCount"):
                build_overview_data(run_dir, REGISTRY_PATH)

    def test_build_overview_rejects_source_path_escape_and_missing_material_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            status_path = run_dir / "source-data" / "result-parse-status.json"
            _write_json(status_path, {
                "filename": "../outside.csv",
                "recordCount": 3,
            })
            with self.assertRaisesRegex(OverviewReportError, "inside|path"):
                build_overview_data(run_dir, REGISTRY_PATH)

            _make_run(Path(temp_dir), record_count=3)
            source = run_dir / "source-data" / "materials.csv.structured.json"
            rows = json.loads(source.read_text(encoding="utf-8"))
            del rows[0]["dimensions"]["material_id"]
            _write_json(source, rows)
            with self.assertRaisesRegex(OverviewReportError, "material_id"):
                build_overview_data(run_dir, REGISTRY_PATH)


class OverviewHtmlTests(unittest.TestCase):
    def test_rendered_html_has_title_id_search_field_drawer_and_ten_row_pagination(self):
        model = {
            "schemaVersion": 1,
            "kind": "qianchuan_material_leaderboard_overview",
            "runId": "run-test",
            "source": {"path": "source-data/materials.csv.structured.json", "sha256": "a" * 64, "recordCount": 1},
            "queryContext": {"period": {}, "filters": {}, "ranking": {}},
            "fields": [{"id": "material_id", "label": "素材ID", "kind": "dimension", "group": "素材基础信息", "sortable": False}],
            "rows": [{"materialId": "m-1", "values": {"material_id": "m-1"}}],
        }
        html = render_overview_html(model)

        self.assertIn("输入素材标题或 ID", html)
        self.assertIn("pageSize = 10", html)
        self.assertIn("normalize(needle)", html)
        self.assertIn("field-management", html)
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn('id="field-groups"', html)
        self.assertIn('id="field-options"', html)
        self.assertIn('id="selected-fields"', html)
        self.assertIn('data-field-action="confirm"', html)
        self.assertIn('data-field-action="cancel"', html)
        self.assertIn('id="field-button" type="button">自定义指标</button>', html)
        self.assertIn('id="field-management-title">自定义指标</h2>', html)
        self.assertNotIn('class="drawer"', html)

    def test_rendered_html_moves_id_under_title_and_keeps_title_column_sticky(self):
        model = {
            "schemaVersion": 1,
            "kind": "qianchuan_material_leaderboard_overview",
            "runId": "run-test",
            "source": {"path": "source-data/materials.csv.structured.json", "sha256": "a" * 64, "recordCount": 1},
            "queryContext": {"period": {}, "filters": {}, "ranking": {}},
            "defaultFieldIds": ["roi2_material_video_name"],
            "fields": [
                {"id": "roi2_material_video_name", "label": "素材名称", "kind": "dimension", "group": "素材基础信息", "sortable": False},
                {"id": "material_id", "label": "素材ID", "kind": "dimension", "group": "素材基础信息", "sortable": False},
            ],
            "rows": [{"materialId": "m-1", "values": {"roi2_material_video_name": "标题", "material_id": "m-1"}}],
        }
        html = render_overview_html(model)

        self.assertIn("素材ID：", html)
        self.assertIn("material-name-column", html)
        self.assertIn("position: sticky", html)
        self.assertIn("z-index: 1", html)
        self.assertIn("grid-template-columns", html)
        self.assertIn('const presentationFields = model.fields.filter(field => field.id !== "material_id");', html)
        self.assertIn("const orderedFieldIds = orderedPresentationFields.map(field => field.id);", html)
        self.assertIn("function orderedVisibleFieldIds(fieldIds)", html)
        self.assertIn("state.visibleFieldIds = orderedVisibleFieldIds(migrated);", html)
        self.assertIn("state.draftFieldIds = orderedVisibleFieldIds(state.visibleFieldIds);", html)
        self.assertIn("state.visibleFieldIds = orderedVisibleFieldIds(state.draftFieldIds || state.visibleFieldIds);", html)
        self.assertIn("const tableFieldIds = orderedVisibleFieldIds(state.visibleFieldIds);", html)
        self.assertIn('const fieldGroupOrder = ["素材基础信息", "整体指标", "净成交指标", "周期结算", "追投调控", "视频互动"];', html)
        self.assertIn("const fieldGroupNames = fieldGroupOrder.filter(group => presentationFields.some(field => field.group === group));", html)

    def test_rendered_html_does_not_request_network_or_render_analysis_copy(self):
        model = {
            "schemaVersion": 1,
            "kind": "qianchuan_material_leaderboard_overview",
            "runId": "run-test",
            "source": {"path": "source-data/materials.csv.structured.json", "sha256": "a" * 64, "recordCount": 0},
            "queryContext": {"period": {}, "filters": {}, "ranking": {}},
            "fields": [],
            "rows": [],
        }
        html = render_overview_html(model)

        self.assertNotIn("fetch(", html)
        self.assertNotIn("file://", html)
        self.assertNotIn("AI 分析", html)
        self.assertNotIn("行动建议", html)


class OverviewWriteTests(unittest.TestCase):
    def test_write_overview_writes_two_outputs_with_source_hash_and_no_partial_files_on_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = _make_run(root)
            output_dir = run_dir / "overview"
            receipt = write_overview(run_dir, output_dir, REGISTRY_PATH)
            source_path = run_dir / "source-data" / "materials.csv.structured.json"
            expected_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(receipt["recordCount"], 3)
            self.assertEqual(receipt["sourceSha256"], expected_hash)
            self.assertEqual(receipt["outputPaths"], ["overview/overview-data.json", "overview/index.html"])
            self.assertTrue((output_dir / "overview-data.json").is_file())
            self.assertTrue((output_dir / "index.html").is_file())

            broken_run = _make_run(root / "broken")
            broken_output = broken_run / "overview"
            broken_output.mkdir()
            (broken_output / "marker.txt").write_text("keep", encoding="utf-8")
            _write_json(broken_run / "source-data" / "result-parse-status.json", {
                "filename": "materials.csv",
                "recordCount": 2,
            })
            with self.assertRaises(OverviewReportError):
                write_overview(broken_run, broken_output, REGISTRY_PATH)
            self.assertFalse((broken_output / "overview-data.json").exists())
            self.assertFalse((broken_output / "index.html").exists())
            self.assertEqual((broken_output / "marker.txt").read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
