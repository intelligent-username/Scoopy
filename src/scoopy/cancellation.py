"""Shared cooperative cancellation for background operations."""
import time
from threading import Event


class OperationCancelled(Exception):
    """An operation stopped at a safe cancellation boundary."""

    def __init__(self, message: str = "Operation cancelled"):
        super().__init__(message)


def check_cancel(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled()


def wait_cancel(cancel_event: Event | None, seconds: float) -> None:
    if cancel_event is None:
        time.sleep(seconds)
    elif cancel_event.wait(seconds):
        raise OperationCancelled()
