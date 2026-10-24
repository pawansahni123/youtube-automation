import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agents'))
from _content_guard import build_upload_plan, file_sha256, load_history, short_title, write_json_atomic
from growth_agent import required_views
import qa_agent
import upload_agent
import topic_agent


class UploadGuardsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video_dir = self.root / 'assets/final_videos'
        self.video_dir.mkdir(parents=True)
        (self.video_dir / 'final_short.mp4').write_bytes(b'current short')
        (self.video_dir / 'final_long.mp4').write_bytes(b'stale long')
        self.scripts = {'timestamp': '2026-10-24T12:00:00+00:00', 'topic': {}, 'short_script': {'title': 'fresh'}, 'long_script': None}
        self.seo = {'script_timestamp': self.scripts['timestamp'], 'seo': {'title_variants': [{'title': 'A New Discovery #Shorts'}], 'description': 'Description', 'tags': []}}
        self.qa = {'passed': True, 'script_timestamp': self.scripts['timestamp'], 'video_sha256': {'short': file_sha256(self.video_dir / 'final_short.mp4')}}

    def plan(self, history=None):
        return build_upload_plan(self.root, self.scripts, self.seo, self.qa, history or [])

    def test_short_only_ignores_stale_long_file(self):
        plan = self.plan()
        self.assertEqual([item['format'] for item in plan], ['short'])
        self.assertEqual(plan[0]['title'], 'A New Discovery #Shorts')

    def test_changed_video_after_qa_rejected(self):
        (self.video_dir / 'final_short.mp4').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed since QA'):
            self.plan()

    def test_duplicate_bytes_rejected_even_after_retitle(self):
        with self.assertRaisesRegex(ValueError, 'bytes were already uploaded'):
            self.plan([{'video_id': 'old', 'title': 'different', 'video_sha256': self.qa['video_sha256']['short']}])

    def test_duplicate_title_ignores_case_punctuation_and_hashtag(self):
        with self.assertRaisesRegex(ValueError, 'title already uploaded'):
            self.plan([{'video_id': 'old', 'format': 'short', 'title': 'A NEW Discovery! #Shorts #Shorts'}])

    def test_same_title_allowed_across_formats(self):
        self.assertEqual(len(self.plan([{'video_id': 'old', 'format': 'long', 'title': 'A New Discovery'}])), 1)

    def test_stale_seo_and_qa_rejected(self):
        self.seo['script_timestamp'] = 'old'
        with self.assertRaisesRegex(ValueError, 'SEO'):
            self.plan()
        self.seo['script_timestamp'] = self.scripts['timestamp']
        self.qa['script_timestamp'] = 'old'
        with self.assertRaisesRegex(ValueError, 'QA'):
            self.plan()

    def test_failed_qa_rejected(self):
        self.qa['passed'] = False
        with self.assertRaisesRegex(ValueError, 'QA'):
            self.plan()

    def test_missing_active_video_rejected(self):
        self.scripts['long_script'] = {'title': 'new long'}
        (self.video_dir / 'final_long.mp4').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or empty long'):
            self.plan()

    def test_corrupt_history_is_not_silently_reset(self):
        path = self.root / 'uploads.json'
        original = '[\n<<<<<<< HEAD\n]'
        path.write_text(original)
        with self.assertRaisesRegex(ValueError, 'repair history'):
            load_history(path)
        self.assertEqual(path.read_text(), original)

    def test_atomic_history_round_trip(self):
        path = self.root / 'uploads.json'
        write_json_atomic(path, [{'video_id': 'one'}])
        write_json_atomic(path, load_history(path) + [{'video_id': 'two'}])
        self.assertEqual([row['video_id'] for row in load_history(path)], ['one', 'two'])

    def test_short_title_has_one_hashtag_and_fits_limit(self):
        title = short_title('x' * 120 + ' #Shorts #SHORTS')
        self.assertLessEqual(len(title), 100)
        self.assertEqual(title.lower().count('#shorts'), 1)

    def test_invalid_plan_fails_before_authentication(self):
        with mock.patch.object(upload_agent, 'load_latest', side_effect=[self.seo, self.scripts]), \
             mock.patch.object(upload_agent, 'load_history', side_effect=[[], []]), \
             mock.patch.object(upload_agent, 'get_authenticated_service') as authenticate:
            with self.assertRaisesRegex(ValueError, 'QA'):
                upload_agent.run()
            authenticate.assert_not_called()

    def test_empty_topic_search_stops_pipeline(self):
        with mock.patch.object(topic_agent, 'collect_trending_candidates', return_value=[]), \
             mock.patch.object(topic_agent, 'save_result') as save:
            with self.assertRaisesRegex(ValueError, 'No fresh topic'):
                topic_agent.run()
            save.assert_not_called()

    def test_failed_ffmpeg_analysis_fails_qa(self):
        issues = []
        with mock.patch.object(qa_agent.subprocess, 'run', return_value=mock.Mock(returncode=1, stderr='decode failure')):
            qa_agent.check_black_screen_ratio('video.mp4', 'short', 45, issues)
        self.assertTrue(any('analysis failed' in issue for issue in issues))

    def test_missing_audio_fails_qa(self):
        issues = []
        with mock.patch.object(qa_agent, 'AUDIO_DIR', str(self.root)):
            qa_agent.check_duration_matches_audio(45, 'short', issues)
        self.assertTrue(any('Voiceover file is missing' in issue for issue in issues))

    def test_income_calculator_accounts_for_costs(self):
        self.assertEqual(required_views(100000, 100), 1000000)
        self.assertEqual(required_views(100000, 100, 10000), 1100000)
        with self.assertRaises(ValueError):
            required_views(100000, 0)


if __name__ == '__main__':
    unittest.main()
