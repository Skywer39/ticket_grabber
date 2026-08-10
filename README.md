# ticket-grabber

Watches ticketing sites and tells you the moment something you care about goes on
sale — or the moment a seat frees up on a screening that was already sold out.

It exists because of a specific failure. A cinema said its new program would drop on
Tuesday, published it early, and by the time anyone looked, every good seat for the
IMAX 70mm run was gone. That is a *detection latency* problem, not a checkout speed
problem, and this fixes the detection.

## What it actually does

Live figures from Cinema City Praha Flora while this was being built:

| Screening | Hall | Seats free |
|---|---|---|
| Odyssea, Tue 09:00 | IMAX VOLVO | **1.6%** |
| Odyssea, Tue 16:40 | IMAX VOLVO | **2.1%** |
| Odyssea, Thu 16:40 | IMAX VOLVO | **0.5%** |
| Odyssea, Tue 12:30 | Sál 04 | 96.9% |

The 70mm screenings are 98–99.5% sold while ordinary halls sit near-empty — and the
site flags none of them as sold out, so nothing on the page tells you they are gone.

## How it works

**Two-tier polling.** Sweeping a 60-day horizon costs one request per date. Doing
that every 45 seconds would be slow and rude, so each cycle:

1. Asks the site's calendar endpoint which dates have anything on sale — **one
   request**. A date appearing here is the earliest possible sign a program was
   published.
2. Fetches full detail only for dates that are new, plus a rotating slice of the
   dates your watches care about.
3. Reads an exact seat map only if step 2 says a watched screening moved.

A hot-mode cycle is about nine requests, and an early release is caught within one.

**Adapters normalize any site** into a shared vocabulary — events, screenings, seats,
format tags — so watch rules and seat preferences are written once and work anywhere.
Extraction is tried in order of preference: the site's own JSON, then structured data
(JSON-LD, sitemaps), then DOM selectors, then a headless browser.

**The diff engine reports what changed; watches decide whether it matters.** One
change log serves several watches with different sensitivities.

**Quiet by default.** The first poll seeds a baseline silently — otherwise every
screening in the database would look new. Bursts of the same change collapse into one
digest, because a newly published week is one event to a human, not eighty messages.

## Quick start

```bash
pip install -e .
cp config.example.yaml config.yaml
cp .env.example .env          # add a Discord webhook — fastest channel to set up

tg venues cinemacity_cz       # find your cinema's id
tg probe cinemacity_cz --title odyss   # see live data, touches no database
tg run --once                 # seeds the baseline
tg run                        # start watching
tg serve                      # dashboard on http://127.0.0.1:8756
```

Docker:

```bash
docker compose up -d && docker compose logs -f poller
```

## Running it when you're away from your own box

`.github/workflows/watch.yml` runs the poller on a schedule and pushes alerts to
Discord, with state kept on an orphan `state` branch (rewritten each run, so it stays
at one commit and never grows the repo). Add one repository secret —
`TG_DISCORD_WEBHOOK_URL` — and it starts on its own.

Each run is a **5h40m polling session**, not a single poll, because GitHub's scheduler
cannot be trusted for cadence. Measured on this repo: a `*/10` cron dispatched at gaps
of **85, 75, 60 and 65 minutes**; an hourly cron at gaps of **205 and 266 minutes**.
With 52-minute sessions that left the site unwatched about three quarters of the time.

Sizing the session just under GitHub's 6-hour job ceiling fixes it. A dispatch now lands
while a session is still running, waits in the `concurrency` group, and starts the moment
the current one ends — so sessions abut instead of leaving holes. The cron is no longer
the cadence; it is what keeps the chain unbroken. Inside a session the cadence is the
poller's own: 45s in the hot window, 15 min overnight.

The group holds a *slot*, not a queue — only the newest waiting run survives, and earlier
ones are cancelled. That is harmless for the scheduled chain, where every dispatch is
interchangeable, and it is exactly what broke deploys until the change below.

**A merge pre-empts the running session.** Without that a deploy waits for the current
session to time out, and the concurrency group is not a queue: GitHub keeps one running
job and *at most one* pending job, so a newly queued run evicts the waiting one instead of
lining up behind it. Measured here, three consecutive new-code runs were cancelled while a
stale session held the slot, and the fix they carried sat undeployed for a day. So
`push` to the default branch cancels in progress; `schedule` never does, which is what
keeps sessions abutting. Every alert carries the running commit in its footer, because a
session outlives several merges and "is my change live yet?" needs an answer.

Three caveats, in order of how likely they are to bite:

- Needs a **public repository**. Private repos meter Actions minutes (2,000/month free),
  which a continuously running job exhausts in under two days.
- State is committed only at session end, so an abrupt runner failure loses up to ~5.7h
  of accumulated state. The next session re-seeds and stays silent for one cycle.
- If a dispatch is missed entirely during a platform incident, the chain breaks and
  coverage stops until the next one lands. This is cover while you are away, not a
  replacement for `tg run` on a box you control.

## Writing a watch

Watches live in `config.yaml` so they stay diffable and version-controlled.

```yaml
profiles:
  flora_imax:                 # what "a good seat" means here, written once
    auditorium_regex: "(?i)imax"
    rows: [8, 14]
    seat_range: [10, 20]
    avoid_rows: [1, 2, 3]

watches:
  - name: "Odyssea IMAX 70mm"
    source: cinemacity_cz
    match:
      title_regex: "(?i)odyss"
      formats: [FILM_70MM]
      auditorium_regex: "(?i)imax"
      time_between: ["16:00", "23:00"]
    seats: { profile: flora_imax, min_contiguous: 2 }
    trigger:
      on: [NEW_SCREENING, AVAILABILITY_RISE]
      min_seats_above_floor: 4         # in seats, measured from the resting level
      max_availability: 0.10           # only while the house is still sold out
    notify: [discord]
    cooldown: 10m
```

## Counting in seats, not percentages

The site publishes a ratio and never a seat count, so the obvious alert reads "2.1% of
seats free" — true, and useless. Two things fix that.

**The hall's size is recoverable.** Every ratio the site has ever published for one
auditorium is a multiple of `1 / capacity`, so the smallest denominator consistent with
all of them *is* the capacity. Flora's IMAX comes out at 385 seats, and "2.1% free"
becomes "8 of 385 seats free (+2)".

**A sold-out house does not rest at zero.** Each screening settles at its own floor of
seats that simply never sell — measured per screening they are 2, 3, 4, 5, 6 and 29, with
six the most common among similarly sold-out shows. Measuring a rise from the previous
reading therefore counts stock that has been sitting there for days. `min_seats_above_floor`
measures it from each screening's own observed minimum instead.

Note what this does *not* claim: why those seats never sell. Tier 1 sees a count and never
an identity, so whether they are restricted-view, wheelchair spaces or simply unwanted is
not something this can tell you — see the limits below.

Why it matters: five days of the old fractional threshold produced 63 alerts, every one
of them a two-seat move on a house resting at six — a cart timing out, the seats
returning to the pool, and someone else taking them within the half hour. All true, none
actionable. Replayed against the same recorded history, the rule above fires 20 times,
and every one is a block of four to six seats genuinely coming back.

`availability_rise_min` still exists and still works; it is the fallback for a hall whose
capacity cannot be estimated yet. Prefer the seats form — a fraction means a different
number of seats in every auditorium.

### Looking twice

Size is only half of it. `availabilityRatio` counts seats that are **not** sitting in
somebody's open checkout, so on a house this full most of its movement is a cart timing
out and being re-taken rather than anyone cancelling. Measured on one IMAX screening:

```
08:02:12  5 -> 7 seats free   alert sent
08:06:34  5 seats free        four minutes later
```

Same screening, same shape twice before: 5→6 back in 35 minutes, 5→7 back in 41. Those
seats were never really on sale — you were watching the gap between one checkout expiring
and the next starting.

So before alerting, the poller waits `poll.confirm_seconds` (default 90) and reads the
screening again, and the *confirmed* number replaces the first one. The ordinary
threshold then decides: a full revert fails it by itself, a partial revert is judged on
what is actually left, and the message quotes what is still there rather than what has
already gone — "still free 90s later".

It costs a real find ninety seconds. Against blocks that survive 24–53 minutes that is
cheap, and against a four-minute blip it is the whole difference. Set it to `0` to alert
on the first reading.

The rule that would have prevented the original miss is the boring one: alert on
`NEW_SCREENING` for anything appearing in the hall you care about, regardless of film.

Key that catch-all on the **auditorium**, not the format. The IMAX hall also hosts
one-off events — concert films, anniversary screenings — that run without a `70-mm`
attribute, and a format filter drops them silently.

```yaml
  - name: "Anything new in the IMAX hall"
    source: cinemacity_cz
    match:
      auditorium_regex: "(?i)imax"
      cinemas: ["1052"]
    trigger: { on: [NEW_SCREENING] }
    notify: [discord]
    cooldown: 1h
```

And subscribe something to `NEW_DATE`, which is the calendar probe — one request, and the
first place a newly published week shows up, before any showtime detail is fetched:

```yaml
  - name: "New dates published at Flora"
    source: cinemacity_cz
    trigger: { on: [NEW_DATE] }
    notify: [discord]
    cooldown: 6h
```

Deliberately unfiltered. A new date carries no film, hall or format, so `match` criteria
naming any of those cannot narrow it — config load warns rather than silently ignoring
them. Filter with a `NEW_SCREENING` watch; use this one as the heads-up that the next
week exists at all.

Check it before trusting it:

```bash
tg watch test "Odyssea IMAX 70mm"   # which screenings match, and why
tg notify test discord              # confirm alerts actually reach you
tg status                           # health, recent alerts, polling mode
```

## Adding a site

```bash
tg adapter discover https://some-cinema.example/program
```

Loads the page, records every JSON response, and ranks them on how schedule-like they
look. The ranked list is usually enough to write an adapter by hand; `--draft` adds a
model-suggested field mapping (needs `ANTHROPIC_API_KEY`).

When an adapter silently starts returning nothing — the failure mode that looks
exactly like a quiet week — `tg adapter heal` says so.

## Limits, stated plainly

**Seat-level detail may not work.** Tier 1 tells you *how many* seats are free, from
a fast public endpoint, reliably. Telling you *which* seats requires the booking flow,
and that host is behind Cloudflare bot management. In testing it returned a hard block
page to an automated session.

The practical consequence is worth stating plainly: with `seatmap` off, a watch's
`seats` block — the row range, the seat range, `min_contiguous` — **is not applied**.
Nothing filters on seat position, so "four seats came back" may still mean four singles
scattered across the front row. Tier 1 is honest about quantity and silent about
quality. So:

- `seatmap` is **off by default**. If enabled and blocked, it disables itself, says
  so, and ratio-based alerting continues unaffected.
- The seat selectors ship as defaults derived from the site's stylesheet, not from a
  live seat page. Verify them on your own machine with `tg seatmap probe <key>` and
  override in config if needed.

**Alerts link to pages, not to booking.** This site has no linkable booking URL at
all: `bookingLink` is a POST-only endpoint (a GET returns 404 "Error Occurred"), and
its own `bookingRouterLaunchLink` serves an auto-submitting form that posts to
`tickets.rel.cinemacity.cz`, which answers 403 to everyone. Booking is entered by POST
from a page that already holds a session, which a notification cannot do. So alerts
link to the film page and the cinema programme — both plain documents that open — and
you pick the showtime there. One extra click, but it works.

Those links carry the screening's **own date**, using the hash routes the site's
booking widget reads (`#/buy-tickets-by-film?in-cinema=…&at=…&for-movie=…`). Without
the date a link opens on today, which is never the day the alert is about — and once
today's showtimes for that film have passed, the page has nothing left to show and
answers *"Bohužel tento film v kině … nehrajeme"*. Two details that only testing
reveals: the film route takes the **city group slug**, rewriting a cinema id to it,
while the cinema route takes the **cinema id**; and the date must be the venue's wall
date, not the UTC one, or late screenings link to the wrong day.

**No bot-check solving, ever.** Not CAPTCHAs, not Turnstile, no evasion, no identity
rotation, no retrying a refusal. When the site says no, this stops and tells you.

**Checkout assistance never buys anything.** The default mode hands the deep link to
your real desktop browser, where you already hold a session — nothing to detect,
nothing to break. `drive` mode additionally steers a Playwright browser using *your
own* logged-in profile to the seat picker and pre-selects matching seats, then stops
dead; a never-click list guards anything resembling payment, in Czech and English.

Automating a booking flow is very likely against the site's terms of service, and
`/booking` is disallowed in its `robots.txt`. Tier-1 polling is not: the data API is
explicitly permitted, and the client enforces robots.txt, a per-host token bucket,
conditional GETs and backoff regardless.

## Development

```bash
pip install -e '.[dev]'
pytest          # 178 tests, run against payloads captured from the live API
ruff check src tests
```

Tests use real captured responses rather than hand-written fixtures, so the
golden-data assertions describe actual site behaviour.
