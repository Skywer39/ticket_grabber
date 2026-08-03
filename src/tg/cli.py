"""Command line interface."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from sqlmodel import col, select

from tg import db
from tg.adapters.base_http import PoliteClient
from tg.config import AppConfig, Secrets, load_config
from tg.core.adapter import build_adapter, registered_adapters
from tg.core.diff import ChangeType
from tg.core.scheduler import Engine, is_hot
from tg.core.timeutil import DEFAULT_TZ, format_local, from_db, to_local, utcnow_aware
from tg.core.watches import evaluate, screening_matches
from tg.models import Alert, Event, PollState, Screening, Venue
from tg.notify.base import Dispatcher, build_notifiers

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Watch ticketing sites for new screenings and seat availability.",
)
watch_app = typer.Typer(no_args_is_help=True, help="Inspect and test watches.")
notify_app = typer.Typer(no_args_is_help=True, help="Notification channels.")
app.add_typer(watch_app, name="watch")
app.add_typer(notify_app, name="notify")

console = Console()

ConfigOpt = Annotated[str, typer.Option("--config", "-c", help="Path to config.yaml")]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _load(config_path: str) -> AppConfig:
    cfg = load_config(config_path)
    db.init_engine(cfg.database_url)
    db.create_all()
    return cfg


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


# --------------------------------------------------------------------- setup


@app.command()
def init(
    config: ConfigOpt = "config.yaml",
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config")] = False,
) -> None:
    """Create config.yaml from the example and initialise the database."""
    target = Path(config)
    example = Path("config.example.yaml")

    if target.exists() and not force:
        console.print(f"[yellow]{target} already exists[/] — pass --force to overwrite")
    elif not example.exists():
        console.print(f"[red]{example} not found[/] — run from the project root")
        raise typer.Exit(1)
    else:
        shutil.copy(example, target)
        console.print(f"[green]wrote {target}[/]")

    cfg = load_config(str(target))
    db.init_engine(cfg.database_url)
    db.create_all()
    console.print(f"[green]database ready[/] at {cfg.database_url}")

    if not Path(".env").exists():
        console.print(
            "[yellow]no .env yet[/] — copy .env.example to .env and fill in a channel "
            "(Discord webhook is the fastest to set up)"
        )


@app.command()
def adapters() -> None:
    """List the source adapters this build knows about."""
    for name in registered_adapters():
        console.print(f"  {name}")


# --------------------------------------------------------------------- reads


@app.command()
def venues(
    source: Annotated[str, typer.Argument(help="Source key from config")],
    config: ConfigOpt = "config.yaml",
) -> None:
    """List every venue a source covers, with the ids to put in `cinemas:`."""
    cfg = _load(config)

    async def _run() -> None:
        async with PoliteClient(cfg.http) as client:
            adapter = build_adapter(source, cfg.sources[source], client)
            await adapter.setup()
            rows = await adapter.venues()

        table = Table("id", "name", "city", title=f"venues for {source}")
        for v in sorted(rows, key=lambda v: v.name):
            table.add_row(v.external_id, v.name, v.city or "")
        console.print(table)

    asyncio.run(_run())


@app.command()
def probe(
    source: Annotated[str, typer.Argument(help="Source key from config")],
    cinema: Annotated[str | None, typer.Option("--cinema", help="Venue id")] = None,
    date: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD")] = None,
    title: Annotated[str | None, typer.Option("--title", help="Case-insensitive substring")] = None,
    config: ConfigOpt = "config.yaml",
) -> None:
    """Fetch and print normalized screenings without touching the database.

    This is the golden-data check: run it against a known date and compare the
    availability figures to what the site shows.
    """
    cfg = _load(config)
    source_cfg = cfg.sources[source]
    if cinema:
        source_cfg = source_cfg.model_copy(deep=True)
        source_cfg.options["cinemas"] = [cinema]

    async def _run() -> None:
        async with PoliteClient(cfg.http) as client:
            adapter = build_adapter(source, source_cfg, client)
            await adapter.setup()

            tz = source_cfg.options.get("timezone", DEFAULT_TZ)
            today = to_local(utcnow_aware(), tz).date()
            target = dt.date.fromisoformat(date) if date else today
            horizon = today + dt.timedelta(days=int(source_cfg.options.get("days_ahead", 60)))

            calendar = await adapter.calendar(today, horizon)
            if calendar:
                console.print(
                    f"[dim]calendar: {len(calendar)} dates on sale, "
                    f"{calendar[0]} .. {calendar[-1]}[/]"
                )

            events, screenings = await adapter.screenings(target, target, dates=[target])
            titles = {e.external_id: e.title for e in events}

        table = Table("time", "title", "auditorium", "formats", "free", "sold out", title=str(target))
        shown = 0
        for s in sorted(screenings, key=lambda s: s.starts_at):
            name = titles.get(s.event_external_id, "?")
            if title and title.lower() not in name.lower():
                continue
            table.add_row(
                format_local(s.starts_at, tz).split(", ")[-1],
                name,
                s.auditorium or "",
                " ".join(sorted(str(f) for f in s.formats)),
                _pct(s.availability_ratio),
                "yes" if s.sold_out else "",
            )
            shown += 1
        console.print(table)
        console.print(f"[dim]{shown} of {len(screenings)} screenings shown[/]")

    asyncio.run(_run())


@app.command()
def status(config: ConfigOpt = "config.yaml", limit: int = 15) -> None:
    """Show source health and recent alerts."""
    cfg = _load(config)
    now_local = to_local(utcnow_aware(), DEFAULT_TZ)
    hot = is_hot(now_local, cfg.poll.hot_windows)
    console.print(
        f"polling mode: [{'red' if hot else 'green'}]{'HOT' if hot else 'baseline'}[/] "
        f"({cfg.poll.hot_seconds if hot else cfg.poll.baseline_seconds}s)"
    )

    with db.session_scope() as session:
        health = Table("source", "last poll", "errors", "empty polls", "last error", title="health")
        for st in session.exec(select(PollState).where(col(PollState.cache_key).like("%:health"))):
            health.add_row(
                st.source,
                format_local(from_db(st.last_polled_at)) if st.last_polled_at else "never",
                str(st.consecutive_errors),
                str(st.consecutive_empty),
                (st.last_error or "")[:60],
            )
        console.print(health)

        counts = Table("entity", "rows", title="database")
        for label, model in (("venues", Venue), ("events", Event), ("screenings", Screening),
                             ("alerts", Alert)):
            counts.add_row(label, str(len(session.exec(select(model)).all())))
        console.print(counts)

        recent = Table("when", "watch", "change", "title", "sent", title="recent alerts")
        rows = session.exec(
            select(Alert).order_by(col(Alert.created_at).desc()).limit(limit)
        ).all()
        for a in rows:
            recent.add_row(
                format_local(from_db(a.created_at)),
                a.watch_name,
                a.change_type,
                a.title[:48],
                "yes" if a.delivered else "[red]no[/]",
            )
        console.print(recent)


# --------------------------------------------------------------------- watches


@watch_app.command("list")
def watch_list(config: ConfigOpt = "config.yaml") -> None:
    """Show configured watches."""
    cfg = _load(config)
    table = Table("name", "source", "triggers", "channels", "seats", "assist", "cooldown")
    for w in cfg.watches:
        table.add_row(
            w.name if w.enabled else f"[dim]{w.name} (disabled)[/]",
            w.source,
            ", ".join(w.trigger.events),
            ", ".join(w.notify) or "[yellow]none[/]",
            w.seats.profile or "",
            w.assist,
            w.cooldown,
        )
    console.print(table)


@watch_app.command("test")
def watch_test(
    name: Annotated[str, typer.Argument(help="Watch name")],
    config: ConfigOpt = "config.yaml",
) -> None:
    """Show which currently-known screenings this watch matches, and why.

    Matching is evaluated against the database, so run `tg run --once` first.
    """
    cfg = _load(config)
    watch = cfg.watch(name)
    tz = cfg.sources[watch.source].options.get("timezone", DEFAULT_TZ)

    with db.session_scope() as session:
        screenings = session.exec(
            select(Screening).where(
                Screening.source == watch.source,
                col(Screening.disappeared_at).is_(None),
            )
        ).all()
        events = {e.key: e for e in session.exec(select(Event)).all()}
        venues_by_key = {v.key: v for v in session.exec(select(Venue)).all()}

        table = Table("time", "title", "auditorium", "free", title=f"matches for {name!r}")
        matched = 0
        for s in sorted(screenings, key=lambda s: s.starts_at):
            ev = events.get(s.event_key)
            venue = venues_by_key.get(s.venue_key)
            if not screening_matches(
                watch.match,
                title=ev.title if ev else None,
                auditorium=s.auditorium,
                venue_external_id=venue.external_id if venue else None,
                starts_at=from_db(s.starts_at),
                formats=list(s.formats or []),
                tz_name=tz,
            ):
                continue
            table.add_row(
                format_local(from_db(s.starts_at), tz),
                ev.title if ev else "?",
                s.auditorium or "",
                _pct(s.availability_ratio),
            )
            matched += 1

    console.print(table)
    if matched == 0:
        console.print(
            "[yellow]no matches[/] — check the filters with `tg probe`, "
            "or run `tg run --once` to populate the database first"
        )
    else:
        console.print(f"[green]{matched} screening(s) match[/]; alerts fire on "
                      f"{', '.join(watch.trigger.events)}")


# --------------------------------------------------------------------- notify


@notify_app.command("test")
def notify_test(
    channel: Annotated[str | None, typer.Argument(help="discord | ntfy | telegram")] = None,
) -> None:
    """Send a sample alert so you can confirm a channel actually reaches you."""
    notifiers = build_notifiers(Secrets())
    if not notifiers:
        console.print("[red]no channels configured[/] — fill in .env (see .env.example)")
        raise typer.Exit(1)

    targets = [channel] if channel else list(notifiers)
    unknown = [c for c in targets if c not in notifiers]
    if unknown:
        console.print(
            f"[red]not configured: {', '.join(unknown)}[/] — available: {', '.join(notifiers)}"
        )
        raise typer.Exit(1)

    sample = Alert(
        watch_name="test",
        screening_key="test:0",
        change_type=str(ChangeType.NEW_SCREENING),
        title="Test alert — Odyssea",
        body="Praha Flora / IMAX VOLVO\nTue 04 Aug 2026, 16:40\nFILM_70MM\n1.6% of seats free",
        url="https://www.cinemacity.cz/",
        channels=targets,
    )

    sent, failed = asyncio.run(Dispatcher(notifiers).deliver([sample]))
    if failed:
        console.print(f"[red]delivery failed:[/] {sample.delivery_error}")
        raise typer.Exit(1)
    console.print(f"[green]sent via {', '.join(targets)}[/]")


# --------------------------------------------------------------------- run


@app.command()
def run(
    once: Annotated[bool, typer.Option("--once", help="Poll a single time and exit")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Do not send notifications")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    config: ConfigOpt = "config.yaml",
) -> None:
    """Run the poller."""
    _setup_logging(verbose)
    cfg = _load(config)

    notifiers = {} if dry_run else build_notifiers(Secrets())
    if not dry_run and not notifiers:
        console.print(
            "[yellow]no notification channels configured[/] — alerts will be stored "
            "but not delivered. Fill in .env to fix."
        )
    dispatcher = Dispatcher(notifiers) if notifiers else None

    async def _run() -> None:
        engine = Engine(cfg, dispatcher)
        await engine.setup()
        try:
            if once:
                for report in await engine.poll_once():
                    console.print(report.summary())
                    for change in report.changes:
                        if change.change_type in (
                            ChangeType.NEW_DATE,
                            ChangeType.NEW_SCREENING,
                            ChangeType.SEAT_FREED,
                        ):
                            console.print(
                                f"  [green]{change.change_type}[/] "
                                f"{change.screening_key or change.new}"
                            )
                    for alert in report.alerts:
                        console.print(f"  [bold]ALERT[/] {alert.title}")
            else:
                console.print("[green]polling — Ctrl-C to stop[/]")
                await engine.run_forever()
        finally:
            await engine.aclose()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("stopped")


if __name__ == "__main__":
    app()
