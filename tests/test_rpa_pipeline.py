import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rpa_pipeline import RpaPipelineError, build_postprocessor, run_rpa_pipeline  # noqa: E402


def candidate(material_id, rank):
    return {"materialId": material_id, "rank": rank, "materialName": material_id}


class RpaPipelineTests(unittest.TestCase):
    def test_backfills_in_ranking_order_when_rpa_request_fails(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fetch(entry, _paths):
                calls.append(entry["materialId"])
                if entry["materialId"] == "m-1":
                    raise RpaPipelineError("rpa_request_timeout", "rpa_request_timeout")
                csv_path = root / "input.csv"
                csv_path.write_text("data", encoding="utf-8")
                return {"records": [{"materialId": entry["materialId"]}], "csvPath": csv_path, "gatewayReceipt": {}}

            result = run_rpa_pipeline(
                root,
                {"materials": [candidate("m-1", 1), candidate("m-2", 2)], "collection": {"candidateTopN": 2, "targetTopN": 1}},
                configuration_fingerprint="fingerprint",
                period={"startDate": "2026-08-01", "endDate": "2026-08-02"},
                options={"rpaConcurrency": 1, "recoveryAttempts": 1},
                fetch_diagnosis=fetch,
                postprocess=lambda entry, _records, _paths: {"frames": []},
            )

            ready = json.loads((root / "rpa" / "top_materials_rpa_ready.json").read_text(encoding="utf-8"))

        self.assertEqual(calls, ["m-1", "m-2"])
        self.assertTrue(result["succeeded"])
        self.assertEqual([item["materialId"] for item in ready["materials"]], ["m-2"])

    def test_never_submits_or_returns_more_than_target_top_n(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fetch(entry, _paths):
                calls.append(entry["materialId"])
                return {"records": [{"materialId": entry["materialId"]}], "csvPath": self._csv(root), "gatewayReceipt": {}}
            result = run_rpa_pipeline(
                root,
                {"materials": [candidate("m-1", 1), candidate("m-2", 2), candidate("m-3", 3)], "collection": {"candidateTopN": 3, "targetTopN": 1}},
                configuration_fingerprint="fingerprint",
                period={"startDate": "2026-08-01", "endDate": "2026-08-02"},
                options={"rpaConcurrency": 3, "recoveryAttempts": 1},
                fetch_diagnosis=fetch,
                postprocess=lambda _entry, _records, _paths: {"frames": []},
            )
            ready = json.loads((root / "rpa" / "top_materials_rpa_ready.json").read_text(encoding="utf-8"))
        self.assertTrue(result["succeeded"])
        self.assertEqual(calls, ["m-1"])
        self.assertEqual([item["materialId"] for item in ready["materials"]], ["m-1"])

    def test_resume_reuses_completed_material_only_when_fingerprint_matches(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "input.csv"
            csv_path.write_text("data", encoding="utf-8")

            def fetch(entry, _paths):
                calls.append(entry["materialId"])
                return {"records": [{"materialId": entry["materialId"]}], "csvPath": csv_path, "gatewayReceipt": {}}

            common = {
                "configuration_fingerprint": "fingerprint",
                "period": {"startDate": "2026-08-01", "endDate": "2026-08-02"},
                "options": {"rpaConcurrency": 1},
                "fetch_diagnosis": fetch,
                "postprocess": lambda entry, _records, _paths: {"frames": []},
            }
            payload = {"materials": [candidate("m-1", 1)], "collection": {"candidateTopN": 1, "targetTopN": 1}}
            run_rpa_pipeline(root, payload, **common)
            run_rpa_pipeline(root, payload, **common)
            with self.assertRaisesRegex(RpaPipelineError, "configuration fingerprint"):
                run_rpa_pipeline(root, payload, **{**common, "configuration_fingerprint": "changed"})

        self.assertEqual(calls, ["m-1"])

    def test_resume_refetches_when_completed_record_artifact_has_been_removed(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = self._csv(root)

            def fetch(entry, _paths):
                calls.append(entry["materialId"])
                return {"records": [{"materialId": entry["materialId"]}], "csvPath": csv_path, "gatewayReceipt": {}}

            arguments = {
                "configuration_fingerprint": "fingerprint",
                "period": {"startDate": "2026-08-01", "endDate": "2026-08-02"},
                "options": {"rpaConcurrency": 1},
                "fetch_diagnosis": fetch,
                "postprocess": lambda _entry, _records, _paths: {"frames": []},
            }
            payload = {"materials": [candidate("m-1", 1)], "collection": {"candidateTopN": 1, "targetTopN": 1}}
            run_rpa_pipeline(root, payload, **arguments)
            (root / "rpa" / "diagnosis-records" / "m-1.json").unlink()
            run_rpa_pipeline(root, payload, **arguments)

        self.assertEqual(calls, ["m-1", "m-1"])

    def test_rejects_duplicate_candidate_material_ids_before_submitting_rpa(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RpaPipelineError, "duplicate"):
                run_rpa_pipeline(
                    Path(directory),
                    {"materials": [candidate("m-1", 1), candidate("m-1", 2)], "collection": {"candidateTopN": 2, "targetTopN": 1}},
                    configuration_fingerprint="fingerprint",
                    period={"startDate": "2026-08-01", "endDate": "2026-08-02"},
                    fetch_diagnosis=lambda *_args: self.fail("must not submit RPA"),
                    postprocess=lambda *_args: self.fail("must not postprocess"),
                )

    def test_postprocessor_deletes_temporary_video_and_returns_verified_jpegs(self):
        downloaded = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def download_video(_url, target, **_kwargs):
                target.write_bytes(b"temporary-video")
                downloaded.append(target)
                return target

            def extract_frames(_records, _policy, _video, output_dir):
                path = Path(output_dir) / "01_0s.jpg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\xff\xd8\xff\xd9")
                return {"frames": [{"framePath": str(path), "durationSec": 0, "selectionEvidence": []}]}

            postprocess = build_postprocessor(
                {"schemaVersion": 1, "includeZeroSecond": True, "policies": []},
                {"videoMaxBytes": 1024, "downloadDeadlineSec": 5},
                download_video=download_video,
                extract_frames=extract_frames,
            )
            result = run_rpa_pipeline(
                root,
                {"materials": [candidate("m-1", 1)], "collection": {"candidateTopN": 1, "targetTopN": 1}},
                configuration_fingerprint="fingerprint",
                period={"startDate": "2026-08-01", "endDate": "2026-08-02"},
                options={"rpaConcurrency": 1},
                fetch_diagnosis=lambda entry, _paths: {"records": [{"recordType": "material_info", "materialId": entry["materialId"], "videoUrl": "https://video.test/m-1"}], "csvPath": self._csv(root), "gatewayReceipt": {}},
                postprocess=postprocess,
            )

            ready = json.loads((root / "rpa" / "top_materials_rpa_ready.json").read_text(encoding="utf-8"))

        self.assertTrue(result["succeeded"])
        self.assertEqual(ready["materials"][0]["highlightFrames"][0]["durationSec"], 0)
        self.assertTrue(downloaded)
        self.assertFalse(downloaded[0].exists())

    def test_ready_artifacts_use_relative_paths_and_integrity_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def extract_frames(_records, _policy, _video, output_dir):
                frame_path = Path(output_dir) / "01_0s.jpg"
                frame_path.parent.mkdir(parents=True, exist_ok=True)
                frame_path.write_bytes(b"\xff\xd8\xff\xd9")
                summary_path = Path(output_dir) / "summary.json"
                summary_path.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
                return {"frames": [{"framePath": str(frame_path), "durationSec": 0, "selectionEvidence": []}]}

            postprocess = build_postprocessor(
                {"schemaVersion": 1, "includeZeroSecond": True, "policies": []},
                {"videoMaxBytes": 1024, "downloadDeadlineSec": 5},
                download_video=lambda _url, target, **_kwargs: target.write_bytes(b"temporary-video"),
                extract_frames=extract_frames,
            )
            run_rpa_pipeline(
                root,
                {"materials": [candidate("m-1", 1)], "collection": {"candidateTopN": 1, "targetTopN": 1}},
                configuration_fingerprint="fingerprint",
                period={"startDate": "2026-08-01", "endDate": "2026-08-02"},
                options={"rpaConcurrency": 1},
                fetch_diagnosis=lambda entry, _paths: {
                    "records": [{"recordType": "material_info", "materialId": entry["materialId"], "videoUrl": "https://video.test/m-1"}],
                    "csvPath": self._csv(root),
                },
                postprocess=postprocess,
            )
            ready = json.loads((root / "rpa" / "top_materials_rpa_ready.json").read_text(encoding="utf-8"))
            receipt = json.loads((root / "rpa" / "rpa-receipts" / "m-1.json").read_text(encoding="utf-8"))

        material = ready["materials"][0]
        for artifact in (
            material["diagnosisCsvArtifact"],
            material["diagnosisRecordsArtifact"],
            material["highlightSummaryArtifact"],
            material["highlightFrames"][0]["artifact"],
            receipt["diagnosisRecordsArtifact"],
        ):
            self.assertFalse(Path(artifact["artifactPath"]).is_absolute())
            self.assertNotIn("..", Path(artifact["artifactPath"]).parts)
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(artifact["sizeBytes"], 0)

    @staticmethod
    def _csv(root):
        path = root / "input.csv"
        path.write_text("data", encoding="utf-8")
        return path
