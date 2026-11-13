import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from selenium.common.exceptions import WebDriverException

from .browser import start_driver, collect_reel_links, save_links
from .download import start_download
from .paths import BASE_DIR, CHROME_PROFILE_DIR, LINKS_FILE
from .session import AppError, read_browser_cookies

# --- Constants ---
APP_TITLE = "Instagram Collections Downloader Pro"
WINDOW_WIDTH = 600
WINDOW_HEIGHT = 580

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
DEBUG_TIMESTAMP_FG = "#78857B"
DEBUG_ERROR_FG = "#F27B72"
DEBUG_SUCCESS_FG = "#7ED488"
DEBUG_WARNING_FG = "#E2B455"


# --- Main GUI Application Class ---
class DownloaderApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.driver = None
        self.browser_was_open = False
        self.debug_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self.busy = False

        self.setup_ui()
        self.configure_styles()
        self.redirect_stdout()
        self.check_debug_queue()

    def setup_ui(self):
        """Initializes and lays out all UI components."""
        self.title(APP_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.configure(bg=COLOR_BG)
        self.resizable(False, False)

        main_frame = ttk.Frame(self, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        folder_frame = ttk.Labelframe(main_frame, text="1. Download Folder", padding=12)
        folder_frame.pack(fill=tk.X, pady=(0, 16))

        ttk.Label(folder_frame, text="Folder Name:").pack(side=tk.LEFT, padx=(0, 12))

        self.folder_name_var = tk.StringVar(value="videos")
        self.folder_entry = ttk.Entry(folder_frame, textvariable=self.folder_name_var, width=60)
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        scraping_frame = ttk.Labelframe(main_frame, text="2. Scrape Video Links", padding=12)
        scraping_frame.pack(fill=tk.X, pady=16)

        self.browser_button = ttk.Button(scraping_frame, text="Open Browser for Login",
                                         command=self.start_browser_thread, style="Secondary.TButton")
        self.browser_button.pack(fill=tk.X, ipady=5)

        self.scrape_button = ttk.Button(scraping_frame, text="Start Scraping Links",
                                        command=self.start_scraping_thread, state=tk.DISABLED)
        self.scrape_button.pack(fill=tk.X, ipady=5, pady=(8, 0))

        download_frame = ttk.Labelframe(main_frame, text="3. Download Videos", padding=12)
        download_frame.pack(fill=tk.X, pady=16)

        self.download_button = ttk.Button(download_frame, text="Download All Videos",
                                          command=self.start_download_thread, style="Primary.TButton")
        self.download_button.pack(fill=tk.X, ipady=10)

        # Inline empty-state hint, toggled by update_buttons() when links.txt
        # is missing or empty so the disabled download button explains itself.
        self.download_hint = ttk.Label(download_frame, text="", style="Hint.TLabel")
        self.download_hint.pack(fill=tk.X, pady=(8, 0))

        debug_frame = ttk.Labelframe(main_frame, text="Debug Log", padding=12)
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
            debug_frame, height=10, width=70, state=tk.DISABLED, wrap=tk.WORD,
            bg=COLOR_FIELD, fg=COLOR_FG, font=("Consolas", 9),
            insertbackground=COLOR_FG, selectbackground=COLOR_SELECT,
            spacing1=2, spacing3=2)
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
        self._debug_line_open = False
        self._debug_count = 0

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

    # --- Threading and Logic ---

    def update_buttons(self):
        """Single source of truth for control states, including the links gate."""
        try:
            links_ready = os.path.getsize(LINKS_FILE) > 0
        except OSError:
            links_ready = False
        self.browser_button.config(
            text="Browser is Open" if self.driver is not None else "Open Browser for Login",
            state=tk.DISABLED if self.busy or self.driver is not None else tk.NORMAL)
        self.scrape_button.config(
            text="Start Scraping Links",
            state=tk.DISABLED if self.busy or self.driver is None else tk.NORMAL)
        self.download_button.config(
            text="Working…" if self.busy else "Download All Videos",
            state=tk.DISABLED if self.busy or not links_ready else tk.NORMAL)
        self.download_hint.config(
            text="" if links_ready or self.busy
            else "Run scraping first — no links collected yet")

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

    def start_browser_thread(self):
        print("Starting browser thread...")
        if self.driver is None:
            self.start_operation(self.open_browser, lambda _: None)

    def open_browser(self):
        """
        Runs in a thread. Opens the browser and stores the driver instance.
        """
        print("Attempting to launch a browser...")
        self.driver = start_driver()
        self.browser_was_open = True
        print("Browser is open. Log in to Instagram and navigate to your collection.")
        print("Your login cookies will be read automatically when downloading; no export is needed.")

    def start_scraping_thread(self):
        if self.busy:
            return
        if self.driver is None:
            messagebox.showwarning("No Browser", "Please open the browser first.")
            return

        print("Starting link scraping...")
        self.start_operation(self.scrape_links, lambda count: messagebox.showinfo(
            "Scraping Complete", f"Saved {count} links. You can now download your videos."))

    def scrape_links(self):
        """
        Runs in a thread. Uses the existing driver to scrape links.
        """
        read_browser_cookies(self.driver)
        links = collect_reel_links(self.driver)
        save_links(links)
        print(f"Scraping complete. {len(links)} links saved.")
        return len(links)

    def start_download_thread(self):
        if self.busy:
            return
        if self.driver is None:
            messagebox.showwarning("Login Required", "Open the browser and log in to Instagram first.")
            return

        if not os.path.exists(LINKS_FILE) or os.path.getsize(LINKS_FILE) == 0:
            print(f"Error: {LINKS_FILE} is empty or missing.")
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

        print(f"Download folder set to: {download_dir_path}")
        self.start_operation(lambda: self.download_videos(download_dir_path),
                             lambda result: messagebox.showinfo(
                                 "Download Complete",
                                 f"Downloaded {result['downloaded']} videos "
                                 f"({result['silent']} without audio)."))

    def download_videos(self, download_dir):
        cookies = read_browser_cookies(self.driver)
        try:
            print("Using the app browser's Instagram session in memory (no cookies file).")
            start_download(download_dir, cookies)
        finally:
            cookies.clear()

    # --- Debug Log and stdout Redirection ---

    def redirect_stdout(self):
        sys.stdout = self.QueueStream(self.debug_queue)
        sys.stderr = self.QueueStream(self.debug_queue)

    def write_to_debug(self, message):
        """Append message(s) to the debug log with timestamps and level tags."""
        if not message:
            return
        self.debug_area.config(state=tk.NORMAL)
        for line in message.splitlines(keepends=True):
            body = line
            newline = ''
            if body.endswith('\n'):
                newline = '\n'
                body = body[:-1]
            if body.endswith('\r'):
                body = body[:-1]
            if body:
                if not self._debug_line_open:
                    self.debug_area.insert(tk.END, time.strftime("%H:%M:%S ") + "│ ", "timestamp")
                    self._debug_line_open = True
                self.debug_area.insert(tk.END, body, self._level_tag(body))
            if newline:
                self.debug_area.insert(tk.END, newline)
                self._debug_line_open = False
                self._debug_count += 1
        if self._debug_follow:
            self.debug_area.see(tk.END)
        self.debug_area.config(state=tk.DISABLED)
        self._update_debug_count()

    @staticmethod
    def _level_tag(text):
        lowered = text.lower()
        if 'error' in lowered or 'failed' in lowered or '❌' in text:
            return "error"
        if 'warning' in lowered or 'retry' in lowered:
            return "warning"
        if '✅' in text or 'complete' in lowered:
            return "success"
        return ()

    def _update_debug_count(self):
        count = self._debug_count
        self.debug_count_label.config(
            text=f"{count} {'entry' if count == 1 else 'entries'}")

    def _debug_at_bottom(self):
        return self.debug_area.yview()[1] >= 0.999

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
        self._debug_line_open = False
        self._debug_count = 0
        self._update_debug_count()

    def check_debug_queue(self):
        while not self.ui_queue.empty():
            result, error, on_success = self.ui_queue.get_nowait()
            self.busy = False
            self.update_buttons()
            if error:
                print(error)
                messagebox.showerror("Operation Failed", error)
            else:
                on_success(result)
        while not self.debug_queue.empty():
            try:
                message = self.debug_queue.get_nowait()
                self.write_to_debug(message)
            except queue.Empty:
                pass

        self.after(100, self.check_debug_queue)

    class QueueStream:
        def __init__(self, queue):
            self.queue = queue

        def write(self, text):
            self.queue.put(text)

        def flush(self):
            pass

    def on_closing(self):
        """Handle window close event: always shut down the managed browser."""
        if self.busy:
            messagebox.showwarning("Operation Running", "Please wait for the current operation to finish before closing.")
            return
        driver, self.driver = self.driver, None
        browser_was_open, self.browser_was_open = self.browser_was_open, False
        if driver is not None:
            graceful = shutdown_driver(driver)
            # quit() can fail on a dead session and leave Chrome orphaned.
            if not graceful:
                close_orphan_profile_chrome()
        elif browser_was_open:
            # A previous cleanup already dropped the driver; make sure no
            # Chrome window bound to our profile survived it.
            close_orphan_profile_chrome()
        self.destroy()



def shutdown_driver(driver):
    """Quit the WebDriver gracefully. Returns True when the session closed."""
    if driver is None:
        return False
    print("Closing browser...")
    try:
        driver.quit()
        return True
    except Exception as error:
        # Exception text can contain session data; log the category only.
        print(f"Graceful browser close failed ({type(error).__name__}); forcing shutdown.")
        return False


def close_orphan_profile_chrome():
    """Force-close leftover chrome.exe processes bound to the app's own profile.

    The dedicated .chrome-profile path is the filter, so only the app's
    managed browser is ever targeted — never the user's installed Chrome.
    """
    profile = CHROME_PROFILE_DIR.replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Could not force-close browser processes ({type(error).__name__}).")


def main():
    app = DownloaderApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
