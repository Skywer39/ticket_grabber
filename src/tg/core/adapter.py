"""The contract every source adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from tg.core.normalize import NormEvent, NormScreening, NormVenue, SeatMap

if TYPE_CHECKING:
    from tg.adapters.base_http import PoliteClient
    from tg.config import SourceConfig


class Capability(StrEnum):
    """What a given adapter can actually do.

    Watches consult these so a rule can degrade gracefully instead of failing: asking
    for seat contiguity against an adapter without ``SEATMAP`` falls back to
    ratio-based alerting and says so in the notification.
    """

    VENUES = "VENUES"
    EVENTS = "EVENTS"
    SCREENINGS = "SCREENINGS"
    #: Source offers a cheap "which dates have anything on sale" probe. This is the
    #: single most valuable capability for catching an early program release, because
    #: it turns a whole-horizon sweep into one request.
    CALENDAR = "CALENDAR"
    AVAILABILITY_RATIO = "AVAILABILITY_RATIO"
    SEATMAP = "SEATMAP"


class AdapterError(RuntimeError):
    """Raised when a source responds but its shape is not what the adapter expects.

    Distinct from transport errors: this is the signal that the site changed and the
    adapter needs healing.
    """


class SourceAdapter(ABC):
    #: Stable identifier used in config and as the ``source`` on every normalized record.
    key: str
    capabilities: set[Capability] = set()

    def __init__(self, key: str, config: SourceConfig, client: PoliteClient) -> None:
        self.key = key
        self.config = config
        self.client = client
        self.options: dict[str, Any] = dict(config.options)

    async def setup(self) -> None:  # noqa: B027 — optional hook, not every adapter needs it
        """Optional one-time preparation (e.g. deriving a tenant id from the site)."""

    @abstractmethod
    async def venues(self) -> list[NormVenue]:
        """All venues this source covers."""

    async def calendar(self, since: date, until: date) -> list[date] | None:
        """Cheap probe for which dates have anything on sale.

        Returns ``None`` when the source has no such endpoint, in which case callers
        fall back to sweeping the full window.
        """
        return None

    @abstractmethod
    async def screenings(
        self, since: date, until: date, dates: list[date] | None = None
    ) -> tuple[list[NormEvent], list[NormScreening]]:
        """Everything on sale in the window.

        Returns events and screenings together because most sites publish them in a
        single response and splitting would double the request count. ``dates``
        narrows the fetch to specific days when the caller already knows which ones
        changed.
        """

    async def seatmap(self, screening: NormScreening) -> SeatMap | None:
        """Exact seat availability. ``None`` when the adapter cannot provide it."""
        return None

    async def aclose(self) -> None:  # noqa: B027 — only browser-backed adapters override
        """Release adapter-owned resources (browsers, etc.). The HTTP client is
        owned by the caller and is not closed here."""


_REGISTRY: dict[str, type[SourceAdapter]] = {}


def register_adapter(name: str):
    """Class decorator that makes an adapter addressable from config."""

    def _wrap(cls: type[SourceAdapter]) -> type[SourceAdapter]:
        _REGISTRY[name] = cls
        return cls

    return _wrap


def build_adapter(key: str, config: SourceConfig, client: PoliteClient) -> SourceAdapter:
    # Importing here (rather than at module scope) keeps the registry populated
    # without adapter modules importing this one at import time.
    from tg import adapters  # noqa: F401
    from tg.adapters import cinemacity  # noqa: F401

    try:
        cls = _REGISTRY[config.adapter]
    except KeyError:
        raise AdapterError(
            f"unknown adapter {config.adapter!r} for source {key!r}; "
            f"registered: {sorted(_REGISTRY)}"
        ) from None
    return cls(key, config, client)


def registered_adapters() -> list[str]:
    from tg.adapters import cinemacity  # noqa: F401

    return sorted(_REGISTRY)
