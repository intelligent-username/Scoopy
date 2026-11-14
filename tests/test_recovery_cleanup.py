"""Recovery deletion is tested only against disposable isolated folders."""

import contextlib
import io
from pathlib import Path
import stat
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scoopy import download, ui
from scoopy.session import AppError


class RecoveryFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.folder = Path(self.temp) / 'downloads'
        self.folder.mkdir()
        self.recovery = self.folder / 'recovery'

    def test_visibility_tracks_missing_empty_and_nonempty_recovery(self):
        self.assertFalse(download.has_failed_media(str(self.folder)))
        self.recovery.mkdir()
        self.assertFalse(download.has_failed_media(str(self.folder)))
        (self.recovery / 'entry-0001').mkdir()
        self.assertTrue(download.has_failed_media(str(self.folder)))

    def test_cleanup_removes_only_recovery_and_is_idempotent(self):
        completed_video = self.folder / 'video.mp4'
        completed_photo = self.folder / 'photo.jpg'
        completed_video.write_bytes(b'completed video')
        completed_photo.write_bytes(b'completed photo')
        partial = self.recovery / 'entry-0001' / 'partial.mp4.part'
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b'partial')
        (self.folder / '.scoopy-work').mkdir()
        unrelated = Path(self.temp) / 'unrelated'
        unrelated.mkdir()
        (unrelated / 'keep').write_bytes(b'keep')
        download.remove_failed_media(str(self.folder))
        download.remove_failed_media(str(self.folder))
        self.assertFalse(self.recovery.exists())
        self.assertEqual(completed_video.read_bytes(), b'completed video')
        self.assertEqual(completed_photo.read_bytes(), b'completed photo')
        self.assertTrue((self.folder / '.scoopy-work').is_dir())
        self.assertTrue((unrelated / 'keep').is_file())

    def test_link_or_windows_junction_root_is_never_removed(self):
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
            info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
            with self.subTest(mode=mode, attributes=attributes), \
                    patch.object(download.os, 'lstat', return_value=info), \
                    patch.object(download.shutil, 'rmtree') as delete:
                self.assertFalse(download.has_failed_media(str(self.folder)))
                with self.assertRaises(AppError):
                    download.remove_failed_media(str(self.folder))
                delete.assert_not_called()

    def test_nested_link_does_not_remove_its_target(self):
        outside = Path(self.temp) / 'outside'
        outside.mkdir()
        keep = outside / 'keep.txt'
        keep.write_text('keep', encoding='utf-8')
        self.recovery.mkdir()
        link = self.recovery / 'linked'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks is not permitted on this system.')
        download.remove_failed_media(str(self.folder))
        self.assertEqual(keep.read_text(encoding='utf-8'), 'keep')

    def test_non_directory_recovery_and_empty_selection_are_rejected(self):
        self.recovery.write_text('not a directory', encoding='utf-8')
        self.assertFalse(download.has_failed_media(str(self.folder)))
        with self.assertRaises(AppError):
            download.remove_failed_media(str(self.folder))
        with self.assertRaises(AppError):
            download.remove_failed_media('')

    def test_permission_error_is_actionable_without_raw_exception(self):
        self.recovery.mkdir()
        with patch.object(download.shutil, 'rmtree',
                          side_effect=PermissionError('PRIVATE_PATH')):
            with self.assertRaises(AppError) as caught:
                download.remove_failed_media(str(self.folder))
        self.assertIn('permissions', str(caught.exception))
        self.assertNotIn('PRIVATE_PATH', str(caught.exception))


class PhotoClassificationTests(unittest.TestCase):
    def test_video_evidence_overrides_contradictory_photo_type(self):
        for field, value in (
                ('video_versions', [{'url': 'https://scontent.cdninstagram.com/video.mp4'}]),
                ('video_dash_manifest', '<MPD/>'),
                ('video_url', 'https://scontent.cdninstagram.com/video.mp4'),
                ('is_video', True)):
            with self.subTest(field=field), \
                    patch.object(download._InstagramIE, '_extract_product_media',
                                 return_value={'formats': []}) as parent:
                product = {'media_type': 1, field: value,
                           'image_versions2': {'candidates': [
                               {'url': 'https://scontent.cdninstagram.com/thumbnail.jpg'}]}}
                info = download.InstagramIE()._extract_product_media(product)
                parent.assert_called_once_with(product)
                self.assertNotIn('_scoopy_photo', info)

    def test_auxiliary_thumbnail_writing_is_explicitly_disabled(self):
        options = download._base_options('unused')
        self.assertFalse(options['writethumbnail'])
        self.assertFalse(options['write_all_thumbnails'])


class RecoveryButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as error:
            raise unittest.SkipTest(f'Tk unavailable: {error}') from error

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.folder = Path(self.temp) / 'downloads'
        self.recovery = self.folder / 'recovery'
        self.recovery.mkdir(parents=True)
        self.frame = ttk.Frame(self.root)
        self.frame.pack()
        self.addCleanup(self.frame.destroy)
        self.button = ttk.Button(self.frame)
        self.app = SimpleNamespace(
            folder_name_var=tk.StringVar(master=self.root, value=str(self.folder)),
            remove_failed_button=self.button, busy=False, closing=threading.Event(),
            after=Mock(), start_operation=Mock(), folder_path_label=Mock(),
        )
        for method in ('_refresh_recovery_button', 'check_recovery_folder',
                       'remove_failed_media_thread'):
            setattr(self.app, method, MethodType(getattr(ui.DownloaderApp, method), self.app))
        self.ask = self.enterContext(patch.object(ui.messagebox, 'askyesno', return_value=True))

    def add_failed_file(self):
        path = self.recovery / 'partial.mp4.part'
        path.write_bytes(b'partial')
        return path

    def add_legacy_image(self, title='example'):
        path = self.folder / f'20260101_Video by {title}.jpg'
        path.write_bytes(b'image')
        return path

    def test_candidate_only_folder_shows_cleanup_without_recovery(self):
        self.recovery.rmdir()
        image = self.add_legacy_image()
        self.app._refresh_recovery_button()
        self.assertEqual(self.button.winfo_manager(), 'pack')
        self.app.busy = True
        self.app._refresh_recovery_button()
        self.assertIn('disabled', self.button.state())
        self.app.busy = False
        self.app.remove_failed_media_thread()
        self.assertIn(image.name, self.ask.call_args.args[1])
        self.assertIn('MAY include legitimate photos', self.ask.call_args.args[1])
        self.assertEqual(self.ask.call_args.kwargs['default'], ui.messagebox.NO)
        work, _ = self.app.start_operation.call_args.args
        work()
        self.assertFalse(image.exists())
        self.app._refresh_recovery_button()
        self.assertEqual(self.button.winfo_manager(), '')

    def test_cancel_keeps_candidate_only_images(self):
        self.recovery.rmdir()
        image = self.add_legacy_image()
        self.ask.return_value = False
        self.app.remove_failed_media_thread()
        self.app.start_operation.assert_not_called()
        self.assertTrue(image.exists())

    def test_confirmation_uses_immutable_candidate_snapshot(self):
        self.add_failed_file()
        image = self.add_legacy_image()
        candidates = [str(image)]
        with patch.object(ui.media_download, 'legacy_image_candidates',
                          return_value=candidates, create=True) as scan:
            self.app.remove_failed_media_thread()
        scan.assert_called_with(str(self.folder))
        candidates.append(str(self.folder / '20260101_Video by later.jpg'))
        self.app.folder_name_var.set(str(Path(self.temp) / 'other'))
        work, _ = self.app.start_operation.call_args.args
        with patch.object(ui, 'remove_failed_media') as remove:
            work()
        remove.assert_called_once_with(str(self.folder), loose_images=(str(image),))

    def test_folder_changed_inside_dialog_does_not_retarget_cleanup(self):
        image = self.add_legacy_image()
        other = Path(self.temp) / 'other'
        other.mkdir()
        other_image = other / image.name
        other_image.write_bytes(b'keep')

        def confirm(*args, **kwargs):
            self.app.folder_name_var.set(str(other))
            return True

        self.ask.side_effect = confirm
        self.app.remove_failed_media_thread()
        work, _ = self.app.start_operation.call_args.args
        work()
        self.assertFalse(image.exists())
        self.assertEqual(other_image.read_bytes(), b'keep')

    def test_new_images_and_successful_subdirectories_are_not_cleanup_targets(self):
        self.add_failed_file()
        image = self.add_legacy_image('confirmed')
        protected = []
        for directory, filename in (
                ('completed_videos', 'complete.mp4'),
                ('photos', image.name)):
            path = self.folder / directory / filename
            path.parent.mkdir()
            path.write_bytes(b'keep')
            protected.append(path)
        ordinary = self.folder / 'holiday.jpg'
        ordinary.write_bytes(b'keep')
        self.app.remove_failed_media_thread()
        new_image = self.add_legacy_image('unconfirmed')
        work, success = self.app.start_operation.call_args.args
        work()
        self.assertFalse(image.exists())
        self.assertFalse(self.recovery.exists())
        for path in [new_image, ordinary, *protected]:
            self.assertTrue(path.is_file(), path)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            success(None)
        self.assertIn('confirmed folder', output.getvalue())
        self.assertIn('photos/', output.getvalue())

    def test_confirmation_truncates_long_candidate_lists_and_reports_total(self):
        candidates = [
            str(self.folder / f'20260101_Video by {number:02d}-{"x" * 140}.jpg')
            for number in range(14)]
        with patch.object(ui.media_download, 'legacy_image_candidates',
                          return_value=candidates, create=True):
            self.app.remove_failed_media_thread()
        prompt = self.ask.call_args.args[1]
        self.assertIn('14 legacy loose image(s)', prompt)
        self.assertIn('4 more (not shown)', prompt)
        self.assertNotIn('x' * 101, prompt)
        self.assertNotIn(Path(candidates[-1]).name, prompt)
        work, _ = self.app.start_operation.call_args.args
        with patch.object(ui, 'remove_failed_media') as remove:
            work()
        remove.assert_called_once_with(str(self.folder), loose_images=tuple(candidates))

    def test_busy_or_closing_during_confirmation_does_not_start_worker(self):
        self.add_legacy_image()
        for state in ('busy', 'closing'):
            with self.subTest(state=state):
                self.app.busy = False
                self.app.closing.clear()

                def confirm(*args, **kwargs):
                    if state == 'busy':
                        self.app.busy = True
                    else:
                        self.app.closing.set()
                    return True

                self.ask.side_effect = confirm
                self.app.remove_failed_media_thread()
                self.app.start_operation.assert_not_called()

    def test_candidate_scan_error_is_actionable_and_does_not_prompt(self):
        with patch.object(ui.media_download, 'legacy_image_candidates',
                          side_effect=PermissionError('PRIVATE_PATH'), create=True), \
                patch.object(ui.messagebox, 'showerror') as error:
            self.app.remove_failed_media_thread()
        self.ask.assert_not_called()
        self.app.start_operation.assert_not_called()
        self.assertIn('permissions', error.call_args.args[1])
        self.assertNotIn('PRIVATE_PATH', error.call_args.args[1])

    def test_cleanup_os_error_reports_partial_failure_without_raw_exception(self):
        self.add_failed_file()
        self.app.remove_failed_media_thread()
        work, _ = self.app.start_operation.call_args.args
        with patch.object(ui, 'remove_failed_media',
                          side_effect=PermissionError('PRIVATE_PATH')):
            with self.assertRaises(AppError) as caught:
                work()
        self.assertIn('Some files may already be removed', str(caught.exception))
        self.assertIn('permissions', str(caught.exception))
        self.assertNotIn('PRIVATE_PATH', str(caught.exception))

    def test_shared_cleanup_error_retains_actionable_message(self):
        self.add_failed_file()
        self.app.remove_failed_media_thread()
        work, _ = self.app.start_operation.call_args.args
        with patch.object(ui, 'remove_failed_media',
                          side_effect=AppError('Folder changed; review before retrying.')):
            with self.assertRaisesRegex(AppError, 'Folder changed'):
                work()

    def test_folder_helper_explains_successful_subdirectories(self):
        ui.DownloaderApp._update_folder_path(self.app)
        helper = self.app.folder_path_label.config.call_args.kwargs['text']
        self.assertIn(str(self.folder), helper)
        self.assertIn('completed_videos/', helper)
        self.assertIn('photos/', helper)

    def test_real_button_hidden_empty_visible_nonempty_and_hidden_after_cleanup(self):
        self.app._refresh_recovery_button()
        self.assertEqual(self.button.winfo_manager(), '')
        self.add_failed_file()
        self.app._refresh_recovery_button()
        self.assertEqual(self.button.winfo_manager(), 'pack')
        self.assertNotIn('disabled', self.button.state())
        download.remove_failed_media(str(self.folder))
        self.app._refresh_recovery_button()
        self.assertEqual(self.button.winfo_manager(), '')

    def test_real_button_disabled_while_busy(self):
        self.add_failed_file()
        self.app.busy = True
        self.app._refresh_recovery_button()
        self.assertEqual(self.button.winfo_manager(), 'pack')
        self.assertIn('disabled', self.button.state())
        self.app.busy = False
        self.app._refresh_recovery_button()
        self.assertNotIn('disabled', self.button.state())

    def test_changing_selected_folder_updates_visibility(self):
        self.add_failed_file()
        self.app._refresh_recovery_button()
        other = Path(self.temp) / 'other'
        self.app.folder_name_var.set(str(other))
        ui.DownloaderApp._update_folder_path(self.app)
        self.assertEqual(self.button.winfo_manager(), '')
        self.app.folder_name_var.set(str(self.folder))
        ui.DownloaderApp._update_folder_path(self.app)
        self.assertEqual(self.button.winfo_manager(), 'pack')

    def test_cancel_never_starts_deletion(self):
        failed = self.add_failed_file()
        self.ask.return_value = False
        self.app.remove_failed_media_thread()
        self.ask.assert_called_once()
        self.app.start_operation.assert_not_called()
        self.assertTrue(failed.exists())

    def test_confirmed_worker_uses_captured_folder_and_preserves_completed_files(self):
        self.add_failed_file()
        completed = self.folder / 'completed.mp4'
        completed.write_bytes(b'complete')
        self.app.remove_failed_media_thread()
        self.app.start_operation.assert_called_once()
        self.assertIn(str(self.recovery), self.ask.call_args.args[1])
        self.assertEqual(self.ask.call_args.kwargs['default'], ui.messagebox.NO)
        other = Path(self.temp) / 'other'
        other_recovery = other / 'recovery'
        other_recovery.mkdir(parents=True)
        (other_recovery / 'keep.part').write_bytes(b'keep')
        self.app.folder_name_var.set(str(other))
        work, success = self.app.start_operation.call_args.args
        work()
        with contextlib.redirect_stdout(io.StringIO()):
            success(None)
        self.assertFalse(self.recovery.exists())
        self.assertTrue((other_recovery / 'keep.part').exists())
        self.assertEqual(completed.read_bytes(), b'complete')

    def test_busy_or_closing_app_never_prompts_or_deletes(self):
        self.add_failed_file()
        self.app.busy = True
        self.app.remove_failed_media_thread()
        self.app.busy = False
        self.app.closing.set()
        self.app.remove_failed_media_thread()
        self.ask.assert_not_called()
        self.app.start_operation.assert_not_called()

    def test_empty_or_missing_recovery_does_not_prompt(self):
        self.app.remove_failed_media_thread()
        self.recovery.rmdir()
        self.app.remove_failed_media_thread()
        self.ask.assert_not_called()
        self.app.start_operation.assert_not_called()

    def test_periodic_refresh_stops_when_closing(self):
        self.add_failed_file()
        self.app.check_recovery_folder()
        self.app.after.assert_called_once_with(1000, self.app.check_recovery_folder)
        self.assertEqual(self.button.winfo_manager(), 'pack')
        self.app.after.reset_mock()
        self.app.closing.set()
        self.app.check_recovery_folder()
        self.app.after.assert_not_called()


if __name__ == '__main__':
    unittest.main()
