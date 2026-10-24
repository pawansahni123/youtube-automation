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

    def test_gemini_retries_temporary_503(self):
        responses = [self.response(503, {}), self.response(200, {'candidates': [{'content': {'parts': [{'text': '{"ok":true}'}]}}]})]
        with mock.patch.object(utils, 'GEMINI_API_KEYS', ['test-key']), \
             mock.patch.object(utils.requests, 'post', side_effect=responses) as post, \
             mock.patch.object(utils.time, 'sleep'):
            self.assertEqual(utils._call_gemini('test'), '{"ok":true}')
            self.assertEqual(post.call_count, 2)

    def test_openrouter_retries_empty_response(self):
        responses = [self.response(200, {'choices': [{'message': {'content': ''}}]}),
                     self.response(200, {'choices': [{'message': {'content': '{"ok":true}'}}]})]
        with mock.patch.object(utils, 'OPENROUTER_API_KEY', 'test-key'), \
             mock.patch.object(utils.requests, 'post', side_effect=responses) as post, \
             mock.patch.object(utils.time, 'sleep'):
            self.assertEqual(utils._call_openrouter('test'), '{"ok":true}')
            self.assertEqual(post.call_count, 2)

    def test_invalid_json_retries_then_falls_back(self):
        with mock.patch.object(utils, 'GEMINI_API_KEYS', ['test-key']), \
             mock.patch.object(utils, 'OPENROUTER_API_KEY', 'test-key'), \
             mock.patch.object(utils, 'ANTHROPIC_API_KEY', None), \
             mock.patch.object(utils, '_call_gemini', return_value='not json') as gemini, \
             mock.patch.object(utils, '_call_openrouter', return_value='{"chosen_topic":"fresh"}'):
            self.assertEqual(utils.call_json('test')['chosen_topic'], 'fresh')
            self.assertEqual(gemini.call_count, 2)

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
