from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agents'))
import editor_agent


class CaptionTextTest(unittest.TestCase):
    def test_short_captions_keep_all_words_in_small_groups(self):
        narration = 'air and reclaims moisture before it leaves the nose'
        captions = editor_agent.build_srt_captions(
            [{'narration': narration}], [4], max_words=3,
        )
        cues = [block.splitlines() for block in captions.strip().split('\n\n')]
        self.assertEqual(' '.join(cue[2] for cue in cues), narration)
        self.assertTrue(all(len(cue[2].split()) <= 3 for cue in cues))
        self.assertTrue(cues[0][1].startswith('00:00:00,000 -->'))
        self.assertTrue(cues[-1][1].endswith('00:00:04,000'))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                     'FFmpeg is required to verify actual caption placement')
class CaptionLayoutIntegrationTest(unittest.TestCase):
    def test_rendered_short_caption_clears_face_and_player_controls(self):
        with tempfile.TemporaryDirectory(prefix='wildfacts-captions-') as temporary:
            root = Path(temporary)

            def ffmpeg(*args):
                subprocess.run(
                    ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args],
                    capture_output=True, check=True, timeout=60,
                )

            ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=1080x1920:r=10:d=0.6',
                   '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast',
                   '-pix_fmt', 'yuv420p', str(root / 'silent_video.mp4'))
            ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.6',
                   str(root / 'voice.wav'))
            (root / 'captions.srt').write_text(
                editor_agent.build_srt_captions(
                    [{'narration': 'air and reclaims'}], [0.6], max_words=3,
                ), encoding='utf-8',
            )
            editor_agent.mux_audio_video_with_captions(
                'silent_video.mp4', str(root / 'voice.wav'), 'captions.srt',
                str(root / 'captioned.mp4'), 'short', str(root),
            )
            ffmpeg('-ss', '0.2', '-i', str(root / 'captioned.mp4'),
                   '-frames:v', '1', str(root / 'frame.png'))
            with Image.open(root / 'frame.png') as frame:
                # Only the white caption has bright red and green on blue.
                red, green, _ = frame.convert('RGB').split()
                from PIL import ImageChops
                mask = ImageChops.darker(red, green).point(lambda value: 255 if value > 180 else 0)
                bounds = mask.getbbox()
            self.assertIsNotNone(bounds, 'Caption was not rendered')
            left, top, right, bottom = bounds
            self.assertGreaterEqual(top, 1300, 'Caption overlaps the central subject area')
            self.assertLessEqual(bottom, 1560, 'Caption overlaps bottom player information')
            self.assertGreaterEqual(left, 90)
            self.assertLessEqual(right, 870, 'Caption overlaps right-side player controls')
            self.assertGreater(bottom - top, 25, 'Caption is too small to read')
            self.assertLess(bottom - top, 100, 'Caption is unnecessarily large')


if __name__ == '__main__':
    unittest.main()
