import os
import re
import shutil
from urllib.parse import urlsplit

import yt_dlp

from . import remux
from .paths import LINKS_FILE
from .session import AppError, is_instagram_domain

WORK_DIR_NAME = '.scoopy-work'
RECOVERY_DIR_NAME = 'recovery'

# Only Instagram post links are accepted: /p/<code>, /reel/<code>,
# /reels/<code>, where <code> is non-empty and contains no '/', '?' or '#'.
_POST_PATH_RE = re.compile(r'^/(?:p|reel|reels)/[^/?#]+')

# Invisible characters that ride along from copy/paste and would otherwise
# silently break URL parsing (BOM, zero-width marks, word joiner).
_INVISIBLE_TABLE = str.maketrans('', '', '\ufeff\u200b\u200c\u200d\u2060')

# Protocols yt-dlp downloads natively (no external tool). Formats with other
# protocols are skipped rather than handed to FFmpeg.
_EXTERNAL_TOOL_PROTOCOLS = ('rtmp', 'rtmpe', 'rtmps', 'rtmpt', 'rtmpte', 'rtmpts', 'mms', 'f4m')


def start_download(download_dir, cookies, links_file=LINKS_FILE):
    """
    Downloads each linked post, preferring a verified combined MP4. Separate
    video/audio streams are combined with pymp4v2 (no FFmpeg). Videos without
    an audio track are saved and reported as silent. Failed media is kept in
    a recovery folder and the batch continues.
    """
    print(f"Reading links from {links_file}...")
    lines = _read_link_lines(links_file)
    if not lines:
        raise AppError("No links found. Scrape your Instagram collection first.")

    urls, dropped = _collect_post_links(lines)
    if dropped:
        # Reasons are static categories keyed by line number; the URL text is
        # never echoed so session material in a hand-pasted link cannot leak.
        report = '\n'.join(f"  line {number}: {reason}" for number, reason in dropped)
        print(f"⚠️ Dropped {len(dropped)} of {len(lines)} link(s) from the links file "
              f"(only line numbers and reasons are shown):\n{report}")
    if not urls:
        raise AppError(
            f"None of the {len(lines)} link(s) in the links file is a valid Instagram "
            "post link. Scrape your Instagram collection again; the reasons each line "
            "was rejected are listed above in the log.")

    os.makedirs(download_dir, exist_ok=True)
    work_root = os.path.join(download_dir, WORK_DIR_NAME)

    print(f"Found {len(urls)} links to download.")
    summary = {'downloaded': 0, 'silent': 0, 'failed': []}
    counter = iter(range(1, 10 ** 9))

    try:
        for url in urls:
            try:
                _process_url(url, download_dir, work_root, cookies, summary, counter)
            except yt_dlp.utils.DownloadError:
                summary['failed'].append(
                    f"{url}: yt-dlp could not download this post (check Instagram login and rate limits).")
                print(f"⚠️ Failed: {url}. Media was not downloaded. Continuing.")
            except remux.RemuxError as error:
                summary['failed'].append(f"{url}: {error}")
                print(f"⚠️ Failed: {url}. {error} Continuing.")
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    print(f"Downloaded {summary['downloaded']} videos ({summary['silent']} without audio).")

    if summary['failed']:
        details = '\n'.join(f"- {item}" for item in summary['failed'])
        raise AppError(
            f"{len(summary['failed'])} download(s) failed. Their downloaded media was kept in the "
            f"'{RECOVERY_DIR_NAME}' folder inside the download folder. See the debug log:\n{details}\n"
            "If Instagram requires login, log in again in the app's browser and retry. "
            "Private posts also require access from that account; Instagram may rate-limit downloads.")

    print("Download process finished.")
    return summary


def _read_link_lines(links_file):
    """Read the links file as cleaned, numbered, non-empty lines.

    A UTF-8 BOM and CRLF line endings are tolerated, and undecodable bytes
    become replacement characters that later fail URL validation instead of
    crashing the run. Missing or unreadable files raise an actionable error
    that says which state the file is in.
    """
    try:
        with open(links_file, 'r', encoding='utf-8-sig', errors='replace') as f:
            raw_lines = f.read().splitlines()
    except FileNotFoundError:
        raise AppError(
            f"The links file {links_file} does not exist. Scrape your Instagram "
            "collection first to create it.") from None
    except OSError as error:
        raise AppError(f"Could not read the links file {links_file}: {error}") from None
    return [(number, cleaned)
            for number, raw in enumerate(raw_lines, 1)
            if (cleaned := raw.translate(_INVISIBLE_TABLE).strip())]


def _collect_post_links(lines):
    """Validate numbered lines; return (valid_urls, dropped) preserving order.

    Only https:// Instagram post links (/p/, /reel/, /reels/) without embedded
    credentials, query strings or fragments are kept, so tracking parameters
    cannot vary between copies of the same post and dedupe stays exact.
    Duplicate lines collapse to their first occurrence and are reported.
    """
    urls, dropped, seen = [], [], set()
    for number, raw in lines:
        url, reason = _validate_post_url(raw)
        if reason is not None:
            dropped.append((number, reason))
        elif url in seen:
            dropped.append((number, 'duplicate of an earlier link in the file'))
        else:
            seen.add(url)
            urls.append(url)
    return urls, dropped


def _validate_post_url(raw):
    """Return (normalized_url, None) for a post link, else (None, reason).

    The reason is a static category string; it never contains the URL text so
    cookie/session material inside a malformed link cannot reach the log.
    """
    try:
        parsed = urlsplit(raw)
        normalized = parsed._replace(query='', fragment='').geturl()
    except ValueError:
        return None, 'not a parsable URL'
    if parsed.scheme != 'https':
        return None, 'not an https:// link'
    if not parsed.hostname:
        return None, 'not a valid URL (no host)'
    if parsed.username is not None or parsed.password is not None:
        return None, 'contains embedded login credentials'
    if not is_instagram_domain(parsed.hostname):
        return None, 'not an Instagram link'
    if not _POST_PATH_RE.match(parsed.path):
        return None, 'not a post link (only /p/, /reel/ and /reels/ links are supported)'
    return normalized, None


def _process_url(url, download_dir, work_root, cookies, summary, counter):
    with yt_dlp.YoutubeDL(_base_options(work_root)) as ydl:
        for cookie in cookies:
            ydl.cookiejar.set_cookie(cookie)
        info = ydl.extract_info(url, download=False)
    entries = [entry for entry in (info.get('entries') or [info]) if entry] if info else []
    if not entries:
        raise remux.RemuxError("No media entries found for this post.")
    for entry in entries:
        _process_entry(entry, download_dir, work_root, cookies, summary, next(counter))


def _process_entry(entry, download_dir, work_root, cookies, summary, index):
    entry_id = str(entry.get('id') or index)
    work_dir = os.path.join(work_root, f'entry-{index:04d}')
    os.makedirs(work_dir, exist_ok=True)
    try:
        path, silent = _download_entry(entry, download_dir, work_dir, cookies, index)
    except Exception as error:
        reason = _safe_reason(error)
        _keep_for_recovery(work_dir, download_dir)
        summary['failed'].append(f"Post {entry_id}: {reason}")
        print(f"⚠️ Post {entry_id} failed: {reason} Downloaded media was kept in '{RECOVERY_DIR_NAME}/'.")
        return
    summary['downloaded'] += 1
    if silent:
        summary['silent'] += 1
        print(f"✅ Saved {os.path.basename(path)} (no audio track available; silent video)")
    else:
        print(f"✅ Saved {os.path.basename(path)}")


def _download_entry(entry, download_dir, work_dir, cookies, index):
    """Download one post entry and return (published_path, silent)."""
    combined, video, audio = _select_formats(entry.get('formats') or [])

    if combined is not None:
        path = _single_file(_download_formats(entry, combined['format_id'], work_dir, cookies))
        remux.validate_video_stream(path)
        has_audio = _readable_audio(remux.inspect_media(path))
        return _publish(path, download_dir), not has_audio

    if video is None:
        raise remux.RemuxError("No downloadable MP4 video format was found for this post.")

    video_path = _single_file(_download_formats(entry, video['format_id'], work_dir, cookies))
    remux.validate_video_stream(video_path)

    if audio is None:
        # No audio format exists, but missing format metadata is not proof of
        # silence; inspect the actual file before calling it a silent video.
        has_audio = _readable_audio(remux.inspect_media(video_path))
        return _publish(video_path, download_dir), not has_audio

    audio_path = _single_file(_download_formats(entry, audio['format_id'], work_dir, cookies))
    remux.validate_audio_stream(audio_path)
    combined_path = os.path.join(work_dir, f'scoopy-combined-{index}.mp4')
    remux.remux_to_mp4(video_path, audio_path, combined_path)
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    return _publish(combined_path, download_dir, base_name), False


def _readable_audio(info):
    """True when the inspected MP4 has a usable, non-encrypted audio track."""
    audio = info['audio']
    return bool(audio and not audio['encrypted'] and not audio['fragmented'] and audio['samples'])


def _select_formats(formats):
    """Choose formats without ever selecting an arbitrary '/best' fallback.

    Returns (combined, video, audio) format dicts, best-first per role.
    """
    combined = _best(formats, lambda f: f.get('ext') == 'mp4'
                     and _is_video_codec(f) and _is_aac(f))
    video = _best(formats, lambda f: f.get('ext') == 'mp4' and _is_video_codec(f)
                  and f.get('acodec') == 'none')
    audio = _best(formats, lambda f: f.get('ext') in ('m4a', 'mp4') and _is_aac(f)
                  and f.get('vcodec') == 'none')
    return combined, video, audio


def _best(formats, matches):
    """yt-dlp lists formats best-first; prefer H.264 video and native protocols."""
    candidates = [f for f in formats if matches(f) and not _needs_external_tool(f)]
    candidates.sort(key=lambda f: 0 if str(f.get('vcodec', '')).startswith('avc') else 1)
    return candidates[0] if candidates else None


def _is_video_codec(fmt):
    vcodec = fmt.get('vcodec')
    return vcodec and vcodec != 'none' and str(vcodec).startswith('avc')


def _is_aac(fmt):
    acodec = fmt.get('acodec')
    return acodec and acodec != 'none' and str(acodec).startswith('mp4a')


def _needs_external_tool(fmt):
    protocol = fmt.get('protocol') or ''
    return any(protocol.startswith(name) for name in _EXTERNAL_TOOL_PROTOCOLS)


def _base_options(out_dir):
    return {
        'outtmpl': os.path.join(out_dir, '%(upload_date)s_%(title)s.%(ext)s'),
        'ignoreerrors': False,
        'continuedl': True,
        'overwrites': False,
        'fixup': 'never',
        'logtostderr': True,
        'cachedir': False,
    }


def _download_formats(entry, format_id, work_dir, cookies):
    """Download exactly one format into work_dir; return the finished files."""
    files = []

    def collect(d):
        if d.get('status') == 'finished':
            files.append(d['filename'])

    options = _base_options(work_dir)
    options['format'] = format_id
    options['progress_hooks'] = [print_progress, collect]
    print(f"  Downloading format {format_id}...")
    with yt_dlp.YoutubeDL(options) as ydl:
        # No cookiefile/cookiesfrombrowser: yt-dlp will not persist this jar.
        for cookie in cookies:
            ydl.cookiejar.set_cookie(cookie)
        # Drop the stale selection from the inspection pass so the new
        # format choice is honored on download.
        clean_entry = {key: value for key, value in entry.items()
                       if key not in ('requested_formats', 'format')}
        ydl.process_ie_result(clean_entry, download=True)
    return [path for path in files if os.path.isfile(path)]


def _single_file(files):
    if len(files) != 1:
        raise remux.RemuxError(
            "Expected a single downloaded file for this stream; keeping components for recovery.")
    return files[0]


def _sanitize_name(name):
    cleaned = ''.join(character for character in str(name)
                      if character.isalnum() or character in ' -_.()')
    return cleaned.strip().strip('.')[:150]


def _publish(path, download_dir, base_name=None):
    """Move a finished file into download_dir under a unique, non-overwriting name."""
    if base_name is None:
        base_name = os.path.splitext(os.path.basename(path))[0]
    base = _sanitize_name(base_name) or 'video'
    candidate = os.path.join(download_dir, f'{base}.mp4')
    number = 2
    while os.path.lexists(candidate):
        candidate = os.path.join(download_dir, f'{base} ({number}).mp4')
        number += 1
    shutil.move(path, candidate)
    return candidate


def _keep_for_recovery(work_dir, download_dir):
    """Preserve downloaded components in recovery/ for manual inspection."""
    if not os.path.isdir(work_dir):
        return
    with os.scandir(work_dir) as entries:
        empty = next(iter(entries), None) is None
    if empty:
        return
    recovery = os.path.join(download_dir, RECOVERY_DIR_NAME)
    os.makedirs(recovery, exist_ok=True)
    destination = os.path.join(recovery, os.path.basename(work_dir))
    number = 2
    while os.path.lexists(destination):
        destination = os.path.join(recovery, f'{os.path.basename(work_dir)} ({number})')
        number += 1
    try:
        shutil.move(work_dir, destination)
    except OSError:
        pass  # Recovery is best-effort; the batch must continue.


def _safe_reason(error):
    """A short, safe failure reason that never includes signed media URLs."""
    if isinstance(error, remux.RemuxError):
        return str(error)
    if isinstance(error, yt_dlp.utils.DownloadError):
        return "yt-dlp could not download this post (check Instagram login and rate limits)."
    return "an unexpected error occurred (see the debug log)."


def print_progress(d):
    """
    Custom progress hook for yt-dlp to print status to stdout.
    """
    if d['status'] == 'downloading':
        filename = "unknown_file"
        if 'filename' in d:
            filename = os.path.basename(d['filename'])

        percent = d.get('_percent_str', '---%')
        speed = d.get('_speed_str', '---B/s')
        eta = d.get('_eta_str', '--:--')

        print(f"  Downloading: {filename} | {percent.strip()} | {speed.strip()} | ETA: {eta.strip()}", end="\r")

    if d['status'] == 'finished':
        filename = os.path.basename(d.get('filename', 'unknown_file'))
        print(f"\n  ✅ Finished: {filename}")
