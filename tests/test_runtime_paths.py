from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from scripts.runtime_paths import RuntimePathError, SKILL_ROOT, external_runtime_path

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import run_meta_skill  # noqa: E402


class RuntimePathTests(unittest.TestCase):
    def test_rejects_skill_internal_output(self):
        with self.assertRaisesRegex(RuntimePathError, "outside the skill installation"):
            external_runtime_path(SKILL_ROOT / "千川数据资源" / "new-run")

    def test_accepts_work_object_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "千川数据资源" / "new-run"
            self.assertEqual(external_runtime_path(target), target.absolute())

    def test_collection_cli_rejects_skill_internal_run_before_writing(self):
        target = SKILL_ROOT / "千川数据资源" / "new-run"
        with mock.patch("builtins.print"):
            status = run_meta_skill.main([
                "query", "--query-spec", "missing-query.json",
                "--highlight-mode", "off", "--output-dir", str(target),
            ])
        self.assertEqual(status, 2)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
