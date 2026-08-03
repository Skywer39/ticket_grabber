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

`.github/workflows/watch.yml` polls on a schedule and pushes alerts to Discord, with
state kept on an orphan `state` branch (rewritten each run, so it stays at one commit
and never grows the repo). Add one repository secret — `TG_DISCORD_WEBHOOK_URL` — and
it starts on its own.

Worth knowing what you give up: GitHub's minimum cron granularity is 5 minutes and
scheduled runs are routinely dispatched 10–15 minutes late, so there is no 45-second
hot mode — each run is a single poll. A newly published date is still fetched in the
very next run, because new dates bypass the rotation. Treat it as cover while you are
away, not as a replacement for `tg run` on a box you control.

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
      on: [NEW_SCREENING, SEAT_FREED, AVAILABILITY_RISE]
      availability_rise_min: 0.005     # ignore jitter
    notify: [discord]
    cooldown: 10m
```

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
page to an automated session. So:

- `seatmap` is **off by default**. If enabled and blocked, it disables itself, says
  so, and ratio-based alerting continues unaffected.
- The seat selectors ship as defaults derived from the site's stylesheet, not from a
  live seat page. Verify them on your own machine with `tg seatmap probe <key>` and
  override in config if needed.

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
pytest          # 104 tests, run against payloads captured from the live API
ruff check src tests
```

Tests use real captured responses rather than hand-written fixtures, so the
golden-data assertions describe actual site behaviour.
