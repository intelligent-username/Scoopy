"""Bounded cleanup primitives; importing this module never starts a worker."""

import platform
import subprocess
import threading
from collections.abc import Callable
from typing import Protocol

QUIT_TIMEOUT = 2.0
FORCE_TIMEOUT = 3.0
CLEANUP_TIMEOUT = 6.0


class BrowserDriver(Protocol):
    def quit(self) -> None:
        """Close the owned browser session."""


def run_bounded(action: Callable[[], object], timeout: float) -> bool:
    """Run potentially stuck third-party code without an unbounded join."""
    completed = threading.Event()
    succeeded = threading.Event()

    def work() -> None:
        try:
            action()
        except Exception as error:
            # Third-party exception text may contain session data.
            print(f"Browser cleanup failed ({type(error).__name__}).")
        else:
            succeeded.set()
        finally:
            completed.set()

    try:
        threading.Thread(target=work, daemon=True).start()
    except RuntimeError:
        return False
    return completed.wait(timeout) and succeeded.is_set()


def start_cleanup(action: Callable[[], object]) -> threading.Thread:
    """Keep cleanup alive after Tk exits, but for at most CLEANUP_TIMEOUT."""
    supervisor = threading.Thread(
        target=run_bounded, args=(action, CLEANUP_TIMEOUT),
        name="scoopy-browser-cleanup", daemon=False,
    )
    supervisor.start()
    return supervisor


def quit_driver(driver: BrowserDriver | None) -> bool:
    """Give graceful quit a finite budget, even if its HTTP request hangs."""
    return driver is not None and run_bounded(driver.quit, QUIT_TIMEOUT)


def profile_cleanup_script(profile: str) -> str:
    """Build a literal, whole-argument Windows profile match, not a wildcard."""
    # Match Chrome's three command-line quoting forms. Escape the PowerShell
    # string separately from regex escaping; paths cannot inject script code.
    literal = profile.replace("'", "''")
    return (
        f"$profile = '{literal}'; "
        "$escaped = [regex]::Escape($profile); "
        "$pattern = '(?:^|\\s)(?:\"--user-data-dir=' + $escaped + "
        "'\"|--user-data-dir=\"' + $escaped + '\"|--user-data-dir=' + "
        "$escaped + ')(?=\\s|$)'; "
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -match $pattern } | "
        "ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F | Out-Null }"
    )


def close_profile_browser(profile: str) -> bool:
    """Force only exact-profile Chrome trees on Windows, with a finite budget."""
    if platform.system() != "Windows":
        print("Owned-profile force cleanup is only supported on Windows.")
        return False

    def force() -> None:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             profile_cleanup_script(profile)],
            capture_output=True, timeout=FORCE_TIMEOUT, check=True,
        )

    return run_bounded(force, FORCE_TIMEOUT)
