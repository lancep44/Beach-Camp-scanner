"""
Thin, dependency-free client for the UseDirect RDR API that powers
ReserveCalifornia.

Read-only, unauthenticated. Endpoint names, request keys and response shapes in
this module were taken from the `camply` project's working UseDirect provider
(github.com/juftin/camply, camply/providers/usedirect/usedirect.py and
camply/containers/usedirect.py), not from guesswork.

Nothing here is imported at runtime that requires a third-party package: the
scanner must keep working untouched for months, so the dependency surface is
the Python standard library and nothing else.
"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.request
import zlib
from typing import Any, Dict, List, Optional

BASE_URL = "https://calirdr.usedirect.com/rdr"

# /rdr/rdr/... — the doubled segment is correct, it is how UseDirect routes.
GRID_ENDPOINT = f"{BASE_URL}/rdr/search/grid"
PLACES_ENDPOINT = f"{BASE_URL}/rdr/fd/places"
FACILITIES_ENDPOINT = f"{BASE_URL}/rdr/fd/facilities"
FILTERS_ENDPOINT = f"{BASE_URL}/rdr/search/filters"

# The public booking site, used only to build human-clickable links.
BOOKING_SITE = "https://www.reservecalifornia.com"

# API wants MM-DD-YYYY, not ISO.
API_DATE_FORMAT = "%m-%d-%Y"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Origin": BOOKING_SITE,
    "Referer": BOOKING_SITE + "/",
    "Connection": "close",
}

REQUEST_TIMEOUT_SECONDS = 45
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 2.0


class UseDirectError(Exception):
    """Network or HTTP-level failure talking to UseDirect."""


class SchemaError(Exception):
    """The API answered, but not in a shape we recognise.

    This is deliberately a hard failure. A silent zero-results run is
    indistinguishable from 'no cancellations', which is the one failure mode
    this whole system exists to avoid.
    """


def booking_url(place_id: int, facility_id: int) -> str:
    """Modern ReserveCalifornia deep link for a campground."""
    return f"{BOOKING_SITE}/park/{place_id}/{facility_id}"


def _decode(response: Any, raw: bytes) -> str:
    encoding = (response.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


def _request_once(url: str, payload: Optional[Dict[str, Any]]) -> Any:
    data = None
    headers = dict(DEFAULT_HEADERS)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = _decode(response, response.read())

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"{url} returned non-JSON ({len(body)} bytes). "
            f"First 500 chars: {body[:500]!r}"
        ) from exc


def request_json(url: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    """POST (or GET) with exponential backoff on transient failures.

    A SchemaError is never retried — the response arrived intact and was
    simply not what we expect, so trying again just wastes the remote's time.
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _request_once(url, payload)
        except SchemaError:
            raise
        except urllib.error.HTTPError as exc:
            # 4xx other than 429 will not fix themselves; fail fast.
            if exc.code < 500 and exc.code != 429:
                detail = ""
                try:
                    detail = exc.read()[:500].decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001 - best effort diagnostics only
                    pass
                raise UseDirectError(
                    f"HTTP {exc.code} from {url}: {exc.reason}. Body: {detail!r}"
                ) from exc
            last_error = UseDirectError(f"HTTP {exc.code} from {url}: {exc.reason}")
        except Exception as exc:  # noqa: BLE001 - urllib raises a wide family
            last_error = UseDirectError(f"{type(exc).__name__} from {url}: {exc}")

        if attempt < MAX_ATTEMPTS:
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            time.sleep(delay + random.uniform(0, 0.75))

    raise last_error if last_error else UseDirectError(f"{url} failed")


def fetch_grid(facility_id: int, start_date, end_date) -> Dict[str, Any]:
    """Availability grid for one campground across a date range.

    `end_date` is the departure date; UseDirect returns one slice per night.
    """
    payload = {
        "FacilityId": int(facility_id),
        "StartDate": start_date.strftime(API_DATE_FORMAT),
        "EndDate": end_date.strftime(API_DATE_FORMAT),
        "UnitSort": "orderby",
        "WebOnly": True,
        "InSeasonOnly": True,
        "IsADA": False,
        "UnitCategoryId": None,
        "UnitTypesGroupIds": [],
        "SleepingUnitId": None,
        "MinVehicleLength": 0,
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != []}

    body = request_json(GRID_ENDPOINT, payload)
    if not isinstance(body, dict):
        raise SchemaError(
            f"Facility {facility_id}: grid returned {type(body).__name__}, expected object"
        )
    if "Message" not in body:
        raise SchemaError(
            f"Facility {facility_id}: grid response missing 'Message'. "
            f"Keys present: {sorted(body)[:25]}"
        )
    if "Facility" not in body:
        raise SchemaError(
            f"Facility {facility_id}: grid response missing 'Facility'. "
            f"Message={body.get('Message')!r} Keys={sorted(body)[:25]}"
        )
    return body


def fetch_places() -> List[Dict[str, Any]]:
    body = request_json(PLACES_ENDPOINT)
    if not isinstance(body, list):
        raise SchemaError(f"{PLACES_ENDPOINT} returned {type(body).__name__}, expected list")
    return body


def fetch_facilities() -> List[Dict[str, Any]]:
    body = request_json(FACILITIES_ENDPOINT)
    if not isinstance(body, list):
        raise SchemaError(
            f"{FACILITIES_ENDPOINT} returned {type(body).__name__}, expected list"
        )
    return body


def fetch_unit_categories() -> Dict[str, str]:
    """UnitCategoryId -> human name (e.g. '1' -> 'Tent')."""
    body = request_json(FILTERS_ENDPOINT)
    if not isinstance(body, dict):
        raise SchemaError(f"{FILTERS_ENDPOINT} returned {type(body).__name__}, expected object")
    categories = body.get("UnitCategories") or []
    return {
        str(item["UnitCategoryId"]): str(item.get("UnitCategoryName") or "")
        for item in categories
        if isinstance(item, dict) and "UnitCategoryId" in item
    }
