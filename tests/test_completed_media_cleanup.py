import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scoopy import download
from scoopy.session import AppError


class CompletedMediaCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'downloads'
        self.root.mkdir()

    def write(self, relative, data=b'original'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def symlink(self, target, path):
        try:
            path.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'Symlinks unavailable: {error}')

    def test_candidates_only_old_app_named_regular_top_level_images(self):
        names = [
            '20261023_Video by example.jpg', 'NA_Video by example.jpeg',
            '20261101_Video by another.PNG', 'NA_Video by account (2).webp',
        ]
        expected = sorted(str(self.write(name)) for name in names)
        for name in (
                'holiday.jpg', '20261023_other.jpg', 'NA_Video by .jpg',
                'na_Video by account.jpg', '2026102_Video by account.jpg',
                'NA_Video by account.mp4', 'NA_Video by account.jpg.part',
                'photos/NA_Video by photo.jpg', 'nested/NA_Video by frame.jpg',
                'completed_videos/NA_Video by account.mp4', 'notes.txt'):
            self.write(name)
        (self.root / 'NA_Video by directory.jpg').mkdir()
        self.assertEqual(download.legacy_image_candidates(str(self.root)), expected)

    def test_missing_and_blank_roots_have_no_candidates(self):
        self.assertEqual(download.legacy_image_candidates(str(self.root / 'missing')), [])
        self.assertEqual(download.legacy_image_candidates(''), [])
        self.assertFalse(download.has_failed_media(str(self.root / 'missing')))
        self.assertFalse(download.has_failed_media(''))

    def test_visibility_is_union_including_images_without_recovery(self):
        self.assertFalse(download.has_failed_media(str(self.root)))
        image = self.write('NA_Video by account.jpg')
        self.assertTrue(download.has_failed_media(str(self.root)))
        image.unlink()
        (self.root / 'recovery').mkdir()
        self.assertFalse(download.has_failed_media(str(self.root)))
        self.write('recovery/entry/stream.mp4')
        self.assertTrue(download.has_failed_media(str(self.root)))

    def test_publish_lazily_separates_video_and_all_supported_images(self):
        self.assertFalse((self.root / 'photos').exists())
        source = self.write('.scoopy-work/clip.mp4', b'video')
        output = Path(download._publish(str(source), str(self.root)))
        self.assertEqual(output, self.root / download.COMPLETED_VIDEO_DIR_NAME / 'clip.mp4')
        self.assertEqual(output.read_bytes(), b'video')
        self.assertFalse((self.root / 'photos').exists())
        for extension in ('.jpg', '.jpeg', '.png', '.webp'):
            with self.subTest(extension=extension):
                source = self.write('.scoopy-work/photo' + extension, b'photo')
                output = Path(download._publish(
                    str(source), str(self.root), extension=extension))
                self.assertEqual(output, self.root / download.PHOTO_DIR_NAME / source.name)
                self.assertEqual(output.read_bytes(), b'photo')
        self.assertEqual(download.legacy_image_candidates(str(self.root)), [])
        self.assertFalse(download.has_failed_media(str(self.root)))

    def test_publish_preserves_collisions_and_old_root_files(self):
        old_video = self.write('clip.mp4', b'old root')
        for extension, folder in (('.mp4', 'completed_videos'), ('.jpg', 'photos')):
            with self.subTest(extension=extension):
                original = self.write(f'{folder}/clip{extension}', b'first')
                self.write(f'{folder}/clip (2){extension}', b'second')
                source = self.write('.scoopy-work/clip' + extension, b'new')
                output = Path(download._publish(
                    str(source), str(self.root), extension=extension))
                self.assertEqual(output.name, 'clip (3)' + extension)
                self.assertEqual(output.read_bytes(), b'new')
                self.assertEqual(original.read_bytes(), b'first')
        self.assertEqual(old_video.read_bytes(), b'old root')

    def test_default_cleanup_preserves_loose_images_and_successful_media(self):
        protected = [
            self.write('NA_Video by account.jpg'),
            self.write('old.mp4'),
            self.write('photos/NA_Video by account.jpg'),
            self.write('completed_videos/clip.mp4'),
            self.write('notes.txt'),
        ]
        self.write('recovery/entry/failed.mp4')
        download.remove_failed_media(str(self.root))
        self.assertFalse((self.root / 'recovery').exists())
        self.assertTrue(all(path.read_bytes() == b'original' for path in protected))

    def test_explicit_cleanup_removes_only_captured_candidates_without_recovery(self):
        captured = self.write('20261101_Video by confirmed.jpg')
        later = self.write('NA_Video by unconfirmed.webp')
        protected = self.write('photos/NA_Video by genuine.jpg')
        arbitrary = self.write('holiday.jpg')
        video = self.write('20261101_Video by account.mp4')
        download.remove_failed_media(
            str(self.root),
            loose_images=(str(captured), str(protected), str(arbitrary), str(video)))
        self.assertFalse(captured.exists())
        self.assertTrue(all(path.exists() for path in (later, protected, arbitrary, video)))

    def test_changed_folder_and_relative_or_nested_paths_are_not_confirmed(self):
        captured = self.write('NA_Video by captured.jpg')
        other = self.root / 'other'
        other.mkdir()
        download.remove_failed_media(str(other), loose_images=(str(captured),))
        download.remove_failed_media(
            str(self.root), loose_images=(captured.name, str(other / '..' / captured.name)))
        self.assertTrue(captured.exists())

    def test_missing_recovery_and_disappeared_images_are_idempotent(self):
        missing = str(self.root / 'NA_Video by gone.jpg')
        download.remove_failed_media(str(self.root), loose_images=(missing,))
        download.remove_failed_media(str(self.root / 'missing'))
        self.assertFalse((self.root / 'missing').exists())

    def test_captured_file_replaced_with_directory_is_preserved(self):
        image = self.write('NA_Video by changed.jpg')
        image.unlink()
        image.mkdir()
        download.remove_failed_media(str(self.root), loose_images=(str(image),))
        self.assertTrue(image.is_dir())

    def test_symlink_candidate_and_replaced_capture_never_delete_target(self):
        target = self.write('outside/original.jpg')
        image = self.write('NA_Video by linked.jpg')
        captured = tuple(download.legacy_image_candidates(str(self.root)))
        self.assertEqual(captured, (str(image),))
        image.unlink()
        self.symlink(target, image)
        self.assertEqual(download.legacy_image_candidates(str(self.root)), [])
        download.remove_failed_media(str(self.root), loose_images=captured)
        self.assertTrue(image.is_symlink())
        self.assertEqual(target.read_bytes(), b'original')

    def test_publish_skips_link_collision_without_overwriting_target(self):
        target = self.write('outside/keep.mp4')
        destination = self.root / 'completed_videos'
        destination.mkdir()
        self.symlink(target, destination / 'clip.mp4')
        source = self.write('.scoopy-work/clip.mp4', b'new')
        result = Path(download._publish(str(source), str(self.root)))
        self.assertEqual(result, destination / 'clip (2).mp4')
        self.assertEqual(target.read_bytes(), b'original')

    def test_linked_root_and_ancestor_are_rejected(self):
        alias = Path(self.temporary.name) / 'alias'
        self.symlink(self.root, alias)
        child = self.root / 'child'
        child.mkdir()
        for root in (alias, alias / 'child'):
            with self.subTest(root=root):
                self.assertEqual(download.legacy_image_candidates(str(root)), [])
                with self.assertRaises(AppError):
                    download.remove_failed_media(str(root))
                source = self.write('.scoopy-work/input.mp4')
                with self.assertRaises(AppError):
                    download._publish(str(source), str(root))
                self.assertTrue(source.exists())

    def test_linked_reserved_folders_refused_and_targets_preserved(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        protected = outside / 'keep.mp4'
        protected.write_bytes(b'keep')
        for folder in ('completed_videos', 'photos', 'recovery'):
            with self.subTest(folder=folder):
                link = self.root / folder
                self.symlink(outside, link)
                if folder == 'recovery':
                    with self.assertRaises(AppError):
                        download.remove_failed_media(str(self.root))
                else:
                    source = self.write('.scoopy-work/input.mp4')
                    with self.assertRaises(AppError):
                        download._publish(
                            str(source), str(self.root),
                            extension='.jpg' if folder == 'photos' else '.mp4')
                    self.assertTrue(source.exists())
                self.assertEqual(protected.read_bytes(), b'keep')
                link.unlink()

    def test_nested_recovery_link_does_not_delete_external_files(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        protected = outside / 'keep.jpg'
        protected.write_bytes(b'keep')
        recovery = self.root / 'recovery'
        recovery.mkdir()
        self.symlink(outside, recovery / 'linked')
        download.remove_failed_media(str(self.root))
        self.assertFalse(recovery.exists())
        self.assertEqual(protected.read_bytes(), b'keep')

    def test_reparse_file_is_not_candidate_or_deletion_target(self):
        image = self.write('NA_Video by reparse.jpg')
        real_lstat = os.lstat

        def reparse_lstat(path, *args, **kwargs):
            if os.fspath(path) == str(image):
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)

        with patch.object(download.os, 'lstat', side_effect=reparse_lstat):
            self.assertEqual(download.legacy_image_candidates(str(self.root)), [])
            download.remove_failed_media(str(self.root), loose_images=(str(image),))
        self.assertTrue(image.exists())

    def test_reparse_reserved_directory_is_refused(self):
        source = self.write('.scoopy-work/input.mp4')
        real_lstat = os.lstat
        destination = self.root / 'completed_videos'

        def reparse_lstat(path, *args, **kwargs):
            if os.fspath(path) == str(destination):
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)

        with patch.object(download.os, 'lstat', side_effect=reparse_lstat):
            with self.assertRaises(AppError):
                download._publish(str(source), str(self.root))
        self.assertTrue(source.exists())

    def test_reparse_download_root_is_not_scanned_or_cleaned(self):
        image = self.write('NA_Video by account.jpg')
        real_lstat = os.lstat

        def reparse_lstat(path, *args, **kwargs):
            if os.fspath(path) == str(self.root):
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)

        with patch.object(download.os, 'lstat', side_effect=reparse_lstat):
            self.assertEqual(download.legacy_image_candidates(str(self.root)), [])
            self.assertFalse(download.has_failed_media(str(self.root)))
            with self.assertRaises(AppError):
                download.remove_failed_media(str(self.root), loose_images=(str(image),))
        self.assertTrue(image.exists())

    def test_invalid_publish_extension_and_regular_destination_are_refused(self):
        source = self.write('.scoopy-work/input.mp4')
        with self.assertRaises(AppError):
            download._publish(str(source), str(self.root), extension='../escape')
        self.write('completed_videos', b'user file')
        with self.assertRaises(AppError):
            download._publish(str(source), str(self.root))
        self.assertTrue(source.exists())

    def test_partial_cleanup_permission_error_is_explicit(self):
        image = self.write('NA_Video by locked.jpg')
        self.write('recovery/failed.mp4')
        with patch.object(download.os, 'unlink', side_effect=PermissionError('locked')):
            # rmtree may itself use unlink; either stage must report incomplete cleanup.
            with self.assertRaisesRegex(AppError, 'Could not remove all failed media'):
                download.remove_failed_media(str(self.root), loose_images=(str(image),))
        self.assertTrue(image.exists())

    def test_image_permission_error_after_recovery_removal_is_explicit(self):
        image = self.write('NA_Video by locked.jpg')
        self.write('recovery/failed.mp4')
        real_unlink = os.unlink

        def locked_unlink(path, *args, **kwargs):
            if os.fspath(path) == str(image):
                raise PermissionError('locked')
            return real_unlink(path, *args, **kwargs)

        with patch.object(download.os, 'unlink', side_effect=locked_unlink):
            with self.assertRaisesRegex(AppError, 'Could not remove all failed media'):
                download.remove_failed_media(str(self.root), loose_images=(str(image),))
        self.assertFalse((self.root / 'recovery').exists())
        self.assertTrue(image.exists())


if __name__ == '__main__':
    unittest.main()
