#!/usr/bin/env python3
"""Development-only iProperty Malaysia -> Rentee Bubble importer.

The scraper stops when iProperty presents an access-control or bot-challenge
page. It does not try to solve or bypass one.
"""

from __future__ import annotations

import argparse
import calendar
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
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Keep Chromium beside the installed Playwright package so Render includes it in
# the deployed build instead of leaving it in a build-machine-only home cache.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LOGGER = logging.getLogger("iproperty_scraper")
BUBBLE_BASE_URL = "https://www.rentee.asia/version-test/api/1.1"
DEFAULT_SEARCH_URL = "https://www.iproperty.com.my/property-for-rent/p/one-menerung"
IPROPERTY_HOSTS = {"iproperty.com.my", "www.iproperty.com.my"}
LISTING_PATH_RE = re.compile(
    r"/(?:property|bm/properti)/[^?#]+/rent-(\d{7,})/?$", re.I
)
MONEY_RE = re.compile(r"RM\s*([\d,]+)(?:\.\d+)?\s*/\s*(?:mo|month|bulan)", re.I)
AREA_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|sf)\b", re.I)
LISTING_ID_RE = re.compile(r"Listing\s*ID\s*[-:#]?\s*(\d{7,})", re.I)
BLOCK_MARKERS = (
    "just a moment...",
    "cf-chl-",
    "captcha",
    "verify you are human",
    "access denied",
    "bot protection",
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


class IPropertyClient:
    def __init__(self, delay: float = 1.5, timeout: float = 30.0):
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self._last_request_at = 0.0
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> "IPropertyClient":
        display = os.environ.get("DISPLAY")
        LOGGER.info("Browser mode: headed")
        LOGGER.info("DISPLAY: %s", display or "<not set>")
        if display:
            LOGGER.info("Launching headed Chromium under virtual display...")
        else:
            LOGGER.info("Launching headed Chromium...")
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=False)
            self.context = self.browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-MY",
                timezone_id="Asia/Kuala_Lumpur",
                java_script_enabled=True,
            )
            self.page = self.context.new_page()
            self.page.set_default_navigation_timeout(self.timeout * 1000)
            self.page.set_default_timeout(min(self.timeout, 15.0) * 1000)
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        # Close in reverse creation order. Each step is independent so a failed
        # context close cannot leave the browser or Playwright driver running.
        for resource_name in ("page", "context", "browser"):
            resource = getattr(self, resource_name)
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:
                    LOGGER.warning("Failed to close Chromium %s: %s", resource_name, error)
                finally:
                    setattr(self, resource_name, None)
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception as error:
                LOGGER.warning("Failed to stop Playwright: %s", error)
            finally:
                self.playwright = None

    @staticmethod
    def _detect_access_control(html: str, title: str, url: str) -> None:
        sample = f"{title}\n{html[:50000]}".lower()
        marker = next((value for value in BLOCK_MARKERS if value in sample), None)
        if marker:
            raise AccessBlockedError(
                f'Playwright encountered an access-control page at {url} '
                f'(detected "{marker}"). No CAPTCHA, challenge, login, or anti-bot '
                "bypass was attempted."
            )

    def _require_page(self) -> Page:
        if self.page is None:
            raise ScraperError("Chromium has not been launched.")
        return self.page

    def get(
        self,
        url: str,
        content_selector: str | None = None,
        pagination_page_number: int | None = None,
    ) -> str:
        host = (urlparse(url).hostname or "").lower()
        if host not in IPROPERTY_HOSTS:
            raise ScraperError(f"Refusing unexpected iProperty host: {host}")
        page = self._require_page()
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            page.wait_for_timeout(
                (remaining + random.uniform(0, min(0.25, self.delay / 4))) * 1000
            )
        LOGGER.info("Loading: %s", url)
        response = page.goto(url, wait_until="domcontentloaded")
        self._last_request_at = time.monotonic()
        if response is not None and response.status in (401, 403):
            raise AccessBlockedError(
                f"Playwright received HTTP {response.status} at {url}. No CAPTCHA, "
                "challenge, login, or anti-bot bypass was attempted."
            )
        if response is not None and response.status >= 400:
            raise ScraperError(f"iProperty returned HTTP {response.status} at {url}.")
        if pagination_page_number is not None:
            LOGGER.info("Page %d DOM loaded", pagination_page_number)
            settle_seconds = random.uniform(2.0, 4.0)
            LOGGER.info(
                "Waiting %.1fs for page state to settle...", settle_seconds
            )
            page.wait_for_timeout(settle_seconds * 1000)
        if content_selector:
            try:
                page.wait_for_selector(content_selector, state="attached", timeout=10000)
            except PlaywrightTimeoutError:
                # Inspect the rendered page below: this may be an empty-result page,
                # a changed selector, or an access-control page with a useful title.
                LOGGER.warning("Expected iProperty content did not appear within 10 seconds")
        html = page.content()
        self._detect_access_control(html, page.title(), page.url)
        return html

    def discover_listings(
        self, search_url: str, max_pages: int, limit: int | None = None
    ) -> list[NormalizedListing]:
        found: dict[str, NormalizedListing] = {}
        LOGGER.info("Fetching results page: %s", search_url)
        html = self.get(search_url, 'a[href*="/rent-"]')
        page = self._require_page()
        seen_page_signatures: set[tuple[str, ...]] = set()
        for page_number in range(1, max_pages + 1):
            LOGGER.info("Parsing page %d...", page_number)
            page_found = parse_search_results(html, page.url, "One Menerung")
            signature = tuple(item.source_listing_id for item in page_found)
            if signature in seen_page_signatures:
                break
            seen_page_signatures.add(signature)
            for item in page_found:
                found.setdefault(item.source_listing_id, item)
            LOGGER.info(
                "Search page %d loaded; found %d listing links so far",
                page_number, len(found),
            )
            LOGGER.info("Page %d parsed", page_number)
            if limit and len(found) >= limit:
                break
            next_page_number = page_number + 1
            current_search_url = page.url
            next_page_url = discover_pagination_url(
                html, current_search_url, next_page_number, "One Menerung"
            )
            if not next_page_url:
                break
            LOGGER.info("Current search URL: %s", current_search_url)
            LOGGER.info(
                "Page %d href discovered: %s", next_page_number, next_page_url
            )
            pagination_delay = random.uniform(8.0, 15.0)
            LOGGER.info(
                "Waiting %.1fs before page %d...",
                pagination_delay, next_page_number,
            )
            page.wait_for_timeout(pagination_delay * 1000)
            LOGGER.info("Navigating to page %d...", next_page_number)
            try:
                html = self.get(
                    next_page_url,
                    'a[href*="/rent-"]',
                    pagination_page_number=next_page_number,
                )
            except AccessBlockedError:
                LOGGER.warning(
                    "WARNING: iProperty blocked page %d. Stopping pagination and "
                    "continuing with %d listings already collected.",
                    next_page_number, len(found),
                )
                break
        listings = list(found.values())
        return listings[:limit] if limit else listings


def canonical_iproperty_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def discover_pagination_url(
    html: str, current_url: str, page_number: int, expected_condo: str
) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    page_link = soup.select_one(
        f'a[title="Page {page_number}"][href], '
        f'a[da-id="hui-pagination-btn-page-{page_number}"][href]'
    )
    if page_link:
        href = urljoin(current_url, page_link["href"])
        # Current iProperty sometimes renders a stale self-link and relies on an
        # obsolete click handler. Use a real, distinct pagination href verbatim.
        if href != current_url and re.search(rf"/{page_number}(?:\?|/?$)", href):
            return href

    script = soup.select_one("script#__NEXT_DATA__")
    if not script:
        return None
    try:
        next_data = json.loads(script.string or script.get_text())
    except (TypeError, json.JSONDecodeError):
        return None
    page_data = (
        next_data.get("props", {}).get("pageProps", {}).get("pageData", {})
    )
    search_params = page_data.get("searchParams", {})
    listing_type = compact_text(search_params.get("listingType")).casefold()
    if listing_type != "rent":
        return None

    project_ids = set()
    for wrapper in nested_items(page_data, "data", "listingsData"):
        if not isinstance(wrapper, dict):
            continue
        metadata = (
            wrapper.get("segment", {}).get("parameters", {})
            .get("metaData", {}).get("listingData", {})
        )
        listing = wrapper.get("listingData", {})
        if not isinstance(metadata, dict) or not isinstance(listing, dict):
            continue
        if (
            re.match(
                rf"^{re.escape(expected_condo)}(?:,|$)",
                compact_text(listing.get("localizedTitle")), re.I,
            )
            and re.search(r"\bJalan Menerung\b", compact_text(listing.get("fullAddress")), re.I)
            and compact_text((listing.get("property") or {}).get("subTypeText")).casefold()
            == "condominium"
        ):
            project_id = compact_text(metadata.get("projectNanoId"))
            if project_id:
                project_ids.add(project_id)
    if len(project_ids) != 1:
        return None

    query = {
        "isCommercial": str(bool(search_params.get("isCommercial", False))).lower(),
        "_freetextDisplay": expected_condo,
        "propertyId": project_ids.pop(),
    }
    base_url = f"{urlparse(current_url).scheme}://{urlparse(current_url).netloc}"
    return f"{base_url}/property-for-rent/{page_number}?{urlencode(query)}"


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


def extract_photos(
    soup: BeautifulSoup, documents: list[Any], listing_id: str
) -> list[str]:
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
    seen_media = set()
    for candidate in candidates:
        url = candidate.replace("\\u002F", "/").replace("\\/", "/")
        if f"/listing/{listing_id}/" not in url or "${" in url:
            continue
        media_match = re.search(r"/UPHO\.(\d+)\.", url, re.I)
        identity = media_match.group(1) if media_match else url
        if meaningful_image(url) and identity not in seen_media:
            seen_media.add(identity)
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
    match = re.search(
        r"Available\s+(?:(?:from|on)\s+)?"
        r"((?:end\s+of\s+)?[A-Za-z]+(?:\s+\d{1,2})?(?:\s+\d{4})?|"
        r"\d{1,2}\s+[A-Za-z]+(?:\s+\d{4})?)",
        text,
        re.I,
    )
    if not match:
        return None
    raw = compact_text(match.group(1))
    end_of_month = re.fullmatch(r"end\s+of\s+([A-Za-z]+)\s+(\d{4})", raw, re.I)
    if end_of_month:
        parsed = datetime.strptime(
            f"1 {end_of_month.group(1)} {end_of_month.group(2)}", "%d %B %Y"
        )
        return parsed.replace(day=calendar.monthrange(parsed.year, parsed.month)[1]).isoformat()
    for fmt in ("%d %b %Y", "%d %B %Y", "%d %b", "%d %B"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=datetime.now().year)
            return parsed.isoformat()
        except ValueError:
            pass
    return None


def nested_items(value: Any, *path: str) -> list[Any]:
    for key in path:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    return value if isinstance(value, list) else []


def listing_feature_texts(listing_data: dict[str, Any]) -> list[str]:
    output = []
    for value in listing_data.get("listingFeatures", []) or []:
        values = value if isinstance(value, list) else [value]
        for feature in values:
            if not isinstance(feature, dict):
                continue
            text = compact_text(feature.get("text"))
            if text and text not in output:
                output.append(text)
    return output


def search_listing_photos(
    listing_data: dict[str, Any], listing_id: str
) -> list[str]:
    items = nested_items(
        listing_data, "mediaCarousel", "previewMedia", "images", "items"
    )
    output = []
    seen_media = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = compact_text(item.get("src"))
        # iProperty mixes generic project photos into the card carousel. Keep
        # only media belonging to this exact listing.
        if f"/listing/{listing_id}/" not in url or not meaningful_image(url):
            continue
        media_match = re.search(r"/UPHO\.(\d+)\.", url, re.I)
        identity = media_match.group(1) if media_match else url
        if identity not in seen_media:
            seen_media.add(identity)
            output.append(url)
    return output


def first_search_video(listing_data: dict[str, Any]) -> str | None:
    preview = (
        listing_data.get("mediaCarousel", {}).get("previewMedia", {})
        if isinstance(listing_data.get("mediaCarousel"), dict)
        else {}
    )
    for key in ("videos", "heroVideos", "virtualTours"):
        for item in nested_items(preview, key, "items"):
            if isinstance(item, dict):
                for url_key in ("src", "url", "embedUrl"):
                    value = item.get(url_key)
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        return value
    return None


def parse_search_results(
    html: str, search_url: str, expected_condo: str
) -> list[NormalizedListing]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    if not script:
        raise ScraperError("iProperty search page did not contain __NEXT_DATA__.")
    try:
        next_data = json.loads(script.string or script.get_text())
    except (TypeError, json.JSONDecodeError) as error:
        raise ScraperError(f"Could not parse iProperty search-page data: {error}") from error

    listings_data = nested_items(
        next_data, "props", "pageProps", "pageData", "data", "listingsData"
    )
    if not listings_data:
        raise ScraperError("iProperty search-page data contained no listing records.")

    output = []
    for wrapper in listings_data:
        listing = wrapper.get("listingData") if isinstance(wrapper, dict) else None
        if not isinstance(listing, dict):
            continue
        relative_url = compact_text(listing.get("url"))
        relative_url_match = LISTING_PATH_RE.search(urlparse(relative_url).path)
        listing_id = relative_url_match.group(1) if relative_url_match else ""
        title = compact_text(listing.get("localizedTitle"))
        address = compact_text(listing.get("fullAddress"))
        property_data = listing.get("property") or {}
        property_type = compact_text(property_data.get("subTypeText"))
        if (
            not re.fullmatch(r"\d{7,}", listing_id)
            or relative_url_match is None
            or not re.match(rf"^{re.escape(expected_condo)}(?:,|$)", title, re.I)
            or not re.search(r"\bJalan Menerung\b", address, re.I)
            or property_type.casefold() != "condominium"
            or compact_text(listing.get("typeCode")).upper() != "RENT"
        ):
            continue

        source_url = canonical_iproperty_url(urljoin(search_url, relative_url))
        url_match = LISTING_PATH_RE.search(urlparse(source_url).path)
        if not url_match:
            raise ScraperError(f"Could not extract iProperty listing ID from {source_url}.")

        features = listing_feature_texts(listing)
        furnish_text = next(
            (
                compact_text(feature.get("text"))
                for value in listing.get("listingFeatures", []) or []
                for feature in (value if isinstance(value, list) else [value])
                if isinstance(feature, dict)
                and feature.get("dataAutomationId") == "listing-card-v2-furnish"
            ),
            "",
        )
        furnished = None
        if re.search(r"\b(?:fully|partially|partly|semi)[ -]?furnished\b", furnish_text, re.I):
            furnished = "Yes"
        elif re.search(r"\bunfurnished\b", furnish_text, re.I):
            furnished = "No"

        badges = [
            compact_text(item.get("text"))
            for item in listing.get("badges", []) or []
            if isinstance(item, dict) and compact_text(item.get("text"))
        ]
        availability_text = compact_text(listing.get("availabilityInfo"))
        recency = compact_text((listing.get("recency") or {}).get("text"))
        price_data = listing.get("price") or {}
        card_summary_parts = [
            title,
            address,
            compact_text(price_data.get("pretty")),
            "; ".join(features),
            availability_text,
            recency,
        ]
        description = ". ".join(value for value in card_summary_parts if value)
        facts = "; ".join(dict.fromkeys(features + badges)) or None
        photos = search_listing_photos(listing, listing_id)

        output.append(NormalizedListing(
            source_listing_id=listing_id,
            source_url=source_url,
            price_rent=parse_number(price_data.get("value") or price_data.get("localeStringValue")),
            beds=parse_number(listing.get("bedrooms")),
            baths=parse_number(listing.get("bathrooms")),
            sq_ft=parse_number(listing.get("floorArea")),
            description=description or None,
            furnished=furnished,
            availability=parse_availability(availability_text),
            video_url=first_search_video(listing),
            keyfacts=facts,
            cover_photo_url=photos[0] if photos else None,
            photo_urls=photos,
        ))
    return output


def parse_listing(html: str, source_url: str, expected_condo: str) -> NormalizedListing:
    soup = BeautifulSoup(html, "html.parser")
    documents = embedded_documents(soup)
    visible = compact_text(soup.get_text(" ", strip=True))
    title = compact_text(soup.select_one("h1").get_text(" ") if soup.select_one("h1") else "")
    structured_name = compact_text(first_value(documents, {"name", "headline"}))
    identity_text = f"{title} {structured_name} {visible[:2500]}"
    exact_title = re.match(rf"^{re.escape(expected_condo)}(?:,|$)", title, re.I)
    if (
        not exact_title
        or not re.search(rf"\b{re.escape(expected_condo)}\b", identity_text, re.I)
        or not re.search(r"\bJalan Menerung\b", visible, re.I)
        or not re.search(r"\bCondominium for rent\b", visible, re.I)
    ):
        raise ScraperError(f"Rejected listing because it does not clearly identify {expected_condo}.")

    url_id = LISTING_PATH_RE.search(urlparse(source_url).path)
    text_id = LISTING_ID_RE.search(visible)
    structured_id = first_value(documents, {"listingId", "listing_id", "sku", "productID"})
    listing_id = str(
        (url_id.group(1) if url_id else "")
        or (text_id.group(1) if text_id else "")
        or structured_id
    )
    if not re.fullmatch(r"\d{7,}", listing_id):
        raise ScraperError(f"Could not determine iProperty listing ID for {source_url}")
    if text_id and text_id.group(1) != listing_id:
        raise ScraperError(
            f"iProperty listing ID mismatch: URL has {listing_id}, page has {text_id.group(1)}."
        )

    price = parse_number(first_value(documents, {"price", "rentPrice", "askingPrice"}))
    if price is None and (match := MONEY_RE.search(visible)):
        price = parse_number(match.group(1))
    beds_raw = first_value(
        documents, {"bedrooms", "beds", "numberOfRooms", "numberOfBedrooms"}
    )
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
    description_block = soup.select_one(
        '[da-id="description-widget"] .description, .about-section .description'
    )
    description_title = soup.select_one('[da-id="description-widget"] .subtitle')
    if description_block:
        description = "\n".join(
            value for value in (
                compact_text(description_title.get_text(" ")) if description_title else "",
                compact_text(description_block.get_text(" ")),
            ) if value
        )
    description = compact_text(description) or None

    furnishing = None
    if re.search(r"\b(?:fully|partially|semi)[ -]?furnished\b", visible, re.I):
        furnishing = "Yes"
    elif re.search(r"\bunfurnished\b", visible, re.I):
        furnishing = "No"
    amenities = []
    for feature_group in values_for_keys(documents, {"amenityFeature"}):
        if not isinstance(feature_group, list):
            continue
        for feature in feature_group:
            if isinstance(feature, dict) and feature.get("value") is True:
                name = compact_text(feature.get("name"))
                if name:
                    amenities.append(name)
    for heading in soup.find_all(["h3", "h4", "h5"]):
        if any(word in heading.get_text(" ").lower() for word in ("amenities", "details", "features")):
            container = heading.parent
            values = [compact_text(x.get_text(" ")) for x in container.find_all(["li", "span"])]
            amenities.extend(value for value in values if 1 < len(value) < 100)
    amenities = list(dict.fromkeys(amenities))
    facts = "; ".join(amenities) or None
    photos = extract_photos(soup, documents, listing_id)
    feature_text = f"{visible} {description or ''} {facts or ''}"
    maid_room = parse_room_count(feature_text, "maid room") or parse_room_count(feature_text, "maids room")
    if maid_room is None and plus_rooms is not None and re.search(r"\bmaid(?:'s)?\b", visible, re.I):
        maid_room = plus_rooms

    video = first_value(documents, {"videoUrl", "videoURL", "contentUrl", "embedUrl"})
    if video and not isinstance(video, str):
        video = next(flatten_urls(video), None)
    return NormalizedListing(
        source_listing_id=listing_id,
        source_url=canonical_iproperty_url(source_url),
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
        if not response.ok:
            try:
                response_body = json.dumps(
                    response.json(), ensure_ascii=False, indent=2
                )
            except (ValueError, TypeError):
                response_body = response.text or "<empty response body>"

            request = response.request
            method = request.method if request is not None else "UNKNOWN"
            request_url = request.url if request is not None else response.url
            payload_keys: list[str] = []
            if request is not None and request.body:
                try:
                    request_body = request.body
                    if isinstance(request_body, bytes):
                        request_body = request_body.decode("utf-8")
                    parsed_body = json.loads(request_body)
                    if isinstance(parsed_body, dict):
                        payload_keys = list(parsed_body.keys())
                except (UnicodeDecodeError, TypeError, ValueError):
                    pass

            details = [
                f"Bubble API error {response.status_code}",
                f"{method} {request_url}",
            ]
            if payload_keys:
                details.append(
                    "Payload keys: " + ", ".join(payload_keys)
                )
            details.extend(["Response:", response_body])
            raise ScraperError("\n".join(details))

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
        LOGGER.info(
            "Outgoing Bubble Listing payload:\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
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
        "keyFacts": item.keyfacts,
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

    stats = {"found": 0, "created": 0, "updated": 0, "failed": 0}
    with IPropertyClient(args.delay, args.timeout) as client:
        LOGGER.info("Searching iProperty...")
        items = client.discover_listings(args.search_url, args.max_pages, args.limit)
        stats["found"] = len(items)
        LOGGER.info("Found %d %s rental listings", len(items), args.condo)
        for index, item in enumerate(items, 1):
            LOGGER.info(
                "[%d/%d] Processing iProperty search result %s",
                index, len(items), item.source_listing_id,
            )
            try:
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
                LOGGER.error("Failed search result %s: %s", item.source_listing_id, error)
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
    parser.add_argument("--search-url", default=DEFAULT_SEARCH_URL, help="iProperty One Menerung rentals page")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--limit", type=int, help="Process at most this many discovered listings")
    parser.add_argument("--delay", type=float, default=1.5, help="Minimum seconds between iProperty requests")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--bubble-base-url", default=BUBBLE_BASE_URL, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return run(build_parser().parse_args())
    except (ScraperError, requests.RequestException, PlaywrightError) as error:
        LOGGER.error("ERROR: %s", error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
