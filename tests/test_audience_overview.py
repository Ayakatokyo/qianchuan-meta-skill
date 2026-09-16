import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from audience_overview import AudienceOverviewError, build_audience_overview  # noqa: E402


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _fact(material_id, dimension_key, label, metric_id, metric_value, fallback_value, record_index):
    source_path = "rpa/diagnosis-records/%s.json" % material_id
    return {
        "Dimensions": {dimension_key: {"Value": label, "ValueStr": label}},
        "Metrics": {
            {
                "SHOW_COUNT": "live_show_count_for_roi2_v2",
                "TRANSACTION_AMOUNT": "total_pay_order_gmv_include_coupon_for_roi2",
                "CLICK_COUNT": "live_watch_count_for_roi2_v2",
            }[metric_id]: {"Value": fallback_value},
        },
        "audienceValue": {"Value": label, "ValueStr": label},
        "dimensionKey": dimension_key,
        "dimensionName": {
            "user_group_label_name": "八大人群",
            "gender": "性别",
            "age": "年龄",
            "province_name": "省份",
            "city_name": "城市",
        }[dimension_key],
        "displayMetric": metric_id,
        "displayMetricName": {
            "SHOW_COUNT": "整体展现次数",
            "TRANSACTION_AMOUNT": "整体成交金额",
            "CLICK_COUNT": "整体点击次数",
        }[metric_id],
        "materialId": material_id,
        "metricValue": metric_value,
        "sourceRef": {
            "path": source_path,
            "recordIndexes": [record_index],
        },
    }


def _make_run(root):
    run_dir = root / "run-test"
    records = []
    record_index = 0
    for material_id, amount in (("m-1", 10.5), ("m-2", 20.0)):
        records.extend([
            _fact(material_id, "user_group_label_name", "精致妈妈", "TRANSACTION_AMOUNT", "", amount, record_index),
            _fact(material_id, "gender", "女", "CLICK_COUNT", "", 6 if material_id == "m-1" else 6, record_index + 1),
            _fact(material_id, "age", "24-30", "SHOW_COUNT", 40 if material_id == "m-1" else 2, 0, record_index + 2),
            _fact(material_id, "province_name", "浙江", "SHOW_COUNT", 4, 0, record_index + 3),
            _fact(material_id, "city_name", "杭州", "SHOW_COUNT", 3, 0, record_index + 4),
            _fact(material_id, "user_group_label_name", "Z世代", "CLICK_COUNT", 2, 0, record_index + 5),
        ])
        record_index += 6

    diagnosis_dir = run_dir / "rpa" / "diagnosis-records"
    diagnosis_dir.mkdir(parents=True, exist_ok=True)
    source_refs = []
    for material_id in ("m-1", "m-2"):
        source = diagnosis_dir / (material_id + ".json")
        source.write_text('{"safe":"fixture"}', encoding="utf-8")
        source_refs.append({
            "path": source.relative_to(run_dir).as_posix(),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "sizeBytes": source.stat().st_size,
        })
    for record in records:
        ref = next(item for item in source_refs if item["path"].endswith(record["materialId"] + ".json"))
        record["sourceRef"].update(ref)

    _write_json(run_dir / "fact-pack" / "audience.json", {
        "audience": records,
        "schemaVersion": 1,
        "sourceRefs": source_refs,
        "status": "available",
    })
    _write_json(run_dir / "fact-pack" / "manifest.json", {
        "schemaVersion": 1,
        "materialIds": ["m-1", "m-2"],
        "counts": {"audienceFacts": len(records), "materials": 2},
        "quality": {"categoryStatuses": {"audience": "available"}},
    })
    return run_dir


def _bucket(model, metric_id, dimension_id, label):
    dimension = model["dimensions"][dimension_id]
    if dimension_id == "geography":
        buckets = dimension["levels"]["province"]["bucketsByMetric"][metric_id]
    else:
        buckets = dimension["bucketsByMetric"][metric_id]
    return next(item for item in buckets if item["label"] == label)


class AudienceOverviewTests(unittest.TestCase):
    def test_reads_each_declared_diagnosis_source_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            diagnosis_dir = (run_dir / "rpa" / "diagnosis-records").resolve()
            original_read_bytes = Path.read_bytes
            diagnosis_reads = []

            def tracked_read_bytes(path):
                resolved = path.resolve()
                if resolved.parent == diagnosis_dir:
                    diagnosis_reads.append(resolved)
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", tracked_read_bytes):
                build_audience_overview(run_dir)

        self.assertEqual(len(diagnosis_reads), 2)
        self.assertEqual(len(set(diagnosis_reads)), 2)

    def test_build_audience_overview_aggregates_all_dimensions_and_metric_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model = build_audience_overview(_make_run(Path(temp_dir)))

        self.assertEqual(model["status"], "available")
        self.assertEqual(model["source"]["recordCount"], 12)
        self.assertEqual(model["materialScope"]["count"], 2)
        self.assertEqual(_bucket(model, "TRANSACTION_AMOUNT", "eight_groups", "精致妈妈")["value"], 30.5)
        self.assertEqual(_bucket(model, "CLICK_COUNT", "gender", "女")["value"], 12)
        self.assertAlmostEqual(_bucket(model, "TRANSACTION_AMOUNT", "eight_groups", "精致妈妈")["share"], 1.0)

    def test_build_audience_overview_rejects_hash_mismatch_or_unapproved_metric_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            source_path = run_dir / "rpa" / "diagnosis-records" / "m-1.json"
            source_path.write_text('{"changed":true}', encoding="utf-8")
            with self.assertRaisesRegex(AudienceOverviewError, "sha256|size"):
                build_audience_overview(run_dir)

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            path = run_dir / "fact-pack" / "audience.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["audience"][0]["Metrics"]["unapproved_metric"] = {"Value": 1}
            _write_json(path, payload)
            with self.assertRaisesRegex(AudienceOverviewError, "metric"):
                build_audience_overview(run_dir)

    def test_build_audience_overview_rejects_record_source_declaration_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            path = run_dir / "fact-pack" / "audience.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["audience"][0]["sourceRef"]["sizeBytes"] += 1
            _write_json(path, payload)

            with self.assertRaisesRegex(AudienceOverviewError, "disagrees"):
                build_audience_overview(run_dir)


if __name__ == "__main__":
    unittest.main()
