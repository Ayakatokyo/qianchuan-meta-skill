import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_verifier import RunVerificationError, verify_completed_run  # noqa: E402


class RunVerifierTests(unittest.TestCase):
    def test_verify_completed_run_checks_artifact_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material_id = "123"
            csv_path = root / "rpa" / "diagnosis-csv" / (material_id + ".csv")
            records_path = root / "rpa" / "diagnosis-records" / (material_id + ".json")
            summary_path = root / "rpa" / "peak-frames" / material_id / "summary.json"
            frame_path = root / "rpa" / "peak-frames" / material_id / "01_0s.jpg"
            csv_path.parent.mkdir(parents=True)
            records_path.parent.mkdir(parents=True)
            summary_path.parent.mkdir(parents=True)
            csv_path.write_text("diagnosis", encoding="utf-8")
            records_path.write_text(json.dumps([{"materialId": material_id}]), encoding="utf-8")
            summary_path.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
            frame_path.write_bytes(b"\xff\xd8frame\xff\xd9")

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
                        "highlightSummaryArtifact": artifact(summary_path),
                        "highlightFrames": [{"artifact": artifact(frame_path)}],
                    }],
                }),
                encoding="utf-8",
            )

            result = verify_completed_run(root)
            frame_path.write_bytes(b"\xff\xd8other\xff\xd9")
            with self.assertRaisesRegex(RunVerificationError, "sha256"):
                verify_completed_run(root)

        self.assertTrue(result["valid"])
        self.assertEqual(result["verifiedMaterialCount"], 1)
