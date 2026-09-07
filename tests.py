#!/usr/bin/env python3
"""
Offline tests for the availability logic and the edge-triggered alerting.

Fixtures mirror the real /rdr/rdr/search/grid response shape as documented by
camply's pydantic models (camply/containers/usedirect.py): Facility.Units is a
map of unit id -> unit, and each unit's Slices is a map of datetime string ->
{Date, IsFree, IsBlocked, IsWalkin, MinStay, ...}.

    python tests.py
"""

from __future__ import annotations

import sys

import availability
import scanner
from availability import evaluate_campground
from usedirect import SchemaError

FRI = "2026-09-11"
SAT = "2026-09-12"
NIGHTS = [FRI, SAT]
CATEGORIES = {"1": "Tent", "2": "RV/Motorhome", "3": "Lodging"}

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def slice_for(date: str, free: bool, **extra) -> dict:
    payload = {
        "Date": f"{date}T00:00:00",
        "IsFree": free,
        "IsBlocked": False,
        "IsWalkin": False,
        "ReservationId": 0,
        "Lock": None,
        "MinStay": 1,
        "IsReservationDraw": False,
    }
    payload.update(extra)
    return payload


def unit(unit_id: int, name: str, fri: dict, sat: dict, category: int = 1, **extra) -> dict:
    payload = {
        "UnitId": unit_id,
        "Name": name,
        "ShortName": name,
        "IsAda": False,
        "AllowWebBooking": True,
        "IsWebViewable": True,
        "UnitCategoryId": category,
        "UnitTypeGroupId": 10,
        "VehicleLength": 0,
        "SliceCount": 2,
        "AvailableCount": 0,
        "Slices": {
            f"{FRI}T00:00:00": fri,
            f"{SAT}T00:00:00": sat,
        },
    }
    payload.update(extra)
    return payload


def grid(*units: dict) -> dict:
    return {
        "Message": "",
        "UnitTypeId": 0,
        "StartDate": FRI,
        "EndDate": "2026-09-13",
        "Facility": {
            "FacilityId": 999,
            "Name": "Test Campground",
            "Latitude": 34.0,
            "Longitude": -119.0,
            "Units": {str(u["UnitId"]): u for u in units},
        },
    }


def run(payload: dict):
    return evaluate_campground(payload, 999, "Test Campground", NIGHTS, CATEGORIES)


# --------------------------------------------------------------------------
print("Availability evaluation")

result = run(grid(unit(1, "42", slice_for(FRI, True), slice_for(SAT, True))))
check("one site free both nights -> single-site hit", result.available and result.stay_kind == "single")
check("single-site hit names the site", [s.name for s in result.sites] == ["42"])
check("single-site hit covers both nights", result.sites[0].nights == [FRI, SAT])

result = run(
    grid(
        unit(1, "42", slice_for(FRI, True), slice_for(SAT, False)),
        unit(2, "77", slice_for(FRI, False), slice_for(SAT, True)),
    )
)
check("different sites each night -> split hit", result.available and result.stay_kind == "split")
check("split names both sites", sorted(s.name for s in result.sites) == ["42", "77"])
check("split assigns one night per site", [s.nights for s in result.sites] == [[FRI], [SAT]])

result = run(grid(unit(1, "42", slice_for(FRI, True), slice_for(SAT, False))))
check("Friday only -> no hit", not result.available)

result = run(grid(unit(1, "42", slice_for(FRI, False), slice_for(SAT, True))))
check("Saturday only -> no hit", not result.available)

result = run(grid(unit(1, "42", slice_for(FRI, False), slice_for(SAT, False))))
check("fully booked -> no hit", not result.available)

result = run(
    grid(unit(1, "42", slice_for(FRI, True, IsBlocked=True), slice_for(SAT, True)))
)
check("blocked Friday slice is not bookable", not result.available)

result = run(
    grid(unit(1, "42", slice_for(FRI, True, IsWalkin=True), slice_for(SAT, True)))
)
check("walk-in Friday slice is not bookable", not result.available)

result = run(
    grid(unit(1, "42", slice_for(FRI, True, MinStay=3), slice_for(SAT, True)))
)
check("MinStay=3 blocks a 2-night stay", not result.available)

result = run(
    grid(unit(1, "42", slice_for(FRI, True, MinStay=2), slice_for(SAT, True)))
)
check("MinStay=2 still allows a 2-night stay", result.available and result.stay_kind == "single")

result = run(
    grid(
        unit(1, "42", slice_for(FRI, True), slice_for(SAT, True), AllowWebBooking=False),
    )
)
check("unit that cannot be web-booked is ignored", not result.available)

result = run(
    grid(unit(1, "RV-9", slice_for(FRI, True), slice_for(SAT, True), category=2))
)
check("RV category is flagged non-tent", result.available and result.has_non_tent_site)
check("RV category name is carried through", result.sites[0].category == "RV/Motorhome")

result = run(grid(unit(1, "42", slice_for(FRI, True), slice_for(SAT, True))))
check("tent category is not flagged", not result.has_non_tent_site)

# Missing slices for the target nights = that unit simply cannot hold them.
lonely = unit(1, "42", slice_for(FRI, True), slice_for(SAT, True))
lonely["Slices"] = {"2026-10-01T00:00:00": slice_for("2026-10-01", True)}
result = run(grid(lonely))
check("units with no target-night slices produce no hit", not result.available)
check("missing target nights raises a warning", bool(result.warnings))

payload = grid(unit(1, "42", slice_for(FRI, True), slice_for(SAT, True)))
payload["Facility"]["Units"] = None
result = run(payload)
check("Units=None is a warning, not a crash", not result.available and bool(result.warnings))

print("\nSchema-drift detection")

for label, mutate in [
    ("Facility is not an object", lambda p: p.__setitem__("Facility", "nope")),
    ("Units is a list", lambda p: p["Facility"].__setitem__("Units", [])),
    ("Slices is a list", lambda p: list(p["Facility"]["Units"].values())[0].__setitem__("Slices", [])),
]:
    payload = grid(unit(1, "42", slice_for(FRI, True), slice_for(SAT, True)))
    mutate(payload)
    try:
        run(payload)
        check(label, False, "expected SchemaError")
    except SchemaError:
        check(label, True)

class FakeResult:
    def __init__(self, units_seen: int, nights_covered: list) -> None:
        self.units_seen = units_seen
        self.nights_covered = nights_covered

try:
    scanner.detect_schema_drift({"1": FakeResult(0, []), "2": FakeResult(0, [])}, NIGHTS)
    check("all-empty fleet raises", False, "expected SchemaError")
except SchemaError:
    check("all-empty fleet raises", True)

try:
    scanner.detect_schema_drift({"1": FakeResult(50, []), "2": FakeResult(50, [])}, NIGHTS)
    check("no target-night slices anywhere raises", False, "expected SchemaError")
except SchemaError:
    check("no target-night slices anywhere raises", True)

try:
    scanner.detect_schema_drift({"1": FakeResult(50, NIGHTS), "2": FakeResult(0, [])}, NIGHTS)
    check("one empty campground is tolerated", True)
except SchemaError as exc:
    check("one empty campground is tolerated", False, str(exc))

print("\nEdge-triggered alerting")

sent: list = []


def fake_send_hit(result, stay_label, link, park_name):  # noqa: ANN001
    sent.append((result.name, result.stay_kind, link))


import notify  # noqa: E402

notify.send_hit = fake_send_hit

CAMPGROUNDS_BY_ID = {
    "999": {
        "name": "Test Campground",
        "facility_id": 999,
        "place_id": 555,
        "park_name": "Test SP",
    }
}

state = scanner.default_state()

open_result = run(grid(unit(1, "42", slice_for(FRI, True), slice_for(SAT, True))))
shut_result = run(grid(unit(1, "42", slice_for(FRI, False), slice_for(SAT, False))))


def poll(result) -> int:
    count, errors = scanner.process_hits(
        results={"999": result},
        campgrounds_by_id=CAMPGROUNDS_BY_ID,
        state=state,
        label="Fri 9/11 - Sun 9/13",
        dry_run=False,
    )
    assert not errors, errors
    return count


check("closed -> no alert", poll(shut_result) == 0)
check("closed -> open fires one alert", poll(open_result) == 1)
check("still open fires nothing", poll(open_result) == 0)
check("still open again fires nothing", poll(open_result) == 0)
check("open -> booked fires nothing", poll(shut_result) == 0)
check("booked -> open fires a SECOND alert", poll(open_result) == 1)
check("exactly two alerts across the whole sequence", len(sent) == 2, f"got {len(sent)}")
check(
    "alert carries the modern booking link",
    sent[0][2] == "https://www.reservecalifornia.com/park/555/999",
    sent[0][2],
)

print("\nFailed HIT email is retried on the next poll")
state2 = scanner.default_state()
attempts: list = []


def failing_send_hit(result, stay_label, link, park_name):  # noqa: ANN001
    attempts.append(result.name)
    raise notify.NotificationError("simulated SMTP outage")


notify.send_hit = failing_send_hit
scanner.process_hits({"999": open_result}, CAMPGROUNDS_BY_ID, state2, "x", False)
check("state stays 'unavailable' when the email fails", not state2["campgrounds"]["999"]["available"])
notify.send_hit = fake_send_hit
count, _ = scanner.process_hits({"999": open_result}, CAMPGROUNDS_BY_ID, state2, "x", False)
check("next poll re-sends the missed alert", count == 1)

print("\nTent classification")
check("'Tent' -> tent", availability.classify_tent_friendly("Tent") is True)
check("'RV/Motorhome' -> not tent", availability.classify_tent_friendly("RV/Motorhome") is False)
check("'Lodging' -> not tent", availability.classify_tent_friendly("Lodging") is False)
check("unknown -> None", availability.classify_tent_friendly("Zorbing") is None)
check("empty -> None", availability.classify_tent_friendly(None) is None)

print("\nDate configuration")
arrival, departure, nights = scanner.target_dates()
check("nights derived from arrival/departure", nights == [FRI, SAT], str(nights))
check("arrival is a Friday", arrival.strftime("%A") == "Friday")
check("departure is a Sunday", departure.strftime("%A") == "Sunday")

print("\nSite labelling")
check("bare number gets a 'Site' prefix", availability.site_label("104") == "Site 104")
check("name already saying Site is untouched", availability.site_label("Site 104") == "Site 104")
check("lowercase 'site 7' is untouched", availability.site_label("site 7") == "site 7")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("All tests passed.")
