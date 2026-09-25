import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import rpa_pipeline as pipeline


class FirstBatchCsvGateTests(unittest.TestCase):
    def run_batch(self, root, fetch, postprocess=None, target=4, concurrency=3):
        return pipeline.run_rpa_pipeline(
            root, {'materials': [{'materialId': str(i)} for i in range(1, 7)],
                   'collection': {'candidateTopN': 6, 'targetTopN': target}},
            configuration_fingerprint='request-1', period={'startDate': '2026-08-01', 'endDate': '2026-08-02'},
            options={'rpaConcurrency': concurrency}, fetch_diagnosis=fetch,
            postprocess=postprocess or self.video)

    @staticmethod
    def video(entry, records, paths):
        path = paths['videos'] / (entry['materialId'] + '.mp4')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'video')
        return {'frames': [], 'videoPath': str(path)}

    @staticmethod
    def csv(entry, paths):
        path = paths['csv'] / (entry['materialId'] + '.source.csv')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('materialId\x01recordType\n' + entry['materialId'] + '\x01material_info\n')
        return {'records': [{'materialId': entry['materialId'], 'recordType': 'material_info'}], 'csvPath': str(path)}

    def test_all_first_batch_failures_stop_before_fourth_submission_and_persist_pause(self):
        calls = []
        def fail(entry, paths):
            calls.append(entry['materialId'])
            raise pipeline.RpaPipelineError('no CSV', 'rpa_csv_artifact_unavailable')
        with tempfile.TemporaryDirectory() as root:
            result = self.run_batch(root, fail)
            self.assertEqual(set(calls), {'1', '2', '3'})
            self.assertEqual(result['firstBatchCsvGate']['status'], 'blocked')
            self.assertIn('入参可能有问题', result['firstBatchCsvGate']['message'])
            self.assertFalse(result['succeeded'])
            manifest = json.loads((Path(root) / 'rpa/batch-manifest.json').read_text())
            self.assertEqual(manifest['status'], 'paused')
            self.assertEqual(manifest['firstBatchCsvGate']['validCsvCount'], 0)
            with self.assertRaisesRegex(pipeline.RpaPipelineError, '入参可能有问题'):
                self.run_batch(root, lambda *_: self.fail('resume submitted RPA'))

    def test_empty_invalid_wrong_identity_and_payload_only_do_not_open_gate(self):
        for kind in ('empty', 'header', 'html', 'wrong_id', 'unknown_type', 'payload_only'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                calls = []
                def fetch(entry, paths):
                    calls.append(entry['materialId'])
                    result = self.csv(entry, paths)
                    if kind == 'payload_only':
                        result['csvPath'] = None
                    else:
                        text = {'empty': '', 'header': 'materialId\x01recordType\n', 'html': '<html>login</html>',
                                'wrong_id': 'materialId\x01recordType\nother\x01material_info\n',
                                'unknown_type': 'materialId\x01recordType\n1\x01error\n'}[kind]
                        Path(result['csvPath']).write_text(text)
                    return result
                result = self.run_batch(root, fetch, target=1)
                self.assertEqual(calls, ['1'])
                self.assertEqual(result['firstBatchCsvGate']['status'], 'blocked')

    def test_partial_valid_first_batch_keeps_normal_backfill(self):
        calls = []
        def fetch(entry, paths):
            calls.append(entry['materialId'])
            if entry['materialId'] in ('1', '2'):
                raise pipeline.RpaPipelineError('no CSV', 'rpa_csv_artifact_unavailable')
            return self.csv(entry, paths)
        with tempfile.TemporaryDirectory() as root:
            result = self.run_batch(root, fetch)
        self.assertTrue(result['succeeded'])
        self.assertEqual(result['firstBatchCsvGate']['status'], 'passed')
        self.assertEqual(set(calls), {'1', '2', '3', '4', '5', '6'})

    def test_postprocess_failure_does_not_become_csv_failure(self):
        calls = []
        def fetch(entry, paths):
            calls.append(entry['materialId'])
            return self.csv(entry, paths)
        def no_video(*_):
            raise RuntimeError('video download failed')
        with tempfile.TemporaryDirectory() as root:
            result = self.run_batch(root, fetch, no_video)
        self.assertEqual(set(calls), {'1', '2', '3', '4'})
        self.assertEqual(result['firstBatchCsvGate']['status'], 'passed')
        self.assertFalse(result['succeeded'])

    def test_unknown_first_batch_member_blocks_next_wave_even_with_valid_csv(self):
        calls = []
        def fetch(entry, paths):
            calls.append(entry['materialId'])
            if entry['materialId'] == '1':
                raise pipeline.RpaPipelineError('remote status unknown', 'rpa_result_unconfirmed')
            return self.csv(entry, paths)
        with tempfile.TemporaryDirectory() as root:
            result = self.run_batch(root, fetch)
            self.assertEqual(set(calls), {'1', '2', '3'})
            self.assertEqual(result['firstBatchCsvGate']['status'], 'unconfirmed')
            with self.assertRaises(pipeline.RpaPipelineError):
                self.run_batch(root, lambda *_: self.fail('resubmission'))

    def test_restart_during_first_wave_cannot_resubmit(self):
        class Interrupted(BaseException):
            pass
        def interrupted(*_):
            raise Interrupted()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(Interrupted):
                self.run_batch(root, interrupted, target=1)
            with self.assertRaises(pipeline.RpaPipelineError):
                self.run_batch(root, lambda *_: self.fail('restart submitted RPA'))

    def test_cli_paused_run_stops_before_report_or_account_requests(self):
        import run_meta_skill
        from test_run_meta_skill import MetaSkillCliTests
        def fail(entry, paths):
            raise pipeline.RpaPipelineError("no CSV", "rpa_csv_artifact_unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query, highlight = MetaSkillCliTests().write_specs(root)
            run = root / "run"
            self.run_batch(run, fail)
            with patch.object(run_meta_skill, "fetch_material_report", side_effect=AssertionError("new report request")), patch.object(
                run_meta_skill, "list_rpa_accounts", side_effect=AssertionError("new account request")):
                code = run_meta_skill.main(["workflow", "--query-spec", str(query), "--highlight-spec", str(highlight),
                                           "--execute-rpa", "--rpa-shop-id", "account-1", "--output-dir", str(run)])
            self.assertEqual(code, 2)

    def test_untyped_isolated_worker_failure_does_not_claim_terminal_result(self):
        from rpa_runtime import run_isolated
        from test_rpa_runtime import unknown_worker
        calls = []
        def fetch(entry, paths):
            calls.append(entry['materialId'])
            return run_isolated({}, timeout_sec=1, worker=unknown_worker)
        with tempfile.TemporaryDirectory() as root:
            result = self.run_batch(root, fetch, target=1)
        self.assertEqual(calls, ['1'])
        self.assertEqual(result['firstBatchCsvGate']['status'], 'unconfirmed')

    def test_standalone_detail_fetch_cannot_bypass_batch_gate(self):
        import rpa_dynamic_client as dynamic
        for extra in ([], ['--no-download']):
            args = dynamic.build_parser().parse_args(['fetch', '--shop-id', 'a', '--params-json', '{}', '--output-dir', '/tmp/rpa-no-submit', *extra])
            with patch.object(dynamic, 'fetch_connector_detail', side_effect=AssertionError('direct remote request')):
                with self.assertRaisesRegex(dynamic.ContractError, 'run_meta_skill'):
                    dynamic.cmd_fetch(args)
