# Beach Camp Scanner

Watches a fixed list of California state-park beach campgrounds for **2-night
(Friday + Saturday) availability** and emails you the moment one opens, with a
direct booking link.

Runs on GitHub Actions every 10 minutes. No VPS, no database, no local machine.
Standard-library Python only — there is no `pip install` step, so there is no
dependency that can break or disappear six months from now.

---

## How it decides something is worth emailing

A campground is a **hit** when the Friday night *and* the Saturday night are
each bookable there — even if that means two different site numbers. Single-night
availability is never a hit.

Alerts are **edge-triggered**: the scanner remembers the previous poll's verdict
per campground in `state.json` and emails only on the transition
`unavailable → available`. While a site stays open across consecutive polls you
hear nothing. If it opens, gets booked by somebody else, and opens again, that is
a genuine second transition and it emails you again.

You will only ever receive three kinds of message:

| Type | When | Contains |
| --- | --- | --- |
| **HIT** | A campground newly meets the 2-night criteria | Campground, site number(s), which night each site covers, whether it is one site or a split stay, and the booking link. Subject is prefixed `🏕️ BOOK NOW`, and `[RV/NON-TENT]` when a hit only lands on a non-tent unit. |
| **ERROR** | The run failed — API error, schema change, parse failure, network, expired target dates | Summary, consecutive-failure count, and the full traceback. |
| **HEARTBEAT** | Weekly | Last run time and total polls since the previous heartbeat, so silence from this system genuinely means something is wrong. |

---

## Setup

### 1. Resolve the campground FacilityIDs

The scanner needs numeric UseDirect FacilityIDs. These are resolved **once, at
build time**, and committed — never looked up during a scan.

Go to **Actions → Resolve Facility IDs → Run workflow**.

It fetches the live `places` and `facilities` metadata, matches the 11 named
campgrounds, verifies each resolved ID against the real availability grid, prints
a review table to the run summary, and commits `campgrounds.json`.

**Read every row of that table before trusting it.** A wrong-but-valid FacilityID
is undetectable at runtime — the scanner would happily watch the wrong campground
forever. The table gives you the campground name, the FacilityID, the parent park
and PlaceId, the name the API itself reports for that facility, its live unit
count and a few sample site numbers, and the booking URL.

If a campground comes back `AMBIGUOUS` or `UNRESOLVED`, the workflow **fails on
purpose** rather than guessing. It prints every candidate facility in that park.
Pick the right one and pin it in `resolve_facilities.py`:

```python
OVERRIDES = {
    "Pismo SB — North Beach Campground": 1234,
}
```

Then re-run the workflow.

### 2. Add the email secrets

**Settings → Secrets and variables → Actions → New repository secret.**

| Secret | Required | Notes |
| --- | --- | --- |
| `SMTP_USERNAME` | yes | Your full Gmail address. |
| `SMTP_PASSWORD` | yes | A Google **App Password** (16 characters), not your account password. |
| `ALERT_EMAIL_TO` | yes | Where alerts are delivered. |
| `SMTP_HOST` | no | Defaults to `smtp.gmail.com`. |
| `SMTP_PORT` | no | Defaults to `465` (implicit TLS). Port `587` switches to STARTTLS automatically. |
| `ALERT_EMAIL_FROM` | no | Defaults to `SMTP_USERNAME`. |

To create the app password: enable 2-Step Verification on your Google account,
then visit **myaccount.google.com → Security → 2-Step Verification → App
passwords**. Paste the 16 characters with or without spaces.

Credentials are read from the environment only. They are never logged, written to
`state.json`, or committed.

### 3. Confirm it works

**Actions → Scan for campsites → Run workflow**, with `dry_run` checked. That
polls every campground and prints the results without sending mail or writing
state. Then run it once with `dry_run` unchecked — the first real run also sends
a heartbeat email, which confirms your SMTP secrets end to end.

After that the schedule takes over. There is nothing else to do.

---

## Changing things

### Target dates

Top of `scanner.py`:

```python
ARRIVAL_DATE = "2026-09-11"    # Friday
DEPARTURE_DATE = "2026-09-13"  # Sunday
```

`DEPARTURE_DATE` must be exactly two nights after `ARRIVAL_DATE`; the scanner
refuses to start otherwise. Changing the dates automatically clears the
remembered availability, so a campground that is already open for the new
weekend alerts on the next poll rather than being mistaken for "unchanged".

Once the target weekend passes, the scanner starts sending ERROR emails telling
you to update these dates — it will not sit there quietly scanning a weekend
that is already gone.

### Adding or removing a campground

Edit the `TARGETS` list in `resolve_facilities.py`:

```python
Target(
    "Bolsa Chica SB",
    place_patterns=["bolsa chica"],
    facility_required=["campground"],   # all of these must appear in the name
    facility_excluded=["group"],        # none of these may appear
    place_excluded=[],                  # rules out same-named parks
),
```

Then re-run **Resolve Facility IDs** and review the table again. To remove one,
delete its `Target` and re-run. Stale entries left in `state.json` are harmless
and get ignored.

### Tuning

| Setting | File | Default |
| --- | --- | --- |
| Delay between campground requests | `scanner.py` `POLITE_DELAY_SECONDS` | 1.5s (~17s of spacing per run) |
| ERROR email throttle | `scanner.py` `ERROR_EMAIL_THROTTLE_HOURS` | 6h |
| Heartbeat cadence | `scanner.py` `HEARTBEAT_INTERVAL_DAYS` | 7 days |
| Poll frequency | `.github/workflows/scan.yml` cron | `*/10 * * * *` |

The first failure, and any failure with a new signature, always emails
immediately; the throttle only suppresses repeats of an identical error, so a
broken API cannot deliver 144 identical emails a day.

---

## State file

`state.json` is committed back to the repo on every run. It stores the previous
poll's verdict (which is what makes alerting edge-triggered), and the resulting
commit stream keeps the repository active so GitHub never disables the schedule
for 60-day inactivity. You never have to touch this repo to keep it alive.

```jsonc
{
  "schema_version": 1,
  "target": { "arrival": "2026-09-11", "departure": "2026-09-13" },
  "last_run_utc": "2026-09-14T03:20:00+00:00",
  "last_run_status": "ok",              // "ok" | "error"
  "last_success_utc": "2026-09-14T03:20:00+00:00",
  "consecutive_failures": 0,
  "polls_since_heartbeat": 431,
  "failures_since_heartbeat": 0,
  "last_heartbeat_utc": "2026-09-11T03:14:00+00:00",
  "last_error_email_utc": null,         // drives the ERROR throttle
  "last_error_signature": null,
  "campgrounds": {
    "674": {                            // keyed by FacilityID
      "name": "Leo Carrillo SP",
      "available": false,               // the edge-trigger flag
      "stay_kind": null,                // "single" | "split" | null
      "sites": [],
      "units_seen": 137,
      "last_checked_utc": "2026-09-14T03:20:00+00:00",
      "last_change_utc": "2026-09-13T17:40:00+00:00",
      "last_alert_utc": "2026-09-13T17:40:00+00:00"
    }
  }
}
```

A corrupt `state.json` is discarded and rebuilt rather than wedging the scanner;
the worst case is one duplicate alert.

**Note on commit volume:** a commit per run is ~144 commits/day, which is what
keeps the repo unambiguously active. Each commit is a few hundred bytes. If you
ever want to trim that, change the commit step in `scan.yml` to skip runs where
only `last_run_utc` changed — but then you must keep some other guaranteed
periodic commit, or the 60-day clock starts running again.

---

## Why it fails loudly

A silent zero-results run is indistinguishable from "no cancellations", which is
the worst possible failure for this system. So the scanner escalates rather than
shrugging:

- Response missing `Message` or `Facility`, or `Units`/`Slices` arriving as the
  wrong JSON type → `SchemaError`.
- **Every** campground returning zero units → `SchemaError`. Every California
  beach campground being closed at once is far less likely than a schema change.
- **No** campground returning slices for the target nights → `SchemaError`.
- Any campground failing after retries → ERROR email naming which ones.
- A campground that errors keeps its previous state, so a transient failure can
  never manufacture a fake `unavailable → available` edge on the next run.
- If the HIT email itself fails to send, the campground is deliberately left
  recorded as unavailable so the next poll retries the notification instead of
  swallowing it.

A single campground legitimately returning no units (closed, out of season) is
recorded as a warning, not an error.

---

## Being a polite client

- One request per campground per run, spaced 1.5s apart.
- A real browser `User-Agent`, `Origin` and `Referer`.
- 4 attempts with exponential backoff (2s, 4s, 8s) plus jitter, on network
  errors, 5xx and 429 only.
- 4xx other than 429 fail immediately — retrying a rejected request just wastes
  the remote's capacity.
- A malformed response is never retried; it is reported.

---

## Files

| File | Purpose |
| --- | --- |
| `scanner.py` | The scan loop, target dates, state, alert decisions. **Edit dates here.** |
| `availability.py` | Turns one grid response into a campground-level verdict. |
| `usedirect.py` | Dependency-free UseDirect API client: endpoints, retries, schema guards. |
| `notify.py` | SMTP delivery and the three message templates. |
| `resolve_facilities.py` | Build-time name → FacilityID resolution. **Edit the campground list here.** |
| `campgrounds.json` | Generated. The hardcoded campground → FacilityID table. |
| `state.json` | Previous poll's availability plus run counters. |
| `tests.py` | Offline tests for the availability and edge-trigger logic. |
| `.github/workflows/scan.yml` | The every-10-minutes scan. |
| `.github/workflows/resolve-facilities.yml` | Manual build-time resolver. |

---

## Local development

```bash
python tests.py             # 48 offline assertions, no network needed
python scanner.py --dry-run # poll live, print, send nothing, write nothing
python resolve_facilities.py --print-only   # resolve and print, write nothing
```

`--dry-run` and `--print-only` require outbound access to
`calirdr.usedirect.com`.

---

## Data source

ReserveCalifornia runs on the UseDirect platform. Read-only, no authentication.

```
POST https://calirdr.usedirect.com/rdr/rdr/search/grid
     {"FacilityId": 674, "StartDate": "09-11-2026", "EndDate": "09-13-2026", ...}

GET  https://calirdr.usedirect.com/rdr/rdr/fd/places        # parks
GET  https://calirdr.usedirect.com/rdr/rdr/fd/facilities    # campgrounds
GET  https://calirdr.usedirect.com/rdr/rdr/search/filters   # unit categories
```

Booking links use the modern path format:
`https://www.reservecalifornia.com/park/<placeId>/<facilityId>`

Endpoint paths, request keys and response shapes were taken from
[`camply`](https://github.com/juftin/camply)'s working UseDirect provider
(`camply/providers/usedirect/usedirect.py` and
`camply/containers/usedirect.py`).
