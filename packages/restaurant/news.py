"""News and controversy stage of the restaurant investigation (arch.md 11.1 step 4).

Reads Google News' public RSS search feed (no API key). It looks for headlines
that name the restaurant *and* carry an incident keyword (food poisoning,
licence suspended, raid, hygiene lapse, lawsuit...). Anything else is dropped:
a headline that merely mentions the brand is not a finding.

What comes back is a headline, outlet, date and link, stated as a report
("News report: ..."), never as a fact about today's conditions. Nothing here
is verified by us.

The feed rate-limits (HTTP 429). A feed that could not be read is reported as
*unavailable*, never as "no news" - those mean different things to someone
deciding where to eat.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote_plus

from packages.guards.fetch_broker import FetchError, get_broker

logger = logging.getLogger(__name__)

_FEED_URL = "https://news.google.com/rss/search"
_MAX_AGE_DAYS = 730  # two years: older stories say little about a place today
_MAX_FINDINGS = 8

_OK_TTL = 30 * 60  # successful lookups are reused for 30 minutes
_FAIL_TTL = 60  # a refused/failed lookup is not retried for a minute
_CACHE_MAX = 200

# (pattern, kind, severity). First match decides the kind; any "high" match
# makes the finding high severity.
_SIGNALS: list[tuple[str, str, str]] = [
    (r"food poisoning|poisoned|poisoning|outbreak|hospitali[sz]ed|\bdied\b|\bdeath\b", "news", "high"),
    (r"licen[cs]e (?:suspended|cancell?ed|revoked)|suspended licen[cs]e|licen[cs]es? .{0,40}suspended|sealed|shut down|shuts down|closed down|ordered to close|\bclosure\b", "closure", "high"),
    (r"cockroach|\brats?\b|rodent|insect|\bworms?\b|maggot|expired|adulterat|contaminat|stale|rotten|fungus|mould|mold", "news", "high"),
    (r"\brecall(?:s|ed)?\b", "recall", "moderate"),
    (r"hygiene|unhygienic|unsanitary|\bfssai\b|\bfda\b|food safety|\braid(?:s|ed)?\b|\bprobe\b|\binspection\b", "news", "moderate"),
    (r"\bfine[ds]?\b|penalt(?:y|ies)|show[- ]cause|\bnotice\b|lawsuit|\bsued\b|\bcourt\b|controvers|boycott|backlash|\brow\b|outrage|scandal|complaint", "news", "moderate"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), kind, sev) for p, kind, sev in _SIGNALS]

_cache: dict[str, tuple[float, Optional[list["NewsItem"]]]] = {}
_cache_lock = threading.Lock()


class NewsItem:
    __slots__ = ("title", "outlet", "url", "published", "kind", "severity")

    def __init__(self, title: str, outlet: Optional[str], url: Optional[str],
                 published: Optional[date], kind: str, severity: str) -> None:
        self.title = title
        self.outlet = outlet
        self.url = url
        self.published = published
        self.kind = kind
        self.severity = severity


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def name_variants(name: str) -> list[str]:
    """Normalised spellings of the restaurant a headline must contain.

    "Mc Donald's" -> "mcdonalds"; "Pizza Hut, Mumbai" -> "pizzahutmumbai",
    "pizzahut" (a trailing city is dropped). A lone short word such as "pizza"
    is too generic to match on.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9']+", name or "") if w]
    variants: list[str] = []
    for count in range(len(words), 0, -1):
        joined = _norm("".join(words[:count]))
        if not joined:
            continue
        if count == len(words) or count >= 2 or len(joined) >= 6:
            if joined not in variants:
                variants.append(joined)
    return variants


def classify(title: str) -> Optional[tuple[str, str]]:
    """(kind, severity) if the headline reports an incident, else None."""
    kind: Optional[str] = None
    severity = "moderate"
    for pattern, signal_kind, signal_severity in _COMPILED:
        if pattern.search(title):
            kind = kind or signal_kind
            if signal_severity == "high":
                severity = "high"
    return (kind, severity) if kind else None


def _query(name: str) -> str:
    terms = (
        '"food poisoning" OR hygiene OR FSSAI OR licence OR sealed OR raid OR '
        "controversy OR lawsuit OR recall OR cockroach OR fine"
    )
    return f'"{name.strip()}" ({terms})'


def _parse(body: bytes) -> Optional[list[ET.Element]]:
    # Entity declarations are how XML bombs are built, and a news feed has no
    # reason to contain any.
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    return list(root.iter("item"))


def _item(element: ET.Element) -> Optional[NewsItem]:
    title = (element.findtext("title") or "").strip()
    if not title:
        return None
    outlet = (element.findtext("source") or "").strip() or None
    if outlet and title.endswith(f" - {outlet}"):
        title = title[: -len(outlet) - 3].rstrip()
    published: Optional[date] = None
    raw = element.findtext("pubDate")
    if raw:
        try:
            published = parsedate_to_datetime(raw).astimezone(timezone.utc).date()
        except (TypeError, ValueError):
            published = None
    link = (element.findtext("link") or "").strip() or None
    return NewsItem(title, outlet, link, published, "news", "moderate")


def _fetch(name: str) -> Optional[list[NewsItem]]:
    """All usable headlines for `name`, or None if the feed could not be read."""
    params = {"q": _query(name), "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    try:
        response = get_broker().get(
            _FEED_URL,
            params=params,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MoveneticsBot/1.0)"},
            timeout=15,
        )
    except FetchError as exc:
        logger.info("news feed unavailable: %s", exc)
        return None
    if response.status_code != 200:
        logger.info("news feed HTTP %s", response.status_code)
        return None
    elements = _parse(response.content)
    if elements is None:
        return None

    variants = name_variants(name)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=_MAX_AGE_DAYS)
    seen: set[str] = set()
    items: list[NewsItem] = []
    for element in elements:
        item = _item(element)
        if item is None:
            continue
        flat = _norm(item.title)
        if variants and not any(v in flat for v in variants):
            continue  # a headline that does not name the restaurant
        signal = classify(item.title)
        if signal is None:
            continue  # names it, but reports no incident
        if item.published and item.published < cutoff:
            continue
        if flat in seen:
            continue
        seen.add(flat)
        item.kind, item.severity = signal
        items.append(item)

    items.sort(key=lambda i: i.published or date.min, reverse=True)
    return items[:_MAX_FINDINGS]


def search(name: str) -> tuple[list[NewsItem], bool]:
    """(items, answered). `answered` is False when the feed could not be read."""
    key = _norm(name)
    if not key:
        return [], False

    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] > now:
        return (hit[1] or []), hit[1] is not None

    items = _fetch(name)
    ttl = _OK_TTL if items is not None else _FAIL_TTL
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            for stale in [k for k, (exp, _) in _cache.items() if exp <= now] or [next(iter(_cache))]:
                _cache.pop(stale, None)
        _cache[key] = (now + ttl, items)
    return (items or []), items is not None
