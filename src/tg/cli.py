"""Command line interface."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from sqlmodel import col, select

from tg import db
from tg.adapters.base_http import PoliteClient
from tg.assist.checkout import CheckoutAssistant
from tg.config import AppConfig, Secrets, load_config
from tg.core.adapter import build_adapter, registered_adapters
from tg.core.capacity import reconcile
from tg.core.diff import ChangeType
from tg.core.normalize import NormScreening
from tg.core.scheduler import Engine, is_hot
from tg.core.timeutil import DEFAULT_TZ, format_local, from_db, to_local, utcnow_aware
from tg.core.watches import hall_capacity, screening_matches
from tg.models import Alert, Event, PollState, Screening, Venue
from tg.notify.base import Dispatcher, build_notifiers

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Watch ticketing sites for new screenings and seat availability.",
)
watch_app = typer.Typer(no_args_is_help=True, help="Inspect and test watches.")
notify_app = typer.Typer(no_args_is_help=True, help="Notification channels.")
seatmap_app = typer.Typer(no_args_is_help=True, help="Tier-2 seat map tools.")
adapter_app = typer.Typer(no_args_is_help=True, help="Build and repair source adapters.")
app.add_typer(watch_app, name="watch")
app.add_typer(notify_app, name="notify")
app.add_typer(seatmap_app, name="seatmap")
app.add_typer(adapter_app, name="adapter")

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


def _channel_report(cfg: AppConfig, notifiers: dict) -> tuple[str, list[str]]:
    """Summarise which channels are wired up, and which watches want one that isn't.

    A watch can only deliver through a channel that has credentials; naming one that
    does not is silent misconfiguration, so it is surfaced everywhere the user might
    look rather than only at delivery time.
    """
    configured = sorted(notifiers)
    wanted = {c for w in cfg.watches if w.enabled for c in w.notify}
    missing = sorted(wanted - set(configured))
    summary = ", ".join(configured) if configured else "none"
    return summary, missing


def _report_channels(cfg: AppConfig, notifiers: dict) -> None:
    summary, missing = _channel_report(cfg, notifiers)
    if not notifiers:
        console.print(
            "[red]no notification channels configured[/] — alerts will be recorded "
            "but not delivered. Set TG_DISCORD_WEBHOOK_URL in .env (or as a repository "
            "secret) to fix."
        )
    else:
        console.print(f"channels: [green]{summary}[/]")
    if missing:
        console.print(
            f"[yellow]watches request {', '.join(missing)} but it has no credentials[/] "
            "— those alerts will record a delivery error"
        )


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

        table = Table(
            "time", "title", "auditorium", "formats", "free", "sold out", title=str(target)
        )
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
    _report_channels(cfg, build_notifiers(Secrets()))

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
    if not dry_run:
        _report_channels(cfg, notifiers)

    # Always dispatch outside dry-run, even with zero notifiers. Skipping delivery
    # entirely would leave alerts recorded as `delivered=0` with no `delivery_error`,
    # which is indistinguishable from "nothing was sent yet" — the exact ambiguity
    # that made a missing webhook secret take hours to diagnose. Going through the
    # dispatcher records "discord: not configured" on the alert instead.
    dispatcher = None if dry_run else Dispatcher(notifiers)

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


# --------------------------------------------------------------------- tier 2


def _screening_or_exit(key: str):  # type: ignore[no-untyped-def]
    with db.session_scope() as session:
        row = session.exec(select(Screening).where(Screening.key == key)).first()
        if row is None:
            console.print(
                f"[red]no screening {key!r} in the database[/] — "
                "run `tg run --once` first, then `tg watch test <name>` to find keys"
            )
            raise typer.Exit(1)
        ev = session.exec(select(Event).where(Event.key == row.event_key)).first()
        return row, (ev.title if ev else "?")


async def _live_ratio(cfg: AppConfig, source: str, row) -> float | None:  # type: ignore[no-untyped-def]
    """This screening's availabilityRatio, fetched now rather than read from the database.

    The whole point of the comparison is that the two sources are read at the same moment;
    a stored ratio from the last poll could be minutes old and would make a real
    disagreement look like a stale one, or the reverse.
    """
    source_cfg = cfg.sources[source]
    tz = source_cfg.options.get("timezone", DEFAULT_TZ)
    day = to_local(from_db(row.starts_at), tz).date()
    try:
        async with PoliteClient(cfg.http) as client:
            adapter = build_adapter(source, source_cfg, client)
            await adapter.setup()
            _, screenings = await adapter.screenings(day, day, dates=[day])
        return next((s.availability_ratio for s in screenings if s.key == row.key), None)
    except Exception as exc:  # noqa: BLE001 — the seat map is the point; this is context
        console.print(f"[yellow]could not re-read tier 1:[/] {exc}")
        return None


@seatmap_app.command("probe")
def seatmap_probe(
    screening_key: Annotated[str, typer.Argument(help="e.g. cinemacity_cz:220716")],
    raw: Annotated[
        bool, typer.Option("--raw", help="Dump each seat element's classes and labels")
    ] = False,
    config: ConfigOpt = "config.yaml",
) -> None:
    """Read one screening's seat map and reconcile it against the availability ratio.

    Two things this answers that tier 1 cannot. First, whether the shipped selectors
    actually match a live seat page — they were derived from the site's stylesheet,
    because the booking host blocks automated sessions from datacenter addresses, so
    they have never been run against the real thing. `--raw` shows the unmapped
    elements, which is what separates "sold out" from "parsed nothing".

    Second, and the reason this exists: whether `availabilityRatio` counts the same
    seats the booking flow will actually sell you. A screening reported four seats above
    its floor while the picker offered none, and only reading both at once settles which
    number to believe.
    """
    cfg = _load(config)
    row, title = _screening_or_exit(screening_key)

    async def _run() -> None:
        from tg.adapters.cinemacity_seats import CinemaCitySeatReader
        from tg.browser import AccessBlocked

        reader = CinemaCitySeatReader(cfg.seatmap)
        screening = NormScreening(
            source=row.source,
            external_id=row.external_id,
            event_external_id=row.event_key.split(":", 1)[-1],
            venue_external_id=row.venue_key.split(":", 1)[-1],
            starts_at=from_db(row.starts_at),
            auditorium=row.auditorium,
            booking_url=row.booking_url,
        )
        console.print(f"reading seats for [bold]{title}[/] — {row.auditorium} — {row.booking_url}")
        try:
            smap = await reader.read(screening)
        except AccessBlocked as exc:
            console.print(f"[red]blocked:[/] {exc}")
            console.print(
                "[yellow]This is the site refusing automation, not a bug.[/] "
                "Ratio-based alerting still works; consider assist mode 'open'."
            )
            raise typer.Exit(2) from None
        finally:
            await reader.aclose()

        if raw:
            if reader.last_raw is None:
                # Never reached the parsing step. Saying "the selector matched nothing"
                # here would blame the wrong thing entirely.
                console.print(
                    "[yellow]no extraction ran[/] — the page was never reached or was "
                    "refused, so this says nothing about the selectors. The error above "
                    "is the real one."
                )
            elif not reader.last_raw:
                console.print(
                    "[red]the seat selector matched nothing.[/] The page loaded and "
                    "parsing ran, so this is the selector rather than the site — set "
                    "seatmap.selectors in config and probe again."
                )
            else:
                dump = Table("classes", "row", "seat", "aria-label", title="raw seat elements")
                for item in reader.last_raw[:40]:
                    dump.add_row(
                        str(item.get("cls"))[:60],
                        str(item.get("row")),
                        str(item.get("seat") or item.get("text")),
                        str(item.get("label"))[:40],
                    )
                console.print(dump)

        if smap is None:
            console.print("[yellow]no seat map returned[/] — see the log for why")
            raise typer.Exit(1)

        by_status = Counter(str(s.status) for s in smap.seats)
        table = Table("status", "seats", title=f"{len(smap.seats)} seats on the picker")
        for status, count in by_status.most_common():
            table.add_row(status, str(count))
        console.print(table)

        # The reconciliation. Everything above is detail; these lines are the question.
        ratio = await _live_ratio(cfg, row.source, row)
        seats_on_page = len(smap.seats)
        picker_free = len(smap.available)
        api_free, agreement = reconcile(ratio, seats_on_page, picker_free)

        # The picker knows the true capacity, so this is also the first real check of the
        # denominator every alert has been quoting, which until now was inferred from the
        # ratios alone (see tg.core.capacity).
        with db.session_scope() as session:
            estimated = hall_capacity(session, row.source, row.venue_key, row.auditorium)
        if estimated:
            verdict = "matches" if estimated == seats_on_page else "is off by"
            delta = "" if estimated == seats_on_page else f" {abs(estimated - seats_on_page)}"
            console.print(
                f"[dim]capacity: inferred {estimated}, picker shows {seats_on_page} — "
                f"{verdict}{delta}[/]"
            )

        console.print(
            f"\n[bold]tier 1[/] says {_pct(ratio)} free"
            + (f" ≈ {api_free} of {seats_on_page} seats" if api_free is not None else "")
        )
        console.print(f"[bold]the picker[/] offers {picker_free} of {seats_on_page} seats")

        if agreement == "unknown":
            console.print("[yellow]no tier-1 reading to compare against.[/]")
        elif agreement == "agree":
            console.print(
                "[green]they agree.[/] The ratio is counting bookable seats, so the "
                "remaining question is only whether they sit together — filter on "
                "min_contiguous and a seat profile."
            )
        elif agreement == "over":
            console.print(
                f"[red]tier 1 claims {api_free - picker_free} seats the picker will not "
                "sell.[/] availabilityRatio is not counting bookable stock, so every seat "
                "count alerted on so far is overstated by roughly this much."
            )
            hints = Counter(
                str(i.get("cls"))[:40] for i in (reader.last_raw or []) if i.get("cls")
            )
            if hints:
                console.print("[dim]seat classes seen on the page, most common first:[/]")
                for cls, n in hints.most_common(6):
                    console.print(f"  [dim]{n:>4}  {cls}[/]")
        else:
            console.print(
                f"[yellow]the picker offers {picker_free - api_free} more than tier 1 "
                "reports.[/] Chances are being missed rather than invented — the ratio is "
                "conservative, or it was read at a different moment."
            )

    asyncio.run(_run())


@adapter_app.command("discover")
def adapter_discover(
    url: Annotated[str, typer.Argument(help="A page that lists showings")],
    limit: int = 8,
    draft: Annotated[
        bool, typer.Option("--draft", help="Also ask a model for a field mapping")
    ] = False,
    config: ConfigOpt = "config.yaml",
) -> None:
    """Find the JSON endpoints a site feeds its own front end, ranked.

    Read-only: it loads the page and watches the network. The ranked list alone is
    usually enough to write an adapter by hand; --draft adds a suggested mapping.
    """
    cfg = _load(config)

    async def _run() -> None:
        from tg.agent.discover import discover_endpoints

        console.print(f"loading [bold]{url}[/] and recording JSON traffic…")
        candidates = await discover_endpoints(url, browser=cfg.seatmap)
        if not candidates:
            console.print("[yellow]no JSON endpoints seen[/] — the data may be server-rendered")
            raise typer.Exit(1)

        for c in candidates[:limit]:
            colour = "green" if c.score >= 5 else ("yellow" if c.score >= 2 else "dim")
            console.print(f"[{colour}]{c.describe()}[/]")

        best = candidates[0]
        if best.score < 5:
            console.print("\n[yellow]Nothing looks strongly schedule-like.[/]")
        if draft:
            from tg.agent.scaffold import ModelUnavailable, draft_mapping

            try:
                result = draft_mapping(best)
            except ModelUnavailable as exc:
                console.print(f"[yellow]no draft:[/] {exc}")
                return
            console.print("\n[bold]suggested mapping[/]")
            console.print(result.to_yaml())

    asyncio.run(_run())


@adapter_app.command("heal")
def adapter_heal(config: ConfigOpt = "config.yaml") -> None:
    """Diagnose adapters that have stopped returning data."""
    _load(config)
    from tg.agent.scaffold import diagnose_health

    with db.session_scope() as session:
        states = session.exec(
            select(PollState).where(col(PollState.cache_key).like("%:health"))
        ).all()

    if not states:
        console.print("[yellow]no poll history yet[/] — run `tg run --once` first")
        raise typer.Exit(1)

    for st in states:
        verdict = diagnose_health(st.consecutive_empty, st.consecutive_errors)
        style = "green" if verdict == "healthy" else "red"
        console.print(f"[bold]{st.source}[/]: [{style}]{verdict}[/]")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8756,
    config: ConfigOpt = "config.yaml",
) -> None:
    """Serve the status dashboard."""
    import uvicorn

    from tg.web.app import create_app

    console.print(f"[green]dashboard on http://{host}:{port}[/]")
    uvicorn.run(create_app(config), host=host, port=port, log_level="warning")


@app.command()
def assist(
    screening_key: Annotated[str, typer.Argument(help="e.g. cinemacity_cz:220716")],
    watch: Annotated[
        str | None, typer.Option("--watch", help="Use this watch's seat profile")
    ] = None,
    config: ConfigOpt = "config.yaml",
) -> None:
    """Open checkout for a screening. Never buys — you finish the purchase."""
    cfg = _load(config)
    row, title = _screening_or_exit(screening_key)

    if not cfg.assist.enabled:
        console.print("[yellow]assist is disabled[/] — set `assist.enabled: true` in config.yaml")
        raise typer.Exit(1)

    profile = None
    min_contiguous = 1
    if watch:
        w = cfg.watch(watch)
        profile = cfg.profiles.get(w.seats.profile or "")
        min_contiguous = w.seats.min_contiguous

    async def _run() -> None:
        assistant = CheckoutAssistant(cfg.assist)
        result = await assistant.assist(
            row.booking_url or "", screening_key, preference=profile, min_contiguous=min_contiguous
        )
        console.print(f"[bold]{title}[/] — {row.auditorium}")
        console.print(result.summary())
        if result.handed_over:
            console.print(
                "[yellow]A human check appeared.[/] Solve it in the browser window — "
                "this tool will not do that for you."
            )

    asyncio.run(_run())


if __name__ == "__main__":
    app()
