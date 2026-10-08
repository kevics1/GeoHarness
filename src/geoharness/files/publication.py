"""Publish prepared directories without repeating the work that produced them."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from geoharness.config.settings import PublicationConfig


class DirectoryPublicationError(OSError):
    """A publication failure with bounded diagnostics and a recoverable source."""

    def __init__(self, diagnostics: dict[str, Any]):
        self.diagnostics = diagnostics
        last_error = diagnostics["errors"][-1]
        super().__init__(
            last_error["errno"],
            f"Directory publication failed after {diagnostics['attempts']} attempt(s): "
            f"{last_error['message']} (winerror={last_error['winerror']}). "
            f"Prepared files retained at {diagnostics['source']}; "
            f"destination: {diagnostics['destination']}.",
        )


def _error_details(error: OSError) -> dict[str, Any]:
    return {
        "type": type(error).__name__,
        "errno": error.errno,
        "winerror": getattr(error, "winerror", None),
        "message": (error.strerror or type(error).__name__)[:240],
    }


def publish_directory(
    staging: Path,
    destination: Path,
    *,
    settings: PublicationConfig | None = None,
    before_attempt: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Rename prepared output, retrying only Windows access/sharing errors.

    Callers close their writers before publication. The source is never deleted on
    failure, and existing destinations are never removed or deliberately replaced.
    ``before_attempt`` can enforce an analysis deadline before each rename.
    An interruption keeps its original exception type and carries ``diagnostics``
    so callers can retain completed work without reporting publication success.
    """
    settings = settings or PublicationConfig()
    staging = Path(staging).resolve()
    destination = Path(destination).resolve()
    diagnostics: dict[str, Any] = {
        "status": "failed", "attempts": 0,
        "source": str(staging), "destination": str(destination), "errors": [],
    }
    for attempt in range(1, settings.max_attempts + 1):
        if before_attempt is not None:
            try:
                before_attempt()
            except BaseException as error:
                diagnostics["status"] = "interrupted"
                diagnostics["interruption"] = {
                    "type": type(error).__name__, "message": str(error)[:240],
                }
                error.diagnostics = diagnostics
                raise
        # Never delete an existing result to work around a failed rename. Check
        # before every attempt, since another publisher may finish while waiting.
        if destination.exists() or destination.is_symlink():
            diagnostics["errors"].append(_error_details(
                FileExistsError(errno.EEXIST, "Destination already exists")
            ))
            raise DirectoryPublicationError(diagnostics)
        diagnostics["attempts"] = attempt
        try:
            staging.rename(destination)
        except OSError as error:
            diagnostics["errors"].append(_error_details(error))
            transient = os.name == "nt" and getattr(error, "winerror", None) in (5, 32)
            if not transient or attempt == settings.max_attempts:
                raise DirectoryPublicationError(diagnostics) from error
            time.sleep(settings.retry_delay_seconds)
        else:
            diagnostics["status"] = "published"
            return diagnostics
    raise AssertionError("Publication settings must allow at least one attempt")
