import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rpa_runtime import RpaRuntimeError, run_isolated  # noqa: E402


def slow_worker(_params):
    time.sleep(1)
    return {"ok": True}


class RpaRuntimeTests(unittest.TestCase):
    def test_timeout_terminates_isolated_material_process(self):
        with self.assertRaisesRegex(RpaRuntimeError, "rpa_request_timeout"):
            run_isolated({}, timeout_sec=0.01, worker=slow_worker)
