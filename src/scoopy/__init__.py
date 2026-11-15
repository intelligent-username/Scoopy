"""Scoopy: download media from Instagram saved collections."""

from __future__ import annotations

import sys


def _harden_console_streams() -> None:
    """Keep status output from crashing on legacy console codepages.

    Windows consoles default to codepages such as cp1252, which cannot encode
    the emoji used in progress messages. An unencodable character raises
    UnicodeEncodeError and aborts whatever worker thread printed it, so degrade
    unencodable characters instead of failing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # Redirected, detached, or already-finalised streams are fine as-is.
            pass


_harden_console_streams()
