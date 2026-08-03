"""Configuration: non-secret structure comes from YAML, secrets come from the environment.

Splitting them this way means ``config.yaml`` describes *what* to watch and can be
read/diffed freely, while tokens and webhook URLs stay in ``.env``.
"""

from __future__ import annotations

import os
import re
from datetime import date, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class Secrets(BaseSettings):
    """Everything sensitive. Read from the environment / .env, never from YAML."""

    model_config = SettingsConfigDict(
        env_prefix="TG_", env_file=".env", extra="ignore", case_sensitive=False
    )

    discord_webhook_url: str | None = None

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    ntfy_token: str | None = None

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    database_url: str | None = None
    config_file: str = "config.yaml"


class HttpConfig(BaseModel):
    user_agent: str = "ticket-grabber/0.1 (self-hosted availability monitor)"
    timeout_seconds: float = 25.0
    requests_per_minute: int = 30
    max_retries: int = 3
    respect_robots: bool = True


class PollConfig(BaseModel):
    """Cadence. Hot mode exists because programs get published early — that is the
    whole reason this project exists."""

    baseline_seconds: int = 900
    hot_seconds: int = 45
    jitter_ratio: float = Field(0.15, ge=0.0, le=1.0)
    max_concurrency: int = 4
    #: Windows during which every source polls at ``hot_seconds``.
    hot_windows: list[HotWindow] = Field(default_factory=list)


class HotWindow(BaseModel):
    """A recurring or absolute period of heightened polling."""

    name: str = "hot"
    weekdays: list[str] | None = None
    start: time | None = None
    end: time | None = None
    date_from: date | None = None
    date_to: date | None = None

    @field_validator("weekdays")
    @classmethod
    def _check_weekdays(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        bad = [d for d in v if d.lower() not in WEEKDAYS]
        if bad:
            raise ValueError(f"unknown weekday(s): {bad}; expected any of {sorted(WEEKDAYS)}")
        return [d.lower() for d in v]


class AssistConfig(BaseModel):
    """Human-in-the-loop checkout assistance.

    Disabled by default and gated per-watch. Never solves challenges — if a bot check
    appears the browser is handed to the human.
    """

    enabled: bool = False
    browser_profile_dir: str = "data/browser-profile"
    headless: bool = False
    stop_before_payment: bool = True
    max_concurrent_sessions: int = 1


class SeatMapConfig(BaseModel):
    """Tier-2 seat map reading. Expensive, so it is gated behind tier-1 deltas."""

    enabled: bool = False
    browser_profile_dir: str = "data/browser-profile"
    headless: bool = True
    #: Never read a seat map for the same screening more often than this.
    min_interval_seconds: int = 300
    timeout_seconds: float = 45.0


class SourceConfig(BaseModel):
    adapter: str
    base_url: str
    enabled: bool = True
    #: Adapter-specific settings (locale, tenant_id, cinemas, ...).
    options: dict[str, Any] = Field(default_factory=dict)


class SeatPreference(BaseModel):
    """Per-venue definition of what counts as a good seat.

    Written once per auditorium so watch rules stay portable across venues.
    """

    venue: str | None = None
    auditorium_regex: str | None = None
    rows: tuple[int, int] | None = None
    seat_range: tuple[int, int] | None = None
    avoid_rows: list[int] = Field(default_factory=list)


class WatchMatch(BaseModel):
    title_regex: str | None = None
    formats: list[str] = Field(default_factory=list)
    auditorium_regex: str | None = None
    cinemas: list[str] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    weekdays: list[str] = Field(default_factory=list)
    time_between: tuple[time, time] | None = None

    @field_validator("weekdays")
    @classmethod
    def _check_weekdays(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if d.lower() not in WEEKDAYS]
        if bad:
            raise ValueError(f"unknown weekday(s): {bad}; expected any of {sorted(WEEKDAYS)}")
        return [d.lower() for d in v]

    @field_validator("title_regex", "auditorium_regex")
    @classmethod
    def _compilable(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid regex {v!r}: {exc}") from exc
        return v

    @field_validator("cinemas", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [str(x) for x in v]
        return v


class WatchSeats(BaseModel):
    profile: str | None = None
    min_contiguous: int = 1


class WatchTrigger(BaseModel):
    #: Change types that fire this watch.
    #:
    #: Spelled ``events`` internally but written ``on:`` in YAML, which YAML 1.1
    #: resolves to the boolean ``True`` as a mapping key — hence the normalisation
    #: below rather than a plain alias.
    events: list[str] = Field(default_factory=lambda: ["NEW_SCREENING", "AVAILABILITY_RISE"])
    #: Minimum increase in availabilityRatio before AVAILABILITY_RISE fires. Filters noise.
    availability_rise_min: float = 0.005
    availability_drop_min: float = 0.05
    #: Only alert while the screening still has at least this fraction free.
    max_availability: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_on_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and "events" not in data:
            for key in (True, "on", "On", "ON"):
                if key in data:
                    data = {k: v for k, v in data.items() if k != key} | {"events": data[key]}
                    break
        return data


class WatchConfig(BaseModel):
    name: str
    source: str
    enabled: bool = True
    match: WatchMatch = Field(default_factory=WatchMatch)
    seats: WatchSeats = Field(default_factory=WatchSeats)
    trigger: WatchTrigger = Field(default_factory=WatchTrigger)
    notify: list[Literal["discord", "ntfy", "telegram"]] = Field(default_factory=list)
    #: When one cycle produces more than this many alerts of the same kind, they are
    #: replaced by a single digest. A newly published week is one event to a human,
    #: not eighty.
    digest_threshold: int = 5
    cooldown: str = "10m"
    #: "off" | "arm" — whether an alert may open an assisted checkout browser.
    assist: Literal["off", "arm"] = "off"

    @field_validator("assist", mode="before")
    @classmethod
    def _accept_yaml_bool(cls, v: Any) -> Any:
        # Bare ``off``/``on`` in YAML arrive as booleans.
        if isinstance(v, bool):
            return "arm" if v else "off"
        return v

    @field_validator("cooldown", mode="before")
    @classmethod
    def _stringify_cooldown(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) else v

    @property
    def cooldown_seconds(self) -> int:
        return parse_duration(self.cooldown)


class AppConfig(BaseModel):
    database_url: str = "sqlite:///data/tg.db"
    http: HttpConfig = Field(default_factory=HttpConfig)
    poll: PollConfig = Field(default_factory=PollConfig)
    assist: AssistConfig = Field(default_factory=AssistConfig)
    seatmap: SeatMapConfig = Field(default_factory=SeatMapConfig)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    profiles: dict[str, SeatPreference] = Field(default_factory=dict)
    watches: list[WatchConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_check(self) -> AppConfig:
        for w in self.watches:
            if w.source not in self.sources:
                raise ValueError(
                    f"watch {w.name!r} references unknown source {w.source!r}; "
                    f"known sources: {sorted(self.sources)}"
                )
            if w.seats.profile and w.seats.profile not in self.profiles:
                raise ValueError(
                    f"watch {w.name!r} references unknown seat profile {w.seats.profile!r}; "
                    f"known profiles: {sorted(self.profiles)}"
                )
        return self

    def watch(self, name: str) -> WatchConfig:
        for w in self.watches:
            if w.name == name:
                return w
        raise KeyError(f"no watch named {name!r}; known: {[w.name for w in self.watches]}")


PollConfig.model_rebuild()

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str | int) -> int:
    """``"10m"`` -> 600. Bare numbers are treated as seconds."""
    if isinstance(text, int):
        return text
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"invalid duration {text!r}; expected forms like '30s', '10m', '2h', '1d'")
    return int(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]


def load_config(path: str | Path | None = None, secrets: Secrets | None = None) -> AppConfig:
    """Load YAML config, then let environment secrets override the database URL."""
    secrets = secrets or Secrets()
    path = Path(path or secrets.config_file)
    if not path.exists():
        raise FileNotFoundError(
            f"config file {path} not found — copy config.example.yaml to {path} and edit it"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = AppConfig.model_validate(raw)
    if secrets.database_url:
        cfg.database_url = secrets.database_url
    return cfg


def resolve_path(value: str) -> Path:
    """Expand ``~`` and make relative paths relative to the working directory."""
    return Path(os.path.expanduser(value)).resolve()
