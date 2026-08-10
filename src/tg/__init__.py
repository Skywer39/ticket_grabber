"""ticket-grabber: self-hosted availability monitoring for ticketing sites."""

from __future__ import annotations

import os

__version__ = "0.1.0"


def running_revision() -> str | None:
    """Short commit this process is running, when the environment names one.

    A polling session is sized in hours and outlives several merges, so "is my change
    live yet?" is a real question with a genuinely non-obvious answer — two fixes once
    sat undeployed for a day behind a session that had started before them, and nothing
    in the alerts said so. Stamping the revision on what the poller sends makes that
    answerable from wherever the alerts are read.

    ``GITHUB_SHA`` is exported by Actions; ``TG_REVISION`` is the override for anywhere
    else. ``None`` when neither is set, and callers then say nothing rather than guess.
    """
    rev = os.environ.get("TG_REVISION") or os.environ.get("GITHUB_SHA")
    return rev[:7] if rev else None
