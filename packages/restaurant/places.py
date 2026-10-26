"""Place search and resolution (arch.md 11.1 step 1).

Backed by Google Places API (New). Nothing resolves without a `place_id`, and a
`place_id` can only come from here, so this is the front door of the whole
restaurant pipeline: the app calls `places.search` as the user types, the user
picks a row, and the chosen `place_id` goes into the `scan` request.

Without GOOGLE_PLACES_API_KEY the provider is simply unavailable. That is
reported as such — an empty result list from a missing key would read to the
user as "no such restaurant".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from packages.config import get_settings
from packages.guards.fetch_broker import FetchError, get_broker

logger = logging.getLogger(__name__)

_BASE = "https://places.googleapis.com/v1"
_PRIMARY_TYPES = ["restaurant", "cafe", "bakery", "meal_takeaway", "bar"]


class PlacesUnavailable(Exception):
    """No provider configured, or the provider call failed."""


@dataclass
class PlaceSuggestion:
    place_id: str
    name: str
    address: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"place_id": self.place_id, "name": self.name, "address": self.address}


def available() -> bool:
    return bool(get_settings().places.api_key)


def _headers(field_mask: Optional[str] = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": get_settings().places.api_key,
    }
    if field_mask:
        headers["X-Goog-FieldMask"] = field_mask
    return headers


def search(
    query: str,
    *,
    session_token: Optional[str] = None,
    near: Optional[tuple[float, float]] = None,
    limit: int = 5,
) -> list[PlaceSuggestion]:
    """Autocomplete restaurants/cafes matching `query`.

    `near` is an optional (lat, lng) used as a location bias only; it never
    filters results out. `session_token` groups a type-ahead session for
    billing — pass the same value for every keystroke of one search.
    """
    query = (query or "").strip()
    if not query:
        return []
    if not available():
        raise PlacesUnavailable("place search is not configured (GOOGLE_PLACES_API_KEY)")

    body: dict[str, Any] = {
        "input": query[:200],
        "includedPrimaryTypes": _PRIMARY_TYPES,
    }
    if session_token:
        body["sessionToken"] = session_token
    if near:
        body["locationBias"] = {
            "circle": {"center": {"latitude": near[0], "longitude": near[1]}, "radius": 20000.0}
        }

    try:
        response = get_broker().post(
            f"{_BASE}/places:autocomplete",
            headers=_headers(),
            content=json.dumps(body).encode("utf-8"),
            timeout=get_settings().places.timeout_seconds,
        )
    except FetchError as exc:
        raise PlacesUnavailable(str(exc)) from exc
    if response.status_code != 200:
        logger.warning("places autocomplete HTTP %s", response.status_code)
        raise PlacesUnavailable(f"places provider returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PlacesUnavailable("places provider returned invalid JSON") from exc

    results: list[PlaceSuggestion] = []
    for suggestion in (payload or {}).get("suggestions") or []:
        prediction = (suggestion or {}).get("placePrediction") or {}
        place_id = prediction.get("placeId")
        if not place_id:
            continue
        structured = prediction.get("structuredFormat") or {}
        name = (structured.get("mainText") or {}).get("text") or (
            (prediction.get("text") or {}).get("text")
        )
        address = (structured.get("secondaryText") or {}).get("text")
        if not name:
            continue
        results.append(PlaceSuggestion(place_id=str(place_id), name=str(name), address=address))
        if len(results) >= limit:
            break
    return results


def details(place_id: str) -> Optional[PlaceSuggestion]:
    """Name and address for a chosen place, or None if it cannot be fetched."""
    if not available() or not place_id:
        return None
    try:
        response = get_broker().get(
            f"{_BASE}/places/{place_id}",
            headers=_headers("id,displayName,formattedAddress"),
            timeout=get_settings().places.timeout_seconds,
        )
        if response.status_code != 200:
            return None
        data = response.json() or {}
    except (FetchError, ValueError):
        return None

    name = (data.get("displayName") or {}).get("text")
    if not name:
        return None
    return PlaceSuggestion(
        place_id=str(data.get("id") or place_id),
        name=str(name),
        address=data.get("formattedAddress"),
    )
