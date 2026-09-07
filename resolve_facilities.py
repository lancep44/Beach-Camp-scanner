#!/usr/bin/env python3
"""
One-time (build-time) resolution of campground names -> UseDirect FacilityIDs.

This is NOT part of the polling loop. It runs from the "Resolve Facility IDs"
workflow, prints a table for a human to eyeball, and writes campgrounds.json,
which the scanner then reads without ever hitting a lookup endpoint at runtime.

A wrong-but-valid FacilityID is undetectable at runtime — it would just quietly
watch the wrong campground forever. So this script refuses to guess: anything
ambiguous is reported as UNRESOLVED with its candidates listed, and the run
fails rather than committing a plausible-looking mistake.

    python resolve_facilities.py            # resolve, verify, print, write file
    python resolve_facilities.py --print-only   # resolve and print, write nothing
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional

from usedirect import (
    booking_url,
    fetch_facilities,
    fetch_grid,
    fetch_places,
    fetch_unit_categories,
)

OUTPUT_FILE = pathlib.Path(__file__).resolve().parent / "campgrounds.json"

# Manual escape hatch. If the resolver reports a target as ambiguous, read the
# candidate list it prints, pick the right FacilityId, and pin it here:
#     OVERRIDES = {"Pismo SB — North Beach Campground": 1234}
OVERRIDES: Dict[str, int] = {}

# Facility names that are never the campground we mean.
NON_CAMPING_HINTS = (
    "day use",
    "dayuse",
    "day-use",
    "parking",
    "boat launch",
    "museum",
    "tour",
    "annual pass",
    "pass sales",
    "picnic",
    "pavilion",
    "visitor center",
    "cabins only",
    "gate",
    "entrance",
    "hike in",
    "hike-in",
    "en route",
    "enroute",
    "overflow",
)

# Preferred when a park exposes several facilities.
CAMPING_HINTS = ("campground", "camp", "campsites", "sites")


class Target:
    """One campground we want to watch, described the way a human would."""

    def __init__(
        self,
        label: str,
        place_patterns: List[str],
        facility_required: Optional[List[str]] = None,
        facility_excluded: Optional[List[str]] = None,
        place_excluded: Optional[List[str]] = None,
    ) -> None:
        self.label = label
        self.place_patterns = [p.lower() for p in place_patterns]
        self.facility_required = [p.lower() for p in (facility_required or [])]
        self.facility_excluded = [p.lower() for p in (facility_excluded or [])]
        self.place_excluded = [p.lower() for p in (place_excluded or [])]


TARGETS: List[Target] = [
    Target(
        "Thornhill Broome Campground, Point Mugu SP",
        place_patterns=["point mugu", "mugu"],
        facility_required=["thornhill"],
        facility_excluded=["sycamore"],
    ),
    Target(
        "Leo Carrillo SP",
        place_patterns=["leo carrillo"],
        facility_excluded=["group", "hike"],
    ),
    Target(
        "Doheny SB",
        place_patterns=["doheny"],
        facility_excluded=["group"],
    ),
    Target(
        "San Clemente SB",
        place_patterns=["san clemente"],
        facility_excluded=["group"],
    ),
    Target(
        "San Onofre SB — Bluffs Campground",
        place_patterns=["san onofre"],
        facility_required=["bluff"],
    ),
    Target(
        "Carpinteria SB",
        place_patterns=["carpinteria"],
        facility_excluded=["group"],
    ),
    Target(
        "El Capitan SB",
        place_patterns=["el capitan"],
        place_excluded=["canyon"],
        facility_excluded=["group"],
    ),
    Target(
        "Refugio SB",
        place_patterns=["refugio"],
        facility_excluded=["group"],
    ),
    Target(
        "Pismo SB — North Beach Campground",
        place_patterns=["pismo"],
        facility_required=["north beach"],
        facility_excluded=["oceano"],
    ),
    Target(
        "Oceano Dunes SVRA",
        place_patterns=["oceano dunes"],
        facility_excluded=["group"],
    ),
    Target(
        "Morro Strand SB",
        place_patterns=["morro strand"],
        place_excluded=["morro bay"],
        facility_excluded=["group"],
    ),
]


def matches(name: str, patterns: List[str]) -> bool:
    lowered = (name or "").lower()
    return any(pattern in lowered for pattern in patterns)


def looks_like_camping(name: str) -> bool:
    lowered = (name or "").lower()
    if any(hint in lowered for hint in NON_CAMPING_HINTS):
        return False
    return True


def resolve_target(
    target: Target,
    places: List[Dict[str, Any]],
    facilities: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return {'status', 'facility'|None, 'place'|None, 'candidates', 'note'}."""
    matched_places = [
        place
        for place in places
        if matches(place.get("Name", ""), target.place_patterns)
        and not (target.place_excluded and matches(place.get("Name", ""), target.place_excluded))
    ]

    if not matched_places:
        return {
            "status": "UNRESOLVED",
            "facility": None,
            "place": None,
            "candidates": [],
            "note": f"no park matched {target.place_patterns}",
        }

    place_ids = {place["PlaceId"]: place for place in matched_places}
    park_facilities = [
        facility for facility in facilities if facility.get("PlaceId") in place_ids
    ]

    candidates = list(park_facilities)
    if target.facility_required:
        candidates = [
            facility
            for facility in candidates
            if all(token in (facility.get("Name") or "").lower() for token in target.facility_required)
        ]
    if target.facility_excluded:
        candidates = [
            facility
            for facility in candidates
            if not matches(facility.get("Name", ""), target.facility_excluded)
        ]

    note = ""
    if len(candidates) > 1:
        bookable = [
            facility for facility in candidates if facility.get("AllowWebBooking") is not False
        ]
        if bookable and len(bookable) < len(candidates):
            candidates = bookable
            note = "narrowed to web-bookable facilities"

    if len(candidates) > 1:
        campish = [facility for facility in candidates if looks_like_camping(facility.get("Name", ""))]
        if campish and len(campish) < len(candidates):
            candidates = campish
            note = "narrowed by excluding day-use/non-camping facilities"

    if len(candidates) > 1:
        explicit = [
            facility
            for facility in candidates
            if matches(facility.get("Name", ""), list(CAMPING_HINTS))
        ]
        if len(explicit) == 1:
            candidates = explicit
            note = "narrowed to the one facility named as a campground"

    if not candidates:
        return {
            "status": "UNRESOLVED",
            "facility": None,
            "place": matched_places[0],
            "candidates": park_facilities,
            "note": "filters eliminated every facility in this park",
        }

    if len(candidates) > 1:
        return {
            "status": "AMBIGUOUS",
            "facility": None,
            "place": matched_places[0],
            "candidates": candidates,
            "note": f"{len(candidates)} facilities still match; pin one via OVERRIDES",
        }

    facility = candidates[0]
    return {
        "status": "OK",
        "facility": facility,
        "place": place_ids.get(facility.get("PlaceId")) or matched_places[0],
        "candidates": park_facilities,
        "note": note,
    }


def verify_live(facility_id: int) -> Dict[str, Any]:
    """Hit the real availability grid so a resolved ID is proven to work.

    Confirms the ID addresses a real, unit-bearing campground and reports the
    name the API itself uses, which is the strongest signal available that the
    ID matches the campground you meant.
    """
    start = datetime.date.today() + datetime.timedelta(days=30)
    grid = fetch_grid(facility_id, start, start + datetime.timedelta(days=2))
    facility = grid.get("Facility") or {}
    units = facility.get("Units") or {}
    sample = [
        str(unit.get("Name"))
        for unit in list(units.values())[:4]
        if isinstance(unit, dict)
    ]
    return {
        "api_name": facility.get("Name"),
        "unit_count": len(units),
        "sample_sites": sample,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Resolve and print the table without writing campgrounds.json.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip the live grid check for each resolved facility.",
    )
    args = parser.parse_args()

    print("Fetching UseDirect metadata (places, facilities, unit categories)...")
    places = fetch_places()
    facilities = fetch_facilities()
    unit_categories = fetch_unit_categories()
    print(
        f"  {len(places)} places, {len(facilities)} facilities, "
        f"{len(unit_categories)} unit categories\n"
    )

    resolved: List[Dict[str, Any]] = []
    failures: List[str] = []

    for target in TARGETS:
        if target.label in OVERRIDES:
            forced_id = OVERRIDES[target.label]
            facility = next(
                (f for f in facilities if f.get("FacilityId") == forced_id), None
            )
            if facility is None:
                failures.append(f"{target.label}: OVERRIDE {forced_id} is not a known FacilityId")
                continue
            place = next(
                (p for p in places if p.get("PlaceId") == facility.get("PlaceId")), {}
            )
            outcome = {
                "status": "OK",
                "facility": facility,
                "place": place,
                "candidates": [],
                "note": "pinned via OVERRIDES",
            }
        else:
            outcome = resolve_target(target, places, facilities)

        if outcome["status"] != "OK":
            failures.append(f"{target.label}: {outcome['status']} — {outcome['note']}")
            print(f"[{outcome['status']}] {target.label}: {outcome['note']}")
            for candidate in outcome["candidates"][:25]:
                print(
                    f"      FacilityId={candidate.get('FacilityId')!s:<8} "
                    f"PlaceId={candidate.get('PlaceId')!s:<8} "
                    f"AllowWebBooking={candidate.get('AllowWebBooking')!s:<6} "
                    f"{candidate.get('Name')}"
                )
            print()
            continue

        facility = outcome["facility"]
        place = outcome["place"] or {}
        entry = {
            "name": target.label,
            "facility_id": int(facility["FacilityId"]),
            "place_id": int(place.get("PlaceId") or facility.get("PlaceId")),
            "park_name": place.get("Name") or "",
            "api_facility_name": facility.get("Name") or "",
            "booking_url": booking_url(
                int(place.get("PlaceId") or facility.get("PlaceId")),
                int(facility["FacilityId"]),
            ),
        }

        if not args.skip_verify:
            time.sleep(1.5)
            try:
                check = verify_live(entry["facility_id"])
                entry["verified_api_name"] = check["api_name"]
                entry["verified_unit_count"] = check["unit_count"]
                entry["verified_sample_sites"] = check["sample_sites"]
            except Exception as exc:  # noqa: BLE001 - report, do not abort the table
                entry["verify_error"] = f"{type(exc).__name__}: {exc}"
                failures.append(f"{target.label}: live verification failed — {exc}")

        if outcome["note"]:
            entry["resolution_note"] = outcome["note"]
        resolved.append(entry)

    print_table(resolved)

    if failures:
        print("\nUNRESOLVED / PROBLEM TARGETS:")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nRefusing to write campgrounds.json. Pin the correct FacilityId in the "
            "OVERRIDES dict at the top of resolve_facilities.py and re-run."
        )
        return 1

    if args.print_only:
        print("\n--print-only: campgrounds.json not written.")
        return 0

    payload = {
        "resolved": True,
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "source": "https://calirdr.usedirect.com/rdr/rdr/fd/{places,facilities}",
        "unit_categories": unit_categories,
        "campgrounds": resolved,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nWrote {OUTPUT_FILE.name} with {len(resolved)} campgrounds.")
    return 0


def print_table(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 118)
    print("RESOLVED CAMPGROUNDS — review every row before this goes live")
    print("=" * 118)
    header = f"{'CAMPGROUND':<44}{'FACILITY':<10}{'PARK (PlaceId)':<34}{'API NAME':<30}"
    print(header)
    print("-" * 118)
    for row in rows:
        park = f"{row['park_name']} ({row['place_id']})"
        print(
            f"{row['name'][:43]:<44}"
            f"{row['facility_id']!s:<10}"
            f"{park[:33]:<34}"
            f"{(row.get('verified_api_name') or row.get('api_facility_name') or '')[:29]:<30}"
        )
        print(f"    {row['booking_url']}")
        if "verified_unit_count" in row:
            sample = ", ".join(row.get("verified_sample_sites") or [])
            print(f"    live check: {row['verified_unit_count']} units   sample sites: {sample}")
        if row.get("verify_error"):
            print(f"    LIVE CHECK FAILED: {row['verify_error']}")
        if row.get("resolution_note"):
            print(f"    note: {row['resolution_note']}")
    print("=" * 118)


if __name__ == "__main__":
    sys.exit(main())
