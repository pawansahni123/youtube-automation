import datetime as dt
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agents'))
from _content_guard import build_upload_plan, load_history, write_json_atomic
import qa_agent


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required for media integration')
class QAIntegrationTest(unittest.TestCase):
    def test_real_video_passes_then_black_video_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='wildfacts-qa-') as temporary:
            root = Path(temporary)
            video_dir = root / 'assets/final_videos'
            audio_dir = root / 'assets/audio'
            video_dir.mkdir(parents=True)
            audio_dir.mkdir(parents=True)
            video = video_dir / 'final_short.mp4'
            audio = audio_dir / 'latest_voiceover_short.mp3'
            script = {'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
                      'short_script': {'title': 'Integration test'}, 'long_script': None}
            scripts_db = root / 'database/scripts.json'
            qa_db = root / 'database/qa_reports.json'
            write_json_atomic(scripts_db, [script])
            def ffmpeg(*args):
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args],
                               capture_output=True, check=True, timeout=40)
            ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', str(audio))
            def encode(source):
                ffmpeg('-f', 'lavfi', '-i', source, '-i', str(audio), '-t', '3',
                       '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast',
                       '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(video))
            encode('color=c=blue:s=1080x1920:r=15:d=3')
            with mock.patch.object(qa_agent, 'FINAL_VIDEOS_DIR', str(video_dir)), \
                 mock.patch.object(qa_agent, 'AUDIO_DIR', str(audio_dir)), \
                 mock.patch.object(qa_agent, 'SCRIPTS_DB_PATH', str(scripts_db)), \
                 mock.patch.object(qa_agent, 'QA_DB_PATH', str(qa_db)), \
                 mock.patch.object(qa_agent, 'GEMINI_API_KEYS', []):
                qa_agent.run()
                report = load_history(qa_db)[-1]
                self.assertTrue(report['passed'])
                seo = {'script_timestamp': script['timestamp'], 'seo': {'title_variants': [{'title': 'Integration test'}]}}
                self.assertEqual(len(build_upload_plan(root, script, seo, report, [])), 1)
                encode('color=c=black:s=1080x1920:r=15:d=3')
                with self.assertRaisesRegex(RuntimeError, 'QA check failed'):
                    qa_agent.run()
                failed = load_history(qa_db)[-1]
                self.assertFalse(failed['passed'])
                self.assertTrue(any('solid black' in issue for issue in failed['issues']))


if __name__ == '__main__':
    unittest.main()
