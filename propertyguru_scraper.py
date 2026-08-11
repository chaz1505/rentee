#!/usr/bin/env python3
"""Development-only PropertyGuru -> Rentee Bubble importer.

The scraper deliberately uses ordinary HTTP and stops when PropertyGuru presents
an access-control or bot-challenge page. It does not try to solve or bypass one.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LOGGER = logging.getLogger("propertyguru_scraper")
BUBBLE_BASE_URL = "https://www.rentee.asia/version-test/api/1.1"
DEFAULT_SEARCH_URL = "https://www.propertyguru.com.my/property-for-rent/p/one-menerung-bangsar"
PROPERTYGURU_HOSTS = {"propertyguru.com.my", "www.propertyguru.com.my"}
LISTING_PATH_RE = re.compile(r"/(?:property-listing|bm/senarai-hartanah)/[^?#]*?(\d{7,})/?$")
MONEY_RE = re.compile(r"RM\s*([\d,]+)(?:\.\d+)?\s*/\s*(?:mo|month|bulan)", re.I)
AREA_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|sf)\b", re.I)
LISTING_ID_RE = re.compile(r"Listing\s*ID\s*[-:#]?\s*(\d{7,})", re.I)
BLOCK_MARKERS = (
    "just a moment...",
    "cf-chl-",
    "captcha",
    "verify you are human",
    "access denied",
)


class ScraperError(RuntimeError):
    pass


class AccessBlockedError(ScraperError):
    pass


class SafetyError(ScraperError):
    pass


@dataclass
class NormalizedListing:
    source_listing_id: str
    source_url: str
    price_rent: float | None = None
    beds: float | None = None
    baths: float | None = None
    sq_ft: float | None = None
    description: str | None = None
    furnished: str | None = None
    balcony: str | None = None
    maid_room: float | None = None
    study: float | None = None
    family_room: float | None = None
    outdoor_area: str | None = None
    availability: str | None = None
    unit_number: str | None = None
    video_url: str | None = None
    keyfacts: str | None = None
    cover_photo_url: str | None = None
    photo_urls: list[str] = field(default_factory=list)


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else None


def display_number(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.1f}"


def make_session(token: str | None = None) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PATCH"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/json",
            "User-Agent": "Mozilla/5.0 (compatible; RenteeDevelopmentImporter/1.0)",
        }
    )
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def assert_development_endpoint(base_url: str) -> None:
    if "/version-test/" not in base_url:
        raise SafetyError(
            "SAFETY ERROR: Refusing to write because Bubble endpoint is not "
            "the development environment."
        )


class PropertyGuruClient:
    def __init__(self, delay: float = 1.5, timeout: float = 30.0):
        self.session = make_session()
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self._last_request_at = 0.0

    def get(self, url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        if host not in PROPERTYGURU_HOSTS:
            raise ScraperError(f"Refusing unexpected PropertyGuru host: {host}")
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining + random.uniform(0, min(0.25, self.delay / 4)))
        response = self.session.get(url, timeout=self.timeout)
        self._last_request_at = time.monotonic()
        lowered = response.text[:20000].lower()
        if response.status_code in (401, 403) or any(x in lowered for x in BLOCK_MARKERS):
            raise AccessBlockedError(
                f"PropertyGuru blocked ordinary HTTP retrieval at {url} "
                f"(HTTP {response.status_code}). No CAPTCHA, challenge, login, or "
                "anti-bot bypass was attempted."
            )
        response.raise_for_status()
        return response.text

    def discover_listing_urls(self, search_url: str, max_pages: int) -> list[str]:
        found: dict[str, str] = {}
        next_url: str | None = search_url
        visited: set[str] = set()
        for _ in range(max_pages):
            if not next_url or next_url in visited:
                break
            visited.add(next_url)
            LOGGER.info("Fetching results page: %s", next_url)
            soup = BeautifulSoup(self.get(next_url), "html.parser")
            for anchor in soup.select("a[href]"):
                absolute = canonical_propertyguru_url(urljoin(next_url, anchor["href"]))
                match = LISTING_PATH_RE.search(urlparse(absolute).path)
                if match:
                    found.setdefault(match.group(1), absolute)
            next_link = soup.select_one('link[rel="next"][href], a[rel="next"][href]')
            next_url = urljoin(next_url, next_link["href"]) if next_link else None
        return list(found.values())


def canonical_propertyguru_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def walk_json(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def embedded_documents(soup: BeautifulSoup) -> list[Any]:
    documents = []
    for script in soup.select('script[type="application/ld+json"], script#__NEXT_DATA__'):
        try:
            documents.append(json.loads(script.string or script.get_text()))
        except (TypeError, json.JSONDecodeError):
            continue
    return documents


def values_for_keys(documents: Iterable[Any], keys: set[str]) -> list[Any]:
    wanted = {key.lower() for key in keys}
    return [value for key, value in walk_json(list(documents)) if key.lower() in wanted]


def first_value(documents: Iterable[Any], keys: set[str]) -> Any:
    for value in values_for_keys(documents, keys):
        if value not in (None, "", [], {}):
            return value
    return None


def flatten_urls(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "//")):
            yield "https:" + value if value.startswith("//") else value
    elif isinstance(value, dict):
        # Prefer original/high-resolution variants before walking every value.
        priority = ("original", "originalUrl", "large", "url", "src")
        emitted = set()
        for key in priority:
            if key in value:
                for url in flatten_urls(value[key]):
                    emitted.add(url)
                    yield url
        for child in value.values():
            for url in flatten_urls(child):
                if url not in emitted:
                    yield url
    elif isinstance(value, list):
        for child in value:
            yield from flatten_urls(child)


def meaningful_image(url: str) -> bool:
    lowered = url.lower()
    if not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", lowered):
        return False
    excluded = ("logo", "avatar", "profile", "agent", "icon", "placeholder", "sprite", "tracking")
    return not any(marker in lowered for marker in excluded)


def extract_photos(soup: BeautifulSoup, documents: list[Any]) -> list[str]:
    candidates: list[str] = []
    photo_keys = {"photos", "images", "media", "gallery", "image", "imageurl", "imageurls"}
    for value in values_for_keys(documents, photo_keys):
        candidates.extend(flatten_urls(value))
    for meta in soup.select('meta[property="og:image"][content]'):
        candidates.append(meta["content"])
    for image in soup.select("main img, article img"):
        for attribute in ("data-original", "data-src", "src"):
            if image.get(attribute):
                candidates.append(image[attribute])
        if image.get("srcset"):
            srcset = [part.strip().split()[0] for part in image["srcset"].split(",")]
            candidates.extend(reversed(srcset))
    output = []
    seen = set()
    for candidate in candidates:
        url = candidate.replace("\\u002F", "/").replace("\\/", "/")
        if meaningful_image(url) and url not in seen:
            seen.add(url)
            output.append(url)
    return output


def parse_room_count(text: str, label: str) -> float | None:
    patterns = (
        rf"(\d+(?:\.\d+)?)\s*{re.escape(label)}s?\b",
        rf"{re.escape(label)}s?\s*[:\-]?\s*(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1))
    return None


def explicit_yes_no(text: str, feature: str) -> str | None:
    if re.search(rf"\b(?:no|without)\s+(?:a\s+)?{re.escape(feature)}\b", text, re.I):
        return "No"
    if re.search(rf"\b(?:with\s+(?:a\s+)?|has\s+(?:a\s+)?|spacious\s+)?{re.escape(feature)}\b", text, re.I):
        return "Yes"
    return None


def parse_bedrooms(value: Any) -> tuple[float | None, float | None]:
    if value is None:
        return None, None
    match = re.search(r"(\d+)\s*(?:\+\s*(\d+))?", str(value))
    if not match:
        return None, None
    # Bubble's beds field gets principal bedrooms; an explicitly separated +N is
    # retained as maid rooms only when the page labels it that way elsewhere.
    return float(match.group(1)), float(match.group(2)) if match.group(2) else None


def parse_availability(text: str) -> str | None:
    match = re.search(r"Available\s+(?:from|on)\s+([^|\n.,;]+(?:\s+\d{4})?)", text, re.I)
    if not match:
        return None
    raw = compact_text(match.group(1))
    for fmt in ("%d %b %Y", "%d %B %Y", "%d %b", "%d %B"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=datetime.now().year)
            return parsed.isoformat()
        except ValueError:
            pass
    return None


def parse_listing(html: str, source_url: str, expected_condo: str) -> NormalizedListing:
    soup = BeautifulSoup(html, "html.parser")
    documents = embedded_documents(soup)
    visible = compact_text(soup.get_text(" ", strip=True))
    title = compact_text(soup.select_one("h1").get_text(" ") if soup.select_one("h1") else "")
    structured_name = compact_text(first_value(documents, {"name", "headline"}))
    identity_text = f"{title} {structured_name} {visible[:2500]}"
    if not re.search(rf"\b{re.escape(expected_condo)}\b", identity_text, re.I):
        raise ScraperError(f"Rejected listing because it does not clearly identify {expected_condo}.")

    url_id = LISTING_PATH_RE.search(urlparse(source_url).path)
    text_id = LISTING_ID_RE.search(visible)
    structured_id = first_value(documents, {"listingId", "listing_id", "sku", "productID"})
    listing_id = str(structured_id or (text_id.group(1) if text_id else "") or (url_id.group(1) if url_id else ""))
    if not re.fullmatch(r"\d{7,}", listing_id):
        raise ScraperError(f"Could not determine PropertyGuru listing ID for {source_url}")

    price = parse_number(first_value(documents, {"price", "rentPrice", "askingPrice"}))
    if price is None and (match := MONEY_RE.search(visible)):
        price = parse_number(match.group(1))
    beds_raw = first_value(documents, {"bedrooms", "beds", "numberOfRooms"})
    beds, plus_rooms = parse_bedrooms(beds_raw)
    if beds is None and (match := re.search(r"(\d+(?:\s*\+\s*\d+)?)\s*Beds?\b", visible, re.I)):
        beds, plus_rooms = parse_bedrooms(match.group(1))
    baths = parse_number(first_value(documents, {"bathrooms", "baths", "numberOfBathroomsTotal"}))
    if baths is None and (match := re.search(r"(\d+)\s*Baths?\b", visible, re.I)):
        baths = parse_number(match.group(1))
    area = parse_number(first_value(documents, {"floorSize", "builtUpSize", "builtUpArea", "area"}))
    if area is None and (match := AREA_RE.search(visible)):
        area = parse_number(match.group(1))

    description = first_value(documents, {"description"})
    about = soup.select_one('[data-testid*="description"], section:has(h2), article')
    if about and (not description or len(compact_text(about.get_text(" "))) > len(compact_text(description))):
        description = compact_text(about.get_text(" "))
    description = compact_text(description) or None

    furnishing = None
    if re.search(r"\b(?:fully|partially|semi)[ -]?furnished\b", visible, re.I):
        furnishing = "Yes"
    elif re.search(r"\bunfurnished\b", visible, re.I):
        furnishing = "No"
    amenities = []
    for heading in soup.find_all(["h3", "h4", "h5"]):
        if any(word in heading.get_text(" ").lower() for word in ("amenities", "details", "features")):
            container = heading.parent
            values = [compact_text(x.get_text(" ")) for x in container.find_all(["li", "span"])]
            amenities.extend(value for value in values if 1 < len(value) < 100)
    amenities = list(dict.fromkeys(amenities))
    facts = "; ".join(amenities) or None
    photos = extract_photos(soup, documents)
    feature_text = f"{visible} {description or ''} {facts or ''}"
    maid_room = parse_room_count(feature_text, "maid room") or parse_room_count(feature_text, "maids room")
    if maid_room is None and plus_rooms is not None and re.search(r"\bmaid(?:'s)?\b", visible, re.I):
        maid_room = plus_rooms

    video = first_value(documents, {"videoUrl", "videoURL", "contentUrl", "embedUrl"})
    if video and not isinstance(video, str):
        video = next(flatten_urls(video), None)
    return NormalizedListing(
        source_listing_id=listing_id,
        source_url=canonical_propertyguru_url(source_url),
        price_rent=price,
        beds=beds,
        baths=baths,
        sq_ft=area,
        description=description,
        furnished=furnishing,
        balcony=explicit_yes_no(feature_text, "balcony"),
        maid_room=maid_room,
        study=parse_room_count(feature_text, "study"),
        family_room=parse_room_count(feature_text, "family room"),
        outdoor_area=explicit_yes_no(feature_text, "outdoor area"),
        availability=parse_availability(visible),
        unit_number=(re.search(r"\bUnit\s*(?:No\.?|Number|#)\s*[:\-]?\s*([\w-]+)", visible, re.I).group(1)
                     if re.search(r"\bUnit\s*(?:No\.?|Number|#)\s*[:\-]?\s*([\w-]+)", visible, re.I) else None),
        video_url=video,
        keyfacts=facts,
        cover_photo_url=photos[0] if photos else None,
        photo_urls=photos,
    )


class BubbleClient:
    def __init__(self, token: str, base_url: str = BUBBLE_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = make_session(token)
        self.timeout = 30

    def _json(self, response: requests.Response) -> dict[str, Any]:
        response.raise_for_status()
        data = response.json()
        return data.get("response", data)

    def get_all(self, data_type: str, constraints: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        results = []
        cursor = 0
        while True:
            params: dict[str, Any] = {"cursor": cursor}
            if constraints:
                params["constraints"] = json.dumps(constraints, separators=(",", ":"))
            page = self._json(self.session.get(
                f"{self.base_url}/obj/{data_type}", params=params, timeout=self.timeout
            ))
            batch = page.get("results", [])
            results.extend(batch)
            if not batch or not (page.get("remaining", 0) or 0):
                return results
            cursor += len(batch)

    def find_condo(self, condo_name: str) -> tuple[str, str]:
        # The current repo does not document the Condo display field. Discover it
        # from development data instead of hard-coding an unverified field name.
        matches: list[tuple[dict[str, Any], str]] = []
        target = compact_text(condo_name).casefold()
        for record in self.get_all("condo"):
            matching_fields = [
                key for key, value in record.items()
                if isinstance(value, str) and compact_text(value).casefold() == target
            ]
            if matching_fields:
                matches.append((record, matching_fields[0]))
        if not matches:
            raise ScraperError(f'No exact Condo match found for "{condo_name}" in Bubble development.')
        if len(matches) > 1:
            printable = [{"_id": row.get("_id"), "matching_field": field} for row, field in matches]
            raise ScraperError(
                f'Multiple exact Condo matches found for "{condo_name}": '
                + json.dumps(printable, indent=2)
            )
        record, field_name = matches[0]
        if not record.get("_id"):
            raise ScraperError("Matching Condo has no Bubble _id.")
        return record["_id"], field_name

    def find_listing(self, source_listing_id: str) -> dict[str, Any] | None:
        matches = self.get_all("listing", [{
            "key": "sourceListingID", "constraint_type": "equals", "value": source_listing_id
        }])
        if len(matches) > 1:
            raise ScraperError(
                f"Multiple Bubble Listings have sourceListingID={source_listing_id}; refusing to guess."
            )
        return matches[0] if matches else None

    def write_listing(self, payload: dict[str, Any], existing_id: str | None) -> str:
        assert_development_endpoint(self.base_url + "/")
        if existing_id:
            response = self.session.patch(
                f"{self.base_url}/obj/listing/{existing_id}", json=payload, timeout=self.timeout
            )
            self._json(response)
            return existing_id
        response = self.session.post(
            f"{self.base_url}/obj/listing", json=payload, timeout=self.timeout
        )
        data = self._json(response)
        new_id = data.get("id") or data.get("_id")
        if not new_id:
            raise ScraperError("Bubble create succeeded but returned no Listing ID.")
        return new_id


def bubble_payload(item: NormalizedListing, condo_id: str) -> dict[str, Any]:
    mapping = {
        "priceRent": item.price_rent,
        "beds": item.beds,
        "baths": item.baths,
        "Sq Ft": item.sq_ft,
        "Description": item.description,
        "furnished": item.furnished,
        "balcony": item.balcony,
        "maid room": item.maid_room,
        "study": item.study,
        "family room": item.family_room,
        "outdoor area": item.outdoor_area,
        "availability": item.availability,
        "unitNumber": item.unit_number,
        "Video URL": item.video_url,
        "keyfacts": item.keyfacts,
        "coverPhoto": item.cover_photo_url,
        "photos": item.photo_urls or None,
        "sourceURL": item.source_url,
        "sourceListingID": item.source_listing_id,
        "condo": condo_id,
        "propertyType": "Condo",
        "TransactionType": ["Rent/Let"],
        "scraped?": True,
    }
    return {key: value for key, value in mapping.items() if value is not None}


def run(args: argparse.Namespace) -> int:
    if compact_text(args.condo).casefold() != "one menerung":
        raise ScraperError('Version 1 only supports --condo "One Menerung".')
    assert_development_endpoint(args.bubble_base_url + "/")
    token = os.environ.get("BUBBLE_API_TOKEN")
    if not token:
        raise ScraperError("BUBBLE_API_TOKEN is required for development Condo/listing reads.")
    bubble = BubbleClient(token, args.bubble_base_url)
    LOGGER.info("Environment: development")
    LOGGER.info("Bubble endpoint verified: version-test")
    if args.dry_run:
        LOGGER.info("DRY RUN: no Bubble writes will be performed")
    LOGGER.info("Looking up Condo: %s", args.condo)
    condo_id, condo_field = bubble.find_condo(args.condo)
    LOGGER.info("Found %s: %s (matched field: %s)", args.condo, condo_id, condo_field)

    client = PropertyGuruClient(args.delay, args.timeout)
    LOGGER.info("Searching PropertyGuru...")
    urls = client.discover_listing_urls(args.search_url, args.max_pages)
    if args.limit:
        urls = urls[:args.limit]
    LOGGER.info("Found %d %s rental listing links", len(urls), args.condo)
    stats = {"found": len(urls), "created": 0, "updated": 0, "failed": 0}
    for index, url in enumerate(urls, 1):
        url_match = LISTING_PATH_RE.search(urlparse(url).path)
        LOGGER.info("[%d/%d] Processing PropertyGuru listing %s", index, len(urls), url_match.group(1) if url_match else url)
        try:
            item = parse_listing(client.get(url), url, args.condo)
            LOGGER.info(
                "Parsed: RM%s | %s beds | %s baths | %s sq ft | %d photos",
                display_number(item.price_rent), display_number(item.beds),
                display_number(item.baths), display_number(item.sq_ft), len(item.photo_urls),
            )
            existing = bubble.find_listing(item.source_listing_id)
            payload = bubble_payload(item, condo_id)
            LOGGER.info("%s Bubble listing found", "Existing" if existing else "No existing")
            if args.dry_run:
                print(json.dumps({
                    "action": "update" if existing else "create",
                    "normalized": asdict(item), "bubble_payload": payload,
                }, ensure_ascii=False, indent=2))
            else:
                action = "updated" if existing else "created"
                LOGGER.info("%s...", "Updating" if existing else "Creating")
                bubble.write_listing(payload, existing.get("_id") if existing else None)
                stats[action] += 1
        except AccessBlockedError:
            raise
        except Exception as error:  # one malformed listing must not terminate the run
            stats["failed"] += 1
            LOGGER.error("Failed %s: %s", url, error)
    LOGGER.info("Complete")
    LOGGER.info("Found: %d", stats["found"])
    LOGGER.info("Created: %d", stats["created"])
    LOGGER.info("Updated: %d", stats["updated"])
    LOGGER.info("Failed: %d", stats["failed"])
    return 0 if stats["failed"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condo", required=True, help='Must be "One Menerung" for v1')
    parser.add_argument("--dry-run", action="store_true", help="Read Bubble development but perform no writes")
    parser.add_argument("--search-url", default=DEFAULT_SEARCH_URL, help="PropertyGuru One Menerung rentals page")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--limit", type=int, help="Process at most this many discovered listings")
    parser.add_argument("--delay", type=float, default=1.5, help="Minimum seconds between PropertyGuru requests")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--bubble-base-url", default=BUBBLE_BASE_URL, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return run(build_parser().parse_args())
    except (ScraperError, requests.RequestException) as error:
        LOGGER.error("ERROR: %s", error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
