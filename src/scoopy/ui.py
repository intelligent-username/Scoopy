import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from selenium.common.exceptions import WebDriverException

from .browser import start_driver, collect_reel_links, save_links
from .download import start_download
from .paths import BASE_DIR, LINKS_FILE
from .session import AppError, read_browser_cookies

# --- Constants ---
APP_TITLE = "Instagram Collections Downloader Pro"
WINDOW_WIDTH = 600
WINDOW_HEIGHT = 550

# --- COLOR PALETTE ---
COLOR_BG = "#000000"
COLOR_BG_LIGHT = "#4A5A70"
COLOR_FG = "#FFFFFF"
COLOR_PRIMARY = "#4CAF50"
COLOR_PRIMARY_HOVER = "#45a049"
COLOR_SECONDARY = "#008CBA"
COLOR_SECONDARY_HOVER = "#007B9E"
COLOR_DANGER = "#f44336"
COLOR_DANGER_HOVER = "#e53935"


# --- Main GUI Application Class ---
class DownloaderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        
        self.driver = None  
        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self.busy = False

        self.setup_ui()
        self.configure_styles()
        self.redirect_stdout()
        self.check_log_queue()

    def setup_ui(self):
        """Initializes and lays out all UI components."""
        self.title(APP_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.configure(bg=COLOR_BG)
        self.resizable(False, False)

        main_frame = ttk.Frame(self, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        folder_frame = ttk.Labelframe(main_frame, text=" 1. Download Folder ", padding=10)
        folder_frame.pack(fill=tk.X, pady=(0, 15))
        
        ttk.Label(folder_frame, text="Folder Name:").pack(side=tk.LEFT, padx=(0, 10))
        
        self.folder_name_var = tk.StringVar(value="videos")
        self.folder_entry = ttk.Entry(folder_frame, textvariable=self.folder_name_var, width=60)
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        scraping_frame = ttk.Labelframe(main_frame, text=" 2. Scrape Video Links ", padding=15)
        scraping_frame.pack(fill=tk.X, pady=15)

        self.browser_button = ttk.Button(scraping_frame, text="Open Browser for Login", 
                                         command=self.start_browser_thread, style="Secondary.TButton")
        self.browser_button.pack(fill=tk.X, ipady=5)

        self.scrape_button = ttk.Button(scraping_frame, text="Start Scraping Links", 
                                        command=self.start_scraping_thread, state=tk.DISABLED)
        self.scrape_button.pack(fill=tk.X, ipady=5, pady=(10, 0))

        download_frame = ttk.Labelframe(main_frame, text=" 3. Download Videos ", padding=15)
        download_frame.pack(fill=tk.X, pady=15)

        self.download_button = ttk.Button(download_frame, text="Download All Videos", 
                                          command=self.start_download_thread, style="Primary.TButton")
        self.download_button.pack(fill=tk.X, ipady=10)

        log_frame = ttk.Labelframe(main_frame, text=" Status Log ", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(15, 0))

        self.log_area = scrolledtext.ScrolledText(log_frame, height=10, width=70, state=tk.DISABLED,
                                                  bg=COLOR_BG_LIGHT, fg=COLOR_FG,
                                                  font=("Inter", 9))
        self.log_area.pack(fill=tk.BOTH, expand=True)
        
    def configure_styles(self):
        """Configures all ttk styles for the professional look."""
        style = ttk.Style(self)
        style.theme_use('clam')

        style.configure(".", background=COLOR_BG, foreground=COLOR_FG, fieldbackground=COLOR_BG_LIGHT)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG, font=("Inter", 10))
        style.configure("TEntry", fieldbackground=COLOR_BG_LIGHT, foreground=COLOR_FG, insertcolor=COLOR_FG)
        style.configure("TLabelframe", background=COLOR_BG, foreground=COLOR_FG, font=("Inter", 11, "bold"))
        style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR_FG)

        style.map("TButton",
                  foreground=[('!active', COLOR_FG), ('active', COLOR_FG)],
                  background=[('!active', COLOR_SECONDARY), ('active', COLOR_SECONDARY_HOVER)])
        
        style.configure("TButton", font=("Inter", 10, "bold"), borderwidth=0, padding=10)
        
        style.configure("Primary.TButton", background=COLOR_PRIMARY)
        style.map("Primary.TButton",
                  background=[('!active', COLOR_PRIMARY), ('active', COLOR_PRIMARY_HOVER)])

        style.configure("Secondary.TButton", background=COLOR_SECONDARY)
        style.map("Secondary.TButton",
                  background=[('!active', COLOR_SECONDARY), ('active', COLOR_SECONDARY_HOVER)])
        
        style.map("TButton",
                  background=[('disabled', '#555')],
                  foreground=[('disabled', '#999')])

    # --- Threading and Logic ---

    def update_buttons(self):
        self.browser_button.config(
            text="Browser is Open" if self.driver is not None else "Open Browser for Login",
            state=tk.DISABLED if self.busy or self.driver is not None else tk.NORMAL)
        self.scrape_button.config(
            text="Start Scraping Links",
            state=tk.DISABLED if self.busy or self.driver is None else tk.NORMAL)
        self.download_button.config(
            text="Working... See log" if self.busy else "Download All Videos",
            state=tk.DISABLED if self.busy else tk.NORMAL)

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
                error = ("The operation failed. Check the status log, internet connection, "
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

    # --- Log and stdout Redirection ---

    def redirect_stdout(self):
        sys.stdout = self.QueueStream(self.log_queue)
        sys.stderr = self.QueueStream(self.log_queue)

    def write_to_log(self, message):
        self.log_area.config(state=tk.NORMAL)
        self.log_area.insert(tk.END, message)
        self.log_area.see(tk.END) # Auto-scroll
        self.log_area.config(state=tk.DISABLED)

    def check_log_queue(self):
        while not self.ui_queue.empty():
            result, error, on_success = self.ui_queue.get_nowait()
            self.busy = False
            self.update_buttons()
            if error:
                print(error)
                messagebox.showerror("Operation Failed", error)
            else:
                on_success(result)
        while not self.log_queue.empty():
            try:
                message = self.log_queue.get_nowait()
                self.write_to_log(message)
            except queue.Empty:
                pass
        
        self.after(100, self.check_log_queue)

    class QueueStream:
        def __init__(self, queue):
            self.queue = queue
        
        def write(self, text):
            self.queue.put(text)
        
        def flush(self):
            pass

    def on_closing(self):
        """Handle window close event."""
        if self.busy:
            messagebox.showwarning("Operation Running", "Please wait for the current operation to finish before closing.")
            return
        if self.driver is not None:
            print("Closing browser...")
            try:
                self.driver.quit()
            except Exception as e:
                print(f"Error quitting driver: {e}")
        self.destroy()



def main():
    app = DownloaderApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
