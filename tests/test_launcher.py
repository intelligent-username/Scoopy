import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import venv

from scoopy import browser, download, paths, ui

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_startup_and_wrapper(self):
        with patch.object(ui, 'DownloaderApp') as app_class:
            ui.main()
            app = app_class.return_value
            app_class.assert_called_once_with()
            self.assertEqual(app.method_calls, [unittest.mock.call.protocol('WM_DELETE_WINDOW', app.on_closing), unittest.mock.call.mainloop()])
        with patch.object(ui, 'main') as main:
            runpy.run_path(str(ROOT / 'main.py'), run_name='__main__')
            main.assert_called_once_with()

    def test_paths_and_consumers(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1')
            code = "import json; from scoopy import paths; print(json.dumps([paths.BASE_DIR, paths.LINKS_FILE, paths.CHROME_PROFILE_DIR]))"
            result = subprocess.run([sys.executable, '-B', '-c', code], cwd=temp, env=env, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout), [str(ROOT), str(ROOT / 'links.txt'), str(ROOT / '.chrome-profile')])
            frozen = "import runpy,sys,json; sys.frozen=True; sys.executable=sys.argv[1]; p=runpy.run_path(sys.argv[2]); print(json.dumps([p['BASE_DIR'],p['LINKS_FILE'],p['CHROME_PROFILE_DIR']]))"
            executable = str(Path(temp) / 'app' / 'scoopy.exe')
            result = subprocess.run([sys.executable, '-B', '-c', frozen, executable, str(ROOT / 'src/scoopy/paths.py')], cwd=temp, env=env, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout), [str(Path(temp) / 'app'), str(Path(temp) / 'app/links.txt'), str(Path(temp) / 'app/.chrome-profile')])
            self.assertEqual(list(Path(temp).iterdir()), [])
        self.assertEqual(browser.CHROME_PROFILE_DIR, paths.CHROME_PROFILE_DIR)
        self.assertEqual(browser.LINKS_FILE, paths.LINKS_FILE)
        self.assertEqual(download.start_download.__defaults__, (paths.LINKS_FILE,))
        app = SimpleNamespace(busy=False, driver=Mock(), folder_name_var=Mock(), start_operation=Mock())
        for folder in ('videos', os.path.abspath('absolute')):
            app.folder_name_var.get.return_value = folder
            with patch.object(ui.os.path, 'exists', return_value=True), patch.object(ui.os.path, 'getsize', return_value=1):
                ui.DownloaderApp.start_download_thread(app)
            with patch.object(ui.DownloaderApp, 'download_videos', create=True):
                app.download_videos = Mock()
                app.start_operation.call_args.args[0]()
                app.download_videos.assert_called_once_with(os.path.join(paths.BASE_DIR, folder))

    def test_editable_command_from_another_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            envdir = temp / 'venv'
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(envdir)
            scripts = envdir / ('Scripts' if os.name == 'nt' else 'bin')
            python = scripts / ('python.exe' if os.name == 'nt' else 'python')
            env = dict(os.environ, PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONDONTWRITEBYTECODE='1')
            env.pop('PYTHONPATH', None)
            subprocess.run([str(python), '-m', 'pip', 'install', '-e', str(ROOT), '--no-build-isolation', '--no-deps'], cwd=temp, env=env, check=True, capture_output=True, text=True)
            code = "import json,importlib.metadata as m; from scoopy import paths; e=[e.value for e in m.distribution('scoopy-instagram-collections').entry_points if e.name=='scoopy']; print(json.dumps([e,paths.BASE_DIR,paths.LINKS_FILE,paths.CHROME_PROFILE_DIR]))"
            result = subprocess.run([str(python), '-B', '-c', code], cwd=temp, env=env, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout), [['scoopy.ui:main'], str(ROOT), str(ROOT / 'links.txt'), str(ROOT / '.chrome-profile')])
            hook = temp / 'hook'; hook.mkdir()
            marker = temp / 'marker'
            (hook / 'sitecustomize.py').write_text(
                "import os\n"
                "from pathlib import Path\n"
                "import scoopy.ui\n"
                "def stub(): Path(os.environ['SCOOPY_MARKER']).write_text('called')\n"
                "scoopy.ui.main = stub\n", encoding='utf-8')
            env['PYTHONPATH'] = str(hook)
            env['SCOOPY_MARKER'] = str(marker)
            command = scripts / ('scoopy.exe' if os.name == 'nt' else 'scoopy')
            subprocess.run([str(command)], cwd=temp, env=env, check=True, capture_output=True, text=True)
            self.assertEqual(marker.read_text(encoding='utf-8'), 'called')


if __name__ == '__main__':
    unittest.main()
