import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pymp4v2 import raw

from scoopy import remux

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'media'
FFPROBE = shutil.which('ffprobe')
FFMPEG = shutil.which('ffmpeg')


def decoded_video_md5s(path):
    """Independent frame-by-frame md5 listing via ffmpeg (developer-only tool)."""
    result = subprocess.run(
        [FFMPEG, '-v', 'error', '-i', str(path), '-map', '0:v:0', '-f', 'framemd5', '-'],
        check=True, capture_output=True, text=True)
    return [line.rsplit(',', 1)[-1].strip() for line in result.stdout.splitlines()
            if line and not line.startswith('#')]


def stream_field(path, field):
    result = subprocess.run(
        [FFPROBE, '-v', 'error', '-show_entries', f'stream={field}',
         '-of', 'default=noprint_wrappers=1', str(path)],
        check=True, capture_output=True, text=True)
    return [line.split('=')[1] for line in result.stdout.splitlines() if line.strip()]


def decoded_audio_pcm_hash(path):
    result = subprocess.run(
        [FFMPEG, '-v', 'error', '-i', str(path), '-f', 's16le', '-'],
        check=True, capture_output=True)
    return hashlib.sha256(result.stdout).hexdigest()


class InspectionTests(unittest.TestCase):
    def test_video_only_fixture_reports_video_without_audio(self):
        info = remux.inspect_media(FIXTURES / 'video-only.mp4')
        self.assertEqual(info['video']['entry'], 'avc1')
        self.assertGreater(info['video']['samples'], 0)
        self.assertFalse(info['video']['encrypted'])
        self.assertFalse(info['video']['fragmented'])
        self.assertIsNone(info['audio'])
        self.assertFalse(remux.combined_mp4_has_audio(FIXTURES / 'video-only.mp4'))

    def test_audio_fixture_reports_aac(self):
        info = remux.inspect_media(FIXTURES / 'audio.m4a')
        self.assertIsNone(info['video'])
        self.assertEqual(info['audio']['entry'], 'mp4a')
        self.assertGreater(info['audio']['samples'], 0)
        self.assertGreaterEqual(info['audio']['channels'], 1)

    def test_combined_fixture_reports_both_tracks(self):
        info = remux.inspect_media(FIXTURES / 'combined.mp4')
        self.assertEqual(info['video']['entry'], 'avc1')
        self.assertEqual(info['audio']['entry'], 'mp4a')
        self.assertTrue(remux.combined_mp4_has_audio(FIXTURES / 'combined.mp4'))

    def test_non_mp4_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            bogus = Path(temp, 'bogus.mp4')
            bogus.write_bytes(b'not really an mp4')
            with self.assertRaises(remux.RemuxError):
                remux.inspect_media(bogus)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)

    def create_mp4(self, name, tracks):
        path = self.temp_path / name
        with raw.MP4CreateEx(str(path)) as media:
            for kind in tracks:
                if kind == 'video':
                    track = raw.MP4AddVideoTrack(media, 90000, 3000, 64, 64)
                else:
                    track = raw.MP4AddAudioTrack(media, 44100, 1024)
                for _ in range(tracks[kind]):
                    data = b'\x00' * 16
                    if kind == 'video':
                        raw.MP4WriteSample(media, track, data)
                    else:
                        raw.MP4WriteSample(media, track, data)
        return path

    def test_validate_accepts_fixture_streams(self):
        remux.validate_video_stream(FIXTURES / 'video-only.mp4')
        remux.validate_audio_stream(FIXTURES / 'audio.m4a')

    def test_video_stream_requires_a_video_track(self):
        with self.assertRaisesRegex(remux.RemuxError, 'expected exactly one video track'):
            remux.validate_video_stream(FIXTURES / 'audio.m4a')

    def test_audio_stream_requires_an_audio_track(self):
        with self.assertRaisesRegex(remux.RemuxError, 'expected exactly one audio track'):
            remux.validate_audio_stream(FIXTURES / 'video-only.mp4')

    def test_empty_video_track_is_rejected(self):
        path = self.create_mp4('empty.mp4', {'video': 0})
        with self.assertRaisesRegex(remux.RemuxError, 'no readable samples'):
            remux.validate_video_stream(path)

    def test_unsupported_video_codec_is_rejected(self):
        # MP4AddVideoTrack's default type creates an 'mp4v' (MPEG-4 Visual)
        # sample entry, which the remuxer does not support.
        path = self.create_mp4('mp4v.mp4', {'video': 1})
        self.assertEqual(remux.inspect_media(path)['video']['entry'], 'mp4v')
        with self.assertRaisesRegex(remux.RemuxError, 'unsupported video codec'):
            remux.validate_video_stream(path)

    def test_multiple_audio_tracks_are_rejected(self):
        path = self.create_mp4('two-audio.mp4', {'audio': 1, 'extra-audio': 1})
        self.assertIsNone(remux.inspect_media(path)['audio'])
        with self.assertRaisesRegex(remux.RemuxError, 'expected exactly one audio track'):
            remux.validate_audio_stream(path)


class RemuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)

    def test_remux_output_validates_and_keeps_sources_intact(self):
        sources = [FIXTURES / name for name in ('video-only.mp4', 'audio.m4a')]
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        output = self.temp_path / 'out.mp4'
        remux.remux_to_mp4(sources[0], sources[1], output)

        info = remux.inspect_media(output)
        self.assertEqual(info['video']['entry'], 'avc1')
        self.assertEqual(info['audio']['entry'], 'mp4a')
        remux._validate_output(str(output), require_audio=True)
        self.assertEqual(output.stat().st_size > 0, True)

        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        self.assertEqual(before, after)

    def test_unsupported_audio_rejects_and_cleans_output(self):
        output = self.temp_path / 'out.mp4'
        with self.assertRaisesRegex(remux.RemuxError, 'expected exactly one audio track'):
            remux.remux_to_mp4(FIXTURES / 'video-only.mp4', FIXTURES / 'video-only.mp4', output)
        self.assertFalse(output.exists())

    @unittest.skipUnless(FFMPEG, 'ffmpeg is required for decoded-content checks')
    def test_decoded_frames_match_the_video_source(self):
        output = self.temp_path / 'out.mp4'
        remux.remux_to_mp4(FIXTURES / 'video-only.mp4', FIXTURES / 'audio.m4a', output)
        self.assertEqual(decoded_video_md5s(FIXTURES / 'video-only.mp4'),
                         decoded_video_md5s(output))

    @unittest.skipUnless(FFPROBE and FFMPEG, 'ffmpeg/ffprobe are required for sync checks')
    def test_audio_sync_and_duration_stay_within_tolerance(self):
        # The remuxer accepts about 21 ms of additional decoded audio; sync
        # offsets must stay far below the 50 ms tolerance for this fixture.
        tolerance = 0.05
        output = self.temp_path / 'out.mp4'
        remux.remux_to_mp4(FIXTURES / 'video-only.mp4', FIXTURES / 'audio.m4a', output)

        starts = stream_field(output, 'start_time')
        self.assertEqual(len(starts), 2)
        for start in starts:
            self.assertLess(abs(float(start)), tolerance)

        source_audio = float(stream_field(FIXTURES / 'audio.m4a', 'duration')[0])
        output_audio = float(stream_field(output, 'duration')[1])
        self.assertLess(abs(output_audio - source_audio), tolerance)


if __name__ == '__main__':
    unittest.main()
