"""Pydantic domain models for the Flat Finder application.

These models are shared across all modules — scrapers, database, LLM, and CLI.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ValidationStatus(str, Enum):
    """Status assigned after LLM enrichment + validation pass."""
    VERIFIED = "VERIFIED"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    UTILITIES_UNCONFIRMED = "UTILITIES_UNCONFIRMED"
    TOTAL_PRICE_UNCONFIRMED = "TOTAL_PRICE_UNCONFIRMED"
    EXCLUDED = "EXCLUDED"


class SourceType(str, Enum):
    NORMAL_LISTING_SITE = "normal_listing_site"
    FACEBOOK_GROUP = "facebook_group"
    CUSTOM = "custom"


class FurnishedStatus(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class OfferType(str, Enum):
    RENT = "rent"
    SALE = "sale"
    FLATSHARE = "flatshare"


class PropertyType(str, Enum):
    FLAT = "flat"
    ROOM = "room"
    STUDIO = "studio"
    HOUSE = "house"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Raw listing — stored after scraping, before LLM enrichment
# ---------------------------------------------------------------------------

class RawListing(BaseModel):
    """One scraped listing, stored as-is from the website."""

    id: Optional[int] = None
    source_id: int
    external_id: Optional[str] = None
    listing_url: str
    canonical_url: str
    raw_title: Optional[str] = None
    raw_price_text: Optional[str] = None
    raw_location_text: Optional[str] = None
    raw_description: Optional[str] = None
    raw_layout_text: Optional[str] = None
    raw_area_text: Optional[str] = None
    raw_fees_text: Optional[str] = None
    raw_deposit_text: Optional[str] = None
    raw_image_url: Optional[str] = None
    raw_image_urls_json: Optional[str] = None
    raw_metadata_json: Optional[str] = None
    listing_hash: str = ""
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = True

    def set_image_urls(self, urls: list[str]) -> None:
        """Serialize a list of image URLs to JSON."""
        self.raw_image_urls_json = json.dumps(urls) if urls else None

    def get_image_urls(self) -> list[str]:
        """Deserialize stored image URLs."""
        if not self.raw_image_urls_json:
            return []
        return json.loads(self.raw_image_urls_json)

    def set_metadata(self, meta: dict) -> None:
        """Serialize arbitrary metadata dict to JSON."""
        self.raw_metadata_json = json.dumps(meta, ensure_ascii=False) if meta else None

    def get_metadata(self) -> dict:
        """Deserialize stored metadata."""
        if not self.raw_metadata_json:
            return {}
        return json.loads(self.raw_metadata_json)


# ---------------------------------------------------------------------------
# Enriched listing — populated after LLM + deterministic normalization
# ---------------------------------------------------------------------------

class EnrichedListing(BaseModel):
    """Normalized / enriched listing produced from a RawListing."""

    id: Optional[int] = None
    raw_listing_id: int
    property_type: Optional[str] = None
    offer_type: Optional[str] = None
    price_value: Optional[float] = None
    currency: Optional[str] = None
    fees_value: Optional[float] = None
    total_monthly_value: Optional[float] = None
    deposit_value: Optional[float] = None
    district: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    layout: Optional[str] = None
    area_m2: Optional[float] = None
    furnished_status: Optional[str] = None
    balcony: Optional[bool] = None
    elevator: Optional[bool] = None
    parking: Optional[bool] = None
    pets_allowed: Optional[bool] = None
    flatshare: Optional[bool] = None
    room_private: Optional[bool] = None
    owner_or_agency: Optional[str] = None
    available_from: Optional[str] = None
    validation_status: Optional[str] = None
    suitability_score: Optional[int] = None
    commute_note: Optional[str] = None
    llm_summary: Optional[str] = None
    llm_json: Optional[str] = None
    enriched_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# LLM response schema — what we ask Ollama to produce
# ---------------------------------------------------------------------------

class LLMEnrichmentResponse(BaseModel):
    """JSON schema for structured Ollama output.

    The JSON schema of this model is injected into the system prompt so the
    LLM knows exactly what fields to populate.
    """

    property_type: Optional[str] = Field(
        None, description="flat, room, studio, house, other"
    )
    offer_type: Optional[str] = Field(
        None, description="rent, sale, flatshare"
    )
    price_value: Optional[float] = Field(
        None, description="Numeric price (monthly rent or sale price)"
    )
    currency: Optional[str] = Field(
        None, description="CZK, EUR, USD, etc."
    )
    fees_value: Optional[float] = Field(
        None, description="Monthly fees/utilities if mentioned"
    )
    total_monthly_cost: Optional[float] = Field(
        None, description="Total monthly cost including rent + fees if derivable"
    )
    deposit_value: Optional[float] = Field(
        None, description="Deposit amount if mentioned"
    )
    district: Optional[str] = Field(
        None, description="City district, e.g. Praha 8, Karlín"
    )
    city: Optional[str] = Field(
        None, description="City name, default Prague"
    )
    address: Optional[str] = Field(
        None, description="Street address if available"
    )
    layout: Optional[str] = Field(
        None, description="Normalized layout: studio, 1+kk, 1+1, 2+kk, 2+1, 3+kk, room, etc."
    )
    area_m2: Optional[float] = Field(
        None, description="Area in square metres"
    )
    furnished_status: Optional[str] = Field(
        None, description="yes, no, partially, unknown"
    )
    balcony: Optional[bool] = Field(None, description="Has balcony/terrace")
    elevator: Optional[bool] = Field(None, description="Building has elevator")
    parking: Optional[bool] = Field(None, description="Parking available")
    pets_allowed: Optional[bool] = Field(None, description="Pets allowed")
    flatshare: Optional[bool] = Field(
        None, description="Is this a flatshare/shared apartment"
    )
    room_private: Optional[bool] = Field(
        None, description="If flatshare, is the room private"
    )
    owner_or_agency: Optional[str] = Field(
        None, description="owner, agency, unknown"
    )
    available_from: Optional[str] = Field(
        None, description="Availability date as text or ISO date"
    )
    validation_status: Optional[str] = Field(
        None,
        description=(
            "VERIFIED, PRICE_MISMATCH, UTILITIES_UNCONFIRMED, "
            "TOTAL_PRICE_UNCONFIRMED, EXCLUDED"
        ),
    )
    summary: Optional[str] = Field(
        None, description="2-3 sentence listing summary"
    )
    suitability_notes: Optional[str] = Field(
        None,
        description="Notes about suitability for a working adult looking in Prague",
    )


# ---------------------------------------------------------------------------
# Supporting records
# ---------------------------------------------------------------------------

class ImageRecord(BaseModel):
    """A downloaded image linked to a raw listing."""

    id: Optional[int] = None
    raw_listing_id: int
    image_url: str
    local_path: Optional[str] = None
    is_primary: bool = False
    downloaded_at: Optional[datetime] = None


class ScrapeRunRecord(BaseModel):
    """Metadata for one scraping run against a source."""

    id: Optional[int] = None
    source_id: int
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    status: str = "running"
    listings_seen: int = 0
    listings_inserted: int = 0
    listings_updated: int = 0
    error_text: Optional[str] = None


class SourceRecord(BaseModel):
    """Registered scraping source (website / facebook group)."""

    id: Optional[int] = None
    site_name: str
    source_type: str
    base_url: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
