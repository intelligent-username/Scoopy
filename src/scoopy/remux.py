"""Combine separate MP4 video and M4A AAC audio without external tools.

Uses pymp4v2 to copy AAC audio samples into a copy of a video-only MP4.
No FFmpeg, transcoding, or audio decoding is involved: the audio samples
are copied bit-for-bit. Observed behavior with synthetic fixtures: decoded
video frames match the source exactly, while decoded audio is about 21 ms
longer than the source (priming/edit-list differences). Synchronization is
preserved within that tolerance for the tested fixtures.
"""
import os
import shutil
from threading import Event

from pymp4v2 import raw

from .cancellation import OperationCancelled, check_cancel

# Keep mp4v2 diagnostics out of the app's debug log; failures are reported
# as RemuxError with a safe message instead.
raw.MP4LogSetLevel(raw.MP4LogLevel.MP4_LOG_NONE)


class RemuxError(Exception):
    """The streams cannot be combined safely. Downloaded components are kept."""


# Sample entry four-ccs this remuxer is proven to handle. Instagram MP4
# video is H.264 (avc1) and its audio is AAC (mp4a). Other entries are kept
# unrecombined in the recovery folder rather than guessed at.
SUPPORTED_VIDEO_ENTRY = 'avc1'
SUPPORTED_AUDIO_ENTRY = 'mp4a'


def _track_ids(media, track_type):
    count = raw.MP4GetNumberOfTracks(media, track_type)
    return [raw.MP4FindTrackId(media, index, track_type) for index in range(count)]


def _reject(label, path, reason):
    raise RemuxError(f"Cannot combine {label} stream {os.path.basename(path)}: {reason}")


def inspect_media(path):
    """Report the tracks of an MP4 file without modifying it.

    Returns a dict with 'video' and 'audio' entries (or None). Raises
    RemuxError when the file cannot be opened as an MP4 container.
    """
    path = str(path)
    info = {'path': path, 'video': None, 'audio': None}
    try:
        with raw.MP4Read(path) as media:
            for kind, track_type in (('video', raw.MP4_VIDEO_TRACK_TYPE),
                                     ('audio', raw.MP4_AUDIO_TRACK_TYPE)):
                ids = _track_ids(media, track_type)
                if len(ids) != 1:
                    info[kind] = None
                    continue
                track_id = ids[0]
                samples = raw.MP4GetTrackNumberOfSamples(media, track_id)
                entry = raw.MP4GetTrackMediaDataName(media, track_id)
                details = {
                    'entry': entry,
                    'samples': samples,
                    'encrypted': entry in ('encv', 'enca'),
                    'fragmented': samples == 0,
                }
                if kind == 'audio':
                    try:
                        details['channels'] = raw.MP4GetTrackAudioChannels(media, track_id)
                    except raw.MP4Error:
                        details['channels'] = 0
                info[kind] = details
    except raw.MP4Error:
        raise RemuxError(
            f"{os.path.basename(path)} is not a readable MP4 file.") from None
    return info


def validate_video_stream(path):
    """Require a single unencrypted, non-fragmented H.264 video track."""
    path = str(path)
    info = inspect_media(path)
    video = info['video']
    if video is None:
        _reject('video', path, 'expected exactly one video track.')
    if video['encrypted']:
        _reject('video', path, 'the video track is encrypted.')
    if video['fragmented']:
        _reject('video', path, 'the video track has no readable samples (fragmented or empty).')
    if video['entry'] != SUPPORTED_VIDEO_ENTRY:
        _reject('video', path, f"unsupported video codec '{video['entry']}' (need {SUPPORTED_VIDEO_ENTRY}).")
    return info


def validate_audio_stream(path):
    """Require a single unencrypted AAC audio track with a usable configuration."""
    path = str(path)
    info = inspect_media(path)
    audio = info['audio']
    if audio is None:
        _reject('audio', path, 'expected exactly one audio track.')
    if audio['encrypted']:
        _reject('audio', path, 'the audio track is encrypted.')
    if audio['fragmented']:
        _reject('audio', path, 'the audio track has no readable samples (fragmented or empty).')
    if audio['entry'] != SUPPORTED_AUDIO_ENTRY:
        _reject('audio', path, f"unsupported audio codec '{audio['entry']}' (need {SUPPORTED_AUDIO_ENTRY}).")
    if not audio['channels']:
        _reject('audio', path, 'the audio track has no channel information.')
    return info


def _copy_language(audio, audio_track, output, output_track, *,
                   cancel_event: Event | None = None):
    """Copy the audio track language when it is known; never guess."""
    try:
        check_cancel(cancel_event)
        language = raw.MP4GetTrackLanguage(audio, audio_track)
    except raw.MP4Error:
        return  # Leave the output's default (undefined) language untouched.
    if language and len(language) == 3:
        check_cancel(cancel_event)
        raw.MP4SetTrackLanguage(output, output_track, language)


def _mux(audio_path, temporary_output, *, cancel_event: Event | None = None):
    """Copy the audio track of audio_path into temporary_output (a video MP4)."""
    check_cancel(cancel_event)
    with raw.MP4Read(str(audio_path)) as audio:
        check_cancel(cancel_event)
        with raw.MP4Modify(str(temporary_output)) as output:
            _copy_audio_samples(audio, output, audio_path, cancel_event=cancel_event)
    check_cancel(cancel_event)


def _copy_audio_samples(audio, output, audio_path, *, cancel_event: Event | None = None):
    """Copy AAC configuration and samples at cooperative native-call boundaries."""
    def native(function, *args):
        check_cancel(cancel_event)
        result = function(*args)
        check_cancel(cancel_event)
        return result

    audio_track = native(raw.MP4FindTrackId, audio, 0, raw.MP4_AUDIO_TRACK_TYPE)
    sample_duration = native(raw.MP4GetTrackFixedSampleDuration, audio, audio_track)
    timescale = native(raw.MP4GetTrackTimeScale, audio, audio_track)
    output_track = native(raw.MP4AddAudioTrack, output, timescale, sample_duration)
    configuration = native(raw.MP4GetTrackESConfiguration, audio, audio_track)
    if not configuration:
        raise RemuxError(
            f"Cannot combine audio stream {os.path.basename(audio_path)}: "
            "the AAC configuration is missing.")
    native(raw.MP4SetTrackESConfiguration, output, output_track, configuration)
    check_cancel(cancel_event)
    _copy_language(audio, audio_track, output, output_track, cancel_event=cancel_event)
    sample_count = native(raw.MP4GetTrackNumberOfSamples, audio, audio_track)
    for sample_id in range(1, sample_count + 1):
        native(raw.MP4CopySample, audio, audio_track, sample_id, output, output_track)


def _validate_output(path, require_audio):
    info = inspect_media(path)
    if info['video'] is None or info['video']['samples'] == 0:
        raise RemuxError("The combined file has no readable video track.")
    if require_audio:
        audio = info['audio']
        if audio is None or audio['samples'] == 0:
            raise RemuxError("The combined file has no readable audio track.")


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


def remux_to_mp4(video_path, audio_path, temporary_output, *,
                 cancel_event: Event | None = None):
    """Combine a separate MP4 video and M4A audio into temporary_output.

    temporary_output is an app-owned work path; it is removed if the merge
    fails. Source files are never modified or deleted. Raises RemuxError
    when either stream is unsupported or the output does not validate.
    """
    video_path, audio_path, temporary_output = (
        str(video_path), str(audio_path), str(temporary_output))
    try:
        check_cancel(cancel_event)
        validate_video_stream(video_path)
        check_cancel(cancel_event)
        validate_audio_stream(audio_path)
        check_cancel(cancel_event)
        shutil.copyfile(video_path, temporary_output)
        check_cancel(cancel_event)
        if cancel_event is None:
            _mux(audio_path, temporary_output)
        else:
            _mux(audio_path, temporary_output, cancel_event=cancel_event)
        check_cancel(cancel_event)
        _validate_output(temporary_output, require_audio=True)
        check_cancel(cancel_event)
    except (OperationCancelled, RemuxError):
        _discard(temporary_output)
        raise
    except raw.MP4Error:
        _discard(temporary_output)
        raise RemuxError(
            "Could not combine the video and audio streams.") from None
    except OSError:
        _discard(temporary_output)
        raise RemuxError(
            "Could not write the combined video file. Check disk space and permissions.") from None
    return temporary_output


def combined_mp4_has_audio(path):
    """True when a finished MP4 contains a readable audio track."""
    info = inspect_media(path)
    return bool(info['audio'] and info['audio']['samples'])
