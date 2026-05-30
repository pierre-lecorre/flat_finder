"""Abstract base scraper and shared card-field processing methods.

Implements standard methods for text extraction, URL absolute mapping,
white space cleanup, transform execution, and keyword filtering.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from app.browser import BrowserManager
from app.config import AppSettings, FieldSelector, FieldsConfig, SiteConfig
from app.logging_config import get_logger
from app.models import RawListing
from app.utils import absolutize_url, clean_whitespace, generate_listing_hash

logger = get_logger("app.scrapers.base")


class BaseScraper(ABC):
    """Abstract Base Class for all real-estate listing scrapers."""

    def __init__(
        self,
        site_config: SiteConfig,
        settings: AppSettings,
        browser_manager: BrowserManager,
    ) -> None:
        self.site_config = site_config
        self.settings = settings
        self.browser_manager = browser_manager
        self.source_id: Optional[int] = None  # Populated at run time by pipeline

    @abstractmethod
    def scrape(self) -> list[RawListing]:
        """Scrape listings from configured search pages and return them."""
        pass

    # -----------------------------------------------------------------------
    # Selector Extraction helpers
    # -----------------------------------------------------------------------

    def extract_field(self, parent: Any, selector: Optional[FieldSelector]) -> Optional[str]:
        """Extract value from card element using a FieldSelector config."""
        if not selector or not selector.selector:
            return None

        if selector.attr == "text":
            return self.browser_manager.extract_text(parent, selector.selector)
        else:
            return self.browser_manager.extract_attr(parent, selector.selector, selector.attr)

    def extract_fields_from_card(self, card: Any, fields: FieldsConfig, base_url: str) -> dict[str, Any]:
        """Extract a dictionary of listing properties from an individual listing card."""
        data: dict[str, Any] = {}

        # Core fields mapping
        mappings = {
            "title": fields.title,
            "raw_price_text": fields.price_text,
            "raw_location_text": fields.location,
            "raw_layout_text": fields.layout,
            "raw_area_text": fields.area,
            "raw_fees_text": fields.fees,
            "raw_deposit_text": fields.deposit,
            "raw_image_url": fields.image_url,
            "listing_url": fields.listing_url,
            "external_id": fields.external_id,
            "raw_description": fields.description,
        }

        for field_name, selector in mappings.items():
            if selector:
                val = self.extract_field(card, selector)
                data[field_name] = val
            else:
                data[field_name] = None

        return data

    # -----------------------------------------------------------------------
    # Transforms & Filters
    # -----------------------------------------------------------------------

    def apply_transforms(self, data: dict[str, Any], base_url: str) -> dict[str, Any]:
        """Apply cleanup, whitespace normalization, and URL canonicalization."""
        cfg = self.site_config.transforms

        # 1. Clean whitespace
        if cfg.clean_whitespace:
            for k, v in data.items():
                if isinstance(v, str):
                    data[k] = clean_whitespace(v)

        # 2. Make URLs absolute
        if cfg.absolutize_urls:
            for k in ("listing_url", "raw_image_url"):
                if data.get(k):
                    data[k] = absolutize_url(data[k], base_url)

        return data

    def apply_filters(self, data: dict[str, Any]) -> bool:
        """Validate if listing matches required fields or passes keyword blocks."""
        cfg = self.site_config.filters

        # 1. Required fields check
        for req in cfg.required_fields:
            if not data.get(req):
                logger.debug("Listing filtered out: missing required field '%s'", req)
                return False

        # 2. Exclude keywords check
        if cfg.exclude_keywords and data.get("raw_description"):
            desc = data["raw_description"].lower()
            for kw in cfg.exclude_keywords:
                if kw.lower() in desc:
                    logger.debug("Listing filtered out: contains excluded keyword '%s'", kw)
                    return False

        # 3. Include keywords check
        if cfg.include_keywords and data.get("raw_description"):
            desc = data["raw_description"].lower()
            matched = False
            for kw in cfg.include_keywords:
                if kw.lower() in desc:
                    matched = True
                    break
            if not matched:
                logger.debug("Listing filtered out: does not match include keywords")
                return False

        return True

    def build_raw_listing(self, data: dict[str, Any], source_id: int) -> RawListing:
        """Convert extracted dictionary to a validated Pydantic RawListing model."""
        # Clean canonical URL for dedup
        listing_url = data.get("listing_url") or ""
        canonical_url = listing_url
        if self.site_config.transforms.strip_query_params_from_url:
            # Strip query parameters from URL
            if "?" in canonical_url:
                canonical_url = canonical_url.split("?")[0]

        # Calculate a deterministic listing hash
        listing_hash = generate_listing_hash(
            source_id=source_id,
            title=data.get("title"),
            price_text=data.get("raw_price_text"),
            location_text=data.get("raw_location_text"),
        )

        raw = RawListing(
            source_id=source_id,
            external_id=data.get("external_id"),
            listing_url=listing_url,
            canonical_url=canonical_url,
            raw_title=data.get("title"),
            raw_price_text=data.get("raw_price_text"),
            raw_location_text=data.get("raw_location_text"),
            raw_description=data.get("raw_description"),
            raw_layout_text=data.get("raw_layout_text"),
            raw_area_text=data.get("raw_area_text"),
            raw_fees_text=data.get("raw_fees_text"),
            raw_deposit_text=data.get("raw_deposit_text"),
            raw_image_url=data.get("raw_image_url"),
            listing_hash=listing_hash,
        )

        # Store arbitrary original fields into metadata
        raw.set_metadata(
            {
                "original_title": data.get("title"),
                "scraped_via": self.site_config.site_name,
            }
        )

        return raw
