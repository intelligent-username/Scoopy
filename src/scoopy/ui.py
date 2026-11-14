import logging
import os
import platform
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from selenium.common.exceptions import WebDriverException

from . import download as media_download
from .browser import start_driver, collect_reel_links, save_links
from .download import RECOVERY_DIR_NAME, has_failed_media, remove_failed_media, start_download
from .paths import BASE_DIR, CHROME_PROFILE_DIR, LINKS_FILE
from .session import AppError, navigate_to_saved, read_browser_cookies

APP_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'app-icon.png')
APP_ICON_ICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'app-icon.ico')

# --- Constants ---
APP_TITLE = "Scoopy"
WINDOW_WIDTH = 760
WINDOW_HEIGHT = 720

# --- COLOR PALETTE ---
# Built from the brand green #4CAF50 (hue ~122) with the color-palette skill:
# a green-tinted graphite ramp for surfaces, the brand green as the single
# accent (reserved for the primary action), amber for warnings. No pure black,
# no second hue, so the green download button reads as the one accent.
COLOR_BG = "#141815"            # window background, deep green-charcoal
COLOR_SURFACE = "#1F2722"       # raised controls (secondary buttons)
COLOR_SURFACE_HOVER = "#2A352D"
COLOR_FIELD = "#0E110E"         # inset fields: debug log, entry
COLOR_BORDER = "#2B362E"        # quiet hairline borders
COLOR_FG = "#E6EBE6"            # primary text
COLOR_FG_MUTED = "#9BA89D"      # secondary text
COLOR_DISABLED_BG = "#1A201B"
COLOR_DISABLED_FG = "#5E6B61"
COLOR_PRIMARY = "#4CAF50"       # brand green, the single accent
COLOR_PRIMARY_HOVER = "#5FC163"
COLOR_PRIMARY_PRESS = "#3E9443"
COLOR_ON_PRIMARY = "#0D130E"    # dark text on green for AA contrast
COLOR_WARNING = "#E2B455"       # amber, for action-needed hints
COLOR_SELECT = "#33513A"        # muted green text selection

# Debug-log level colors (on COLOR_FIELD), tuned for AA contrast.
DEBUG_TIMESTAMP_FG = "#95A69B"
DEBUG_ERROR_FG = "#FF8C84"
DEBUG_SUCCESS_FG = "#8BDC96"
DEBUG_WARNING_FG = "#E9BC66"


# --- Main GUI Application Class ---
class DownloaderApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.driver = None
        self.browser_was_open = False
        self.debug_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self.busy = False
        self.closing = threading.Event()
        self.driver_lock = threading.Lock()

        self.setup_ui()
        self.configure_styles()
        self.redirect_stdout()
        self.update_buttons()
        self.check_debug_queue()
        self.check_recovery_folder()

    def setup_ui(self):
        """Initializes and lays out all UI components."""
        self.title(APP_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.configure(bg=COLOR_BG)
        self.resizable(True, True)
        # Default size is the floor: users can grow the window freely,
        # but never below the layout's intended proportions.
        self.minsize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self._apply_window_icon()

        main_frame = ttk.Frame(self, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        folder_frame = ttk.Labelframe(main_frame, text="1. Download Folder", padding=12)
        folder_frame.pack(fill=tk.X, pady=(0, 16))

        self.folder_name_var = tk.StringVar(value="videos")
        self.folder_entry = ttk.Entry(folder_frame, textvariable=self.folder_name_var,
                                      font=("Segoe UI", 10))
        self.folder_entry.pack(fill=tk.X, ipady=3)

        # Live helper line under the entry so users always see exactly where
        # media lands without having to interpret relative paths.
        self.folder_path_label = ttk.Label(folder_frame, text="", style="Muted.TLabel")
        self.folder_path_label.pack(anchor=tk.W, pady=(4, 0))
        self.folder_name_var.trace_add('write', lambda *_: self._update_folder_path())
        self._update_folder_path()

        scraping_frame = ttk.Labelframe(main_frame, text="2. Download Photos and Videos", padding=12)
        scraping_frame.pack(fill=tk.X, pady=16)

        self.browser_button = ttk.Button(scraping_frame, text="Open Browser",
                                         command=self.start_browser_thread, style="Secondary.TButton")
        self.browser_button.pack(fill=tk.X, ipady=5)

        self.scrape_button = ttk.Button(scraping_frame, text="Scrape Links",
                                        command=self.start_scraping_thread, state=tk.DISABLED)
        self.scrape_button.pack(fill=tk.X, ipady=5, pady=(8, 0))

        # Same workflow as scraping: log in -> scrape -> download, so the
        # download action lives in this section instead of its own box.
        self.download_button = ttk.Button(scraping_frame, text="Download Collection",
                                          command=self.start_download_thread, style="Primary.TButton")
        self.download_button.pack(fill=tk.X, ipady=10, pady=(12, 0))

        # Inline empty-state hint, toggled by update_buttons() when links.txt
        # is missing or empty so the disabled download button explains itself.
        self.download_hint = ttk.Label(scraping_frame, text="", style="Hint.TLabel")
        self.download_hint.pack(fill=tk.X, pady=(8, 0))

        view_frame = ttk.Labelframe(main_frame, text="3. View Downloads", padding=12)
        view_frame.pack(fill=tk.X, pady=(0, 16))

        self.view_button = ttk.Button(view_frame, text="Open Download Folder",
                                      command=self.view_videos, style="Secondary.TButton")
        self.view_button.pack(fill=tk.X, ipady=5)

        self.remove_failed_button = ttk.Button(
            view_frame, text="Remove failed media",
            command=self.remove_failed_media_thread, style="Secondary.TButton")
        # Packed only when recovery or legacy loose-image candidates exist.

        debug_frame = ttk.Labelframe(main_frame, text="Logs", padding=12)
        debug_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        debug_toolbar = ttk.Frame(debug_frame)
        debug_toolbar.pack(fill=tk.X, pady=(0, 8))

        self.debug_count_label = ttk.Label(debug_toolbar, text="0 entries", style="Muted.TLabel")
        self.debug_count_label.pack(side=tk.LEFT)

        clear_button = ttk.Button(debug_toolbar, text="Clear", command=self.clear_debug,
                                  style="LogTool.TButton", width=7)
        clear_button.pack(side=tk.RIGHT)
        copy_button = ttk.Button(debug_toolbar, text="Copy All", command=self.copy_debug,
                                 style="LogTool.TButton", width=9)
        copy_button.pack(side=tk.RIGHT, padx=(0, 8))

        self.debug_area = scrolledtext.ScrolledText(
            debug_frame, height=14, width=80, state=tk.DISABLED, wrap=tk.WORD,
            bg=COLOR_FIELD, fg=COLOR_FG, font=("Consolas", 10),
            insertbackground=COLOR_FG, selectbackground=COLOR_SELECT,
            padx=8, pady=6, spacing1=2, spacing3=2)
        self.debug_area.pack(fill=tk.BOTH, expand=True)

        # Debug-log tags: level coloring + muted timestamp prefix.
        self.debug_area.tag_configure("timestamp", foreground=DEBUG_TIMESTAMP_FG)
        self.debug_area.tag_configure("error", foreground=DEBUG_ERROR_FG)
        self.debug_area.tag_configure("success", foreground=DEBUG_SUCCESS_FG)
        self.debug_area.tag_configure("warning", foreground=DEBUG_WARNING_FG)

        self.debug_menu = tk.Menu(self, tearoff=0)
        self.debug_menu.add_command(label="Copy All", command=self.copy_debug)
        self.debug_menu.add_command(label="Select All", command=self.select_debug)
        self.debug_menu.add_separator()
        self.debug_menu.add_command(label="Clear Log", command=self.clear_debug)
        self.debug_area.bind("<Button-3>", self._show_debug_menu)
        self.debug_area.bind("<Control-a>", lambda event: (self.select_debug(), "break")[1])

        # Only auto-scroll while the view is already at the bottom.
        self._debug_follow = True
        self.debug_area.bind("<Button-1>", lambda event: setattr(self, "_debug_follow", False))
        self.debug_area.bind("<ButtonRelease-1>",
                             lambda event: setattr(self, "_debug_follow", self._debug_at_bottom()))
        self.debug_area.bind("<MouseWheel>", self._on_debug_wheel)
        self._line_open = False
        self._debug_replace = False
        self._debug_count = 0
        # In-place overwrite mark for carriage-return progress ticks.
        self.debug_area.mark_set('debug_line', '1.0')
        self.debug_area.mark_gravity('debug_line', tk.LEFT)

    def _apply_window_icon(self):
        """Set the window/taskbar icon; keep a reference to avoid GC."""
        try:
            if platform.system() == 'Windows':
                # The multi-size .ico lets Windows pick a natively drawn
                # 16px titlebar icon instead of shrinking the 256px bitmap.
                self.iconbitmap(APP_ICON_ICO)
                return
            self._app_icon = tk.PhotoImage(file=APP_ICON_PATH)
            self.iconphoto(True, self._app_icon)
        except tk.TclError:
            pass  # Decorative; a missing or invalid icon must not block startup.

    def configure_styles(self):
        """Configures all ttk styles for the dark green palette."""
        style = ttk.Style(self)
        style.theme_use('clam')

        style.configure(".", background=COLOR_BG, foreground=COLOR_FG,
                        fieldbackground=COLOR_FIELD, bordercolor=COLOR_BORDER)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG,
                        font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=COLOR_BG, foreground=COLOR_FG_MUTED,
                        font=("Segoe UI", 9))
        style.configure("Hint.TLabel", background=COLOR_BG, foreground=COLOR_WARNING,
                        font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground=COLOR_FIELD, foreground=COLOR_FG,
                        insertcolor=COLOR_FG, bordercolor=COLOR_BORDER,
                        lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER)
        style.map("TEntry", bordercolor=[("focus", COLOR_PRIMARY)])

        style.configure("TLabelframe", background=COLOR_BG, foreground=COLOR_FG_MUTED,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER,
                        darkcolor=COLOR_BORDER, borderwidth=1)
        style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR_FG_MUTED,
                        font=("Segoe UI", 9))

        # Secondary/neutral buttons: quiet surface, hairline border.
        style.configure("TButton", font=("Segoe UI", 10), borderwidth=1, padding=10,
                        background=COLOR_SURFACE, foreground=COLOR_FG,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER,
                        darkcolor=COLOR_BORDER)
        style.map("TButton",
                  background=[("disabled", COLOR_DISABLED_BG), ("pressed", COLOR_SURFACE),
                              ("active", COLOR_SURFACE_HOVER)],
                  foreground=[("disabled", COLOR_DISABLED_FG)])

        style.configure("Secondary.TButton", background=COLOR_SURFACE, foreground=COLOR_FG)
        style.map("Secondary.TButton",
                  background=[("disabled", COLOR_DISABLED_BG), ("pressed", COLOR_SURFACE),
                              ("active", COLOR_SURFACE_HOVER)],
                  foreground=[("disabled", COLOR_DISABLED_FG)])

        # Primary: the one green accent, with dark text for AA contrast.
        style.configure("Primary.TButton", background=COLOR_PRIMARY,
                        foreground=COLOR_ON_PRIMARY, bordercolor=COLOR_PRIMARY)
        style.map("Primary.TButton",
                  background=[("disabled", COLOR_DISABLED_BG),
                              ("pressed", COLOR_PRIMARY_PRESS),
                              ("active", COLOR_PRIMARY_HOVER)],
                  foreground=[("disabled", COLOR_DISABLED_FG)],
                  bordercolor=[("disabled", COLOR_DISABLED_BG),
                               ("active", COLOR_PRIMARY_HOVER)])

        # Small, quiet toolbar buttons above the debug log.
        style.configure("LogTool.TButton", font=("Segoe UI", 9), padding=(8, 2),
                        background=COLOR_SURFACE, foreground=COLOR_FG_MUTED)
        style.map("LogTool.TButton",
                  background=[("disabled", COLOR_DISABLED_BG), ("pressed", COLOR_SURFACE),
                              ("active", COLOR_SURFACE_HOVER)],
                  foreground=[("disabled", COLOR_DISABLED_FG), ("active", COLOR_FG)])

        # Dark scrollbar matching the debug-log field (both orientations).
        style.configure("TScrollbar", background=COLOR_SURFACE,
                        troughcolor=COLOR_FIELD, bordercolor=COLOR_BORDER,
                        lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        arrowcolor=COLOR_FG_MUTED, gripcount=0)
        style.map("TScrollbar",
                  background=[("active", COLOR_SURFACE_HOVER),
                              ("pressed", COLOR_PRIMARY_PRESS)],
                  arrowcolor=[("active", COLOR_FG)])

    # --- Threading and Logic ---

    def update_buttons(self):
        """Single source of truth for control states, including the links gate."""
        try:
            links_ready = os.path.getsize(LINKS_FILE) > 0
        except OSError:
            links_ready = False
        self.browser_button.config(
            text="Browser is Open" if self.driver is not None else "Open Browser",
            state=tk.DISABLED if self.busy or self.driver is not None else tk.NORMAL)
        self.scrape_button.config(
            text="Scrape Links",
            state=tk.DISABLED if self.busy or self.driver is None else tk.NORMAL)
        self.download_button.config(
            text="Working…" if self.busy else "Download Collection",
            state=tk.DISABLED if self.busy or not links_ready else tk.NORMAL)
        self.download_hint.config(
            text="" if links_ready or self.busy
            else "Run scraping first — no links collected yet")
        if self.__dict__.get('remove_failed_button') is not None:
            self._refresh_recovery_button()

    def start_operation(self, target, on_success):
        if self.busy:
            return
        self.busy = True
        self.update_buttons()

        def work():
            try:
                result = target()
            except WebDriverException:
                driver, self.driver = self.driver, None
                if driver is not None:
                    try:
                        driver.quit()
                    except WebDriverException:
                        pass
                error = "The browser was closed or disconnected. Open it again and log in to Instagram."
                self.ui_queue.put((None, error, on_success))
            except AppError as error:
                self.ui_queue.put((None, str(error), on_success))
            except Exception:
                # Exception strings from browser/downloader libraries can contain session data.
                error = ("The operation failed. Check the debug log, internet connection, "
                         "and file permissions. For downloads, also check your Instagram "
                         "login and that the app's browser is still open on Instagram.")
                self.ui_queue.put((None, error, on_success))
            else:
                self.ui_queue.put((result, None, on_success))

        try:
            threading.Thread(target=work, daemon=True).start()
        except RuntimeError:
            self.busy = False
            self.update_buttons()
            messagebox.showerror("Worker Error", "Could not start a worker. Please try again.")

    def _update_folder_path(self):
        """Keep the helper line under the folder entry in sync while typing."""
        name = self.folder_name_var.get().strip()
        if name:
            self.folder_path_label.config(
                text=f"Saves to:  {os.path.join(BASE_DIR, name)}\n"
                "New downloads: videos in completed_videos/; photos in photos/.")
        else:
            self.folder_path_label.config(text="Enter a folder name above.")
        if self.__dict__.get('remove_failed_button') is not None:
            self._refresh_recovery_button()

    def _refresh_recovery_button(self):
        """Show cleanup only for failed media in the selected download folder."""
        name = self.folder_name_var.get().strip()
        nonempty = bool(name) and has_failed_media(os.path.join(BASE_DIR, name))
        if nonempty:
            if not self.remove_failed_button.winfo_manager():
                self.remove_failed_button.pack(fill=tk.X, ipady=5, pady=(8, 0))
            self.remove_failed_button.config(state=tk.DISABLED if self.busy else tk.NORMAL)
        else:
            self.remove_failed_button.pack_forget()

    def check_recovery_folder(self):
        """Notice recovery and top-level legacy-image changes."""
        if self.closing.is_set():
            return
        try:
            self._refresh_recovery_button()
            self.after(1000, self.check_recovery_folder)
        except tk.TclError:
            pass  # The window may have been destroyed since the closing check.

    def remove_failed_media_thread(self):
        """Confirm recovery cleanup and an explicit snapshot of loose images."""
        if self.busy or self.closing.is_set():
            return
        name = self.folder_name_var.get().strip()
        if not name:
            return
        # Capture the selected path before the dialog/worker; edits to the folder
        # field during cleanup must not change what was approved for deletion.
        download_dir = os.path.abspath(os.path.join(BASE_DIR, name))
        try:
            snapshot = tuple(media_download.legacy_image_candidates(download_dir))
            cleanup_available = has_failed_media(download_dir)
        except (OSError, AppError):
            messagebox.showerror(
                "Could Not Check Cleanup",
                "Could not check this folder. Check folder permissions and try again.",
                parent=self)
            return
        if not cleanup_available and not snapshot:
            self._refresh_recovery_button()
            return
        recovery = os.path.join(download_dir, RECOVERY_DIR_NAME)
        prompt = (f"Permanently delete recovery media in (if present):\n\n{recovery}\n")
        if snapshot:
            filenames = [repr(os.path.basename(path)) for path in snapshot[:10]]
            listing = '\n'.join(
                filename if len(filename) <= 100 else filename[:97] + '...'
                for filename in filenames)
            prompt += (
                f"\nAlso delete {len(snapshot)} legacy loose image(s) directly in:\n"
                f"{download_dir}\n\n"
                "WARNING: These loose images MAY include legitimate photos, "
                "not just bad thumbnails or frames. Review before deleting:\n\n"
                f"{listing}\n")
            if len(snapshot) > 10:
                prompt += f"... and {len(snapshot) - 10} more (not shown).\n"
            prompt += "\nChoose No to keep these images or inspect them in the download folder.\n"
        prompt += (
            "\nThe completed_videos/ and photos/ folders and MP4 files outside "
            "recovery will not be removed.")
        if not messagebox.askyesno(
                "Remove failed media?", prompt,
                parent=self, default=messagebox.NO):
            return
        # Tk dialogs run a nested event loop: another operation or shutdown may
        # have started while confirmation was open.
        if self.busy or self.closing.is_set():
            return

        def cleanup():
            try:
                remove_failed_media(download_dir, loose_images=snapshot)
            except OSError:
                raise AppError(
                    "Cleanup could not finish. Some files may already be removed. "
                    "Check folder permissions and remaining files before retrying.") from None

        self.start_operation(
            cleanup,
            lambda _: print(
                "Cleanup finished for the confirmed folder and loose-image selection. "
                "The completed_videos/ and photos/ folders were kept."))

    def view_videos(self):
        """Open the download folder from section 1 in the file explorer."""
        name = self.folder_name_var.get().strip()
        if not name:
            messagebox.showwarning("Invalid Folder", "Please enter a valid folder name first.")
            return
        path = os.path.join(BASE_DIR, name)
        if not os.path.isdir(path):
            messagebox.showinfo(
                "Nothing Here Yet",
                f"'{name}' does not exist yet.\n\nDownload a collection first. "
                "New videos go in completed_videos/ and photos in photos/ inside this folder.")
            return
        try:
            if platform.system() == 'Windows':
                os.startfile(path)  # noqa: S606 - opens Explorer, by design
            else:
                print(f"Videos folder: {path}")
        except OSError as error:
            messagebox.showerror("Could Not Open Folder", f"Could not open '{path}' ({error}).")

    def start_browser_thread(self):
        print("🚀 Open Browser button clicked.")
        if self.driver is None:
            self.start_operation(self.open_browser, self._on_browser_opened)
        else:
            print("⚠️ Browser already open.")

    def _on_browser_opened(self, _):
        """Callback when browser opens successfully - refresh button states immediately."""
        print("✅ Browser open callback, updating buttons...")
        self.update_buttons()

    def open_browser(self):
        """
        Runs in a thread. Opens the browser and stores the driver instance.
        """
        from .browser_cleanup import start_cleanup

        with self.driver_lock:
            if self.closing.is_set():
                return
            # Record launch intent so close also scans for a not-yet-published
            # browser. start_driver itself still needs early registration.
            self.browser_was_open = True
        print("🌐 [thread] Attempting to launch a browser...")
        driver = start_driver()
        with self.driver_lock:
            closing = self.closing.is_set()
            if not closing:
                self.driver = driver
                self.browser_was_open = True
        if closing:
            def cleanup():
                if not shutdown_driver(driver):
                    close_orphan_profile_chrome()
            start_cleanup(cleanup)
            return
        print("🌐 [thread] Browser is open. Log in to Instagram and navigate to your collection.")
        print("🔑 [thread] Your login cookies will be read automatically when downloading; no export is needed.")
        threading.Thread(target=self.monitor_login, args=(driver,), daemon=True).start()
        print("✅ [thread] Browser open complete, driver stored.")

    def monitor_login(self, driver):
        """Report whether a session was present on the first check or appeared later."""
        logged_in = False
        first_check = True
        navigation_attempted = False
        print("🔎 Checking Instagram login status...")
        while not self.closing.wait(2):
            if self.driver is not driver:
                return
            if self.busy:
                continue
            try:
                cookies = read_browser_cookies(driver)
            except AppError:
                if first_check:
                    print("🔑 No saved Instagram session detected. Log in in the browser.")
                logged_in = False
            except WebDriverException:
                print("⚠️ Could not check Instagram login: the browser disconnected.")
                return
            except Exception as error:
                print(f"⚠️ Could not check Instagram login ({type(error).__name__}).")
                return
            else:
                cookies.clear()
                if not logged_in:
                    if first_check:
                        print("✅ Existing Instagram session detected. Navigate to your collection and click Scrape Links.")
                    else:
                        print("✅ Instagram session detected after waiting for login. Navigate to your collection and click Scrape Links.")
                    logged_in = True
                if not navigation_attempted and not self.closing.is_set():
                    navigation_attempted = True
                    navigate_to_saved(driver)
            first_check = False

    def start_scraping_thread(self):
        if self.busy:
            print("⚠️ Scrape clicked but app is busy.")
            return
        if self.driver is None:
            print("⚠️ Scrape clicked but no browser driver.")
            messagebox.showwarning("No Browser", "Please open the browser first.")
            return

        print("🔎 Scrape button clicked — starting link scraping...")
        self.start_operation(self.scrape_links,
                             lambda count: messagebox.showinfo("Scraping Complete", scrape_summary(count)))

    def scrape_links(self):
        """
        Runs in a thread. Uses the existing driver to scrape links.
        """
        print("🔎 [thread] Starting scrape_links...")
        cookies = read_browser_cookies(self.driver)
        cookies.clear()
        print("✅ Logged into Instagram.")
        print("🔎 [thread] Cookies read, collecting links...")
        links = collect_reel_links(self.driver)
        print(f"🔎 [thread] collect_reel_links returned {len(links)} links")
        if not links:
            raise AppError(
                "No post links were collected. Open your saved collection's "
                "post grid, wait for thumbnails to load, and retry Scrape Links. "
                "Your existing saved links were kept.")
        save_links(links)
        print(f"✅ [thread] Scraping complete. {len(links)} links saved.")
        return len(links)

    def start_download_thread(self):
        if self.busy:
            return
        if self.driver is None:
            messagebox.showwarning("Login Required", "Open the browser and log in to Instagram first.")
            return

        if not os.path.exists(LINKS_FILE) or os.path.getsize(LINKS_FILE) == 0:
            print("⚠️ Error: links file is empty or missing.")
            messagebox.showerror("Links Not Found",
                                 f"Error: '{os.path.basename(LINKS_FILE)}' is empty or missing.\n\n"
                                 "Please use the scraping tool first to collect links.")
            return

        download_dir_name = self.folder_name_var.get().strip()
        if not download_dir_name:
            messagebox.showwarning("Invalid Folder", "Please enter a valid folder name.")
            return

        # Create absolute path relative to BASE_DIR
        download_dir_path = os.path.join(BASE_DIR, download_dir_name)

        print(f"📁 Download folder set to: {download_dir_path}")
        self.start_operation(lambda: self.download_videos(download_dir_path),
                             lambda result: messagebox.showinfo(
                                 "Download Complete", download_summary(result)))

    def download_videos(self, download_dir):
        cookies = read_browser_cookies(self.driver)
        try:
            print("🍪 Using the app browser's Instagram session in memory (no cookies file).")
            return start_download(download_dir, cookies)
        finally:
            cookies.clear()

    # --- Debug Log and stdout Redirection ---

    def redirect_stdout(self):
        sys.stdout = self.QueueStream(self.debug_queue)
        sys.stderr = self.QueueStream(self.debug_queue)
        # Mute third-party loggers that spam HTTP/debug lines into stdout.
        for name in ('urllib3', 'requests', 'yt_dlp', 'selenium'):
            logging.getLogger(name).setLevel(logging.WARNING)

    def write_to_debug(self, message):
        """Render streamed text without letting progress overwrite older entries."""
        if not message:
            return
        self.debug_area.config(state=tk.NORMAL)
        try:
            # print() writes its text and terminator separately; a carriage return
            # must apply to the next chunk, even when it arrives on another tick.
            for body in re.split(r'([\r\n])', message):
                if body == '\r':
                    self._debug_replace = True
                elif body == '\n':
                    if self._line_open:
                        self.debug_area.insert(tk.END, '\n')
                        self._debug_count += 1
                    self._line_open = False
                    self._debug_replace = False
                elif body and not self._is_request_noise(body):
                    if self._debug_replace and self._line_open:
                        self.debug_area.delete('end-1c linestart', 'end-1c lineend')
                        self._line_open = False
                    self._debug_replace = False
                    if not self._line_open:
                        self.debug_area.mark_set('debug_line', 'end-1c linestart')
                        self.debug_area.insert(tk.END, time.strftime("%H:%M:%S ") + "│ ", "timestamp")
                        self._line_open = True
                    self.debug_area.insert(tk.END, body, self._level_tag(body))
            if self._debug_follow or self._debug_at_bottom():
                self.debug_area.see(tk.END)
            self._update_debug_count()
        finally:
            self.debug_area.config(state=tk.DISABLED)

    def _overwrite_debug_line(self, body):
        """Replace the last line's content (for \r progress ticks)."""
        self.debug_area.delete('end-1c linestart', 'end-1c lineend')
        self.debug_area.insert(tk.END, body, self._level_tag(body))

    @staticmethod
    def _level_tag(text: str) -> str:
        """Choose a configured text tag without depending on a Tk attribute."""
        lower = text.lower()
        if any(word in lower for word in ('error', 'failed', 'exception', 'traceback')):
            return 'error'
        if any(word in lower for word in ('warning', 'could not', 'no saved')) or '⚠' in text:
            return 'warning'
        if any(word in lower for word in ('saved', 'finished', 'complete', 'session detected')) or '✅' in text:
            return 'success'
        return ''

    @staticmethod
    def _is_request_noise(text):
        """Return True for lines that are just HTTP/urllib3/yt-dlp spam."""
        t = text.lower()
        # Keep all app messages (they contain our emojis anywhere in the line).
        if any(emoji in text for emoji in
               ('🚀', '🌐', '🔑', '🔎', '✅', '⚠️', '🔗', '💾',
                '🍪', '📄', '📥', '📁', '📂', '🔄', '🛑', '📦', '🗂️')):
            return False
        # urllib3 / requests boilerplate
        if t.startswith(('get ', 'post ', 'put ', 'delete ', 'head ', 'options ', 'patch ')):
            return True
        # URLs that are ONLY noise — but keep lines with our keywords.
        if 'http://' in t or 'https://' in t:
            if any(kw in t for kw in ('saved', 'downloaded', 'complete', 'error',
                                       'failed', 'dropped', 'found', 'folder',
                                       'browser', 'scraping', 'launch', 'grid',
                                       'scrolling', 'links', 'cookie', 'session')):
                return False
            return True
        # yt-dlp progress ticks and format noise
        if t.startswith(('  downloading', '  finished', '  [download]')):
            return True
        if '[debug]' in t and 'writing' in t:
            return True
        # generic chatter
        if t.startswith(('retry', 'attempt')) and 'http' in t:
            return True
        return False

    def _update_debug_count(self):
        count = self._debug_count
        self.debug_count_label.config(
            text=f"{count} {'entry' if count == 1 else 'entries'}")

    def _debug_at_bottom(self):
        return self.debug_area.yview()[1] >= 0.999

    def _on_debug_wheel(self, event):
        """Wheel-up pauses auto-follow; reaching the bottom re-enables it."""
        if event.delta > 0:
            self._debug_follow = False
        return None  # Let the Text widget perform the normal scroll.

    def _show_debug_menu(self, event):
        self.debug_menu.tk_popup(event.x_root, event.y_root)

    def copy_debug(self):
        content = self.debug_area.get("1.0", tk.END)
        self.clipboard_clear()
        self.clipboard_append(content)

    def select_debug(self):
        self.debug_area.tag_add("sel", "1.0", tk.END)

    def clear_debug(self):
        self.debug_area.config(state=tk.NORMAL)
        self.debug_area.delete("1.0", tk.END)
        self.debug_area.config(state=tk.DISABLED)
        self._line_open = False
        self._debug_replace = False
        self._debug_count = 0
        self.debug_area.mark_set('debug_line', '1.0')
        self._update_debug_count()

    def check_debug_queue(self):
        """Dispatch bounded batches on Tk's thread, surviving callback failures."""
        def app_alive() -> bool:
            if self.closing.is_set():
                return False
            try:
                return bool(self.winfo_exists())
            except tk.TclError:
                return False

        def report_completion_failure() -> None:
            # Callback exceptions can contain cookies or signed URLs.
            message = ("Could not display the operation result. Check the download "
                       "folder and debug log before retrying.")
            print(message)
            if app_alive():
                try:
                    messagebox.showerror("Completion Error", message)
                except Exception:
                    # A failing dialog must not disable future queue processing.
                    pass

        try:
            # Independent budgets prevent either continuous producer from starving
            # the other queue or the rest of Tk's event loop.
            for _ in range(20):
                if not app_alive():
                    return
                try:
                    completion = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                self.busy = False
                try:
                    self.update_buttons()
                    result, error, on_success = completion
                    if not app_alive():
                        return
                    if error:
                        print(error)
                        messagebox.showerror("Operation Failed", error)
                    else:
                        on_success(result)
                except Exception:
                    report_completion_failure()
            for _ in range(100):
                if not app_alive():
                    return
                try:
                    message = self.debug_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.write_to_debug(message)
                except Exception:
                    # stderr is redirected to this same queue. Printing here
                    # would create an endless error -> render -> error loop.
                    try:
                        if sys.__stderr__ is not None:
                            sys.__stderr__.write("Could not display a debug log entry.\n")
                    except Exception:
                        pass
                    break
        finally:
            if app_alive():
                try:
                    self.after(100, self.check_debug_queue)
                except tk.TclError:
                    pass  # Destruction can occur between the check and scheduling.

    class QueueStream:
        def __init__(self, queue):
            self.queue = queue

        def write(self, text):
            self.queue.put(text)

        def flush(self):
            pass

    def on_closing(self):
        """Close the window even when an operation or browser call is stuck."""
        from .browser_cleanup import start_cleanup

        with self.driver_lock:
            if self.closing.is_set():
                return
            self.closing.set()
            driver, self.driver = self.driver, None
            browser_was_open, self.browser_was_open = self.browser_was_open, False
        def cleanup():
            if driver is not None:
                if not shutdown_driver(driver):
                    close_orphan_profile_chrome()
            elif browser_was_open:
                close_orphan_profile_chrome()
        # Register the bounded non-daemon owner before root/mainloop disappears.
        # Only this main-thread caller touches Tk, never the cleanup worker.
        try:
            start_cleanup(cleanup)
        except RuntimeError:
            print("Could not start browser cleanup worker.")
        finally:
            self.destroy()



def shutdown_driver(driver):
    """Quit within a finite budget; False requests owned-profile fallback."""
    from .browser_cleanup import quit_driver

    return quit_driver(driver)


def close_orphan_profile_chrome():
    """Boundedly force only Chrome trees using the exact app profile."""
    from .browser_cleanup import close_profile_browser

    return close_profile_browser(CHROME_PROFILE_DIR)


def _set_windows_taskbar_identity():
    """Give the process its own Windows AppUserModelID so the taskbar uses
    the window icon instead of the python.exe icon. No-op elsewhere."""
    if platform.system() != 'Windows':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('scoopy.instagram.collections.0.1.0')
    except OSError:
        pass


def download_summary(result: dict[str, object]) -> str:
    """Completion message for a download batch, with correct pluralization."""
    if not isinstance(result, dict):
        raise ValueError("Invalid download summary.")
    downloaded, silent_count = result.get('downloaded'), result.get('silent')
    photo_count = result.get('photos', 0)
    if (type(downloaded) is not int or type(silent_count) is not int
            or type(photo_count) is not int or photo_count < 0
            or downloaded < 0 or not 0 <= silent_count <= downloaded
            or result.get('failed')):
        raise ValueError("Invalid download summary.")
    videos = "1 video" if result['downloaded'] == 1 else f"{result['downloaded']} videos"
    silent = "1 without audio" if result['silent'] == 1 else f"{result['silent']} without audio"
    if photo_count:
        photos = "1 photo" if photo_count == 1 else f"{photo_count} photos"
        if downloaded == 0:
            return f"Downloaded {photos}."
        return f"Downloaded {photos} and {videos} ({silent})."
    return f"Downloaded {videos} ({silent})."


def scrape_summary(count):
    """Completion message for a scrape, with correct pluralization."""
    if count == 0:
        return "Saved 0 links. Did something go wrong?"
    if count == 1:
        return "Saved 1 link. You can now download your photos and videos."
    return f"Saved {count} links. You can now download your photos and videos."


def main():
    from .interrupts import make_interrupt_handler

    try:
        signal.signal(signal.SIGINT, make_interrupt_handler())
    except (OSError, ValueError):
        pass  # Signal registration may be unavailable outside the main thread.

    _set_windows_taskbar_identity()
    app = DownloaderApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
