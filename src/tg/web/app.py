"""Small read-only web dashboard.

Deliberately read-only: watches live in ``config.yaml`` so they are diffable and
version-controllable, and a second place to edit them would only create drift. This
answers "is it running, and what has it seen?" — the questions you actually have at
3am when a program is about to drop.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import col, select

from tg import db
from tg.config import AppConfig, load_config
from tg.core.scheduler import is_hot
from tg.core.timeutil import DEFAULT_TZ, format_local, from_db, to_local, utcnow_aware
from tg.core.watches import screening_matches
from tg.models import Alert, Event, PollState, Screening, Venue

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(config_path: str = "config.yaml") -> FastAPI:
    cfg = load_config(config_path)
    db.init_engine(cfg.database_url)
    db.create_all()

    app = FastAPI(title="ticket-grabber", docs_url=None, redoc_url=None)
    app.state.config = cfg
    app.state.config_path = config_path

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Any:
        return TEMPLATES.TemplateResponse(
            request=request, name="dashboard.html", context=_context(app.state.config)
        )

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        ctx = _context(app.state.config)
        return JSONResponse(
            {
                "hot": ctx["hot"],
                "sources": ctx["health"],
                "watches": [w["name"] for w in ctx["watches"]],
                "alerts_24h": ctx["alerts_24h"],
                "matches": len(ctx["matches"]),
            }
        )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness probe that also reports whether polling has actually happened."""
        with db.session_scope() as session:
            states = session.exec(
                select(PollState).where(col(PollState.cache_key).like("%:health"))
            ).all()
        stale = [s.source for s in states if s.consecutive_errors > 3]
        return JSONResponse(
            {"ok": not stale, "sources": len(states), "failing": stale},
            status_code=200 if not stale else 503,
        )

    return app


def _context(cfg: AppConfig) -> dict[str, Any]:
    now = utcnow_aware()
    hot = is_hot(to_local(now, DEFAULT_TZ), cfg.poll.hot_windows)

    with db.session_scope() as session:
        health = [
            {
                "source": s.source,
                "last_poll": (
                    format_local(from_db(s.last_polled_at)) if s.last_polled_at else "never"
                ),
                "errors": s.consecutive_errors,
                "empty": s.consecutive_empty,
                "last_error": s.last_error,
            }
            for s in session.exec(
                select(PollState).where(col(PollState.cache_key).like("%:health"))
            ).all()
        ]

        alerts = session.exec(
            select(Alert).order_by(col(Alert.created_at).desc()).limit(30)
        ).all()
        alerts_24h = len(
            [a for a in alerts if from_db(a.created_at) > now - timedelta(hours=24)]
        )

        events = {e.key: e for e in session.exec(select(Event)).all()}
        venues = {v.key: v for v in session.exec(select(Venue)).all()}
        screenings = session.exec(
            select(Screening).where(col(Screening.disappeared_at).is_(None))
        ).all()

        matches: list[dict[str, Any]] = []
        for watch in cfg.watches:
            if not watch.enabled:
                continue
            tz = cfg.sources[watch.source].options.get("timezone", DEFAULT_TZ)
            for s in screenings:
                if s.source != watch.source or from_db(s.starts_at) < now:
                    continue
                ev, venue = events.get(s.event_key), venues.get(s.venue_key)
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
                matches.append(
                    {
                        "watch": watch.name,
                        "title": ev.title if ev else "?",
                        "when": format_local(from_db(s.starts_at), tz),
                        "venue": venue.name if venue else "",
                        "auditorium": s.auditorium or "",
                        "free": s.availability_ratio,
                        "url": s.booking_url,
                        "sold_out": s.sold_out,
                    }
                )

        matches.sort(key=lambda m: m["when"])

        return {
            "hot": hot,
            "interval": cfg.poll.hot_seconds if hot else cfg.poll.baseline_seconds,
            "health": health,
            "alerts": [
                {
                    "when": format_local(from_db(a.created_at)),
                    "watch": a.watch_name,
                    "type": a.change_type,
                    "title": a.title,
                    "body": a.body,
                    "url": a.url,
                    "delivered": a.delivered,
                    "suppressed": a.suppressed,
                }
                for a in alerts
            ],
            "alerts_24h": alerts_24h,
            "watches": [
                {
                    "name": w.name,
                    "enabled": w.enabled,
                    "source": w.source,
                    "triggers": w.trigger.events,
                    "channels": w.notify,
                    "profile": w.seats.profile,
                    "assist": w.assist,
                }
                for w in cfg.watches
            ],
            "matches": matches[:60],
            "counts": {
                "venues": len(venues),
                "events": len(events),
                "screenings": len(screenings),
            },
        }
