"""Cinema City / Cineworld-group adapter, built on the public "quickbook" data API.

Endpoint shapes (verified against www.cinemacity.cz, tenant 10101)::

    {base}/{locale}/data-api-service/v1/quickbook/{tenant}
        /cinemas/with-event/until/{date}
        /films/until/{date}
        /dates/in-cinema/{cinemaId}/until/{date}
        /film-events/in-cinema/{cinemaId}/at-date/{date}

All take ``?attr=&lang={lang}``. No authentication and no bot challenge — the
challenge only guards the separate booking host.

``robots.txt`` on the main host disallows ``/booking`` but not ``/data-api-service``,
so tier-1 polling here is permitted; :class:`~tg.adapters.base_http.PoliteClient`
enforces that independently.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from tg.core.adapter import AdapterError, Capability, SourceAdapter, register_adapter
from tg.core.normalize import (
    EventKind,
    NormEvent,
    NormScreening,
    NormVenue,
    parse_attributes,
)
from tg.core.timeutil import local_to_utc, to_local

log = logging.getLogger(__name__)

#: Hash routes of the site's own "quickbook" widget. It boots from the fragment and
#: re-reads it on ``hashchange``, so these open on the date they name rather than on
#: today — which is the whole point of building them.
BY_FILM_ROUTE = "#/buy-tickets-by-film"
BY_CINEMA_ROUTE = "#/buy-tickets-by-cinema"

#: Tenant ids seen in the wild for this platform. Used only when the id is not
#: configured and cannot be scraped — see :meth:`CinemaCityAdapter._derive_tenant`.
TENANT_SWEEP_RANGE = range(10100, 10112)

_TENANT_IN_HTML = re.compile(r'["\']tenant-id["\']\s*:\s*["\']?(\d{4,6})')
_QUICKBOOK_IN_JS = re.compile(r"quickbook/(\d{4,6})")


@register_adapter("cinemacity")
class CinemaCityAdapter(SourceAdapter):
    capabilities = {
        Capability.VENUES,
        Capability.EVENTS,
        Capability.SCREENINGS,
        Capability.CALENDAR,
        Capability.AVAILABILITY_RATIO,
        Capability.SEATMAP,
    }

    @staticmethod
    def make_seat_reader(config):  # type: ignore[no-untyped-def]
        from tg.adapters.cinemacity_seats import CinemaCitySeatReader

        return CinemaCitySeatReader(config)

    def __init__(self, key, config, client) -> None:  # type: ignore[no-untyped-def]
        super().__init__(key, config, client)
        self.base_url: str = config.base_url.rstrip("/")
        self.locale: str = self.options.get("locale", "cz")
        self.lang: str = self.options.get("lang", "cs_CZ")
        self.timezone: str = self.options.get("timezone", "Europe/Prague")
        self.days_ahead: int = int(self.options.get("days_ahead", 60))
        self.cinemas: list[str] = [str(c) for c in self.options.get("cinemas", [])]
        self._tenant_id: str | None = (
            str(self.options["tenant_id"]) if self.options.get("tenant_id") else None
        )
        #: cinema id -> ``{"group": city group slug, "url": cinema page}``, both needed
        #: to build deep links. Filled by :meth:`_to_venue`.
        self._cinema_meta: dict[str, dict[str, str | None]] = {}

    # ------------------------------------------------------------------ setup

    async def setup(self) -> None:
        if self._tenant_id is None:
            self._tenant_id = await self._derive_tenant()
            log.info(
                "derived tenant id %s for %s — pin it in config to skip this probe",
                self._tenant_id,
                self.key,
            )

        if not self._cinema_meta:
            # One request per process, purely to learn each cinema's group slug and
            # page URL. Best-effort: without it deep links fall back to plain pages,
            # which is a worse link, not a broken poller.
            try:
                await self.venues()
            except Exception as exc:  # noqa: BLE001 — never fail setup over a nicety
                log.warning("could not load cinema metadata for deep links: %s", exc)

    async def _derive_tenant(self) -> str:
        """Recover the tenant id without hardcoding it.

        The value is injected into the page at runtime rather than served in the
        markup, so scraping usually fails and the bounded sweep is what actually
        succeeds. Both paths are kept because the sweep is the expensive one.
        """
        probe_date = (date.today() + timedelta(days=7)).isoformat()

        for url in (self.base_url, f"{self.base_url}/xmedia/js/config.js"):
            try:
                res = await self.client.get(url, accept="text/html,application/javascript,*/*")
            except Exception as exc:  # noqa: BLE001 — scraping is strictly best-effort
                log.debug("tenant scrape of %s failed: %s", url, exc)
                continue
            for pattern in (_TENANT_IN_HTML, _QUICKBOOK_IN_JS):
                if m := pattern.search(res.text):
                    return m.group(1)

        for candidate in TENANT_SWEEP_RANGE:
            url = self._api(str(candidate), f"films/until/{probe_date}")
            try:
                res = await self.client.get(url)
            except Exception:  # noqa: BLE001 — 404s are the expected case here
                continue
            if res.status_code == 200 and (res.json() or {}).get("body", {}).get("films"):
                return str(candidate)

        raise AdapterError(
            f"could not determine the quickbook tenant id for {self.base_url}; "
            "set sources.<name>.options.tenant_id explicitly"
        )

    # ------------------------------------------------------------------ urls

    @property
    def tenant_id(self) -> str:
        if self._tenant_id is None:
            raise AdapterError("adapter not set up — call setup() before use")
        return self._tenant_id

    def _api(self, tenant: str, path: str) -> str:
        return (
            f"{self.base_url}/{self.locale}/data-api-service/v1/quickbook/{tenant}/"
            f"{path}?attr=&lang={self.lang}"
        )

    def url(self, path: str) -> str:
        return self._api(self.tenant_id, path)

    async def _body(self, path: str) -> dict:
        res = await self.client.get(self.url(path))
        payload = res.json()
        if not isinstance(payload, dict) or "body" not in payload:
            raise AdapterError(f"unexpected response shape from {path}: {str(payload)[:200]}")
        return payload["body"] or {}

    # ------------------------------------------------------------------ reads

    async def venues(self) -> list[NormVenue]:
        until = (date.today() + timedelta(days=self.days_ahead)).isoformat()
        body = await self._body(f"cinemas/with-event/until/{until}")
        return [self._to_venue(c) for c in body.get("cinemas", [])]

    def _to_venue(self, c: dict) -> NormVenue:
        addr = c.get("addressInfo") or {}
        # Recorded rather than returned: ``groupId`` has no place in the normalized
        # vocabulary, but the deep-link builder needs it and this is the only place
        # the cinemas payload is parsed.
        self._cinema_meta[str(c["id"])] = {"group": c.get("groupId"), "url": c.get("link")}
        return NormVenue(
            source=self.key,
            external_id=str(c["id"]),
            name=c.get("displayName") or str(c["id"]),
            city=addr.get("city") or c.get("groupId"),
            url=c.get("link"),
            latitude=c.get("latitude"),
            longitude=c.get("longitude"),
        )

    async def calendar(self, since: date, until: date) -> list[date] | None:
        """Which dates have anything on sale, across the configured cinemas.

        One request per cinema. A new week being published shows up here first, which
        is the cheapest possible early-release signal.
        """
        if not self.cinemas:
            return None
        seen: set[date] = set()
        for cinema_id in self.cinemas:
            body = await self._body(f"dates/in-cinema/{cinema_id}/until/{until.isoformat()}")
            for raw in body.get("dates", []):
                try:
                    d = date.fromisoformat(raw)
                except ValueError:
                    log.warning("unparseable date %r from calendar endpoint", raw)
                    continue
                if since <= d <= until:
                    seen.add(d)
        return sorted(seen)

    async def films(self, until: date) -> list[NormEvent]:
        body = await self._body(f"films/until/{until.isoformat()}")
        return [self._to_event(f) for f in body.get("films", [])]

    async def screenings(
        self, since: date, until: date, dates: list[date] | None = None
    ) -> tuple[list[NormEvent], list[NormScreening]]:
        if not self.cinemas:
            raise AdapterError(
                f"source {self.key!r} has no cinemas configured; "
                "set sources.<name>.options.cinemas (run `tg venues` to list them)"
            )

        targets = dates if dates is not None else await self._all_dates(since, until)
        events: dict[str, NormEvent] = {}
        screenings: list[NormScreening] = []

        for cinema_id in self.cinemas:
            for day in targets:
                body = await self._body(
                    f"film-events/in-cinema/{cinema_id}/at-date/{day.isoformat()}"
                )
                for f in body.get("films", []):
                    ev = self._to_event(f)
                    events.setdefault(ev.external_id, ev)
                for e in body.get("events", []):
                    screenings.append(self._to_screening(e, events))

        return list(events.values()), screenings

    async def _all_dates(self, since: date, until: date) -> list[date]:
        """Fall back to the calendar probe, then to a dense sweep."""
        if (cal := await self.calendar(since, until)) is not None:
            return cal
        span = (until - since).days
        return [since + timedelta(days=i) for i in range(span + 1)]

    # ------------------------------------------------------------------ mapping

    def _to_event(self, f: dict) -> NormEvent:
        parsed = parse_attributes(f.get("attributeIds") or [])
        release: date | None = None
        if raw := f.get("releaseDate"):
            try:
                release = datetime.fromisoformat(raw).date()
            except ValueError:
                log.debug("unparseable releaseDate %r", raw)
        return NormEvent(
            source=self.key,
            external_id=str(f["id"]),
            title=f.get("name") or str(f["id"]),
            kind=EventKind.FILM,
            url=f.get("link"),
            poster_url=f.get("posterLink"),
            duration_minutes=f.get("length"),
            release_date=release,
            genres=parsed.genres,
            formats=parsed.formats,
            raw_attributes=list(f.get("attributeIds") or []),
        )

    def _film_deep_link(
        self, film_url: str | None, film_id: str, cinema_id: str, on_date: str
    ) -> str | None:
        """The film page, opened on ``on_date`` with that day's showtimes listed.

        ``in-cinema`` takes the city *group* slug here, not the cinema id: passing an id
        makes the app rewrite the fragment to the group anyway. Without a known group the
        plain film page is still the right answer — it just opens on today.
        """
        if not film_url:
            return None
        group = (self._cinema_meta.get(cinema_id) or {}).get("group")
        if not group:
            return film_url
        query = urlencode(
            {"in-cinema": group, "at": on_date, "for-movie": film_id, "view-mode": "list"}
        )
        return f"{film_url}{BY_FILM_ROUTE}?{query}"

    def _venue_deep_link(self, cinema_id: str, on_date: str) -> str | None:
        """That cinema's whole programme for ``on_date``.

        The sibling route, and the one place ``in-cinema`` really does take the venue id.
        """
        url = (self._cinema_meta.get(cinema_id) or {}).get("url")
        if not url:
            return None
        query = urlencode({"in-cinema": cinema_id, "at": on_date, "view-mode": "list"})
        return f"{url}{BY_CINEMA_ROUTE}?{query}"

    def _to_screening(self, e: dict, events: dict[str, NormEvent]) -> NormScreening:
        parsed = parse_attributes(e.get("attributeIds") or [])

        # The site publishes venue-local wall time with no offset.
        starts_at = local_to_utc(datetime.fromisoformat(e["eventDateTime"]), self.timezone)

        composite = e.get("compositeBookingLink") or {}
        booking_url = _booking_url(e, composite)

        # Languages come from a dedicated object here; merge in anything the attribute
        # parser also inferred so both encodings end up in one place.
        languages = {k: v for k, v in (e.get("languages") or {}).items() if v}
        for role, langs in parsed.languages.items():
            languages.setdefault(role, langs)

        film_id, cinema_id = str(e["filmId"]), str(e["cinemaId"])
        film = events.get(film_id)
        # The date the *cinema* calls this screening. A 20:30 Prague show is 18:30 UTC
        # the same day, but a 00:30 one is the previous day in UTC — link by wall date.
        on_date = to_local(starts_at, self.timezone).date().isoformat()

        ratio = e.get("availabilityRatio")
        return NormScreening(
            source=self.key,
            external_id=str(e["id"]),
            event_external_id=film_id,
            venue_external_id=cinema_id,
            starts_at=starts_at,
            auditorium=e.get("auditorium"),
            booking_url=booking_url,
            info_url=self._film_deep_link(
                film.url if film else None, film_id, cinema_id, on_date
            ),
            venue_info_url=self._venue_deep_link(cinema_id, on_date),
            sold_out=bool(e.get("soldOut")),
            availability_ratio=float(ratio) if ratio is not None else None,
            formats=parsed.formats,
            raw_attributes=list(e.get("attributeIds") or []),
            languages=languages,
            sales_blocked=bool(composite.get("blockOnlineSales")),
        )


def _booking_url(event: dict, composite: dict) -> str | None:
    """Pick the link a human can actually open.

    ``bookingLink`` looks like the obvious choice and is not: it points at
    ``tickets.cinemacity.cz/api/order/{id}``, an endpoint the site's own front end
    posts to. Opened directly it answers HTTP 404 with the body text "Error Occurred",
    which renders as a blank page. The payload flags this itself — the same URL appears
    under ``compositeBookingLink`` as ``obsoleteBookingUrl``.

    ``bookingRouterLaunchLink`` is the real entry point (200, "Redirecting to
    booking…") and was present on every event observed, live and in fixtures. The rest
    of the chain is belt-and-braces for a payload that omits it.
    """
    if router := event.get("bookingRouterLaunchLink"):
        return router

    booking = composite.get("bookingUrl") or {}
    if url := booking.get("url"):
        query = urlencode(booking.get("params") or {})
        return f"{url}?{query}" if query else url

    return event.get("bookingLink") or composite.get("obsoleteBookingUrl")
