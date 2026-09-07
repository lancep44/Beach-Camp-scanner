"""
Turn a UseDirect availability grid into a campground-level verdict.

The rule we care about: a campground is a HIT if the Friday night AND the
Saturday night are each bookable there, even if that means two different site
numbers. Single-night availability is never a hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from usedirect import SchemaError

# Unit-category names that mean "you cannot pitch a tent on this".
NON_TENT_HINTS = (
    "rv",
    "motorhome",
    "trailer",
    "cabin",
    "yurt",
    "lodging",
    "boat",
    "day use",
    "dayuse",
    "picnic",
    "shelter",
    "group",
)

TENT_HINTS = ("tent", "camp", "standard", "primitive", "hike", "bike", "walk")


def classify_tent_friendly(category_name: Optional[str]) -> Optional[bool]:
    """True / False / None when the category is unknown or ambiguous.

    Used only to *label* a hit, never to suppress one — mislabelling costs a
    confusing line in an email, whereas over-filtering costs a missed campsite.
    """
    if not category_name:
        return None
    name = category_name.strip().lower()
    if not name:
        return None
    if any(hint in name for hint in NON_TENT_HINTS):
        return False
    if any(hint in name for hint in TENT_HINTS):
        return True
    return None


@dataclass
class SiteNight:
    """One site holding one night of the stay."""

    unit_id: Optional[int]
    name: str
    nights: List[str]
    category: Optional[str]
    tent_friendly: Optional[bool]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "name": self.name,
            "nights": list(self.nights),
            "category": self.category,
            "tent_friendly": self.tent_friendly,
        }


@dataclass
class CampgroundResult:
    facility_id: int
    name: str
    available: bool = False
    stay_kind: Optional[str] = None  # "single" | "split"
    sites: List[SiteNight] = field(default_factory=list)
    units_seen: int = 0
    nights_covered: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def has_non_tent_site(self) -> bool:
        return any(site.tent_friendly is False for site in self.sites)

    @property
    def site_summary(self) -> str:
        if not self.sites:
            return "—"
        return ", ".join(
            f"{site_label(site.name)} ({'+'.join(_pretty(n) for n in site.nights)})"
            for site in self.sites
        )


def site_label(name: str) -> str:
    """'104' -> 'Site 104', but 'Site 104' is left alone.

    UseDirect unit names are inconsistent across parks: some are bare numbers,
    some already carry the word "Site".
    """
    text = str(name).strip()
    return text if text.lower().startswith("site") else f"Site {text}"


def _pretty(iso_date: str) -> str:
    """'2026-09-11' -> 'Fri 9/11'."""
    import datetime

    try:
        day = datetime.date.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    return f"{day.strftime('%a')} {day.month}/{day.day}"


def _slice_is_open(slice_obj: Dict[str, Any]) -> bool:
    """Bookable-tonight test for a single slice.

    IsFree alone is what camply keys on; we additionally reject slices the API
    has explicitly blocked or reserved for walk-ins, because those are not
    bookable online and would produce a dead-end alert.
    """
    if slice_obj.get("IsFree") is not True:
        return False
    if slice_obj.get("IsBlocked") is True:
        return False
    if slice_obj.get("IsWalkin") is True:
        return False
    return True


def _min_stay_ok(slice_obj: Dict[str, Any], nights: int) -> bool:
    """A slice advertising MinStay=3 cannot be booked as a 2-night arrival."""
    min_stay = slice_obj.get("MinStay")
    if isinstance(min_stay, bool) or not isinstance(min_stay, int):
        return True
    return min_stay <= nights


def _unit_is_web_bookable(unit: Dict[str, Any]) -> bool:
    if unit.get("AllowWebBooking") is False:
        return False
    if unit.get("IsWebViewable") is False:
        return False
    return True


def _slices_by_date(unit: Dict[str, Any], facility_id: int) -> Dict[str, Dict[str, Any]]:
    slices = unit.get("Slices")
    if slices is None:
        return {}
    if not isinstance(slices, dict):
        raise SchemaError(
            f"Facility {facility_id}: unit {unit.get('UnitId')!r} has "
            f"Slices of type {type(slices).__name__}, expected object"
        )

    by_date: Dict[str, Dict[str, Any]] = {}
    for key, slice_obj in slices.items():
        if not isinstance(slice_obj, dict):
            raise SchemaError(
                f"Facility {facility_id}: unit {unit.get('UnitId')!r} slice "
                f"{key!r} is {type(slice_obj).__name__}, expected object"
            )
        raw_date = slice_obj.get("Date") or key
        # Both keys and Date values look like '2026-09-11T00:00:00'.
        by_date[str(raw_date)[:10]] = slice_obj
    return by_date


def evaluate_campground(
    grid: Dict[str, Any],
    facility_id: int,
    name: str,
    nights: List[str],
    unit_categories: Dict[str, str],
) -> CampgroundResult:
    """Reduce a grid response to 'is this campground a 2-night hit?'."""
    if len(nights) != 2:
        raise ValueError("evaluate_campground expects exactly two nights")
    first_night, second_night = nights

    result = CampgroundResult(facility_id=facility_id, name=name)

    facility = grid.get("Facility")
    if not isinstance(facility, dict):
        raise SchemaError(
            f"Facility {facility_id}: 'Facility' is {type(facility).__name__}, expected object"
        )

    units = facility.get("Units")
    if units is None:
        # Legitimate for a campground that is closed / out of season. Recorded
        # as a warning; scanner.py escalates if *every* campground looks
        # like this, which would mean the schema moved under us.
        result.warnings.append("API returned no units (closed, out of season, or filtered out)")
        return result
    if not isinstance(units, dict):
        raise SchemaError(
            f"Facility {facility_id}: 'Units' is {type(units).__name__}, expected object"
        )

    result.units_seen = len(units)

    both_nights: List[SiteNight] = []
    first_only: List[SiteNight] = []
    second_only: List[SiteNight] = []
    nights_with_slices = set()

    for unit in units.values():
        if not isinstance(unit, dict):
            raise SchemaError(
                f"Facility {facility_id}: unit entry is {type(unit).__name__}, expected object"
            )

        by_date = _slices_by_date(unit, facility_id)
        nights_with_slices.update(by_date.keys() & set(nights))

        if not _unit_is_web_bookable(unit):
            continue

        category = unit_categories.get(str(unit.get("UnitCategoryId")))
        site_name = str(unit.get("Name") or unit.get("ShortName") or unit.get("UnitId") or "?")

        def make(nights_held: List[str]) -> SiteNight:
            return SiteNight(
                unit_id=unit.get("UnitId"),
                name=site_name,
                nights=nights_held,
                category=category,
                tent_friendly=classify_tent_friendly(category),
            )

        first_slice = by_date.get(first_night)
        second_slice = by_date.get(second_night)
        first_open = bool(first_slice) and _slice_is_open(first_slice)
        second_open = bool(second_slice) and _slice_is_open(second_slice)

        # One site for both nights = a single 2-night reservation.
        if first_open and second_open and _min_stay_ok(first_slice, 2):
            both_nights.append(make([first_night, second_night]))
            continue
        # Otherwise each night would be booked on its own, as 1-night stays.
        if first_open and _min_stay_ok(first_slice, 1):
            first_only.append(make([first_night]))
        if second_open and _min_stay_ok(second_slice, 1):
            second_only.append(make([second_night]))

    result.nights_covered = sorted(nights_with_slices)
    if not nights_with_slices:
        result.warnings.append(
            f"No slices returned for either target night ({first_night}, {second_night}); "
            f"{len(units)} units present"
        )

    if both_nights:
        result.available = True
        result.stay_kind = "single"
        result.sites = both_nights[:3]
        return result

    if first_only and second_only:
        first_pick = first_only[0]
        second_pick = next(
            (site for site in second_only if site.unit_id != first_pick.unit_id),
            None,
        )
        if second_pick is not None:
            result.available = True
            result.stay_kind = "split"
            result.sites = [first_pick, second_pick]
            return result

    return result
