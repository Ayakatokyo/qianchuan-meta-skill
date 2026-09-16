import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from script_formula_overview import (  # noqa: E402
    ScriptFormulaOverviewError,
    build_script_formula_overview,
)
from command_io import CommandFileCache  # noqa: E402


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _source_ref(run_dir, material_id, content=None):
    path = run_dir / "rpa" / "diagnosis-records" / (material_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or '{"recordType":"script_analysis"}', encoding="utf-8")
    return {
        "path": path.relative_to(run_dir).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sizeBytes": path.stat().st_size,
    }


def _script(material_id, source_ref, data, *, formula_labels=True, parse_status="parsed"):
    return {
        "materialId": material_id,
        "parseStatus": parse_status,
        "completeness": {"formulaLabels": formula_labels},
        "data": data,
        "sourceRef": source_ref,
        "text": "不得输出的脚本正文",
        "detail": {"1": {"list": ["不得输出的 detail"]}},
    }


def _primary(label, secondary):
    return {"level_1_label": {"label": label}, "level_2_labels": [{"label": item} for item in secondary]}


def _make_run(root):
    run_dir = root / "run"
    refs = []
    scripts = []
    for material_id, data in (
        ("m-1", [_primary("商品卖点", ["适用场景"]), _primary("商品卖点", ["适用场景"])]),
        ("m-2", [_primary("商品卖点", ["适用场景"]), _primary("价格优惠", ["降价/折扣"])]),
        ("m-3", []),
    ):
        ref = _source_ref(run_dir, material_id)
        refs.append(ref)
        scripts.append(_script(material_id, ref, data, formula_labels=bool(data)))
    _write_json(run_dir / "fact-pack" / "scripts.json", {
        "schemaVersion": 1, "status": "available", "scripts": scripts, "sourceRefs": refs,
    })
    _write_json(run_dir / "fact-pack" / "manifest.json", {
        "schemaVersion": 1,
        "materialIds": ["m-1", "m-2", "m-3"],
        "counts": {"materials": 3, "scriptFacts": 3},
        "quality": {"categoryStatuses": {"scripts": "available"}},
    })
    return run_dir


def _bucket(buckets, label):
    return next(item for item in buckets if item["label"] == label)


class ScriptFormulaOverviewTests(unittest.TestCase):
    def test_each_diagnostic_source_is_read_once_per_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            io_cache = CommandFileCache(run_dir)

            build_script_formula_overview(run_dir, io_cache=io_cache)

            self.assertEqual(
                [io_cache.read_count(run_dir / "rpa" / "diagnosis-records" / f"m-{index}.json") for index in range(1, 4)],
                [1, 1, 1],
            )

    def test_build_script_formula_overview_deduplicates_material_coverage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model = build_script_formula_overview(_make_run(Path(temp_dir)))

        self.assertEqual(model["status"], "available")
        self.assertEqual(model["source"]["recordCount"], 3)
        self.assertEqual(model["coverage"]["formulaMaterialCount"], 2)
        self.assertEqual(_bucket(model["primaryBuckets"], "商品卖点")["value"], 2)
        self.assertEqual(_bucket(model["secondaryBucketsByPrimary"]["商品卖点"], "适用场景")["value"], 2)
        self.assertEqual(_bucket(model["primaryBuckets"], "商品卖点")["total"], 2)
        self.assertEqual(_bucket(model["secondaryBucketsByPrimary"]["商品卖点"], "适用场景")["total"], 2)
        self.assertEqual(_bucket(model["primaryBuckets"], "价格优惠")["value"], 1)
        self.assertEqual(
            _bucket(model["secondaryBucketsByPrimary"]["价格优惠"], "降价/折扣")["total"],
            1,
        )
        self.assertEqual(model["gaps"], [{"code": "formula_labels_unavailable", "count": 1}])

    def test_projection_rejects_manifest_or_diagnostic_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            manifest_path = run_dir / "fact-pack" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["counts"]["scriptFacts"] = 2
            _write_json(manifest_path, manifest)
            with self.assertRaisesRegex(ScriptFormulaOverviewError, "scriptFacts"):
                build_script_formula_overview(run_dir)

            run_dir = _make_run(Path(temp_dir))
            diagnosis = run_dir / "rpa" / "diagnosis-records" / "m-1.json"
            diagnosis.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ScriptFormulaOverviewError, "sha256|size"):
                build_script_formula_overview(run_dir)

    def test_projection_uses_only_structured_formula_labels_and_redacts_script_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            scripts_path = run_dir / "fact-pack" / "scripts.json"
            payload = json.loads(scripts_path.read_text(encoding="utf-8"))
            payload["scripts"][0]["text"] = "标题或逐字稿不能成为公式"
            payload["scripts"][0]["detail"] = {"99": {"label": "detail 不得作为公式"}}
            _write_json(scripts_path, payload)
            model = build_script_formula_overview(run_dir)

        serialized = json.dumps(model, ensure_ascii=False)
        self.assertNotIn("标题或逐字稿", serialized)
        self.assertNotIn("detail", serialized)
        self.assertNotIn("脚本正文", serialized)
        self.assertEqual([item["label"] for item in model["primaryBuckets"]], ["商品卖点", "价格优惠"])

    def test_missing_secondary_label_is_a_gap_without_removing_primary_coverage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            scripts_path = run_dir / "fact-pack" / "scripts.json"
            payload = json.loads(scripts_path.read_text(encoding="utf-8"))
            payload["scripts"][0]["data"].append({"level_1_label": {"label": "适用人群"}, "level_2_labels": []})
            _write_json(scripts_path, payload)
            model = build_script_formula_overview(run_dir)

        self.assertEqual(_bucket(model["primaryBuckets"], "适用人群")["value"], 1)
        self.assertIn({"code": "missing_level_2_label", "count": 1}, model["gaps"])


if __name__ == "__main__":
    unittest.main()
