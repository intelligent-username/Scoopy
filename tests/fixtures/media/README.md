# Synthetic media fixtures

Generated on 2026-11-10 with FFmpeg (developer-only; Scoopy and end users never
need FFmpeg). Synthetic test patterns only — no copyrighted content.

| File | Content | Command |
| --- | --- | --- |
| `video-only.mp4` | 2 s, 320x320, 25 fps H.264 (`avc1`) video, no audio | `ffmpeg -f lavfi -i testsrc2=size=320x320:rate=25:duration=2 -c:v libx264 -pix_fmt yuv420p video-only.mp4` |
| `audio.m4a` | 2 s, 440 Hz sine, AAC (`mp4a`) | `ffmpeg -f lavfi -i sine=frequency=440:duration=2 -c:a aac -b:a 64k audio.m4a` |
| `combined.mp4` | Video and audio streams copied into one MP4 (reference file) | `ffmpeg -i video-only.mp4 -i audio.m4a -c:v copy -c:a copy combined.mp4` |

These fixtures are inputs for `tests/test_remux.py` and
`tests/test_download_pipeline.py`. Regenerate them only with the same
commands and verify the regression tests still pass.
