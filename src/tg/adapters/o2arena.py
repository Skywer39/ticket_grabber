"""O2 arena Praha adapter, built on the JSON feed the site's own front end consumes.

Endpoint shape (verified against www.o2arena.cz on 2026-09-05)::

    {base}/wp-json/arena/v1/events        # Stage 2, the arena itself
    {base}/wp-json/universum/v1/events    # Stage 3, the smaller hall next door

No authentication and no bot challenge. ``robots.txt`` allows these paths; note that it
disallows ``/*?*`` and ``/*&*``, so *anything* carrying a query string is off limits on
this host. These endpoints need none, and
:class:`~tg.adapters.base_http.PoliteClient` enforces the rule independently.

Three things about this feed shape the adapter:

* **It is one document.** ~837 KB carrying the entire programme — 52 events out to 2028
  when measured — with no per-date endpoint and no way to ask for less. Hence
  :data:`~tg.core.adapter.Capability.WHOLE_HORIZON`, and hence the
  ``min_interval_seconds`` this source is expected to be configured with: there is no
  cheaper probe to fall back on.
* **It cannot be cached.** No ``ETag``, no ``Last-Modified``, ``Cache-Control:
  no-store``, and the server does not honour ``Accept-Encoding: gzip``. Conditional GET
  is wired up in the client and simply has nothing to work with here.
* **``Performance_id`` is not unique.** It behaves as a *slot* id: the two "Prohlídkové
  okruhy" guided-tour events reuse ids 94999138 (13:00) and 82221889 (16:00) across two
  different event records. Taken as the screening's identity it collides, and the first
  live poll died on a UNIQUE constraint. The identity used here is the pair
  ``{Event_id}-{Performance_id}``, which is unique across the whole feed as observed and
  survives a performance being rescheduled — a moved date should read as a change to a
  known show, not as one show vanishing and another appearing.
* **It publishes no availability.** No ratio, no seat counts, and ``isSoldout`` was
  ``null`` on all 64 performances observed — the field exists in the schema and is
  never populated. So there is no sold-out alerting to be had, and none is pretended.
  What the feed *does* carry is ``Selling_since``, which is the signal that matters for
  a venue announcing months ahead: the moment a show becomes buyable at all.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, date, datetime

from tg.core.adapter import AdapterError, Capability, SourceAdapter, register_adapter
from tg.core.normalize import EventKind, NormEvent, NormScreening, NormVenue
from tg.core.timeutil import local_to_utc, to_local, utcnow_aware

log = logging.getLogger(__name__)

#: Top-level category id -> normalized kind. Ids are stable in the feed; the titles are
#: Czech display strings and are used only as a fallback when an id is unrecognised.
CATEGORY_KINDS: dict[str, EventKind] = {
    "6": EventKind.CONCERT,  # Hudba
    "4": EventKind.SPORT,  # Sport
    "5": EventKind.OTHER,  # Rodinné a jiné akce
}

_TITLE_KINDS: dict[str, EventKind] = {
    "hudba": EventKind.CONCERT,
    "sport": EventKind.SPORT,
}

#: Which kind wins when an event is filed under several categories at once. Only one of
#: the 52 events observed carried two (Sport + Rodinné), so this is rare — but a
#: `kinds` filter needs a deterministic answer. CONCERT leads deliberately: a mixed
#: billing that is partly music should reach someone watching for music, because a
#: missed concert is the failure this project exists to prevent and a stray sports
#: listing is merely a nuisance.
_KIND_PRECEDENCE = (EventKind.CONCERT, EventKind.SPORT, EventKind.OTHER)

#: Ticket sellers, most-preferred first. Fixed order rather than "whatever came first"
#: because ``booking_url`` feeds the screening's content hash, and a link that flapped
#: between operators would churn the change log every poll. Both were present on
#: essentially every performance observed (Ticketportal 64, Ticketmaster 60).
OPERATOR_PREFERENCE = ("Ticketmaster", "Ticketportal")


@register_adapter("o2arena")
class O2ArenaAdapter(SourceAdapter):
    capabilities = {
        Capability.VENUES,
        Capability.EVENTS,
        Capability.SCREENINGS,
        Capability.WHOLE_HORIZON,
    }

    def __init__(self, key, config, client) -> None:  # type: ignore[no-untyped-def]
        super().__init__(key, config, client)
        self.base_url: str = config.base_url.rstrip("/")
        self.endpoint: str = self.options.get("endpoint", "/wp-json/arena/v1/events")
        self.timezone: str = self.options.get("timezone", "Europe/Prague")
        #: The feed's own hall id (2 = O2 arena, 3 = O2 universum). Used as the venue's
        #: external id so the two halls stay distinct if both are ever configured.
        self.stage: str = str(self.options.get("stage", "2"))
        self.venue_name: str = self.options.get("venue_name", "O2 arena")
        self.city: str = self.options.get("city", "Praha")

    # ------------------------------------------------------------------ reads

    @property
    def feed_url(self) -> str:
        return f"{self.base_url}{self.endpoint}"

    async def _feed(self) -> list[dict]:
        res = await self.client.get(self.feed_url)
        payload = res.json()
        # The envelope is a bare list, not the ``{"body": ...}`` wrapper the cinema
        # API uses. WordPress serves its error pages as HTML with a 200 often enough
        # that "did we get a list of events" is worth asserting rather than assuming.
        if not isinstance(payload, list):
            raise AdapterError(
                f"expected a JSON list from {self.endpoint}, got "
                f"{type(payload).__name__}: {str(payload)[:200]}"
            )
        entries = [e["Event"] for e in payload if isinstance(e, dict) and isinstance(
            e.get("Event"), dict
        )]
        if payload and not entries:
            raise AdapterError(
                f"{self.endpoint} returned {len(payload)} items, none carrying an "
                "'Event' object — the feed's shape has changed"
            )
        return entries

    async def venues(self) -> list[NormVenue]:
        """The arena is the venue. The feed describes no others, so this is synthetic."""
        return [
            NormVenue(
                source=self.key,
                external_id=self.stage,
                name=self.venue_name,
                city=self.city,
                url=self.base_url,
            )
        ]

    async def screenings(
        self, since: date, until: date, dates: list[date] | None = None
    ) -> tuple[list[NormEvent], list[NormScreening]]:
        """The whole programme, in one request.

        ``dates`` is ignored — there is no per-date endpoint to narrow to — but
        ``since``/``until`` are honoured by filtering, because the scheduler treats
        every date in that window as covered and uses it to decide what has been
        withdrawn.
        """
        entries = await self._feed()

        events: dict[str, NormEvent] = {}
        screenings: list[NormScreening] = []
        seen: set[str] = set()

        for raw in entries:
            event = self._to_event(raw)
            if event is None:
                continue
            for perf in raw.get("Performances") or []:
                screening = self._to_screening(perf, raw, event)
                if screening is None:
                    continue
                day = to_local(screening.starts_at, self.timezone).date()
                if not (since <= day <= until):
                    continue
                # The composed key is unique across the feed as observed, but a
                # duplicate would reach the database as a UNIQUE violation and take the
                # whole source down for the cycle. Losing one performance and saying so
                # is a better failure than losing the poll.
                if screening.external_id in seen:
                    log.warning(
                        "duplicate screening id %s in %s feed (event %s) — keeping the "
                        "first and skipping this one",
                        screening.external_id,
                        self.key,
                        event.external_id,
                    )
                    continue
                seen.add(screening.external_id)
                screenings.append(screening)
                # Only events with a screening in the window: an event carried purely
                # for a date we did not ask about is not news we can place.
                events.setdefault(event.external_id, event)

        return list(events.values()), screenings

    # ------------------------------------------------------------------ mapping

    def _wall_time(self, epoch: object) -> datetime | None:
        """Decode one of the feed's timestamps.

        The trap: these epochs are Prague wall-clock time encoded *as if* it were UTC.
        Verified against the site's own rendering — The Pussycat Dolls carries
        ``1790190000``, which is 19:00 UTC, and o2arena.cz displays "23.9.2026 19:00".
        Corroborated across the feed: a Sparta fixture on a Thursday reads 18:30 (the
        standard extraliga weekday slot) and one on a Monday state holiday reads 16:00,
        both of which are right and both of which come out two hours wrong if the epoch
        is taken at face value.

        So: read it as UTC, drop the offset, and reinterpret the naive result as venue
        local — which is exactly what every other adapter does with a site's naive
        timestamp.
        """
        if epoch in (None, "", 0):
            return None
        try:
            naive = datetime.fromtimestamp(int(epoch), UTC).replace(tzinfo=None)
        except (TypeError, ValueError, OSError, OverflowError):
            log.warning("unparseable timestamp %r in %s feed", epoch, self.key)
            return None
        return local_to_utc(naive, self.timezone)

    def _kind(self, raw: dict) -> EventKind:
        categories = raw.get("Categories") or {}
        found: set[EventKind] = set()
        for cid, cat in categories.items():
            kind = CATEGORY_KINDS.get(str(cid))
            if kind is None:
                title = str((cat or {}).get("title", "")).strip().lower()
                kind = _TITLE_KINDS.get(title)
                if kind is None:
                    log.debug(
                        "unmapped O2 category %s/%r — falling back to OTHER", cid, title
                    )
                    kind = EventKind.OTHER
            found.add(kind)
        for candidate in _KIND_PRECEDENCE:
            if candidate in found:
                return candidate
        return EventKind.OTHER

    def _to_event(self, raw: dict) -> NormEvent | None:
        external_id = raw.get("Event_id")
        if external_id is None:
            log.warning("O2 event without an Event_id, skipping: %r", str(raw)[:120])
            return None
        images = raw.get("Images") or {}
        # WordPress serves titles HTML-escaped — 4 of the 52 events observed carried
        # entities. They go straight into notification text and are what `title_regex`
        # is matched against, so "Don&#8217;t Be Dumb" would both read badly and defeat
        # a watch written with a normal apostrophe.
        title = html.unescape(raw.get("Title") or "") or str(external_id)
        return NormEvent(
            source=self.key,
            external_id=str(external_id),
            title=title,
            kind=self._kind(raw),
            url=raw.get("URL"),
            poster_url=images.get("Image_wide") or images.get("Image_square"),
        )

    def _to_screening(
        self, perf: dict, raw: dict, event: NormEvent
    ) -> NormScreening | None:
        performance_id = perf.get("Performance_id")
        if performance_id is None:
            return None
        # Not `performance_id` alone — see the module docstring. It is a slot id and
        # repeats across events.
        external_id = f"{event.external_id}-{performance_id}"
        starts_at = self._wall_time(perf.get("Date"))
        if starts_at is None:
            return None

        selling_since = self._wall_time(perf.get("Selling_since"))
        # "Announced but not yet buyable". When this flips false the diff engine emits
        # ON_SALE, which for a venue that announces months ahead is the whole point.
        sales_blocked = selling_since is not None and utcnow_aware() < selling_since

        sold_out = perf.get("isSoldout")

        return NormScreening(
            source=self.key,
            external_id=external_id,
            event_external_id=event.external_id,
            venue_external_id=self.stage,
            starts_at=starts_at,
            venue_name=self.venue_name,
            # The feed has no notion of an auditorium — one hall, one stage.
            auditorium=None,
            booking_url=_booking_url(perf),
            # Already a plain, dated, GET-able page. No fragment to construct, unlike
            # the cinema's quickbook widget.
            info_url=event.url,
            # Deliberately unset: the site's /program/ path redirects to a magazine
            # article rather than a programme listing, so there is no better second
            # link than the venue's own url, which the alert builder falls back to.
            venue_info_url=None,
            sold_out=bool(sold_out),
            # No ratio, no counts, nothing to infer one from.
            availability_ratio=None,
            sales_blocked=sales_blocked,
        )


def _booking_url(perf: dict) -> str | None:
    """The ticket link, picked by a fixed operator preference.

    Stability is the point: this value is hashed into the screening's content hash, so
    choosing "whichever the feed listed first" would turn a reordering upstream into a
    change event for every performance at once.
    """
    tickets = [t for t in (perf.get("Tickets") or []) if isinstance(t, dict)]
    for operator in OPERATOR_PREFERENCE:
        for ticket in tickets:
            if ticket.get("ticket_operator") == operator and ticket.get("ticket_url"):
                return ticket["ticket_url"]
    for ticket in tickets:
        if url := ticket.get("ticket_url"):
            return url
    return None
