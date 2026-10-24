from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agents'))
import _pipeline_utils as utils
from growth_agent import build_report


class AIRecoveryTests(unittest.TestCase):
    def response(self, status, data, text='temporary unavailable'):
        return mock.Mock(status_code=status, ok=status < 400, text=text, json=mock.Mock(return_value=data))

    def test_gemini_retries_temporary_503_on_same_key(self):
        responses = [self.response(503, {}), self.response(200, {'candidates': [{'content': {'parts': [{'text': '{"ok":true}'}]}}]})]
        with mock.patch.object(utils, 'GEMINI_API_KEYS', ['existing-key']), mock.patch.object(utils.requests, 'post', side_effect=responses) as post, mock.patch.object(utils.time, 'sleep'):
            self.assertEqual(utils._call_gemini('test'), '{"ok":true}')
            self.assertEqual(post.call_count, 2)
            self.assertTrue(all(call.kwargs['params']['key'] == 'existing-key' for call in post.call_args_list))

    def test_invalid_json_retries_only_gemini_then_stops(self):
        with mock.patch.object(utils, 'GEMINI_API_KEYS', ['existing-key']), mock.patch.object(utils, 'OPENROUTER_API_KEY', None), mock.patch.object(utils, '_call_gemini', return_value='not json') as gemini:
            with self.assertRaisesRegex(ValueError, 'JSON'):
                utils.call_json('test')
            self.assertEqual(gemini.call_count, 2)

    def test_daily_quota_stops_without_another_key_or_provider(self):
        response = self.response(429, {}, text='requests per day quota exhausted')
        with mock.patch.object(utils, 'GEMINI_API_KEYS', ['existing-key', 'unused-key']), mock.patch.object(utils.requests, 'post', return_value=response) as post:
            with self.assertRaisesRegex(RuntimeError, 'daily quota exhausted'):
                utils._call_gemini('test')
            self.assertEqual(post.call_count, 1)
            self.assertIn('generativelanguage.googleapis.com', post.call_args.args[0])
            self.assertEqual(post.call_args.kwargs['params']['key'], 'existing-key')

    def test_network_failures_retry_and_stop_on_gemini(self):
        with mock.patch.object(utils, 'GEMINI_API_KEYS', ['existing-key']), mock.patch.object(utils.requests, 'post', side_effect=utils.requests.Timeout()) as post, mock.patch.object(utils.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'connection failed'):
                utils._call_gemini('test', max_retries=2)
            self.assertEqual(post.call_count, 2)
            self.assertTrue(all('generativelanguage.googleapis.com' in call.args[0] for call in post.call_args_list))

    def test_gemini_failure_uses_existing_openrouter_fallback(self):
        with mock.patch.object(utils, 'GEMINI_API_KEYS', ['existing-key']), mock.patch.object(utils, 'OPENROUTER_API_KEY', 'existing-openrouter-key'), mock.patch.object(utils, '_call_gemini', side_effect=RuntimeError('Gemini unavailable')) as gemini, mock.patch.object(utils, '_call_openrouter', return_value='{"ok":true}') as router:
            self.assertEqual(utils.call_json('test'), {'ok': True})
            gemini.assert_called_once()
            router.assert_called_once()

    def test_openrouter_never_selects_a_paid_model(self):
        response = self.response(200, {'choices': [{'message': {'content': '{"ok":true}'}}]})
        with mock.patch.object(utils, 'OPENROUTER_API_KEY', 'existing-openrouter-key'), mock.patch.object(utils, 'OPENROUTER_MODEL', 'paid/model'), mock.patch.object(utils.requests, 'post', return_value=response) as post:
            utils._call_openrouter('test')
            self.assertEqual(post.call_args.kwargs['json']['model'], 'openrouter/free')

    def test_parse_fences_preamble_and_expected_array(self):
        self.assertEqual(utils.parse_json_response('```json\n{"ok":true}\n```'), {'ok': True})
        self.assertEqual(utils.parse_json_response('Here is the result: {"ok":true}'), {'ok': True})
        self.assertEqual(utils.parse_json_response('["animal", "water"]', list), ['animal', 'water'])
        with self.assertRaises(ValueError):
            utils.parse_json_response('[]')

    def test_public_report_does_not_include_local_income_target(self):
        snapshot = {'videos': [], 'channel': {'statistics': {}}, 'timestamp': 'test', 'source': 'test'}
        report = build_report(snapshot, {'channel_handle': '@WildFactsDailyTV'})
        self.assertNotIn('Monthly income target', report)
        self.assertNotIn('Income planning', report)


if __name__ == '__main__':
    unittest.main()
