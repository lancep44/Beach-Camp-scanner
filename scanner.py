#!/usr/bin/env python3
"""
ReserveCalifornia beach-campground scanner.

Polls a fixed list of California state-park beach campgrounds for two-night
(Friday + Saturday) availability and emails on the *transition* from
unavailable to available. Designed to run unattended on GitHub Actions for
months without intervention.

    python scanner.py              # normal run: poll, alert, persist state
    python scanner.py --dry-run    # poll and print; send no email, write no state

Everything you would normally want to change lives in the CONFIG block below.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import notify
from availability import CampgroundResult, evaluate_campground
from usedirect import SchemaError, UseDirectError, booking_url, fetch_grid

# --------------------------------------------------------------------------
# CONFIG — edit these
# --------------------------------------------------------------------------

# Arrival and departure, ISO format. The nights actually booked are every night
# from ARRIVAL up to but not including DEPARTURE, so the pair below is the
# Friday and Saturday nights of that weekend.
ARRIVAL_DATE = "2026-09-11"    # Friday
DEPARTURE_DATE = "2026-09-13"  # Sunday

# Seconds to wait between campgrounds. Eleven campgrounds at 1.5s is ~17s of
# request spacing per run — well under anything that could look like abuse.
POLITE_DELAY_SECONDS = 1.5

# Don't send the same ERROR email more often than this. The first failure, and
# any failure with a new signature, always sends immediately.
ERROR_EMAIL_THROTTLE_HOURS = 6

# "Still running normally" email cadence.
HEARTBEAT_INTERVAL_DAYS = 7

# If at least this fraction of campgrounds come back with zero units, treat it
# as a schema change rather than "everything is closed".
EMPTY_RESPONSE_ALARM_RATIO = 1.0

# --------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parent
CAMPGROUNDS_FILE = REPO_ROOT / "campgrounds.json"
STATE_FILE = REPO_ROOT / "state.json"
STATE_SCHEMA_VERSION = 1

PARK_TIMEZONE = "America/Los_Angeles"


class ConfigError(Exception):
    """The scanner is not set up correctly and cannot run."""


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def local_today() -> datetime.date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo(PARK_TIMEZONE)).date()
    except Exception:  # noqa: BLE001 - missing tzdata should never stop a run
        return datetime.datetime.utcnow().date()


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def target_dates() -> Tuple[datetime.date, datetime.date, List[str]]:
    try:
        arrival = datetime.date.fromisoformat(ARRIVAL_DATE)
        departure = datetime.date.fromisoformat(DEPARTURE_DATE)
    except ValueError as exc:
        raise ConfigError(
            f"ARRIVAL_DATE/DEPARTURE_DATE must be ISO YYYY-MM-DD: {exc}"
        ) from exc

    nights_count = (departure - arrival).days
    if nights_count != 2:
        raise ConfigError(
            f"This scanner watches exactly two nights, but {ARRIVAL_DATE} -> "
            f"{DEPARTURE_DATE} is {nights_count} night(s)."
        )

    nights = [(arrival + datetime.timedelta(days=offset)).isoformat() for offset in range(2)]
    return arrival, departure, nights


def stay_label(arrival: datetime.date, departure: datetime.date) -> str:
    return (
        f"{arrival.strftime('%a')} {arrival.month}/{arrival.day} – "
        f"{departure.strftime('%a')} {departure.month}/{departure.day} "
        f"({arrival.year})"
    )


# --------------------------------------------------------------------------
# Campground list and state
# --------------------------------------------------------------------------


def load_campgrounds() -> Dict[str, Any]:
    if not CAMPGROUNDS_FILE.exists():
        raise ConfigError(
            f"{CAMPGROUNDS_FILE.name} is missing. Run the 'Resolve Facility IDs' "
            f"workflow to generate it."
        )
    try:
        data = json.loads(CAMPGROUNDS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{CAMPGROUNDS_FILE.name} is not valid JSON: {exc}") from exc

    if not data.get("resolved"):
        raise ConfigError(
            f"{CAMPGROUNDS_FILE.name} has not been resolved yet. Run the "
            f"'Resolve Facility IDs' workflow (Actions tab -> Resolve Facility IDs "
            f"-> Run workflow), review the printed table, and commit the result."
        )

    campgrounds = data.get("campgrounds") or []
    if not campgrounds:
        raise ConfigError(f"{CAMPGROUNDS_FILE.name} contains no campgrounds.")

    for entry in campgrounds:
        for key in ("name", "facility_id", "place_id", "park_name"):
            if entry.get(key) in (None, ""):
                raise ConfigError(
                    f"{CAMPGROUNDS_FILE.name}: campground {entry!r} is missing '{key}'."
                )
    return data


def default_state() -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "target": {"arrival": None, "departure": None},
        "last_run_utc": None,
        "last_run_status": None,
        "last_success_utc": None,
        "consecutive_failures": 0,
        "polls_since_heartbeat": 0,
        "failures_since_heartbeat": 0,
        "last_heartbeat_utc": None,
        "last_error_email_utc": None,
        "last_error_signature": None,
        "campgrounds": {},
    }


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt state file must not wedge the scanner forever. Starting
        # fresh costs at most one duplicate alert.
        print("WARNING: state.json was unreadable; starting from a fresh state.")
        return default_state()

    merged = default_state()
    if isinstance(state, dict):
        merged.update(state)
    if not isinstance(merged.get("campgrounds"), dict):
        merged["campgrounds"] = {}
    return merged


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


def poll_all(
    campgrounds: List[Dict[str, Any]],
    unit_categories: Dict[str, str],
    arrival: datetime.date,
    departure: datetime.date,
    nights: List[str],
) -> Tuple[Dict[str, CampgroundResult], List[str]]:
    """Poll every campground. Returns (results by facility id, error strings)."""
    results: Dict[str, CampgroundResult] = {}
    errors: List[str] = []

    for index, entry in enumerate(campgrounds):
        facility_id = int(entry["facility_id"])
        name = entry["name"]

        if index:
            time.sleep(POLITE_DELAY_SECONDS)

        try:
            grid = fetch_grid(facility_id, arrival, departure)
            result = evaluate_campground(
                grid=grid,
                facility_id=facility_id,
                name=name,
                nights=nights,
                unit_categories=unit_categories,
            )
        except (UseDirectError, SchemaError) as exc:
            errors.append(f"{name} (facility {facility_id}): {type(exc).__name__}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - never let one campground kill the run
            errors.append(
                f"{name} (facility {facility_id}): unexpected "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        results[str(facility_id)] = result
        status = "OPEN" if result.available else "full"
        print(
            f"  {name:<45} facility={facility_id:<7} {status:<5} "
            f"units={result.units_seen}"
            + (f"  -> {result.site_summary}" if result.available else "")
        )
        for warning in result.warnings:
            print(f"      warning: {warning}")

    return results, errors


def detect_schema_drift(results: Dict[str, CampgroundResult], nights: List[str]) -> None:
    """Escalate 'everything is quietly empty' into a loud failure.

    A silent zero-results run looks exactly like 'no cancellations', so the
    only safe response to a whole-fleet blank is to raise.
    """
    if not results:
        return

    total = len(results)
    no_units = sum(1 for result in results.values() if result.units_seen == 0)
    if no_units / total >= EMPTY_RESPONSE_ALARM_RATIO:
        raise SchemaError(
            f"All {total} campgrounds returned zero units. This is far more likely "
            f"to be an API/schema change than every California beach campground "
            f"being closed at once."
        )

    no_target_slices = sum(1 for result in results.values() if not result.nights_covered)
    if no_target_slices == total:
        raise SchemaError(
            f"No campground returned availability slices for the target nights "
            f"({', '.join(nights)}). Either the date window moved out of the "
            f"bookable range or the response shape changed."
        )


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------


def process_hits(
    results: Dict[str, CampgroundResult],
    campgrounds_by_id: Dict[str, Dict[str, Any]],
    state: Dict[str, Any],
    label: str,
    dry_run: bool,
) -> Tuple[int, List[str]]:
    """Edge-triggered alerting: fire only on unavailable -> available."""
    alerts_sent = 0
    alert_errors: List[str] = []
    now = utc_now_iso()

    for facility_id, result in results.items():
        entry = campgrounds_by_id[facility_id]
        previous = state["campgrounds"].get(facility_id) or {}
        was_available = bool(previous.get("available"))

        record = {
            "name": result.name,
            "available": result.available,
            "stay_kind": result.stay_kind,
            "sites": [site.to_dict() for site in result.sites],
            "units_seen": result.units_seen,
            "last_checked_utc": now,
            "last_change_utc": previous.get("last_change_utc"),
            "last_alert_utc": previous.get("last_alert_utc"),
        }
        if result.available != was_available:
            record["last_change_utc"] = now

        # The transition is the alert. Staying open across polls is silent;
        # open -> booked -> open is a genuine second transition and fires again.
        if result.available and not was_available:
            link = booking_url(int(entry["place_id"]), int(entry["facility_id"]))
            print(f"  *** NEW AVAILABILITY: {result.name} -> {result.site_summary}")
            if dry_run:
                print(f"      (dry run, no email) {link}")
            else:
                try:
                    notify.send_hit(
                        result=result,
                        stay_label=label,
                        link=link,
                        park_name=entry["park_name"],
                    )
                    record["last_alert_utc"] = now
                    alerts_sent += 1
                except notify.NotificationError as exc:
                    # Do NOT record the alert: leaving state unavailable means the
                    # next poll retries the notification instead of swallowing it.
                    record["available"] = False
                    record["last_change_utc"] = previous.get("last_change_utc")
                    alert_errors.append(f"HIT email for {result.name} failed: {exc}")

        state["campgrounds"][facility_id] = record

    return alerts_sent, alert_errors


def maybe_send_error(state: Dict[str, Any], summary: str, detail: str, dry_run: bool) -> None:
    """Send an ERROR email, throttled so a broken API cannot flood the inbox."""
    signature = summary[:200]
    last_sent = parse_iso(state.get("last_error_email_utc"))
    same_signature = state.get("last_error_signature") == signature

    if same_signature and last_sent is not None:
        age_hours = (
            datetime.datetime.now(datetime.timezone.utc) - last_sent
        ).total_seconds() / 3600.0
        if age_hours < ERROR_EMAIL_THROTTLE_HOURS:
            print(
                f"  ERROR email suppressed ({age_hours:.1f}h since last identical "
                f"error, throttle is {ERROR_EMAIL_THROTTLE_HOURS}h)."
            )
            return

    if dry_run:
        print(f"  (dry run, no email) ERROR: {summary}")
        return

    try:
        notify.send_error(summary, detail, int(state.get("consecutive_failures") or 0))
        state["last_error_email_utc"] = utc_now_iso()
        state["last_error_signature"] = signature
    except notify.NotificationError as exc:
        print(f"  FAILED to send ERROR email: {exc}", file=sys.stderr)


def maybe_send_heartbeat(
    state: Dict[str, Any],
    campground_count: int,
    label: str,
    dry_run: bool,
) -> None:
    last = parse_iso(state.get("last_heartbeat_utc"))
    due = last is None or (
        datetime.datetime.now(datetime.timezone.utc) - last
    ) >= datetime.timedelta(days=HEARTBEAT_INTERVAL_DAYS)
    if not due:
        return

    open_now = sorted(
        record.get("name", facility_id)
        for facility_id, record in state["campgrounds"].items()
        if record.get("available")
    )

    if dry_run:
        print("  (dry run, no email) HEARTBEAT due")
        return

    try:
        notify.send_heartbeat(
            last_run=state.get("last_run_utc") or utc_now_iso(),
            polls=int(state.get("polls_since_heartbeat") or 0),
            campground_count=campground_count,
            stay_label=label,
            failures=int(state.get("failures_since_heartbeat") or 0),
            open_now=open_now,
        )
        state["last_heartbeat_utc"] = utc_now_iso()
        state["polls_since_heartbeat"] = 0
        state["failures_since_heartbeat"] = 0
    except notify.NotificationError as exc:
        print(f"  FAILED to send heartbeat email: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Poll and print results without sending email or writing state.",
    )
    args = parser.parse_args()

    state = load_state()
    state["polls_since_heartbeat"] = int(state.get("polls_since_heartbeat") or 0) + 1
    state["last_run_utc"] = utc_now_iso()

    run_failed = False
    failure_summary = ""
    failure_detail = ""

    try:
        arrival, departure, nights = target_dates()
        label = stay_label(arrival, departure)

        # A target weekend that has already passed would otherwise scan forever
        # and find nothing, which is indistinguishable from "no cancellations".
        if departure <= local_today():
            raise ConfigError(
                f"The target stay ({label}) is in the past. Update ARRIVAL_DATE and "
                f"DEPARTURE_DATE at the top of scanner.py, or the scanner will keep "
                f"running and never find anything."
            )

        config = load_campgrounds()
        campgrounds = config["campgrounds"]
        unit_categories = {
            str(key): str(value) for key, value in (config.get("unit_categories") or {}).items()
        }
        campgrounds_by_id = {str(entry["facility_id"]): entry for entry in campgrounds}

        if not args.dry_run:
            problems = notify.config_problems()
            if problems:
                raise ConfigError(
                    "Email is not configured, so alerts could not be delivered: "
                    + "; ".join(problems)
                )

        # Changing the target weekend invalidates every remembered verdict.
        previous_target = state.get("target") or {}
        current_target = {"arrival": ARRIVAL_DATE, "departure": DEPARTURE_DATE}
        if previous_target != current_target:
            print(
                f"Target dates changed {previous_target or '(none)'} -> {current_target}; "
                f"clearing remembered availability."
            )
            state["campgrounds"] = {}
            state["target"] = current_target

        print(f"Scanning {len(campgrounds)} campgrounds for {label}")
        print(f"Nights required: {', '.join(nights)}")

        results, poll_errors = poll_all(
            campgrounds=campgrounds,
            unit_categories=unit_categories,
            arrival=arrival,
            departure=departure,
            nights=nights,
        )

        if not results:
            raise UseDirectError(
                "Every campground failed to return usable availability:\n  - "
                + "\n  - ".join(poll_errors)
            )

        detect_schema_drift(results, nights)

        alerts_sent, alert_errors = process_hits(
            results=results,
            campgrounds_by_id=campgrounds_by_id,
            state=state,
            label=label,
            dry_run=args.dry_run,
        )

        run_errors = poll_errors + alert_errors
        if run_errors:
            run_failed = True
            failure_summary = (
                f"{len(run_errors)} of {len(campgrounds)} campgrounds failed this run"
            )
            failure_detail = "\n".join(f"- {item}" for item in run_errors)
        else:
            print(
                f"Run complete: {len(results)} campgrounds checked, "
                f"{alerts_sent} new alert(s) sent."
            )

    except Exception as exc:  # noqa: BLE001 - top level: everything becomes an ERROR email
        run_failed = True
        failure_summary = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:200]
        failure_detail = traceback.format_exc()
        print(failure_detail, file=sys.stderr)

    if run_failed:
        state["last_run_status"] = "error"
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state["failures_since_heartbeat"] = int(state.get("failures_since_heartbeat") or 0) + 1
        maybe_send_error(state, failure_summary, failure_detail, args.dry_run)
    else:
        state["last_run_status"] = "ok"
        state["consecutive_failures"] = 0
        state["last_success_utc"] = state["last_run_utc"]
        state["last_error_signature"] = None

    try:
        maybe_send_heartbeat(
            state=state,
            campground_count=len(state.get("campgrounds") or {}),
            label=stay_label(*target_dates()[:2]),
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - heartbeat must never fail a run
        print(f"Heartbeat step failed: {exc}", file=sys.stderr)

    if args.dry_run:
        print("Dry run: state.json not written.")
    else:
        save_state(state)

    return 1 if run_failed else 0


if __name__ == "__main__":
    sys.exit(main())
