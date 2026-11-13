# Scoopy

Scoopy is a desktop app for collecting links from your Instagram saved collections and downloading accessible videos with yt-dlp. Separate video and audio streams are combined with a built-in MP4 muxer — no FFmpeg installation needed.

## Requirements

- Windows x64, macOS (Intel or Apple Silicon), or Linux (glibc, x86-64 or ARM64)
- Python 3.11, 3.12, or 3.13, with Tkinter
- A GUI desktop session, internet access, and an Instagram account
- No browser install required: Scoopy asks Selenium to download a managed Chrome for Testing (first run needs internet and disk space; after that it's cached)

## Install

Clone the repository, then create a virtual environment in the project folder:

```bash
git clone https://github.com/ShaadyEmad/instagram-collection-downloader.git
cd instagram-collection-downloader
python -m venv .venv
```

Activate the environment:

- **Windows PowerShell:** `.venv\Scripts\Activate.ps1`
- **Windows Command Prompt:** `.venv\Scripts\activate.bat`
- **macOS/Linux:** `source .venv/bin/activate`

Install Scoopy and its dependencies in editable mode:

```bash
python -m pip install --only-binary=pymp4v2 -e .
```

The `--only-binary` flag prevents pip from silently compiling native code on an unsupported platform; if your Python or platform has no published wheel, the install fails clearly. Supported wheels cover CPython 3.11–3.13 on the platforms listed above. Windows ARM64, 32-bit systems, musl-based Linux, and Python 3.14 are not covered by published wheels.

With the environment active, launch from any directory:

```bash
scoopy
```

Alternatively, run `python main.py` from the cloned project folder. Keep the clone in place: the editable install uses it to run the app and store its data. Non-editable installs are not supported for clone-relative storage and may use an unwritable location.

## Use

1. Choose a download folder in the app; the default is `videos`.
2. Click **Open Browser for Login**. Scoopy uses an installed browser in preference order — Chrome, then Edge, then Firefox — and signs in separately in a dedicated profile.
3. Sign in to Instagram and navigate to a saved collection.
4. Click **Start Scraping Links**. Scoopy scrolls through the collection and saves the links to `links.txt`.
5. Keep the app's browser open on Instagram, then click **Download All Videos**.

Downloads prefer a verified combined MP4. When video and audio come separately, Scoopy combines them internally without FFmpeg. Videos with no audio track available are saved as silent videos and reported as such. If a download or merge fails, the downloaded media is kept in a `recovery/` folder inside your download folder and the rest of the batch continues.

Scoopy reads Instagram cookies from its own browser session into memory for each download. No cookie export or browser extension is needed. You must have access to the posts; Instagram can still require verification, expire your session, or rate-limit requests.

## Data and privacy

For source and editable installs, the browser profiles (`.chrome-profile/`, `.edge-profile/`, `.firefox-profile/`), `links.txt`, and the default `videos/` folder are stored in the clone, regardless of where you run `scoopy`. A frozen executable stores them beside the executable.

Each browser gets its own dedicated persistent Scoopy profile, separate from your regular browser profile, so its Instagram login must be completed once and can persist between runs. **Profile folders contain sensitive session data.** They are listed in `.gitignore`, but do not share or commit them. To reset a profile, close Scoopy's browser and delete the matching folder; you will need to sign in again. Avoid running multiple app instances against the same profile.

## Troubleshooting

- **`scoopy` not found:** Activate the environment and reinstall with `python -m pip install -e .`, or add its scripts directory to PATH.
- **No browser opens:** Install or update Google Chrome, Microsoft Edge, or Mozilla Firefox, or check your internet connection for the managed browser download. Scoopy reports each browser it could not start and moves on to the next.
- **No links found:** Make sure you are signed in and viewing the intended saved collection in Scoopy's browser.
- **Download fails:** Keep that browser open on Instagram, complete any verification, and check post permissions, network access, and rate limits. If the browser was closed, retry to detect the disconnection, then reopen it in the app. Media from failed downloads is kept in `recovery/` for manual inspection.

## Build an executable

PyInstaller is optional. To build a Windows executable from the project folder:

```bash
python -m pip install -e ".[build]"
pyinstaller --onefile --windowed main.py
```

The executable is created in `dist/`. A browser (or the managed download) is still required. Verify on each platform you distribute that the build includes the `pymp4v2` native extension.

## Third-party notices

Scoopy uses [pymp4v2](https://pypi.org/project/pymp4v2/) (alpha), Python bindings that statically link [mp4v2](https://github.com/enzo1982/mp4v2), distributed under the Mozilla Public License 1.1. Review and include the required notices if you redistribute release artifacts.

## Tests

From the project folder, with the project installed:

```bash
python -B -m unittest discover -s tests -v
```

The offline tests use synthetic cookies, synthetic media fixtures, and a local media server; they do not launch a browser or contact Instagram. Decoded-content checks skip when FFmpeg is unavailable (developers only; end users never need it). A full GUI, login, scraping, and download check requires a manual test with your own account.

## Disclaimer

Use Scoopy only for content you have the right to download. Downloading content may be subject to Instagram's terms of service.
