"""The normalized vocabulary every adapter emits.

Adapters translate whatever a site happens to call things into these types, so watch
rules and seat-preference profiles are written once and work anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class EventKind(StrEnum):
    FILM = "FILM"
    CONCERT = "CONCERT"
    THEATRE = "THEATRE"
    SPORT = "SPORT"
    OTHER = "OTHER"


class FormatTag(StrEnum):
    """Presentation formats worth filtering on. Extend freely — unknown raw attributes
    are always preserved alongside, so nothing is lost by not having a tag yet."""

    IMAX = "IMAX"
    FILM_70MM = "FILM_70MM"
    FILM_35MM = "FILM_35MM"
    TWO_D = "2D"
    THREE_D = "3D"
    FOUR_DX = "4DX"
    SCREENX = "SCREENX"
    DBOX = "DBOX"
    ATMOS = "ATMOS"
    VIP = "VIP"
    RECLINERS = "RECLINERS"
    DUBBED = "DUBBED"
    SUBTITLED = "SUBTITLED"


class SeatStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    TAKEN = "TAKEN"
    HELD = "HELD"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


#: Raw attribute id -> normalized format tag.
#:
#: These ids come from the Cineworld-group "quickbook" data API (Cinema City CZ/HU/PL,
#: Cineworld UK, ...), so the table is shared rather than adapter-private. Genre and
#: rating attributes are deliberately absent — they are handled separately below.
ATTRIBUTE_FORMAT_MAP: dict[str, FormatTag] = {
    "imax": FormatTag.IMAX,
    "70-mm": FormatTag.FILM_70MM,
    "70mm": FormatTag.FILM_70MM,
    "35-mm": FormatTag.FILM_35MM,
    "2d": FormatTag.TWO_D,
    "3d": FormatTag.THREE_D,
    "4dx": FormatTag.FOUR_DX,
    "screenx": FormatTag.SCREENX,
    "d-box": FormatTag.DBOX,
    "dolby-atmos": FormatTag.ATMOS,
    "atmos": FormatTag.ATMOS,
    "vip": FormatTag.VIP,
    "recliners": FormatTag.RECLINERS,
    "dubbed": FormatTag.DUBBED,
    "subbed": FormatTag.SUBTITLED,
}

#: Attributes that describe audience rating rather than presentation.
_RATING_RE = re.compile(r"^(\d+-plus|suitable-for-all|unrated)$")
#: Attributes that encode language, e.g. ``original-lang-en``, ``dubbed-lang-cs``.
_LANG_RE = re.compile(r"^(original|dubbed|voiceover|first-subbed|subbed)-lang-([a-z]{2})$")

_KNOWN_GENRES = {
    "action", "adventure", "animation", "biography", "comedy", "crime", "documentary",
    "drama", "family", "fantasy", "history", "horror", "musical", "music", "mystery",
    "romance", "sci-fi", "sport", "thriller", "war", "western",
}


@dataclass(slots=True)
class ParsedAttributes:
    formats: set[FormatTag] = field(default_factory=set)
    genres: list[str] = field(default_factory=list)
    ratings: list[str] = field(default_factory=list)
    languages: dict[str, list[str]] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)


def parse_attributes(raw: list[str]) -> ParsedAttributes:
    """Split a flat attribute-id list into formats, genres, ratings and languages.

    Anything unrecognised lands in ``unmapped`` rather than being dropped, so a site
    adding a new tag shows up in logs instead of silently vanishing.
    """
    out = ParsedAttributes()
    for attr in raw:
        key = attr.lower().strip()
        if fmt := ATTRIBUTE_FORMAT_MAP.get(key):
            out.formats.add(fmt)
            continue
        if key in _KNOWN_GENRES:
            out.genres.append(key)
            continue
        if _RATING_RE.match(key):
            out.ratings.append(key)
            continue
        if m := _LANG_RE.match(key):
            role, lang = m.group(1), m.group(2)
            out.languages.setdefault(role, []).append(lang)
            # A "dubbed-lang-cs"/"subbed-lang-cs" also implies the presentation format.
            if role == "dubbed":
                out.formats.add(FormatTag.DUBBED)
            elif role in ("subbed", "first-subbed"):
                out.formats.add(FormatTag.SUBTITLED)
            continue
        out.unmapped.append(key)
    return out


@dataclass(slots=True)
class NormVenue:
    source: str
    external_id: str
    name: str
    city: str | None = None
    url: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass(slots=True)
class NormEvent:
    """A film, concert or show — the thing, not a particular showing of it."""

    source: str
    external_id: str
    title: str
    kind: EventKind = EventKind.FILM
    url: str | None = None
    poster_url: str | None = None
    duration_minutes: int | None = None
    release_date: date | None = None
    genres: list[str] = field(default_factory=list)
    formats: set[FormatTag] = field(default_factory=set)
    raw_attributes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass(slots=True)
class NormScreening:
    """One showing at one time in one auditorium."""

    source: str
    external_id: str
    event_external_id: str
    venue_external_id: str
    starts_at: datetime
    venue_name: str | None = None
    auditorium: str | None = None
    booking_url: str | None = None
    #: A plain page that opens on *this screening's date* — what a notification links
    #: to. Distinct from ``booking_url``, which is often an entrance to a stateful flow
    #: and not GET-able at all.
    info_url: str | None = None
    #: The venue's programme for the same date. Secondary link, and the fallback when
    #: the event has no page of its own.
    venue_info_url: str | None = None
    sold_out: bool = False
    #: Fraction of seats still free (1.0 = empty house). ``None`` if the source
    #: does not publish it.
    availability_ratio: float | None = None
    formats: set[FormatTag] = field(default_factory=set)
    raw_attributes: list[str] = field(default_factory=list)
    languages: dict[str, list[str]] = field(default_factory=dict)
    price_min: float | None = None
    price_max: float | None = None
    #: Set by the adapter when the source explicitly blocks online sales.
    sales_blocked: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def event_key(self) -> str:
        return f"{self.source}:{self.event_external_id}"

    @property
    def venue_key(self) -> str:
        return f"{self.source}:{self.venue_external_id}"

    def content_hash(self) -> str:
        """Hash of the fields whose change is worth reacting to.

        Deliberately excludes cosmetic fields (poster, raw attribute ordering, the
        info URLs) so harmless site churn does not produce alerts. ``info_url``
        especially: it embeds the screening's own date, so hashing it would turn the
        day a link shape changes into a flood of fake "new screening" events.
        """
        payload = {
            "starts_at": self.starts_at.isoformat(),
            "auditorium": self.auditorium,
            "sold_out": self.sold_out,
            # Rounded: the ratio jitters in the 4th decimal as carts are held/released.
            "availability": None
            if self.availability_ratio is None
            else round(self.availability_ratio, 4),
            "formats": sorted(self.formats),
            "booking_url": self.booking_url,
            "price": [self.price_min, self.price_max],
            "sales_blocked": self.sales_blocked,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()


@dataclass(slots=True)
class Seat:
    """One physical seat. ``row_index``/``seat_index`` are the numeric forms used by
    preference profiles; the labels are what the site displays."""

    row_label: str
    seat_label: str
    status: SeatStatus = SeatStatus.UNKNOWN
    row_index: int | None = None
    seat_index: int | None = None
    section: str | None = None
    price_class: str | None = None
    x: float | None = None
    y: float | None = None

    @property
    def id(self) -> str:
        return f"{self.section or ''}|{self.row_label}|{self.seat_label}"

    @property
    def is_available(self) -> bool:
        return self.status is SeatStatus.AVAILABLE


@dataclass(slots=True)
class SeatMap:
    screening_key: str
    seats: list[Seat] = field(default_factory=list)
    captured_at: datetime | None = None

    @property
    def available(self) -> list[Seat]:
        return [s for s in self.seats if s.is_available]

    @property
    def availability_ratio(self) -> float | None:
        if not self.seats:
            return None
        return len(self.available) / len(self.seats)

    def available_ids(self) -> set[str]:
        return {s.id for s in self.available}

    def fingerprint(self) -> str:
        return hashlib.sha256(
            ",".join(sorted(self.available_ids())).encode()
        ).hexdigest()


def parse_row_index(label: str) -> int | None:
    """Turn a row label into a number. Handles ``"12"``, ``"R12"``, ``"A"``..``"Z"``.

    Letter rows map A->1, B->2, ... so ``rows: [8, 14]`` in a profile means the same
    thing whether the venue numbers or letters its rows.
    """
    label = label.strip().upper()
    if not label:
        return None
    if digits := re.search(r"\d+", label):
        return int(digits.group())
    if len(label) == 1 and "A" <= label <= "Z":
        return ord(label) - ord("A") + 1
    if len(label) == 2 and label.isalpha():  # AA, AB, ... for very deep auditoriums
        return 26 + (ord(label[0]) - ord("A")) * 26 + (ord(label[1]) - ord("A")) + 1
    return None


def parse_seat_index(label: str) -> int | None:
    if digits := re.search(r"\d+", label.strip()):
        return int(digits.group())
    return None
