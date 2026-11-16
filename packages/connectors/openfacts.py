"""Open Food / Beauty / Products Facts.

One connector for all three: same API shape, different host and different
product class. Which is useful beyond saving code — the host that answered
tells us whether we are looking at a food, a cosmetic or a general product,
and arch.md 8.5 grades the same chemical differently in each.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from packages.guards.fetch_broker import FetchError, get_broker

logger = logging.getLogger(__name__)

# Ordered: food is the most common scan, then cosmetics, then everything else.
SOURCES: list[tuple[str, str, str]] = [
    ("off", "world.openfoodfacts.org", "food"),
    ("obf", "world.openbeautyfacts.org", "cosmetic"),
    ("opf", "world.openproductsfacts.org", "general"),
]

_FIELDS = ",".join(
    [
        "code",
        "product_name",
        "brands",
        "categories",
        "ingredients_text",
        "ingredients_text_en",
        "quantity",
        "image_url",
        "allergens_tags",
        "traces_tags",
        "nutriscore_grade",
    ]
)


# Fields a name search needs. `ingredients_text` is only used to tell the app
# whether picking the result can be analysed straight away.
_SEARCH_FIELDS = ",".join(
    [
        "code",
        "product_name",
        "brands",
        "categories",
        "quantity",
        "image_front_small_url",
        "image_url",
        "ingredients_text",
    ]
)

# Open Food Facts' current text search. It does not return ingredient text, so
# a food result cannot say whether it can be analysed without a second lookup.
_FOOD_SEARCH_URL = "https://search.openfoodfacts.org/search"
_FOOD_SEARCH_FIELDS = ",".join(
    ["code", "product_name", "brands", "categories", "quantity", "image_front_small_url", "image_url"]
)

# Search is type-ahead traffic and Open Food Facts asks clients to keep search
# volume low, so identical searches are answered from memory for a while. Per
# process (Passenger runs several), bounded, and only successful answers.
_SEARCH_TTL_SECONDS = 600
_SEARCH_CACHE_MAX = 256
_search_cache: dict[tuple, tuple[float, list]] = {}


@dataclass
class ProductRecord:
    barcode: str
    found: bool = False
    name: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    quantity: Optional[str] = None
    image_url: Optional[str] = None
    product_class: Optional[str] = None
    ingredients_text: Optional[str] = None
    allergens: list[str] = field(default_factory=list)
    source: Optional[str] = None
    confidence: float = 0.0
    fetched_at: Optional[datetime] = None
    error: Optional[str] = None
    # False for search results from a source that does not return ingredient
    # text: `has_ingredients` is then "unknown", not "none".
    ingredients_known: bool = True

    @property
    def has_ingredients(self) -> bool:
        return bool((self.ingredients_text or "").strip())


def _first(value: Any) -> Optional[str]:
    """First entry of a comma-separated string or a list; None if empty."""
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").split(",")[0].strip() or None


def _search_record(
    item: Any, key: str, product_class: str, ingredients_known: bool
) -> Optional[ProductRecord]:
    """One search hit as a record, or None if it cannot be shown or looked up."""
    if not isinstance(item, dict):
        return None
    code = str(item.get("code") or "").strip()
    name = str(item.get("product_name") or "").strip()
    # No name, or a code that is not a barcode: not a usable result.
    if not code.isdigit() or not name:
        return None
    return ProductRecord(
        barcode=code,
        found=True,
        name=name,
        brand=_first(item.get("brands")),
        category=_first(item.get("categories")),
        quantity=str(item.get("quantity") or "").strip() or None,
        image_url=str(item.get("image_front_small_url") or item.get("image_url") or "").strip() or None,
        product_class=product_class,
        ingredients_text=str(item.get("ingredients_text") or "").strip() or None,
        source=key,
        confidence=0.6,
        fetched_at=datetime.now(timezone.utc),
        ingredients_known=ingredients_known,
    )


class OpenFactsConnector:
    def __init__(self, timeout: int = 10, search_timeout: int = 6) -> None:
        self.timeout = timeout
        self.search_timeout = search_timeout

    def search(
        self, query: str, *, limit: int = 10, sources: Optional[list[str]] = None
    ) -> tuple[list[ProductRecord], bool]:
        """Search by product name. Returns (records, answered).

        `answered` is False when no source could be reached — the caller must
        not present that as "no such product". Food and cosmetics are searched
        by default; the general-products database is far noisier and is only
        used when asked for.
        """
        wanted = set(sources) if sources else {"off", "obf"}
        cache_key = (query.lower(), limit, tuple(sorted(wanted)))
        cached = _search_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _SEARCH_TTL_SECONDS:
            return list(cached[1]), True

        records: list[ProductRecord] = []
        answered = False
        for key, host, product_class in SOURCES:
            if key not in wanted:
                continue
            batch = self._search_one(query, limit, key, host, product_class)
            if batch is None:
                continue
            answered = True
            records.extend(batch)
            if len(records) >= limit:
                break

        records = records[:limit]
        if answered:
            if len(_search_cache) >= _SEARCH_CACHE_MAX:
                _search_cache.pop(next(iter(_search_cache)))
            _search_cache[cache_key] = (time.monotonic(), list(records))
        return records, answered

    def _search_one(
        self, query: str, limit: int, key: str, host: str, product_class: str
    ) -> Optional[list[ProductRecord]]:
        """One source's matches, or None if it could not be reached/parsed.

        The two databases need different requests. Open Food Facts retired its
        full-text `cgi/search.pl` (it now answers 503) in favour of the
        Search-a-licious service; Open Beauty Facts still serves `cgi/search.pl`.
        """
        if key == "off":
            url = _FOOD_SEARCH_URL
            params: dict[str, Any] = {
                "q": query,
                "page_size": limit,
                "fields": _FOOD_SEARCH_FIELDS,
            }
            list_key = "hits"
        else:
            url = f"https://{host}/cgi/search.pl"
            params = {
                "search_terms": query,
                "search_simple": 1,
                "action": "process",
                "json": 1,
                "page_size": limit,
                "fields": _SEARCH_FIELDS,
                "sort_by": "unique_scans_n",
            }
            list_key = "products"

        try:
            response = get_broker().get(url, params=params, timeout=self.search_timeout)
        except FetchError as exc:
            logger.info("%s search failed: %s", key, exc)
            return None
        if response.status_code != 200:
            logger.info("%s search HTTP %s", key, response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        items = payload.get(list_key) if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return None

        records = (_search_record(item, key, product_class, key != "off") for item in items)
        return [record for record in records if record is not None]

    def lookup(self, barcode: str, *, sources: Optional[list[str]] = None) -> ProductRecord:
        """Try each source in turn. First hit with ingredients wins.

        A hit *without* ingredients is remembered but does not stop the walk:
        the product name alone cannot drive an ingredient analysis, and a later
        source may carry the panel.
        """
        wanted = set(sources) if sources else None
        best_without_ingredients: Optional[ProductRecord] = None

        for key, host, product_class in SOURCES:
            if wanted and key not in wanted:
                continue

            record = self._fetch_one(barcode, key, host, product_class)
            if not record.found:
                continue
            if record.has_ingredients:
                return record
            if best_without_ingredients is None:
                best_without_ingredients = record

        if best_without_ingredients is not None:
            return best_without_ingredients

        return ProductRecord(barcode=barcode, found=False)

    def _fetch_one(
        self, barcode: str, key: str, host: str, product_class: str
    ) -> ProductRecord:
        url = f"https://{host}/api/v2/product/{barcode}"
        try:
            response = get_broker().get(
                url, params={"fields": _FIELDS}, timeout=self.timeout
            )
        except FetchError as exc:
            logger.info("%s lookup failed for %s: %s", key, barcode, exc)
            return ProductRecord(barcode=barcode, found=False, source=key, error=str(exc))

        if response.status_code == 404:
            return ProductRecord(barcode=barcode, found=False, source=key)
        if response.status_code != 200:
            return ProductRecord(
                barcode=barcode,
                found=False,
                source=key,
                error=f"HTTP {response.status_code}",
            )

        try:
            payload = response.json()
        except ValueError:
            return ProductRecord(barcode=barcode, found=False, source=key, error="bad JSON")

        if not isinstance(payload, dict) or payload.get("status") != 1:
            return ProductRecord(barcode=barcode, found=False, source=key)

        product = payload.get("product") or {}
        if not isinstance(product, dict):
            return ProductRecord(barcode=barcode, found=False, source=key)

        ingredients = (
            product.get("ingredients_text_en") or product.get("ingredients_text") or ""
        ).strip()

        name = (product.get("product_name") or "").strip() or None

        # A record with neither a name nor ingredients is a stub someone created
        # by scanning; treating it as "found" would suppress the OCR fallback.
        if not name and not ingredients:
            return ProductRecord(barcode=barcode, found=False, source=key)

        allergens = [
            tag.split(":", 1)[-1]
            for tag in (product.get("allergens_tags") or [])
            if isinstance(tag, str)
        ]

        confidence = 0.6
        if ingredients:
            confidence += 0.3
        if name:
            confidence += 0.1

        return ProductRecord(
            barcode=barcode,
            found=True,
            name=name,
            brand=(product.get("brands") or "").split(",")[0].strip() or None,
            category=(product.get("categories") or "").split(",")[0].strip() or None,
            quantity=(product.get("quantity") or "").strip() or None,
            image_url=(product.get("image_url") or "").strip() or None,
            product_class=product_class,
            ingredients_text=ingredients or None,
            allergens=allergens,
            source=key,
            confidence=round(min(confidence, 1.0), 2),
            fetched_at=datetime.now(timezone.utc),
        )
