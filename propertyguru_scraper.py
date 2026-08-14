#!/usr/bin/env python3
"""iProperty Malaysia -> Rentee Bubble importer.

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
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
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
BUBBLE_LIVE_BASE_URL = "https://www.rentee.asia/api/1.1"
IPROPERTY_RENT_SEARCH_ROOT = "https://www.iproperty.com.my/property-for-rent/p"
DEFAULT_CONDO_CONFIG_PATH = Path(__file__).resolve().parent / "scraper_condos.json"
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
    source_agent_name: str | None = None
    source_agency_name: str | None = None
    price_rent: float | None = None
    beds: float | None = None
    baths: float | None = None
    sq_ft: float | None = None
    description: str | None = None
    furnished: str | None = None
    furnishing: str | None = None
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


@dataclass(frozen=True)
class IPropertyProject:
    name: str
    project_id: str | None
    property_type: str


@dataclass
class CondoConfig:
    requested_name: str
    bubble_name: str
    iproperty_search_name: str
    iproperty_project_name: str | None
    iproperty_project_id: str | None
    group: str | None
    enabled: bool
    iproperty_search_mode: str | None = None


@dataclass(frozen=True)
class ProjectCandidate:
    name: str
    project_id: str | None
    property_type: str
    transaction_type: str
    listing_count: int
    example_url: str | None


def load_condo_configs(
    config_path: Path = DEFAULT_CONDO_CONFIG_PATH,
) -> list[CondoConfig]:
    config_path = config_path.expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ScraperError(f"Condo configuration file not found: {config_path}") from error
    except json.JSONDecodeError as error:
        raise ScraperError(f"Invalid condo configuration JSON: {error}") from error
    if not isinstance(raw, list):
        raise ScraperError("scraper_condos.json must contain a JSON array.")
    configs = []
    for row in raw:
        if not isinstance(row, dict):
            raise ScraperError("Every condo configuration entry must be a JSON object.")
        required = ("requested_name", "bubble_name", "iproperty_search_name")
        if any(not compact_text(row.get(key)) for key in required):
            raise ScraperError("Condo configuration contains an incomplete entry.")
        search_mode = compact_text(row.get("iproperty_search_mode")).casefold() or None
        if search_mode not in (None, "project", "freetext"):
            raise ScraperError(
                f'Invalid iproperty_search_mode for "{row["requested_name"]}": {search_mode}'
            )
        configs.append(CondoConfig(
            requested_name=compact_text(row["requested_name"]),
            bubble_name=compact_text(row["bubble_name"]),
            iproperty_search_name=compact_text(row["iproperty_search_name"]),
            iproperty_project_name=compact_text(row.get("iproperty_project_name")) or None,
            iproperty_project_id=compact_text(row.get("iproperty_project_id")) or None,
            group=compact_text(row.get("group")) or None,
            enabled=row.get("enabled") is True,
            iproperty_search_mode=search_mode,
        ))
    names = [config.requested_name.casefold() for config in configs]
    if len(names) != len(set(names)):
        raise ScraperError("scraper_condos.json contains duplicate requested_name values.")
    return configs


def load_condo_config(
    requested_name: str, config_path: Path = DEFAULT_CONDO_CONFIG_PATH
) -> CondoConfig:
    target = compact_text(requested_name).casefold()
    matches = [config for config in load_condo_configs(config_path)
               if config.requested_name.casefold() == target]
    if len(matches) != 1:
        if not matches:
            raise ScraperError(f'No condo configuration found for "{requested_name}".')
        raise ScraperError(f'Multiple condo configurations found for "{requested_name}".')
    config = matches[0]
    if not config.enabled:
        raise ScraperError(f'Condo configuration for "{requested_name}" is disabled.')
    return config


def select_batch_condos(
    configs: list[CondoConfig], group: str | None = None
) -> list[CondoConfig]:
    enabled = [config for config in configs if config.enabled]
    if group is None:
        return enabled
    target = compact_text(group).casefold()
    return [config for config in enabled if (config.group or "").casefold() == target]


def select_missing_project_id_condos(configs: list[CondoConfig]) -> list[CondoConfig]:
    return [
        config for config in configs
        if (config.iproperty_search_mode or "project") != "freetext"
        and not compact_text(config.iproperty_project_id)
    ]


def slice_batch_condos(
    targets: list[CondoConfig],
    start: int | None = None,
    count: int | None = None,
) -> list[tuple[int, CondoConfig]]:
    start = 1 if start is None else start
    if start < 1:
        raise ScraperError("--start must be at least 1.")
    if count is not None and count < 1:
        raise ScraperError("--count must be at least 1.")
    if start > len(targets):
        raise ScraperError(
            f"--start {start} exceeds the {len(targets)} enabled condos available."
        )
    end = None if count is None else start - 1 + count
    return list(enumerate(targets, 1))[start - 1:end]


def cache_project_identity(
    config: CondoConfig,
    project: IPropertyProject,
    config_path: Path = DEFAULT_CONDO_CONFIG_PATH,
) -> bool:
    if config.iproperty_project_id or not project.project_id:
        return False
    config_path = config_path.expanduser().resolve()
    LOGGER.info("Saving project identity to:")
    LOGGER.info("%s", config_path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScraperError(f"Could not load condo config for atomic update: {error}") from error
    matches = [row for row in raw if isinstance(row, dict) and
               row.get("requested_name") == config.requested_name]
    if len(matches) != 1:
        raise ScraperError(
            f'Expected exactly one config entry with requested_name="{config.requested_name}".'
        )
    entry = matches[0]
    if compact_text(entry.get("iproperty_project_id")):
        return False
    entry["iproperty_project_name"] = project.name
    entry["iproperty_project_id"] = project.project_id

    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config_path)
        temporary_path = None
    except OSError as error:
        raise ScraperError(f"Could not atomically save condo config: {error}") from error
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass

    try:
        persisted = json.loads(config_path.read_text(encoding="utf-8"))
        persisted_matches = [
            row for row in persisted
            if isinstance(row, dict) and row.get("requested_name") == config.requested_name
        ]
    except (OSError, json.JSONDecodeError) as error:
        raise ScraperError(
            f"project identity persistence verification failed: {error}"
        ) from error
    if len(persisted_matches) != 1 or (
        persisted_matches[0].get("iproperty_project_name") != project.name
        or persisted_matches[0].get("iproperty_project_id") != project.project_id
    ):
        raise ScraperError("project identity persistence verification failed")

    config.iproperty_project_name = project.name
    config.iproperty_project_id = project.project_id
    return True


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_project_name(value: Any) -> str:
    """Return a compact name key without punctuation or spacing."""
    normalized = unicodedata.normalize("NFKC", compact_text(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


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


def assert_live_endpoint(base_url: str) -> None:
    normalized = base_url.rstrip("/")
    if normalized != BUBBLE_LIVE_BASE_URL:
        raise SafetyError(
            "SAFETY ERROR: Refusing live write because Bubble endpoint is not "
            "the configured production endpoint."
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
        LOGGER.info("Launching Chromium...")
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
        content_timeout_ms: int = 10000,
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
                page.wait_for_selector(
                    content_selector, state="attached", timeout=content_timeout_ms
                )
            except PlaywrightTimeoutError:
                # Inspect the rendered page below: this may be an empty-result page,
                # a changed selector, or an access-control page with a useful title.
                LOGGER.warning(
                    "Expected iProperty content did not appear within %.0f seconds",
                    content_timeout_ms / 1000,
                )
        html = page.content()
        self._detect_access_control(html, page.title(), page.url)
        return html

    def discover_listings(
        self,
        search_url: str,
        max_pages: int,
        config: CondoConfig,
        limit: int | None = None,
        project_resolved: Callable[[IPropertyProject], None] | None = None,
        discovery_only: bool = False,
        should_process_photos: Callable[[str], bool] | None = None,
    ) -> list[NormalizedListing]:
        found: dict[str, NormalizedListing] = {}
        LOGGER.info("Fetching results page: %s", search_url)
        configured_project_id = compact_text(config.iproperty_project_id) or None
        if configured_project_id:
            html = self.get(
                search_url, "script#__NEXT_DATA__", content_timeout_ms=20000
            )
        else:
            html = self.get(search_url, 'a[href*="/rent-"]')
        page = self._require_page()
        search_mode = config.iproperty_search_mode or "project"
        if search_mode == "freetext":
            expected_name = config.iproperty_project_name or config.iproperty_search_name
            project = IPropertyProject(expected_name, None, "Condominium")
            LOGGER.info("Searching iProperty using free-text mode")
        elif configured_project_id:
            LOGGER.info("Configured project ID found: %s", configured_project_id)
            LOGGER.info("Skipping project discovery.")
            project = IPropertyProject(
                config.iproperty_project_name or config.iproperty_search_name,
                configured_project_id,
                "",
            )
            if not BeautifulSoup(html, "html.parser").select_one("script#__NEXT_DATA__"):
                raise ScraperError(
                    "project-filtered search page did not produce usable listing data "
                    f"for configured project ID {configured_project_id}"
                )
        else:
            project = resolve_iproperty_project(
                html, page.url, config.iproperty_search_name,
                config.iproperty_project_name, config.iproperty_project_id,
            )
        LOGGER.info("Resolved iProperty project:")
        LOGGER.info("Name: %s", project.name)
        LOGGER.info("Project ID: %s", project.project_id or "<not available>")
        if project_resolved and search_mode != "freetext":
            project_resolved(project)
        if discovery_only:
            LOGGER.info("Discovery-only mode: skipping Listing parsing and processing.")
            return []
        seen_page_signatures: set[tuple[str, ...]] = set()
        for page_number in range(1, max_pages + 1):
            LOGGER.info("Parsing page %d...", page_number)
            try:
                page_found = parse_search_results(
                    html, page.url, config.iproperty_search_name, project,
                    should_process_photos,
                )
            except ScraperError as error:
                if page_number == 1 and configured_project_id:
                    raise ScraperError(
                        "project-filtered search page did not produce usable listing data "
                        f"for configured project ID {configured_project_id}: {error}"
                    ) from error
                raise
            if search_mode == "freetext":
                returned_count = sum(
                    candidate.listing_count
                    for candidate in iproperty_project_candidates(html, page.url)
                )
                LOGGER.info(
                    "Rejected %d unrelated free-text results",
                    max(0, returned_count - len(page_found)),
                )
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
            if search_mode == "freetext":
                LOGGER.info("Free-text mode: pagination disabled; using page 1 only.")
                break
            if limit and len(found) >= limit:
                break
            next_page_number = page_number + 1
            current_search_url = page.url
            next_page_url = discover_pagination_url(
                html, current_search_url, next_page_number,
                config.iproperty_search_name,
                project.project_id,
                search_mode,
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

    def discover_projects(self, search_name: str) -> tuple[str, list[ProjectCandidate]]:
        search_url = iproperty_search_url(search_name)
        LOGGER.info("Discovering iProperty projects for: %s", search_name)
        LOGGER.info("Search URL: %s", search_url)
        html = self.get(search_url, 'a[href*="/rent-"]')
        page = self._require_page()
        return page.url, iproperty_project_candidates(html, page.url)


def canonical_iproperty_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def iproperty_project_slug(condo_name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", compact_text(condo_name)).encode(
        "ascii", "ignore"
    ).decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
    if not slug:
        raise ScraperError(f'Could not derive an iProperty search slug for "{condo_name}".')
    return slug


def iproperty_search_url(condo_name: str) -> str:
    return f"{IPROPERTY_RENT_SEARCH_ROOT}/{iproperty_project_slug(condo_name)}"


def iproperty_project_search_url(search_name: str, project_id: str) -> str:
    query = {
        "isCommercial": "false",
        "_freetextDisplay": compact_text(search_name),
        "propertyId": compact_text(project_id),
    }
    return f"https://www.iproperty.com.my/property-for-rent?{urlencode(query)}"


def iproperty_freetext_search_url(search_name: str, page_number: int = 1) -> str:
    query = {
        "page": page_number,
        "isCommercial": "false",
        "listingType": "rent",
        "_freetextDisplay": compact_text(search_name),
        "freetext": compact_text(search_name),
    }
    return f"https://www.iproperty.com.my/property-for-rent?{urlencode(query)}"


def _wrapper_project_details(
    wrapper: dict[str, Any],
) -> tuple[str, str | None, str, str]:
    listing = wrapper.get("listingData") or {}
    metadata = (
        wrapper.get("segment", {}).get("parameters", {})
        .get("metaData", {}).get("listingData", {})
    )
    property_data = listing.get("property") or {}
    explicit_names = [
        listing.get("projectName"), listing.get("propertyName"),
        metadata.get("projectName"), metadata.get("propertyName"),
        property_data.get("projectName"),
    ]
    project_name = next(
        (compact_text(value) for value in explicit_names if compact_text(value)), ""
    )
    if not project_name:
        project_name = compact_text(listing.get("localizedTitle")).split(",", 1)[0]
    project_id = compact_text(
        metadata.get("projectNanoId")
        or metadata.get("projectId")
        or listing.get("projectNanoId")
        or property_data.get("projectNanoId")
    ) or None
    property_type = compact_text(property_data.get("subTypeText"))
    transaction_type = compact_text(listing.get("typeCode")).upper()
    return project_name, project_id, property_type, transaction_type


def iproperty_project_candidates(html: str, search_url: str) -> list[ProjectCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    if not script:
        raise ScraperError("iProperty search page did not contain __NEXT_DATA__.")
    try:
        next_data = json.loads(script.string or script.get_text())
    except (TypeError, json.JSONDecodeError) as error:
        raise ScraperError(f"Could not parse iProperty search-page data: {error}") from error
    wrappers = nested_items(
        next_data, "props", "pageProps", "pageData", "data", "listingsData"
    )
    grouped: dict[tuple[str, str | None, str, str], dict[str, Any]] = {}
    for wrapper in wrappers:
        if not isinstance(wrapper, dict):
            continue
        details = _wrapper_project_details(wrapper)
        name, project_id, property_type, transaction_type = details
        if not name:
            continue
        key = (name, project_id, property_type, transaction_type)
        row = grouped.setdefault(key, {"count": 0, "example": None})
        row["count"] += 1
        listing = wrapper.get("listingData") or {}
        relative_url = compact_text(listing.get("url"))
        if row["example"] is None and relative_url:
            row["example"] = canonical_iproperty_url(urljoin(search_url, relative_url))
    return [
        ProjectCandidate(name, project_id, property_type, transaction_type,
                         values["count"], values["example"])
        for (name, project_id, property_type, transaction_type), values
        in sorted(grouped.items(), key=lambda item: (-item[1]["count"], item[0][0].casefold()))
    ]


def format_project_candidates(candidates: list[ProjectCandidate]) -> str:
    if not candidates:
        return "- <none>"
    return "\n".join(
        f"- {item.name} | {item.project_id or '<unset>'} | "
        f"{item.property_type or '<unknown>'} | {item.transaction_type or '<unknown>'} | "
        f"{item.listing_count} listings"
        for item in candidates
    )


def _project_name_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", compact_text(value)).casefold()
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def _remove_token_sequence(tokens: list[str], sequence: tuple[str, ...]) -> list[str]:
    result = list(tokens)
    position = 0
    while position <= len(result) - len(sequence):
        if tuple(result[position:position + len(sequence)]) == sequence:
            del result[position:position + len(sequence)]
        else:
            position += 1
    return result


PROJECT_LOCATION_QUALIFIERS = (
    ("mont", "kiara"),
    ("bukit", "bandaraya"),
    ("damansara", "heights"),
    ("kuala", "lumpur"),
    ("bangsar",),
    ("klcc",),
)
PROJECT_GENERIC_TOKENS = {
    "the", "residence", "residences", "residensi", "condominium", "apartment",
}
PROJECT_LOCATION_TOKENS = {
    token for qualifier in PROJECT_LOCATION_QUALIFIERS for token in qualifier
}


def decoration_location_project_name(value: str) -> str:
    tokens = _project_name_tokens(value)
    for qualifier in PROJECT_LOCATION_QUALIFIERS:
        tokens = _remove_token_sequence(tokens, qualifier)
    if tokens and tokens[0] == "the":
        tokens.pop(0)
    while tokens and tokens[-1] in {"residence", "residences", "residensi"}:
        tokens.pop()
    numeric_tokens = {token for token in tokens if token.isdigit()}
    tokens = [
        token for token in tokens
        if not (re.fullmatch(r"mk(\d+)", token) and token[2:] in numeric_tokens)
    ]
    return "".join(tokens)


def distinctive_project_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        token for token in _project_name_tokens(value)
        if token not in PROJECT_GENERIC_TOKENS
        and token not in PROJECT_LOCATION_TOKENS
    )


def _deduplicate_rent_projects(
    candidates: list[ProjectCandidate],
) -> list[list[ProjectCandidate]]:
    groups: dict[tuple[str, str], list[ProjectCandidate]] = {}
    for candidate in candidates:
        if candidate.transaction_type != "RENT":
            continue
        key = (
            ("id", candidate.project_id)
            if candidate.project_id
            else ("name", normalize_project_name(candidate.name))
        )
        groups.setdefault(key, []).append(candidate)
    return list(groups.values())


def _project_from_group(group: list[ProjectCandidate]) -> IPropertyProject:
    representative = group[0]
    return IPropertyProject(
        representative.name, representative.project_id, representative.property_type
    )


def _log_relaxed_project_match(
    configured_name: str,
    group: list[ProjectCandidate],
    reason: str,
) -> None:
    project = _project_from_group(group)
    LOGGER.info("Resolved project using relaxed name match:")
    LOGGER.info("Configured search:")
    LOGGER.info("%s", configured_name)
    LOGGER.info("iProperty:")
    LOGGER.info("%s", project.name)
    LOGGER.info("Project ID:")
    LOGGER.info("%s", project.project_id or "<unset>")
    LOGGER.info("Match reason:")
    LOGGER.info("%s", reason)


def resolve_iproperty_project(
    html: str,
    search_url: str,
    search_name: str,
    configured_project_name: str | None = None,
    configured_project_id: str | None = None,
) -> IPropertyProject:
    candidates = iproperty_project_candidates(html, search_url)
    projects = _deduplicate_rent_projects(candidates)
    configured_name = configured_project_name or search_name

    def matching_projects(predicate: Callable[[str], bool]) -> list[list[ProjectCandidate]]:
        return [group for group in projects if any(predicate(row.name) for row in group)]

    def accept_unique(
        matches: list[list[ProjectCandidate]], reason: str, relaxed: bool = False
    ) -> IPropertyProject | None:
        if len(matches) == 1:
            if relaxed:
                _log_relaxed_project_match(configured_name, matches[0], reason)
            return _project_from_group(matches[0])
        if len(matches) > 1:
            raise ScraperError(
                f'SKIPPED: multiple plausible project IDs remain for "{search_name}".\n\n'
                f"Candidates:\n{format_project_candidates(candidates)}\n\nConfig not modified."
            )
        return None

    if configured_project_id:
        result = accept_unique(
            [group for group in projects if any(
                row.project_id == configured_project_id for row in group
            )],
            "configured project ID",
        )
        if result:
            return result
    else:
        exact_target = compact_text(configured_name).casefold()
        result = accept_unique(
            matching_projects(
                lambda name: compact_text(name).casefold() == exact_target
            ),
            "exact case-insensitive project name",
        )
        if result:
            return result

        normalized_target = normalize_project_name(configured_name)
        result = accept_unique(
            matching_projects(
                lambda name: normalize_project_name(name) == normalized_target
            ),
            "normalized project name",
            relaxed=True,
        )
        if result:
            return result

        decorated_target = decoration_location_project_name(configured_name)
        configured_distinctive = distinctive_project_tokens(configured_name)
        decoration_is_specific = (
            len(projects) == 1
            or len(configured_distinctive) >= 2
            or any(token.isdigit() for token in configured_distinctive)
        )
        result = accept_unique(
            matching_projects(
                lambda name: decoration_is_specific and bool(decorated_target)
                and decoration_location_project_name(name) == decorated_target
            ),
            "decoration / location qualifier normalized",
            relaxed=True,
        )
        if result:
            return result

        if len(projects) == 1:
            configured_tokens = configured_distinctive
            aliases = projects[0]
            related = any(
                configured_tokens
                and distinctive_project_tokens(row.name) == configured_tokens
                for row in aliases
            )
            if related:
                _log_relaxed_project_match(
                    configured_name, aliases, "unique candidate with matching distinctive tokens"
                )
                return _project_from_group(aliases)

    configured = (
        f"Search name: {search_name}\n"
        f"Project name: {configured_project_name or '<unset>'}\n"
        f"Project ID: {configured_project_id or '<unset>'}"
    )
    failure_reason = (
        "multiple plausible project IDs remain"
        if len(projects) > 1
        else "no candidate passed relaxed project-name matching"
    )
    raise ScraperError(
        f'SKIPPED: {failure_reason} for "{search_name}".\n\n'
        f"Configured:\n{configured}\n\nCandidate projects found:\n"
        f"{format_project_candidates(candidates)}\n\nConfig not modified."
    )


def discover_pagination_url(
    html: str,
    current_url: str,
    page_number: int,
    expected_condo: str,
    expected_project_id: str | None = None,
    search_mode: str = "project",
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

    if search_mode == "freetext":
        return iproperty_freetext_search_url(expected_condo, page_number)

    if not expected_project_id:
        try:
            expected_project_id = resolve_iproperty_project(
                html, current_url, expected_condo
            ).project_id
        except ScraperError:
            return None
    if not expected_project_id:
        return None

    query = {
        "isCommercial": str(bool(search_params.get("isCommercial", False))).lower(),
        "_freetextDisplay": expected_condo,
        "propertyId": expected_project_id,
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


def normalize_furnishing(value: Any) -> str | None:
    text = compact_text(value)
    if re.search(r"\bfully[ -]?furnished\b", text, re.I):
        return "Fully Furnished"
    if re.search(r"\b(?:partially|partly|semi)[ -]?furnished\b", text, re.I):
        return "Partially Furnished"
    if re.search(r"\bunfurnished\b", text, re.I):
        return "Unfurnished"
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


def _explicit_name(value: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        name = compact_text(value.get(key))
        if name:
            return name
    return None


def search_listing_agent_names(
    listing_data: dict[str, Any],
) -> tuple[str | None, str | None]:
    agent_name = _explicit_name(
        listing_data, ("agentName", "advertiserName", "contactName")
    )
    agency_name = _explicit_name(listing_data, ("agencyName", "companyName"))
    contact_containers = (
        listing_data.get("agent"), listing_data.get("advertiser"),
        listing_data.get("contact"), listing_data.get("contactInfo"),
        listing_data.get("contactData"), listing_data.get("lister"),
    )
    for contact in contact_containers:
        if agent_name is None:
            agent_name = _explicit_name(
                contact, ("agentName", "fullName", "displayName", "name")
            )
        if agency_name is None and isinstance(contact, dict):
            agency_name = _explicit_name(contact, ("agencyName", "companyName"))
            for key in ("agency", "company", "agencyData", "companyData"):
                if agency_name is None:
                    agency_name = _explicit_name(
                        contact.get(key),
                        ("agencyName", "companyName", "displayName", "name"),
                    )
    return agent_name, agency_name


def parse_search_results(
    html: str,
    search_url: str,
    expected_condo: str,
    expected_project: IPropertyProject | None = None,
    should_process_photos: Callable[[str], bool] | None = None,
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
    if expected_project is None:
        expected_project = resolve_iproperty_project(
            html, search_url, expected_condo
        )

    output = []
    for wrapper in listings_data:
        listing = wrapper.get("listingData") if isinstance(wrapper, dict) else None
        if not isinstance(listing, dict):
            continue
        relative_url = compact_text(listing.get("url"))
        relative_url_match = LISTING_PATH_RE.search(urlparse(relative_url).path)
        listing_id = relative_url_match.group(1) if relative_url_match else ""
        project_name, project_id, property_type, transaction_type = (
            _wrapper_project_details(wrapper)
        )
        title = compact_text(listing.get("localizedTitle"))
        address = compact_text(listing.get("fullAddress"))
        project_matches = (
            project_id == expected_project.project_id
            if expected_project.project_id
            else project_name.casefold() == expected_project.name.casefold()
        )
        if (
            not re.fullmatch(r"\d{7,}", listing_id)
            or relative_url_match is None
            or not project_matches
            or property_type.casefold() != "condominium"
            or transaction_type != "RENT"
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
        furnishing = normalize_furnishing(furnish_text)
        furnished = (
            "No" if furnishing == "Unfurnished"
            else "Yes" if furnishing is not None else None
        )

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
        photos = (
            search_listing_photos(listing, listing_id)
            if should_process_photos is None or should_process_photos(listing_id)
            else []
        )
        agent_name, agency_name = search_listing_agent_names(listing)

        output.append(NormalizedListing(
            source_listing_id=listing_id,
            source_url=source_url,
            source_agent_name=agent_name,
            source_agency_name=agency_name,
            price_rent=parse_number(price_data.get("value") or price_data.get("localeStringValue")),
            beds=parse_number(listing.get("bedrooms")),
            baths=parse_number(listing.get("bathrooms")),
            sq_ft=parse_number(listing.get("floorArea")),
            description=description or None,
            furnished=furnished,
            furnishing=furnishing,
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

    furnishing = normalize_furnishing(visible)
    furnished = (
        "No" if furnishing == "Unfurnished"
        else "Yes" if furnishing is not None else None
    )
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
        furnished=furnished,
        furnishing=furnishing,
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
    def __init__(
        self,
        token: str,
        base_url: str = BUBBLE_BASE_URL,
        live_writes_enabled: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.live_writes_enabled = live_writes_enabled
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
        if self.live_writes_enabled:
            assert_live_endpoint(self.base_url)
        else:
            assert_development_endpoint(self.base_url + "/")
        LOGGER.info(
            "Outgoing Bubble Listing payload:\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        if existing_id:
            response = self.session.patch(
                f"{self.base_url}/obj/listing/{existing_id}", json=payload, timeout=self.timeout
            )
            if not response.ok:
                self._json(response)
            LOGGER.info("Bubble update succeeded: HTTP %d", response.status_code)
            LOGGER.info("Updated existing Bubble Listing: %s", existing_id)
            return existing_id
        response = self.session.post(
            f"{self.base_url}/obj/listing", json=payload, timeout=self.timeout
        )
        if response.ok and not (response.content or b"").strip():
            raise ScraperError(
                "Bubble create succeeded but returned no Listing ID (empty response body)."
            )
        data = self._json(response)
        new_id = data.get("id") or data.get("_id")
        if not new_id:
            raise ScraperError("Bubble create succeeded but returned no Listing ID.")
        return new_id


def bubble_payload(
    item: NormalizedListing, condo_id: str, include_photos: bool = True
) -> dict[str, Any]:
    mapping = {
        "sourceAgentName": item.source_agent_name,
        "sourceAgencyName": item.source_agency_name,
        "priceRent": item.price_rent,
        "beds": item.beds,
        "baths": item.baths,
        "Sq Ft": item.sq_ft,
        "Description": item.description,
        "furnished": item.furnished,
        "Furnishing": item.furnishing,
        "balcony": item.balcony,
        "maid room": item.maid_room,
        "study": item.study,
        "family room": item.family_room,
        "outdoor area": item.outdoor_area,
        "availability": item.availability,
        "unitNumber": item.unit_number,
        "Video URL": item.video_url,
        "keyFacts": item.keyfacts,
        "sourceURL": item.source_url,
        "sourceListingID": item.source_listing_id,
        "condo": condo_id,
        "propertyType": "Condo",
        "TransactionType": ["Rent/Let"],
        "scraped?": True,
    }
    if include_photos:
        mapping["coverPhoto"] = item.cover_photo_url
        mapping["photos"] = item.photo_urls or None
    return {key: value for key, value in mapping.items() if value is not None}


def existing_photo_count(existing: dict[str, Any] | None) -> int:
    if not existing:
        return 0
    photos = existing.get("photos")
    return len(photos) if isinstance(photos, list) and photos else 0


def process_condo(
    config: CondoConfig,
    args: argparse.Namespace,
    bubble: BubbleClient,
    client: IPropertyClient,
    limit: int | None,
) -> dict[str, int]:
    LOGGER.info("Requested condo: %s", config.requested_name)
    LOGGER.info("Config:")
    LOGGER.info("Bubble name: %s", config.bubble_name)
    LOGGER.info("iProperty search name: %s", config.iproperty_search_name)
    search_mode = config.iproperty_search_mode or "project"
    LOGGER.info("iProperty search mode: %s", search_mode)
    LOGGER.info("iProperty project name: %s", config.iproperty_project_name or "<unset>")
    LOGGER.info(
        "iProperty project ID: %s",
        config.iproperty_project_id or ("<not required>" if search_mode == "freetext" else "<unset>"),
    )
    LOGGER.info("Looking up Bubble Condo: %s", config.bubble_name)
    condo_id, condo_field = bubble.find_condo(config.bubble_name)
    LOGGER.info("Found Bubble Condo: %s (matched field: %s)", condo_id, condo_field)

    if args.search_url:
        search_url = args.search_url
    elif search_mode == "freetext":
        search_url = iproperty_freetext_search_url(config.iproperty_search_name)
    elif config.iproperty_project_id:
        LOGGER.info("Configured project ID found: %s", config.iproperty_project_id)
        LOGGER.info("Skipping project discovery.")
        search_url = iproperty_project_search_url(
            config.iproperty_search_name, config.iproperty_project_id
        )
        LOGGER.info("Using direct project-filtered search URL:")
        LOGGER.info("%s", search_url)
        LOGGER.info("Loading page 1...")
    else:
        search_url = iproperty_search_url(config.iproperty_search_name)
    LOGGER.info("Searching iProperty for: %s", config.iproperty_search_name)
    LOGGER.info("Search URL: %s", search_url)

    stats = {
        "found": 0, "created": 0, "updated": 0, "failed": 0,
        "identity_discovered": 0, "photos_processed": 0, "photos_skipped": 0,
    }
    configured_project_id = config.iproperty_project_id
    if configured_project_id:
        LOGGER.info("Using configured iProperty project ID: %s", configured_project_id)

    def persist_resolved_project(project: IPropertyProject) -> None:
        if search_mode == "freetext" or configured_project_id or not project.project_id:
            return
        LOGGER.info("Caching discovered iProperty identity...")
        try:
            saved = cache_project_identity(
                config,
                project,
                Path(getattr(args, "condo_config_path", DEFAULT_CONDO_CONFIG_PATH)),
            )
            if saved:
                stats["identity_discovered"] = 1
                LOGGER.info("Saved and verified:")
                LOGGER.info("iproperty_project_name = %s", project.name)
                LOGGER.info("iproperty_project_id = %s", project.project_id)
            else:
                LOGGER.warning("Config not modified; a project ID is already present.")
        except ScraperError as error:
            LOGGER.error("CONFIG CACHE ERROR: %s", error)
            LOGGER.error("Identity was NOT persisted or verified.")

    discovery_only = bool(getattr(args, "discover_only", False))
    discovery_options = {"discovery_only": True} if discovery_only else {}
    existing_by_source_id: dict[str, dict[str, Any] | None | Exception] = {}

    def should_process_photos(source_listing_id: str) -> bool:
        if source_listing_id in existing_by_source_id:
            cached = existing_by_source_id[source_listing_id]
            return bool(
                getattr(args, "refresh_photos", False)
                or isinstance(cached, Exception)
                or not existing_photo_count(cached)
            )
        try:
            existing = bubble.find_listing(source_listing_id)
        except Exception as error:
            existing_by_source_id[source_listing_id] = error
            return True
        existing_by_source_id[source_listing_id] = existing
        return bool(
            getattr(args, "refresh_photos", False)
            or not existing_photo_count(existing)
        )

    if not discovery_only:
        discovery_options["should_process_photos"] = should_process_photos
    items = client.discover_listings(
        search_url, args.max_pages, config, limit, persist_resolved_project,
        **discovery_options,
    )
    if discovery_only:
        return stats
    stats["found"] = len(items)
    LOGGER.info("Found %d %s rental listings", len(items), config.requested_name)
    for index, item in enumerate(items, 1):
        LOGGER.info(
            "[%d/%d] Processing iProperty search result %s",
            index, len(items), item.source_listing_id,
        )
        try:
            existing = existing_by_source_id.get(item.source_listing_id)
            if isinstance(existing, Exception):
                raise existing
            if item.source_listing_id not in existing_by_source_id:
                existing = bubble.find_listing(item.source_listing_id)
            LOGGER.info("%s Bubble listing found", "Existing" if existing else "No existing")
            photo_count = existing_photo_count(existing)
            preserve_photos = bool(
                existing and photo_count and not getattr(args, "refresh_photos", False)
            )
            if preserve_photos:
                stats["photos_skipped"] += 1
                LOGGER.info("Existing photos found: %d", photo_count)
                LOGGER.info("Skipping photo processing")
                LOGGER.info(
                    "Parsed: RM%s | %s beds | %s baths | %s sq ft",
                    display_number(item.price_rent), display_number(item.beds),
                    display_number(item.baths), display_number(item.sq_ft),
                )
                LOGGER.info("Photos: preserved existing")
                LOGGER.info("Updating non-photo fields only...")
            else:
                stats["photos_processed"] += 1
                if existing:
                    if getattr(args, "refresh_photos", False):
                        LOGGER.info("Photo refresh forced")
                    else:
                        LOGGER.info("No existing photos found")
                LOGGER.info("Processing iProperty photos...")
                LOGGER.info(
                    "Parsed: RM%s | %s beds | %s baths | %s sq ft | %d photos",
                    display_number(item.price_rent), display_number(item.beds),
                    display_number(item.baths), display_number(item.sq_ft),
                    len(item.photo_urls),
                )
            payload = bubble_payload(item, condo_id, include_photos=not preserve_photos)
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
        except Exception as error:  # one malformed listing must not terminate the condo
            stats["failed"] += 1
            LOGGER.error("Failed search result %s: %s", item.source_listing_id, error)
    return stats


def log_condo_stats(stats: dict[str, int]) -> None:
    LOGGER.info("Complete")
    LOGGER.info("Found: %d", stats["found"])
    LOGGER.info("Created: %d", stats["created"])
    LOGGER.info("Updated: %d", stats["updated"])
    LOGGER.info("Failed: %d", stats["failed"])
    LOGGER.info("Photos processed: %d listings", stats.get("photos_processed", 0))
    LOGGER.info("Photos skipped: %d listings", stats.get("photos_skipped", 0))


def run(args: argparse.Namespace) -> int:
    missing_project_id_only = bool(getattr(args, "missing_project_id_only", False))
    discovery_only = bool(getattr(args, "discover_only", False))
    if (missing_project_id_only or discovery_only) and (
        getattr(args, "condo", None) or getattr(args, "discover", None)
    ):
        raise ScraperError(
            "--missing-project-id-only and --discover-only are batch-mode options."
        )

    if args.discover:
        if getattr(args, "start", None) is not None or getattr(args, "count", None) is not None:
            raise ScraperError("--start and --count are only valid with --all-condos or --group.")
        search_name = compact_text(args.discover)
        with IPropertyClient(args.delay, args.timeout) as client:
            _, candidates = client.discover_projects(search_name)
        print("\nCandidate projects:\n")
        if not candidates:
            print("<none>")
        for index, item in enumerate(candidates, 1):
            print(
                f"{index}. {item.name}\n"
                f"   Project ID: {item.project_id or '<unset>'}\n"
                f"   Property type: {item.property_type or '<unknown>'}\n"
                f"   Transaction: {item.transaction_type or '<unknown>'}\n"
                f"   Listings: {item.listing_count}\n"
                f"   Example: {item.example_url or '<unavailable>'}"
            )
        return 0

    live = bool(getattr(args, "live", False))
    confirm_live_write = bool(getattr(args, "confirm_live_write", False))
    live_write_requested = live and not args.dry_run and not discovery_only
    if live_write_requested and not confirm_live_write:
        raise SafetyError("Live Bubble writes require --confirm-live-write")
    if confirm_live_write and not live:
        raise SafetyError("--confirm-live-write requires --live")

    if live:
        bubble_base_url = BUBBLE_LIVE_BASE_URL
        if args.dry_run or discovery_only:
            LOGGER.warning("=" * 50)
            LOGGER.warning("LIVE BUBBLE DATABASE - DRY RUN")
            LOGGER.warning("=" * 50)
            LOGGER.warning("Writes enabled: NO")
        else:
            LOGGER.warning("=" * 50)
            LOGGER.warning("WARNING: LIVE BUBBLE DATABASE")
            LOGGER.warning("=" * 50)
            LOGGER.warning("Mode: LIVE")
            LOGGER.warning("Writes enabled: YES")
    else:
        bubble_base_url = getattr(args, "bubble_base_url", BUBBLE_BASE_URL)

    config_path = Path(
        getattr(args, "condo_config_path", DEFAULT_CONDO_CONFIG_PATH)
    ).expanduser().resolve()
    args.condo_config_path = config_path
    LOGGER.info("Config file: %s", config_path)
    all_configs = load_condo_configs(config_path)
    if args.condo:
        if getattr(args, "start", None) is not None or getattr(args, "count", None) is not None:
            raise ScraperError("--start and --count are only valid with --all-condos or --group.")
        config = load_condo_config(args.condo, config_path)
        targets = [config]
        batch = False
    elif getattr(args, "all_condos", False):
        targets = select_batch_condos(all_configs)
        batch = True
    else:
        group = compact_text(getattr(args, "group", None)).casefold()
        targets = select_batch_condos(all_configs, group)
        if not targets:
            raise ScraperError(
                f'No enabled condo configurations found for group "{getattr(args, "group", None)}".'
            )
        batch = True

    batch_scope = list(targets)
    if batch and missing_project_id_only:
        targets = select_missing_project_id_condos(targets)
        if not targets:
            raise ScraperError("No enabled project-mode condos are missing a project ID.")

    positioned_targets: list[tuple[int, CondoConfig]] = []
    available_target_count = len(targets)
    if batch:
        positioned_targets = slice_batch_condos(
            targets, getattr(args, "start", None), getattr(args, "count", None)
        )
        selected_start = positioned_targets[0][0]
        selected_end = positioned_targets[-1][0]
        if missing_project_id_only:
            resolved_count = sum(
                bool(compact_text(config.iproperty_project_id))
                for config in batch_scope
                if (config.iproperty_search_mode or "project") != "freetext"
            )
            freetext_count = sum(
                (config.iproperty_search_mode or "project") == "freetext"
                for config in batch_scope
            )
            LOGGER.info("Discovery batch selection:")
            LOGGER.info("Enabled condos: %d", len(batch_scope))
            LOGGER.info("Already resolved project IDs: %d", resolved_count)
            LOGGER.info("Free-text condos excluded: %d", freetext_count)
            LOGGER.info("Missing project IDs eligible: %d", available_target_count)
            LOGGER.info("Selected range: %d-%d", selected_start, selected_end)
            LOGGER.info("Targets selected: %d", len(positioned_targets))
            LOGGER.info("Selected unresolved condos:")
        else:
            LOGGER.info("Batch selection:")
            LOGGER.info("Enabled targets available: %d", available_target_count)
            LOGGER.info("Start: %d", selected_start)
            LOGGER.info("Count: %s", getattr(args, "count", None) or "<through end>")
            LOGGER.info("Selected: %d", len(positioned_targets))
            LOGGER.info("Selected condos:")
        for position, selected_config in positioned_targets:
            LOGGER.info("%d. %s", position, selected_config.requested_name)

    if live:
        assert_live_endpoint(bubble_base_url)
    else:
        assert_development_endpoint(bubble_base_url + "/")
    token = os.environ.get("BUBBLE_API_TOKEN")
    if not token:
        raise ScraperError("BUBBLE_API_TOKEN is required for development Condo/listing reads.")
    bubble = BubbleClient(
        token, bubble_base_url, live_writes_enabled=live_write_requested
    )
    LOGGER.info("Environment: %s", "LIVE" if live else "development")
    LOGGER.info(
        "Bubble endpoint verified: %s", "LIVE" if live else "version-test"
    )
    if args.dry_run:
        LOGGER.info("DRY RUN: no Bubble writes will be performed")
    if discovery_only:
        LOGGER.info("DISCOVERY ONLY: Bubble Listings will not be parsed or written")

    if not batch:
        with IPropertyClient(args.delay, args.timeout) as client:
            stats = process_condo(targets[0], args, bubble, client, args.limit)
        log_condo_stats(stats)
        return 0 if stats["failed"] == 0 else 1

    summaries: list[tuple[str, dict[str, int] | None, str | None]] = []
    aggregate = {
        "found": 0, "created": 0, "updated": 0, "failed": 0,
        "photos_processed": 0, "photos_skipped": 0,
    }
    selected_count = len(positioned_targets)
    for batch_index, (portfolio_position, config) in enumerate(positioned_targets, 1):
        LOGGER.info("=" * 50)
        LOGGER.info(
            "CONDO %d/%d: %s", portfolio_position, available_target_count,
            config.requested_name,
        )
        LOGGER.info("BATCH ITEM %d/%d", batch_index, selected_count)
        LOGGER.info("=" * 50)
        LOGGER.info("Launching fresh Chromium for %s...", config.requested_name)
        try:
            with IPropertyClient(args.delay, args.timeout) as client:
                stats = process_condo(
                    config, args, bubble, client, getattr(args, "limit_per_condo", None)
                )
            summaries.append((config.requested_name, stats, None))
            for key in aggregate:
                aggregate[key] += stats[key]
            if not discovery_only:
                log_condo_stats(stats)
            LOGGER.info("Finished %s", config.requested_name)
        except Exception as error:
            reason = str(error)
            if "No exact Condo match" in reason:
                LOGGER.error("SKIPPED: Bubble Condo not found: %s", config.bubble_name)
            else:
                LOGGER.error("SKIPPED: %s", reason)
            summaries.append((config.requested_name, None, reason))
        finally:
            LOGGER.info("Closing Chromium for %s...", config.requested_name)
        if batch_index < selected_count:
            wait_seconds = random.uniform(4.0, 7.5)
            LOGGER.info("Waiting %.1fs before next condo...", wait_seconds)
            time.sleep(wait_seconds)

    processed = sum(stats is not None for _, stats, _ in summaries)
    if discovery_only:
        identities = [
            (name, stats) for name, stats, _ in summaries
            if stats is not None and stats.get("identity_discovered", 0)
        ]
        unresolved = [
            name for name, stats, _ in summaries
            if stats is None or not stats.get("identity_discovered", 0)
        ]
        LOGGER.info("DISCOVERY BATCH COMPLETE")
        LOGGER.info("Eligible unresolved condos: %d", available_target_count)
        LOGGER.info("Targets attempted: %d", selected_count)
        LOGGER.info("Project identities discovered: %d", len(identities))
        LOGGER.info("Still unresolved: %d", len(unresolved))
        if identities:
            LOGGER.info("New identities cached:")
            configs_by_name = {
                config.requested_name: config for _, config in positioned_targets
            }
            for name, _ in identities:
                cached = configs_by_name[name]
                LOGGER.info(
                    "%s → %s | %s", name, cached.iproperty_project_name,
                    cached.iproperty_project_id,
                )
        if unresolved:
            LOGGER.info("Still unresolved:")
            for name in unresolved:
                LOGGER.info("%s", name)
        return 0

    LOGGER.info("BATCH COMPLETE")
    LOGGER.info("Portfolio:")
    LOGGER.info("Enabled condos: %d", available_target_count)
    LOGGER.info("Range attempted: %d-%d", selected_start, selected_end)
    LOGGER.info("Targets selected: %d", selected_count)
    LOGGER.info("Targets processed: %d", processed)
    LOGGER.info("Targets skipped: %d", selected_count - processed)
    LOGGER.info("Listings found: %d", aggregate["found"])
    LOGGER.info("Created: %d", aggregate["created"])
    LOGGER.info("Updated: %d", aggregate["updated"])
    LOGGER.info("Failed: %d", aggregate["failed"])
    LOGGER.info("Photos processed: %d listings", aggregate["photos_processed"])
    LOGGER.info("Photos skipped: %d listings", aggregate["photos_skipped"])
    identities = [
        (name, stats) for name, stats, _ in summaries
        if stats is not None and stats.get("identity_discovered", 0)
    ]
    LOGGER.info("Project identities discovered: %d", len(identities))
    if identities:
        LOGGER.info("New iProperty identities cached:")
        configs_by_name = {
            config.requested_name: config for _, config in positioned_targets
        }
        for name, _ in identities:
            cached = configs_by_name[name]
            LOGGER.info(
                "%s → %s | %s", name, cached.iproperty_project_name,
                cached.iproperty_project_id,
            )
    for name, stats, reason in summaries:
        if stats is None:
            LOGGER.info("%s: skipped | %s", name, reason)
        else:
            LOGGER.info(
                "%s: found %d | created %d | updated %d | failed %d",
                name, stats["found"], stats["created"], stats["updated"], stats["failed"],
            )
    return 0 if aggregate["failed"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--condo", help="Configured requested condo name")
    mode.add_argument("--all-condos", action="store_true", help="Process every enabled configured condo")
    mode.add_argument("--group", help="Process enabled condos in a configured group")
    mode.add_argument("--discover", metavar="SEARCH_TEXT", help="List iProperty project candidates without Bubble access")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read the selected Bubble environment but perform no writes",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Use the live Bubble database instead of version-test",
    )
    parser.add_argument(
        "--confirm-live-write", action="store_true",
        help="Explicitly confirm writes to the live Bubble database",
    )
    parser.add_argument(
        "--search-url",
        help="Override the derived iProperty rental project URL when names/slugs differ",
    )
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--limit", type=int, help="Process at most this many discovered listings")
    parser.add_argument("--limit-per-condo", type=int, help="Batch limit for each condo")
    parser.add_argument("--start", type=int, help="1-based first enabled condo in a batch")
    parser.add_argument("--count", type=int, help="Maximum number of condos in a batch")
    parser.add_argument(
        "--refresh-photos", action="store_true",
        help="Include photos when updating existing Bubble Listings",
    )
    parser.add_argument(
        "--missing-project-id-only", action="store_true",
        help="Batch only enabled project-mode condos without an iProperty project ID",
    )
    parser.add_argument(
        "--discover-only", action="store_true",
        help="Resolve/cache project identities without processing Bubble Listings",
    )
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
