"""Offline crypto tests: no Tk, no browser, no network."""

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from scoopy import profile_crypto
from scoopy.session import AppError


class ProfileCryptoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plaintext = os.path.join(self.temp.name, 'plaintext')
        self.enc = os.path.join(self.temp.name, '.chrome-profile.enc')
        self.marker = os.path.join(self.temp.name, '.scoopy-profile.unlocked')
        os.makedirs(self.plaintext, exist_ok=True)

    def _write_file(self, rel_path: str, content: bytes):
        path = os.path.join(self.plaintext, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(content)

    def test_key_derivation_is_deterministic_and_salt_sensitive(self):
        salt = profile_crypto.generate_salt()
        k1 = profile_crypto.derive_key('passphrase', salt)
        k2 = profile_crypto.derive_key('passphrase', salt)
        self.assertEqual(k1, k2)
        k3 = profile_crypto.derive_key('passphrase', profile_crypto.generate_salt())
        self.assertNotEqual(k1, k3)

    def test_round_trip_nested_directories_and_files(self):
        # Nested dirs, empty file, binary data, UTF-8 names, chunk boundary sizes
        self._write_file('dir/subdir/file.txt', b'hello')
        self._write_file('empty', b'')
        self._write_file('binary', bytes(range(256)))
        self._write_file('unicode_\u00e9.txt', b'utf8')
        chunk = profile_crypto.CHUNK_BYTES
        self._write_file('chunk_exact', b'x' * chunk)
        self._write_file('chunk_minus_1', b'x' * (chunk - 1))
        self._write_file('chunk_plus_1', b'x' * (chunk + 1))

        count = profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        self.assertGreater(count, 0)
        self.assertFalse(os.path.isdir(self.plaintext))
        self.assertTrue(profile_crypto.is_encrypted_profile(self.enc))

        # Restore
        key = profile_crypto.prepare_profile('passphrase123', self.enc, self.plaintext, self.marker)
        self.assertTrue(os.path.isdir(self.plaintext))
        self.assertTrue(profile_crypto.is_unlocked(self.marker, self.plaintext))

        # Verify content
        with open(os.path.join(self.plaintext, 'dir', 'subdir', 'file.txt'), 'rb') as f:
            self.assertEqual(f.read(), b'hello')
        with open(os.path.join(self.plaintext, 'empty'), 'rb') as f:
            self.assertEqual(f.read(), b'')
        with open(os.path.join(self.plaintext, 'binary'), 'rb') as f:
            self.assertEqual(f.read(), bytes(range(256)))
        with open(os.path.join(self.plaintext, 'unicode_\u00e9.txt'), 'rb') as f:
            self.assertEqual(f.read(), b'utf8')
        with open(os.path.join(self.plaintext, 'chunk_exact'), 'rb') as f:
            self.assertEqual(f.read(), b'x' * chunk)
        with open(os.path.join(self.plaintext, 'chunk_minus_1'), 'rb') as f:
            self.assertEqual(f.read(), b'x' * (chunk - 1))
        with open(os.path.join(self.plaintext, 'chunk_plus_1'), 'rb') as f:
            self.assertEqual(f.read(), b'x' * (chunk + 1))

        # Lock again with same key
        locked = profile_crypto.lock_profile(key, self.plaintext, self.enc, self.marker)
        self.assertGreater(locked, 0)
        self.assertFalse(os.path.isdir(self.plaintext))
        self.assertFalse(profile_crypto.is_unlocked(self.marker, self.plaintext))

    def test_enable_publishes_then_removes_plaintext(self):
        self._write_file('a.txt', b'a')
        count = profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        self.assertEqual(count, 1)
        self.assertFalse(os.path.isdir(self.plaintext))
        self.assertTrue(profile_crypto.is_encrypted_profile(self.enc))
        # Verify restore
        profile_crypto.prepare_profile('passphrase123', self.enc, self.plaintext, self.marker)
        self.assertTrue(os.path.isdir(self.plaintext))

    def test_wrong_passphrase_is_rejected_before_writing(self):
        self._write_file('a.txt', b'a')
        profile_crypto.enable_encryption('correct123', self.plaintext, self.enc)
        # enable_encryption already removed plaintext

        # verify_passphrase raises
        with self.assertRaises(profile_crypto.ProfileEncryptionError) as cm:
            profile_crypto.verify_passphrase('wrong', self.enc)
        self.assertIn('Incorrect passphrase', str(cm.exception))

        # prepare_profile raises and creates no plaintext
        with self.assertRaises(profile_crypto.ProfileEncryptionError) as cm:
            profile_crypto.prepare_profile('wrong', self.enc, self.plaintext, self.marker)
        self.assertIn('Incorrect passphrase', str(cm.exception))
        self.assertFalse(os.path.isdir(self.plaintext))

    def test_tampered_manifest_and_blob_are_rejected(self):
        self._write_file('a.txt', b'a')
        profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        # enable_encryption already removed plaintext

        # Flip a byte in manifest.enc
        manifest_path = os.path.join(self.enc, 'manifest.enc')
        with open(manifest_path, 'r+b') as f:
            data = f.read()
            if len(data) > 10:
                f.seek(10)
                f.write(bytes([data[10] ^ 0xFF]))
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto.prepare_profile('passphrase123', self.enc, self.plaintext, self.marker)

        # Restore manifest (re-enable with fresh container)
        shutil.rmtree(self.enc)
        os.makedirs(self.plaintext)
        self._write_file('a.txt', b'a')
        profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        # enable_encryption already removed plaintext

        blob_path = os.path.join(self.enc, 'blobs', '00000000.bin')
        with open(blob_path, 'r+b') as f:
            data = f.read()
            if len(data) > 5:
                f.seek(5)
                f.write(bytes([data[5] ^ 0xFF]))
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto.prepare_profile('passphrase123', self.enc, self.plaintext, self.marker)

    def test_reordered_or_truncated_chunks_are_rejected(self):
        self._write_file('a.txt', b'a')
        profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        # enable_encryption already removed plaintext

        # Truncate the last chunk from blob
        blob_path = os.path.join(self.enc, 'blobs', '00000000.bin')
        with open(blob_path, 'r+b') as f:
            f.truncate(max(0, os.path.getsize(blob_path) - 10))
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto.prepare_profile('passphrase123', self.enc, self.plaintext, self.marker)

    def test_manifest_path_traversal_is_rejected(self):
        self._write_file('a.txt', b'a')
        profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        # enable_encryption already removed plaintext

        # Manually tamper manifest.enc to inject a path with ..
        # We can't easily re-encrypt, so we test _safe_relative directly
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto._safe_relative({'path': '../escape'})
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto._safe_relative({'path': 'dir/../../escape'})
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto._safe_relative({'path': 'C:/windows'})
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto._safe_relative({'path': ''})
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto._safe_relative({'path': 'dir/./file'})
        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            profile_crypto._safe_relative({'path': 'dir/\x00file'})
        # Valid paths
        self.assertEqual(profile_crypto._safe_relative({'path': 'dir/file'}), 'dir/file')
        self.assertEqual(profile_crypto._safe_relative({'path': 'file'}), 'file')

    def test_symlinks_and_excluded_caches_are_skipped(self):
        # Create a symlink (skip on Windows if no permission)
        link_target = os.path.join(self.temp.name, 'outside.txt')
        with open(link_target, 'wb') as f:
            f.write(b'secret')
        link_path = os.path.join(self.plaintext, 'link.txt')
        try:
            os.symlink(link_target, link_path)
        except (OSError, NotImplementedError):
            pass  # Skip symlink test if not supported

        # Create excluded cache dirs
        os.makedirs(os.path.join(self.plaintext, 'Cache'), exist_ok=True)
        self._write_file('Cache/data', b'cache')
        os.makedirs(os.path.join(self.plaintext, 'Crashpad'), exist_ok=True)
        self._write_file('Crashpad/dump', b'crash')
        self._write_file('normal.txt', b'ok')

        profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        # enable_encryption already removed plaintext

        profile_crypto.prepare_profile('passphrase123', self.enc, self.plaintext, self.marker)

        # Excluded dirs and their contents should not be restored
        self.assertFalse(os.path.exists(os.path.join(self.plaintext, 'Cache')))
        self.assertFalse(os.path.exists(os.path.join(self.plaintext, 'Crashpad')))
        # Normal file should be restored
        self.assertTrue(os.path.exists(os.path.join(self.plaintext, 'normal.txt')))

        # Outside file should be untouched
        self.assertTrue(os.path.exists(link_target))
        with open(link_target, 'rb') as f:
            self.assertEqual(f.read(), b'secret')

    def test_failed_publish_keeps_source(self):
        self._write_file('a.txt', b'a')
        # Patch os.replace in profile_crypto module to fail on the final publish
        original_replace = os.replace
        def failing_replace(src, dst, *args, **kwargs):
            if dst == self.enc:
                raise OSError('Simulated disk full')
            return original_replace(src, dst, *args, **kwargs)

        with self.assertRaises(profile_crypto.ProfileEncryptionError):
            with mock.patch('scoopy.profile_crypto.os.replace', failing_replace):
                profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        # Plaintext should still exist
        self.assertTrue(os.path.isdir(self.plaintext))
        # Destination should not exist (temp dir cleaned up)
        self.assertFalse(os.path.isdir(self.enc))

    def test_lock_profile_accepts_key_without_passphrase(self):
        self._write_file('a.txt', b'a')
        profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        # enable_encryption already removed plaintext

        key = profile_crypto.verify_passphrase('passphrase123', self.enc)
        profile_crypto.prepare_profile('passphrase123', self.enc, self.plaintext, self.marker)
        profile_crypto.lock_profile(key, self.plaintext, self.enc, self.marker)
        # Should be able to unlock with same passphrase
        profile_crypto.prepare_profile('passphrase123', self.enc, self.plaintext, self.marker)
        self.assertTrue(os.path.isdir(self.plaintext))

    def test_disable_keeps_plaintext_and_removes_container(self):
        self._write_file('a.txt', b'a')
        profile_crypto.enable_encryption('passphrase123', self.plaintext, self.enc)
        profile_crypto.disable_encryption('passphrase123', self.enc, self.plaintext, self.marker)
        self.assertTrue(os.path.isdir(self.plaintext))
        self.assertFalse(os.path.isdir(self.enc))

    def test_marker_logic(self):
        self.assertFalse(profile_crypto.is_unlocked(self.marker, self.plaintext))
        os.makedirs(self.plaintext, exist_ok=True)
        self.assertFalse(profile_crypto.is_unlocked(self.marker, self.plaintext))
        profile_crypto.mark_unlocked(self.marker)
        self.assertTrue(profile_crypto.is_unlocked(self.marker, self.plaintext))
        profile_crypto.clear_unlocked(self.marker)
        self.assertFalse(profile_crypto.is_unlocked(self.marker, self.plaintext))
        # Idempotent
        profile_crypto.clear_unlocked(self.marker)
        self.assertFalse(profile_crypto.is_unlocked(self.marker, self.plaintext))

    def test_session_registry_is_idempotent(self):
        self.assertIsNone(profile_crypto.current_session_key())
        key = profile_crypto.ProfileKey(key=b'x' * 32, salt=b's' * 16)
        profile_crypto.remember_session_key(key)
        self.assertEqual(profile_crypto.current_session_key(), key)
        profile_crypto.remember_session_key(key)
        self.assertEqual(profile_crypto.current_session_key(), key)
        profile_crypto.forget_session_key()
        self.assertIsNone(profile_crypto.current_session_key())
        profile_crypto.forget_session_key()
        self.assertIsNone(profile_crypto.current_session_key())
        # lock_session_profile with no key returns False without touching disk
        self.assertFalse(profile_crypto.lock_session_profile(self.plaintext, self.enc, self.marker))


if __name__ == '__main__':
    unittest.main()