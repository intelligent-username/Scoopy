import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from threading import Event
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.extractor.instagram import InstagramIE as _InstagramIE

from . import remux
from .cancellation import OperationCancelled, check_cancel
from .paths import LINKS_FILE
from .session import AppError, is_instagram_domain

WORK_DIR_NAME = '.scoopy-work'
RECOVERY_DIR_NAME = 'recovery'
COMPLETED_VIDEO_DIR_NAME = 'completed_videos'
PHOTO_DIR_NAME = 'photos'
_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
_LEGACY_IMAGE_RE = re.compile(
    r'(?:[0-9]{8}|NA)_Video by .+\.(?i:jpg|jpeg|png|webp)')

# Only Instagram post links are accepted: /p/<code>, /reel/<code>,
# /reels/<code>, where <code> is non-empty and contains no '/', '?' or '#'.
_POST_PATH_RE = re.compile(r'^/(?:p|reel|reels)/[^/?#]+')

# Invisible characters that ride along from copy/paste and would otherwise
# silently break URL parsing (BOM, zero-width marks, word joiner).
_INVISIBLE_TABLE = str.maketrans('', '', '\ufeff\u200b\u200c\u200d\u2060')

# Protocols yt-dlp downloads natively (no external tool). Formats with other
# protocols are skipped rather than handed to FFmpeg.
_EXTERNAL_TOOL_PROTOCOLS = ('rtmp', 'rtmpe', 'rtmps', 'rtmpt', 'rtmpte', 'rtmpts', 'mms', 'f4m')

@dataclass
class DownloadRetryState:
    """Session-only failed/pending posts and saved carousel entries for one folder."""

    directory: str = ''
    failed_urls: list[str] = field(default_factory=list)
    completed_entries: dict[str, set[str]] = field(default_factory=dict)
    remaining_urls: list[str] = field(default_factory=list)


class InstagramIE(_InstagramIE):
    """Retain confirmed product photos that the video extractor omits.

    This small adapter deliberately uses the same extractor key to replace the
    built-in Instagram extractor. Only explicit media_type=1 qualifies; video
    thumbnails must never become photo downloads.
    """

    def _extract_product_media(self, product_media):
        if (product_media.get('media_type') != 1
                or product_media.get('is_video') is True
                or any(product_media.get(key) for key in
                       ('video_versions', 'video_dash_manifest', 'video_url'))):
            return super()._extract_product_media(product_media)
        candidates = []
        for image in (product_media.get('image_versions2') or {}).get('candidates') or []:
            try:
                parsed = urlsplit(image.get('url') or '')
                host = parsed.hostname or ''
                if (parsed.scheme != 'https' or parsed.username is not None
                        or parsed.password is not None or parsed.port is not None
                        or not any(host == domain or host.endswith('.' + domain)
                                   for domain in ('instagram.com', 'cdninstagram.com', 'fbcdn.net'))):
                    continue
            except (AttributeError, ValueError):
                continue
            candidates.append(image)
        if not candidates:
            raise yt_dlp.utils.ExtractorError(
                "Instagram did not provide a supported photo URL.", expected=True)
        image = max(candidates, key=lambda item:
                    (item.get('width') or 0) * (item.get('height') or 0))
        extension = os.path.splitext(urlsplit(image['url']).path)[1].lower().lstrip('.')
        return {
            'id': str(product_media.get('code') or product_media.get('pk') or 'photo'),
            '_scoopy_photo': True,
            'formats': [{
                'format_id': 'scoopy-photo', 'url': image['url'],
                'ext': extension if extension in ('jpg', 'jpeg', 'png', 'webp') else 'jpg',
                'vcodec': 'images', 'acodec': 'none',
                'width': image.get('width'), 'height': image.get('height'),
                'http_headers': {'Referer': 'https://www.instagram.com/'},
            }],
        }


def start_download(download_dir, cookies, links_file=LINKS_FILE, *,
                   retry_state: DownloadRetryState | None = None, retry_failed: bool = False,
                   continue_remaining: bool = False, cancel_event: Event | None = None):
    """
    Downloads photos and videos from each linked post. Prefers a verified combined MP4. Separate
    video/audio streams are combined with pymp4v2 (no FFmpeg). Videos without
    an audio track are saved and reported as silent. Failed media is kept in
    a recovery folder and the batch continues.
    """
    directory = os.path.normcase(os.path.abspath(download_dir))
    if retry_failed and continue_remaining:
        raise AppError("Choose either retry failed or continue remaining, not both.")
    selected_mode = retry_failed or continue_remaining
    if selected_mode:
        selected = (retry_state.failed_urls if retry_failed else retry_state.remaining_urls
                    ) if retry_state is not None else []
        if (retry_state is None or not selected
                or retry_state.directory != directory):
            raise AppError("There are no posts in the selected queue for this download folder.")
        lines = list(enumerate(tuple(selected), 1))
        print(f"Processing only {len(lines)} queued post(s).")
    else:
        print(f"📄 Reading links from {links_file}...")
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
    state = retry_state if retry_state is not None else DownloadRetryState()
    if not selected_mode:
        state.completed_entries.clear()
        state.failed_urls.clear()
        state.remaining_urls.clear()
    state.directory = directory
    state.failed_urls = [url for url in state.failed_urls if url not in urls]
    state.remaining_urls = list(dict.fromkeys([*state.remaining_urls, *urls]))

    print(f"📥 Found {len(urls)} links to download.")
    summary = {'downloaded': 0, 'silent': 0, 'failed': []}
    counter = iter(range(1, 10 ** 9))

    try:
        for url in urls:
            check_cancel(cancel_event)
            failures_before = len(summary['failed'])
            try:
                options = {'cancel_event': cancel_event} if cancel_event is not None else {}
                if retry_state is not None:
                    options['completed_entries'] = state.completed_entries.setdefault(url, set())
                _process_url(url, download_dir, work_root, cookies, summary, counter, **options)
            except OperationCancelled:
                raise
            except yt_dlp.utils.DownloadError:
                check_cancel(cancel_event)
                summary['failed'].append(
                    f"{url}: yt-dlp could not download this post (check Instagram login and rate limits).")
                print(f"⚠️ Failed: {url}. Media was not downloaded. Continuing.")
            except remux.RemuxError as error:
                check_cancel(cancel_event)
                summary['failed'].append(f"{url}: {error}")
                print(f"⚠️ Failed: {url}. {error} Continuing.")
            except Exception:
                check_cancel(cancel_event)
                raise
            state.remaining_urls.remove(url)
            if len(summary['failed']) != failures_before:
                state.failed_urls.append(url)
            check_cancel(cancel_event)
    except OperationCancelled:
        saved = summary['downloaded'] + summary.get('photos', 0)
        raise OperationCancelled(
            f"Download cancelled — {saved} saved, {len(state.failed_urls)} failed, "
            f"{len(state.remaining_urls)} remaining") from None
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    videos = "1 video" if summary['downloaded'] == 1 else f"{summary['downloaded']} videos"
    silent = f" ({summary['silent']} without audio)" if summary['silent'] else ""
    print(f"📥 Downloaded {videos}{silent}.")
    if summary.get('photos'):
        print(f"Downloaded {summary['photos']} photos.")

    if summary['failed']:
        details = '\n'.join(f"- {item}" for item in summary['failed'])
        raise AppError(
            f"{len(summary['failed'])} download(s) failed. Any downloaded components are in the "
            f"'{RECOVERY_DIR_NAME}' folder inside the download folder. See the debug log:\n{details}\n"
            "Format or container failures are not necessarily login problems. "
            "If Instagram reports an access error, check login, post permissions and rate limits.")

    print("✅ Download process finished.")
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


def _process_url(url, download_dir, work_root, cookies, summary, counter, *,
                 completed_entries: set[str] | None = None, cancel_event: Event | None = None):
    check_cancel(cancel_event)
    options = {'cancel_event': cancel_event} if cancel_event is not None else {}
    with yt_dlp.YoutubeDL(_base_options(work_root, **options)) as ydl:
        ydl.add_info_extractor(InstagramIE())
        for cookie in cookies:
            ydl.cookiejar.set_cookie(cookie)
        check_cancel(cancel_event)
        info = ydl.extract_info(url, download=False)
    check_cancel(cancel_event)
    entries = [entry for entry in (info.get('entries') or [info]) if entry] if info else []
    if not entries:
        raise remux.RemuxError("No media entries found for this post.")
    for position, entry in enumerate(entries):
        check_cancel(cancel_event)
        entry_key = str(entry.get('id') or f'entry-position-{position}')
        if completed_entries is not None and entry_key in completed_entries:
            print("Skipping an item already saved from this post.")
            continue
        failures_before = len(summary['failed'])
        _process_entry(entry, download_dir, work_root, cookies, summary, next(counter), **options)
        if completed_entries is not None and len(summary['failed']) == failures_before:
            completed_entries.add(entry_key)
        check_cancel(cancel_event)


def _process_entry(entry, download_dir, work_root, cookies, summary, index, *,
                   cancel_event: Event | None = None):
    entry_id = str(entry.get('id') or index)
    work_dir = os.path.join(work_root, f'entry-{index:04d}')
    os.makedirs(work_dir, exist_ok=True)
    try:
        check_cancel(cancel_event)
        options = {'cancel_event': cancel_event} if cancel_event is not None else {}
        path, silent = _download_entry(entry, download_dir, work_dir, cookies, index, **options)
    except OperationCancelled:
        _keep_for_recovery(work_dir, download_dir)
        raise
    except Exception as error:
        if cancel_event is not None and cancel_event.is_set():
            _keep_for_recovery(work_dir, download_dir)
            check_cancel(cancel_event)
        reason = _safe_reason(error)
        _keep_for_recovery(work_dir, download_dir)
        summary['failed'].append(f"Post {entry_id}: {reason}")
        print(f"⚠️ Post {entry_id} failed: {reason} "
              f"Any downloaded components were kept in '{RECOVERY_DIR_NAME}/'.")
        return
    if entry.get('_scoopy_photo'):
        summary['photos'] = summary.get('photos', 0) + 1
        print(f"Saved photo: {os.path.basename(path)}")
        return
    summary['downloaded'] += 1
    if silent:
        summary['silent'] += 1
        print(f"✅ Saved {os.path.basename(path)} (no audio track available; silent video)")
    else:
        print(f"✅ Saved {os.path.basename(path)}")


def _download_entry(entry, download_dir, work_dir, cookies, index, *,
                    cancel_event: Event | None = None):
    """Download one post entry and return (published_path, silent)."""
    check_cancel(cancel_event)
    options = {'cancel_event': cancel_event} if cancel_event is not None else {}
    if entry.get('_scoopy_photo'):
        path = _single_file(_download_formats(entry, 'scoopy-photo', work_dir, cookies, **options))
        with open(path, 'rb') as image:
            header = image.read(16)
        if header.startswith(b'\xff\xd8\xff'):
            extension = '.jpg'
        elif header.startswith(b'\x89PNG\r\n\x1a\n'):
            extension = '.png'
        elif header.startswith(b'RIFF') and header[8:12] == b'WEBP':
            extension = '.webp'
        else:
            raise remux.RemuxError("The downloaded photo is not a supported image file.")
        return _publish(path, download_dir, extension=extension, **options), False
    combined, video, audio = _select_formats(entry.get('formats') or [])

    if combined is not None:
        path = _single_file(_download_formats(entry, combined['format_id'], work_dir, cookies, **options))
        check_cancel(cancel_event)
        info = remux.validate_video_stream(path)
        if _readable_audio(info):
            return _publish(path, download_dir, **options), False
        video_path = path
    else:
        if video is None:
            raise remux.RemuxError(
                "No compatible MP4 format was found. The post may expose only "
                "unsupported codecs or fragmented streams.")
        video_path = _single_file(_download_formats(entry, video['format_id'], work_dir, cookies, **options))
        check_cancel(cancel_event)
        remux.validate_video_stream(video_path)

    if audio is None:
        # No audio format exists, but missing format metadata is not proof of
        # silence; inspect the actual file before calling it a silent video.
        has_audio = _readable_audio(remux.inspect_media(video_path))
        return _publish(video_path, download_dir, **options), not has_audio

    audio_path = _single_file(_download_formats(
        entry, audio['format_id'], os.path.join(work_dir, 'audio'), cookies, **options))
    check_cancel(cancel_event)
    remux.validate_audio_stream(audio_path)
    combined_path = os.path.join(work_dir, f'scoopy-combined-{index}.mp4')
    check_cancel(cancel_event)
    remux.remux_to_mp4(video_path, audio_path, combined_path, **options)
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    return _publish(combined_path, download_dir, base_name, **options), False


def _readable_audio(info):
    """True when the inspected MP4 has a usable, non-encrypted audio track."""
    audio = info['audio']
    return bool(audio and not audio['encrypted'] and not audio['fragmented'] and audio['samples'])


def _select_formats(formats):
    """Choose formats without ever selecting an arbitrary '/best' fallback.

    Direct MP4 metadata can omit codecs. Inspect those files instead of rejecting
    them or preferring DASH fragments that the native muxer cannot process.
    """
    combined = _best(formats, lambda f: f.get('ext') == 'mp4'
                      and ((_is_video_codec(f) and _is_aac(f))
                           or (_is_progressive(f)
                               and (not f.get('vcodec') or _is_video_codec(f))
                               and (not f.get('acodec') or _is_aac(f)))))
    video = _best(formats, lambda f: f.get('ext') == 'mp4'
                   and (_is_video_codec(f) or (_is_progressive(f) and not f.get('vcodec')))
                   and f.get('acodec') == 'none')
    audio = _best(formats, lambda f: f.get('ext') in ('m4a', 'mp4')
                   and (_is_aac(f) or (_is_progressive(f) and not f.get('acodec')))
                   and f.get('vcodec') == 'none')
    return combined, video, audio


def _best(formats, matches):
    """yt-dlp sorts worst-to-best; prefer progressive files, then known H.264."""
    candidates = [f for f in reversed(formats)
                  if matches(f) and not _needs_external_tool(f) and not f.get('has_drm')]
    candidates.sort(key=lambda f: (
        not _is_progressive(f),
        not str(f.get('vcodec', '')).startswith('avc')))
    return candidates[0] if candidates else None


def _is_progressive(fmt):
    """Identify direct HTTP files, not DASH/HLS manifests or fragment lists."""
    return (fmt.get('protocol') in ('http', 'https')
            and not fmt.get('fragments') and not fmt.get('manifest_url')
            and not str(fmt.get('format_id', '')).lower().startswith(('dash', 'hls')))


def _is_video_codec(fmt):
    vcodec = fmt.get('vcodec')
    return vcodec and vcodec != 'none' and str(vcodec).startswith('avc')


def _is_aac(fmt):
    acodec = fmt.get('acodec')
    return acodec and acodec != 'none' and str(acodec).startswith('mp4a')


def _needs_external_tool(fmt):
    protocol = fmt.get('protocol') or ''
    return any(protocol.startswith(name) for name in _EXTERNAL_TOOL_PROTOCOLS)


def _base_options(out_dir, *, cancel_event: Event | None = None):
    options = {
        'outtmpl': os.path.join(out_dir, '%(upload_date)s_%(title)s.%(ext)s'),
        'ignoreerrors': False,
        'continuedl': True,
        'overwrites': False,
        'fixup': 'never',
        'logtostderr': True,
        'cachedir': False,
        'writethumbnail': False,
        'write_all_thumbnails': False,
        'socket_timeout': 15,
    }
    if cancel_event is not None:
        options['progress_hooks'] = [lambda data: check_cancel(cancel_event)]
    return options


def _download_formats(entry, format_id, work_dir, cookies, *, cancel_event: Event | None = None):
    """Download exactly one format into work_dir; return the finished files."""
    check_cancel(cancel_event)
    files = []

    def collect(d):
        check_cancel(cancel_event)
        if d.get('status') == 'finished':
            files.append(d['filename'])

    options = _base_options(work_dir, **(
        {'cancel_event': cancel_event} if cancel_event is not None else {}))
    options['format'] = format_id
    options['progress_hooks'] = options.get('progress_hooks', []) + [print_progress, collect]
    print(f"  📥 Downloading format {format_id}...")
    with yt_dlp.YoutubeDL(options) as ydl:
        # No cookiefile/cookiesfrombrowser: yt-dlp will not persist this jar.
        for cookie in cookies:
            ydl.cookiejar.set_cookie(cookie)
        # Drop the stale selection from the inspection pass so the new
        # format choice is honored on download.
        clean_entry = {key: value for key, value in entry.items()
                       if key not in ('requested_formats', 'requested_downloads', 'format')}
        if clean_entry.get('formats'):
            for key in ('url', 'format_id', 'ext', 'protocol', 'vcodec', 'acodec',
                        'fragments', 'fragment_base_url', 'manifest_url'):
                clean_entry.pop(key, None)
        check_cancel(cancel_event)
        ydl.process_ie_result(clean_entry, download=True)
    check_cancel(cancel_event)
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


def _publish(path, download_dir, base_name=None, *, extension='.mp4',
             cancel_event: Event | None = None):
    """Publish finished media in its protected folder without replacing files."""
    check_cancel(cancel_event)
    extension = extension.lower()
    if extension == '.mp4':
        directory_name = COMPLETED_VIDEO_DIR_NAME
    elif extension in _IMAGE_EXTENSIONS:
        directory_name = PHOTO_DIR_NAME
    else:
        raise AppError("Unsupported completed media extension.")
    destination = _reserved_directory(download_dir, directory_name)
    os.makedirs(destination, exist_ok=True)
    destination = _reserved_directory(download_dir, directory_name)
    if base_name is None:
        base_name = os.path.splitext(os.path.basename(path))[0]
    base = _sanitize_name(base_name) or 'media'
    candidate = os.path.join(destination, f'{base}{extension}')
    number = 2
    while os.path.lexists(candidate):
        candidate = os.path.join(destination, f'{base} ({number}){extension}')
        number += 1
    check_cancel(cancel_event)
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


def _is_redirected(info: os.stat_result) -> bool:
    """Recognize both POSIX links and Windows junction/reparse points."""
    return bool(stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _safe_download_root(download_dir: str) -> str:
    """Reject redirected roots and ancestors before any scan, write or cleanup."""
    if not os.fspath(download_dir).strip():
        raise AppError("Choose a download folder first.")
    root = os.path.abspath(download_dir)
    current = root
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        else:
            if _is_redirected(info):
                raise AppError("The download folder contains a link. Choose a regular folder.")
            if not stat.S_ISDIR(info.st_mode):
                raise AppError("The download path is not a folder.")
        parent = os.path.dirname(current)
        if parent == current:
            return root
        current = parent


def _reserved_directory(download_dir: str, directory_name: str) -> str:
    """Resolve one reserved child without following a linked destination."""
    path = os.path.join(_safe_download_root(download_dir), directory_name)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return path
    if _is_redirected(info):
        raise AppError(f"The {directory_name} folder is a link. Use a regular folder.")
    if not stat.S_ISDIR(info.st_mode):
        raise AppError(f"The {directory_name} path is not a folder.")
    return path


def _recovery_directory(download_dir: str) -> str:
    """Resolve only the reserved recovery child, rejecting redirected roots."""
    return _reserved_directory(download_dir, RECOVERY_DIR_NAME)


def _is_legacy_image(path: str, root: str) -> bool:
    """Check a captured absolute path's scope and current nonredirected file type."""
    if (not os.path.isabs(path) or os.path.dirname(path) != root
            or not _LEGACY_IMAGE_RE.fullmatch(os.path.basename(path))):
        return False
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not _is_redirected(info)


def legacy_image_candidates(download_dir: str) -> list[str]:
    """List safe old app-named loose images; filenames do not prove failure."""
    try:
        root = _safe_download_root(download_dir)
    except AppError:
        return []
    try:
        with os.scandir(root) as entries:
            return sorted(entry.path for entry in entries
                          if _is_legacy_image(entry.path, root))
    except FileNotFoundError:
        return []


def has_failed_media(download_dir: str) -> bool:
    """Return whether recovery or confirmable legacy loose images are present."""
    try:
        path = _recovery_directory(download_dir)
        with os.scandir(path) as entries:
            if next(entries, None) is not None:
                return True
    except (OSError, AppError):
        pass
    try:
        return bool(legacy_image_candidates(download_dir))
    except OSError:
        return False


def remove_failed_media(download_dir: str, *, loose_images: tuple[str, ...] = ()) -> None:
    """Remove recovery and only explicitly captured, still-safe legacy images."""
    try:
        path = _recovery_directory(download_dir)
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        for image in loose_images:
            root = _safe_download_root(download_dir)
            if _is_legacy_image(image, root):
                try:
                    os.unlink(image)
                except FileNotFoundError:
                    pass
    except OSError as error:
        raise AppError(
            "Could not remove all failed media. Close files open in other apps, "
            "check folder permissions, and try again.") from error


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
