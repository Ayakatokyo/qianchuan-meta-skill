import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_verifier import RunVerificationError, verify_completed_run  # noqa: E402


class RunVerifierTests(unittest.TestCase):
    def _completed_run(self, root, *, with_highlights=True):
        material_id = "123"
        csv_path = root / "rpa" / "diagnosis-csv" / (material_id + ".csv")
        records_path = root / "rpa" / "diagnosis-records" / (material_id + ".json")
        summary_path = root / "rpa" / "peak-frames" / material_id / "summary.json"
        frame_path = root / "rpa" / "peak-frames" / material_id / "01_0s.jpg"
        video_path = root / "rpa" / "videos" / (material_id + ".mp4")
        csv_path.parent.mkdir(parents=True)
        records_path.parent.mkdir(parents=True)
        if with_highlights:
            summary_path.parent.mkdir(parents=True)
        csv_path.write_text("diagnosis", encoding="utf-8")
        records_path.write_text(json.dumps([{"materialId": material_id}]), encoding="utf-8")
        if with_highlights:
            summary_path.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
            frame_path.write_bytes(b"\xff\xd8frame\xff\xd9")
        video_path.parent.mkdir(parents=True)
        video_path.write_bytes(b"fixture-video")

        def artifact(path):
            payload = path.read_bytes()
            return {
                "artifactPath": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "sizeBytes": len(payload),
            }

        (root / "rpa" / "batch-verification.json").write_text(
            json.dumps({"configurationFingerprint": "fingerprint", "targetTopN": 1, "succeededCount": 1, "status": "succeeded", "errors": []}),
            encoding="utf-8",
        )
        (root / "rpa" / "top_materials_rpa_ready.json").write_text(
            json.dumps({
                "configurationFingerprint": "fingerprint",
                "materialCount": 1,
                "materials": [{
                    "materialId": material_id,
                    "diagnosisCsvArtifact": artifact(csv_path),
                    "diagnosisRecordsArtifact": artifact(records_path),
                    "videoArtifact": artifact(video_path),
                    **({"highlightSummaryArtifact": artifact(summary_path), "highlightFrames": [{"artifact": artifact(frame_path)}]} if with_highlights else {"highlightFrames": []}),
                }],
            }),
            encoding="utf-8",
        )
        return frame_path, video_path

    def test_verify_completed_run_checks_artifact_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame_path, video_path = self._completed_run(root)

            result = verify_completed_run(root)
            frame_path.write_bytes(b"\xff\xd8other\xff\xd9")
            with self.assertRaisesRegex(RunVerificationError, "sizeBytes|sha256"):
                verify_completed_run(root)
            frame_path.write_bytes(b"\xff\xd8frame\xff\xd9")
            video_path.write_bytes(b"other-video")
            with self.assertRaisesRegex(RunVerificationError, "sizeBytes|sha256"):
                verify_completed_run(root)

        self.assertTrue(result["valid"])
        self.assertEqual(result["verifiedMaterialCount"], 1)

    def test_verify_completed_run_succeeds_after_directory_copy(self):
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as target_directory:
            source = Path(source_directory) / "run"
            source.mkdir()
            self._completed_run(source)
            copied = Path(target_directory) / "copied-run"
            shutil.copytree(source, copied)
            result = verify_completed_run(copied)

        self.assertTrue(result["valid"])
        self.assertEqual(result["verifiedFileCount"], 5)

    def test_verify_completed_run_allows_a_video_only_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._completed_run(root, with_highlights=False)
            result = verify_completed_run(root)

        self.assertTrue(result["valid"])
        self.assertEqual(result["verifiedFileCount"], 3)
