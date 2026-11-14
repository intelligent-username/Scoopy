"""Abrupt terminal shutdown, deliberately independent of Tk and graceful close."""

from collections.abc import Callable
import os
from pathlib import Path
import subprocess
import threading
from types import FrameType


INTERRUPT_EXIT_CODE = 130
INTERRUPT_TIMEOUT_SECONDS = 2.0
TREE_TIMEOUT_SECONDS = 1.5


def _terminate_windows_tree() -> None:
    """Force only this process and its descendants, never browser image names."""
    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    subprocess.run(
        [str(taskkill), "/PID", str(os.getpid()), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=TREE_TIMEOUT_SECONDS,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def make_interrupt_handler() -> Callable[[int, FrameType | None], None]:
    """Build a SIGINT handler with a bounded, terminal-only hard-exit path.

    Windows taskkill can terminate us before exit(130) executes. Other platforms
    exit(130) without attempting descendant cleanup: no safe portable owned-tree
    primitive is available here. A second SIGINT skips cleanup and exits at once.
    The watchdog also covers a stuck process-launch/cleanup call, provided Python
    threads can still run. Neither path invokes Tk, logging, or graceful quit.
    """
    interrupted = False

    def handle_interrupt(signum: int, frame: FrameType | None) -> None:
        nonlocal interrupted
        if interrupted:
            os._exit(INTERRUPT_EXIT_CODE)
            return
        interrupted = True
        try:
            if os.name == "nt":
                watchdog = threading.Timer(
                    INTERRUPT_TIMEOUT_SECONDS, os._exit, args=(INTERRUPT_EXIT_CODE,)
                )
                watchdog.daemon = True
                watchdog.start()
                _terminate_windows_tree()
        except (OSError, subprocess.SubprocessError):
            # Terminal termination must survive missing taskkill or timeout.
            pass
        finally:
            os._exit(INTERRUPT_EXIT_CODE)

    return handle_interrupt
