"""Passphrase-based encryption for Scoopy's browser profile at rest."""

import base64
import hmac
import json
import os
import secrets
import shutil
import stat
import struct
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .session import AppError

PROFILE_FORMAT = 'scoopy-profile'
PROFILE_VERSION = 1
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
CHUNK_BYTES = 1024 * 1024
KEYCHECK_PLAINTEXT = b'scoopy-profile-keycheck-v1'
MIN_PASSPHRASE_LENGTH = 8
LOCK_TIMEOUT_SECONDS = 120.0

META_NAME = 'meta.json'
MANIFEST_NAME = 'manifest.enc'
BLOBS_NAME = 'blobs'
UNLOCK_MARKER_NAME = '.scoopy-profile.unlocked'
TEMP_PREFIX = '.scoopy-profile-tmp-'

EXCLUDED_DIR_NAMES = frozenset({
    'Cache', 'Code Cache', 'GPUCache', 'DawnCache', 'DawnGraphiteCache',
    'DawnWebGPUCache', 'GraphiteDawnCache', 'ShaderCache', 'GrShaderCache',
    'Crashpad', 'Service Worker', 'component_crx_cache', 'extensions_crx_cache',
    'optimization_guide_model_store', 'Segmentation Platform', 'BrowserMetrics',
    'Safe Browsing', 'CertificateRevocation', 'PKIMetadata', 'WidevineCdm',
})
EXCLUDED_FILE_NAMES = frozenset({
    'SingletonLock', 'SingletonCookie', 'SingletonSocket', 'lockfile', 'LOCK', '.DS_Store',
})


class ProfileEncryptionError(AppError):
    """Actionable profile-encryption failure; never contains secret material."""


@dataclass(frozen=True)
class ProfileKey:
    """The derived Fernet key and the salt it was derived from."""
    key: bytes
    salt: bytes


_session_key_lock = threading.Lock()
_session_key: ProfileKey | None = None


def generate_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def derive_key(passphrase: str, salt: bytes) -> bytes:
    if not isinstance(passphrase, str) or not passphrase.strip():
        raise ProfileEncryptionError('Enter a profile passphrase.')
    if not isinstance(salt, bytes) or len(salt) != SALT_BYTES:
        raise ProfileEncryptionError(
            'The encrypted profile is damaged. Disable and re-enable encryption.')
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode('utf-8')))


def _read_meta(enc_dir: str) -> dict:
    try:
        with open(os.path.join(enc_dir, META_NAME), 'r', encoding='utf-8') as meta_file:
            meta = json.load(meta_file)
    except (OSError, ValueError, TypeError):
        raise ProfileEncryptionError(
            'The encrypted profile is missing or damaged. Disable encryption '
            'by deleting the encrypted profile folder, then sign in again.') from None
    if (not isinstance(meta, dict) or meta.get('format') != PROFILE_FORMAT
            or meta.get('version') != PROFILE_VERSION):
        raise ProfileEncryptionError(
            'The encrypted profile has an unsupported format. Update Scoopy.')
    return meta


def _key_from_meta(passphrase: str, meta: dict) -> ProfileKey:
    try:
        salt = base64.b64decode(meta['salt'], validate=True)
        token = meta['keycheck'].encode('ascii')
        if not isinstance(meta['keycheck'], str):
            raise TypeError
    except (KeyError, TypeError, ValueError):
        raise ProfileEncryptionError(
            'The encrypted profile is damaged. Disable and re-enable encryption.') from None
    key = derive_key(passphrase, salt)
    try:
        if not hmac.compare_digest(Fernet(key).decrypt(token), KEYCHECK_PLAINTEXT):
            raise InvalidToken
    except (InvalidToken, ValueError):
        raise ProfileEncryptionError('Incorrect passphrase.') from None
    return ProfileKey(key=key, salt=salt)


def verify_passphrase(passphrase: str, enc_dir: str) -> ProfileKey:
    """Derive and verify the key without touching the plaintext profile."""
    return _key_from_meta(passphrase, _read_meta(enc_dir))


def is_encrypted_profile(enc_dir: str) -> bool:
    try:
        _read_meta(enc_dir)
    except ProfileEncryptionError:
        return False
    return True


def _iter_profile_entries(root: str):
    """Yield (posix_relative_path, stat) for dirs and regular files; skip links."""
    stack = ['']
    while stack:
        relative = stack.pop()
        directory = os.path.join(root, *relative.split('/')) if relative else root
        try:
            with os.scandir(directory) as scan:
                current = sorted(scan, key=lambda item: item.name)
        except OSError as error:
            raise ProfileEncryptionError(
                'Could not read the browser profile. Close the browser and try again.'
            ) from error
        for entry in current:
            if entry.is_symlink():
                continue
            child = f'{relative}/{entry.name}' if relative else entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                if entry.name in EXCLUDED_DIR_NAMES:
                    continue
                yield child, info
                stack.append(child)
            elif stat.S_ISREG(info.st_mode):
                if entry.name in EXCLUDED_FILE_NAMES:
                    continue
                yield child, info


def _write_blob(source_path: str, destination_path: str, fernet: Fernet) -> None:
    index = 0
    with open(source_path, 'rb') as source, open(destination_path, 'wb') as output:
        while True:
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            token = fernet.encrypt(struct.pack('<I', index) + chunk)
            output.write(struct.pack('<I', len(token)))
            output.write(token)
            index += 1
        output.flush()
        os.fsync(output.fileno())


def _read_blob(source_path: str, destination_path: str,
               expected_size: int, fernet: Fernet) -> None:
    index = 0
    written = 0
    with open(source_path, 'rb') as source, open(destination_path, 'wb') as output:
        while True:
            header = source.read(4)
            if not header:
                break
            if len(header) != 4:
                raise ProfileEncryptionError('The encrypted profile is damaged.')
            (length,) = struct.unpack('<I', header)
            if length == 0 or length > CHUNK_BYTES * 2 + 4096:
                raise ProfileEncryptionError('The encrypted profile is damaged.')
            token = source.read(length)
            if len(token) != length:
                raise ProfileEncryptionError('The encrypted profile is damaged.')
            try:
                plaintext = fernet.decrypt(token)
            except InvalidToken:
                raise ProfileEncryptionError('Incorrect passphrase.') from None
            if len(plaintext) < 4:
                raise ProfileEncryptionError('The encrypted profile is damaged.')
            (chunk_index,) = struct.unpack('<I', plaintext[:4])
            if chunk_index != index:
                raise ProfileEncryptionError('The encrypted profile is damaged.')
            output.write(plaintext[4:])
            written += len(plaintext) - 4
            index += 1
        output.flush()
        os.fsync(output.fileno())
    if written != expected_size:
        raise ProfileEncryptionError('The encrypted profile is damaged.')


def _publish_directory(temp_dir: str, destination: str, failure: str) -> None:
    backup = f'{destination}.bak-{secrets.token_hex(4)}'
    moved = False
    try:
        if os.path.exists(destination):
            os.replace(destination, backup)
            moved = True
        os.replace(temp_dir, destination)
    except OSError as error:
        if moved and not os.path.exists(destination):
            try:
                os.replace(backup, destination)
            except OSError:
                pass
        raise ProfileEncryptionError(failure) from error
    finally:
        if moved and os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)


def _encrypt_with_key(profile_key: ProfileKey, plaintext_dir: str,
                      enc_dir: str) -> int:
    fernet = Fernet(profile_key.key)
    parent = os.path.dirname(os.path.abspath(enc_dir))
    os.makedirs(parent, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=parent)
    count = 0
    try:
        blobs_dir = os.path.join(temp_dir, BLOBS_NAME)
        os.makedirs(blobs_dir, exist_ok=True)
        entries = []
        if os.path.isdir(plaintext_dir):
            for relative, info in _iter_profile_entries(plaintext_dir):
                if stat.S_ISDIR(info.st_mode):
                    entries.append({'path': relative, 'type': 'dir'})
                    continue
                blob = f'{count:08d}.bin'
                _write_blob(os.path.join(plaintext_dir, *relative.split('/')),
                            os.path.join(blobs_dir, blob), fernet)
                entries.append({
                    'path': relative, 'type': 'file', 'blob': blob,
                    'size': info.st_size, 'mode': stat.S_IMODE(info.st_mode),
                })
                count += 1
        manifest = {'format': PROFILE_FORMAT, 'version': PROFILE_VERSION,
                    'entries': entries}
        manifest_token = fernet.encrypt(
            json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
        with open(os.path.join(temp_dir, MANIFEST_NAME), 'wb') as output:
            output.write(manifest_token)
            output.flush()
            os.fsync(output.fileno())
        meta = {
            'format': PROFILE_FORMAT,
            'version': PROFILE_VERSION,
            'salt': base64.b64encode(profile_key.salt).decode('ascii'),
            'keycheck': fernet.encrypt(KEYCHECK_PLAINTEXT).decode('ascii'),
            'created': datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(temp_dir, META_NAME), 'w', encoding='utf-8') as output:
            json.dump(meta, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        _publish_directory(
            temp_dir, enc_dir,
            'Could not finish encrypting the browser profile. The plaintext copy '
            'was kept. Close the browser, free disk space, and try again.')
        temp_dir = None
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return count


def _decrypt_with_key(profile_key: ProfileKey, enc_dir: str,
                      plaintext_dir: str) -> None:
    fernet = Fernet(profile_key.key)
    try:
        with open(os.path.join(enc_dir, MANIFEST_NAME), 'rb') as manifest_file:
            manifest = json.loads(fernet.decrypt(manifest_file.read()).decode('utf-8'))
        entries = manifest['entries']
        if not isinstance(entries, list):
            raise TypeError
    except (OSError, InvalidToken, KeyError, TypeError, ValueError):
        raise ProfileEncryptionError('The encrypted profile is damaged.') from None
    parent = os.path.dirname(os.path.abspath(plaintext_dir))
    os.makedirs(parent, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=parent)
    try:
        for entry in entries:
            relative = _safe_relative(entry)
            destination = os.path.join(temp_dir, *relative.split('/'))
            if not os.path.abspath(destination).startswith(
                    os.path.abspath(temp_dir) + os.sep):
                raise ProfileEncryptionError('The encrypted profile is damaged.')
            if entry.get('type') == 'dir':
                os.makedirs(destination, exist_ok=True)
                continue
            if entry.get('type') != 'file' or not isinstance(entry.get('size'), int):
                raise ProfileEncryptionError('The encrypted profile is damaged.')
            blob = entry.get('blob')
            if not isinstance(blob, str) or os.path.basename(blob) != blob:
                raise ProfileEncryptionError('The encrypted profile is damaged.')
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            _read_blob(os.path.join(enc_dir, BLOBS_NAME, blob),
                       destination, entry['size'], fernet)
        _publish_directory(
            temp_dir, plaintext_dir,
            'Could not finish unlocking the browser profile. The encrypted copy '
            'was kept. Close the browser, free disk space, and try again.')
        temp_dir = None
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _safe_relative(entry: dict) -> str:
    path = entry.get('path') if isinstance(entry, dict) else None
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ProfileEncryptionError('The encrypted profile is damaged.')
    if ('\\' in path or path.startswith('/')
            or (len(path) > 1 and path[1] == ':')
            or any(part in ('', '.', '..') for part in path.split('/'))):
        raise ProfileEncryptionError('The encrypted profile is damaged.')
    return path


def _validate_new_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_LENGTH or not passphrase.strip():
        raise ProfileEncryptionError(
            f'Use a passphrase of at least {MIN_PASSPHRASE_LENGTH} characters.')


def _remove_plaintext(plaintext_dir: str) -> None:
    if not os.path.isdir(plaintext_dir):
        return
    try:
        shutil.rmtree(plaintext_dir)
    except OSError:
        print('⚠️ Encrypted the profile, but could not remove the plaintext copy. '
              'Close any app using it and delete the folder manually.')


def enable_encryption(passphrase: str, plaintext_dir: str, enc_dir: str) -> int:
    """Encrypt the profile, publish it, then remove the plaintext copy."""
    _validate_new_passphrase(passphrase)
    if is_encrypted_profile(enc_dir):
        raise ProfileEncryptionError('Profile encryption is already enabled.')
    salt = generate_salt()
    profile_key = ProfileKey(key=derive_key(passphrase, salt), salt=salt)
    count = _encrypt_with_key(profile_key, plaintext_dir, enc_dir)
    _remove_plaintext(plaintext_dir)
    return count


def prepare_profile(passphrase: str, enc_dir: str, plaintext_dir: str,
                    marker_path: str) -> ProfileKey:
    """Verify the passphrase and make a complete plaintext profile available."""
    profile_key = verify_passphrase(passphrase, enc_dir)
    if is_unlocked(marker_path, plaintext_dir):
        return profile_key
    if os.path.isdir(plaintext_dir):
        _remove_plaintext(plaintext_dir)
    _decrypt_with_key(profile_key, enc_dir, plaintext_dir)
    mark_unlocked(marker_path)
    return profile_key


def lock_profile(profile_key: ProfileKey, plaintext_dir: str, enc_dir: str,
                 marker_path: str) -> int:
    """Encrypt an unlocked profile and remove its plaintext copy."""
    if not os.path.isdir(plaintext_dir):
        clear_unlocked(marker_path)
        return 0
    count = _encrypt_with_key(profile_key, plaintext_dir, enc_dir)
    _remove_plaintext(plaintext_dir)
    clear_unlocked(marker_path)
    return count


def disable_encryption(passphrase: str, enc_dir: str, plaintext_dir: str,
                       marker_path: str) -> None:
    """Decrypt if needed, then remove the encrypted container."""
    profile_key = verify_passphrase(passphrase, enc_dir)
    if not is_unlocked(marker_path, plaintext_dir):
        if os.path.isdir(plaintext_dir):
            _remove_plaintext(plaintext_dir)
        _decrypt_with_key(profile_key, enc_dir, plaintext_dir)
    clear_unlocked(marker_path)
    try:
        shutil.rmtree(enc_dir)
    except OSError as error:
        raise ProfileEncryptionError(
            'Could not remove the encrypted profile. Close any app using it and retry.'
        ) from error


def mark_unlocked(marker_path: str) -> None:
    try:
        with open(marker_path, 'w', encoding='utf-8') as output:
            json.dump({'version': PROFILE_VERSION,
                       'created': datetime.now(timezone.utc).isoformat()}, output)
    except OSError:
        pass


def clear_unlocked(marker_path: str) -> None:
    try:
        os.unlink(marker_path)
    except OSError:
        pass


def is_unlocked(marker_path: str, plaintext_dir: str) -> bool:
    return os.path.isfile(marker_path) and os.path.isdir(plaintext_dir)


def remember_session_key(profile_key: ProfileKey) -> None:
    global _session_key
    with _session_key_lock:
        _session_key = profile_key


def forget_session_key() -> None:
    global _session_key
    with _session_key_lock:
        _session_key = None


def current_session_key() -> ProfileKey | None:
    with _session_key_lock:
        return _session_key


def lock_session_profile(plaintext_dir: str, enc_dir: str,
                         marker_path: str) -> bool:
    """Best-effort lock for close/disconnect/interrupt; never raises."""
    profile_key = current_session_key()
    if profile_key is None or not os.path.isdir(plaintext_dir):
        return False
    try:
        lock_profile(profile_key, plaintext_dir, enc_dir, marker_path)
    except Exception:
        print('⚠️ Could not encrypt the browser profile. Close the browser '
              'and use Lock profile before exiting.')
        return False
    forget_session_key()
    return True