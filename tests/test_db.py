"""Unit tests for SQLite database CRUD and upsert dedup operations."""

from __future__ import annotations

from datetime import datetime
import pytest
from app.config import AppSettings
from app.db import Database
from app.models import EnrichedListing, RawListing, ScrapeRunRecord


@pytest.fixture
def temp_db(tmp_path) -> Database:
    """Fixture initializing temporary sqlite Database instance."""
    settings = AppSettings.from_env()
    # Force file path inside standard Pytest temp directory
    settings.db_path = tmp_path / "test_flat_finder.db"
    db = Database(settings)
    db.init_db()
    return db


def test_upsert_source(temp_db: Database) -> None:
    source_id = temp_db.upsert_source(
        site_name="sreality",
        source_type="normal_listing_site",
        base_url="https://sreality.cz",
    )
    assert source_id == 1

    # Conflict check: upserting same source returns same ID
    source_id_dup = temp_db.upsert_source(
        site_name="sreality",
        source_type="normal_listing_site",
        base_url="https://sreality.cz/updated",
    )
    assert source_id_dup == 1

    with temp_db.connection() as conn:
        cursor = conn.execute("SELECT base_url FROM sources WHERE id = 1;")
        assert cursor.fetchone()["base_url"] == "https://sreality.cz/updated"


def test_upsert_raw_listing(temp_db: Database) -> None:
    source_id = temp_db.upsert_source("sreality", "normal_listing_site")

    raw = RawListing(
        source_id=source_id,
        external_id="12345",
        listing_url="https://example.com/flat1",
        canonical_url="https://example.com/flat1",
        raw_title="Krásný byt 1+kk",
        raw_price_text="15 000 Kč",
    )

    # 1. First insert
    listing_id, inserted = temp_db.upsert_raw_listing(raw)
    assert listing_id == 1
    assert inserted is True

    # 2. Duplicate insert matching on canonical_url -> performs update
    raw.raw_title = "Krásný byt 1+kk - Upravený titulek"
    listing_id_dup, inserted_dup = temp_db.upsert_raw_listing(raw)
    assert listing_id_dup == 1
    assert inserted_dup is False

    with temp_db.connection() as conn:
        cursor = conn.execute("SELECT raw_title FROM listings_raw WHERE id = 1;")
        assert cursor.fetchone()["raw_title"] == "Krásný byt 1+kk - Upravený titulek"


def test_upsert_raw_external_id_match(temp_db: Database) -> None:
    source_id = temp_db.upsert_source("sreality", "normal_listing_site")

    # Insert first raw record
    raw1 = RawListing(
        source_id=source_id,
        external_id="ABC",
        listing_url="https://example.com/flat_old_url",
        canonical_url="https://example.com/flat_old_url",
        raw_title="Flat Old Url",
    )
    temp_db.upsert_raw_listing(raw1)

    # Upsert second raw listing with same external_id but different canonical_url
    raw2 = RawListing(
        source_id=source_id,
        external_id="ABC",
        listing_url="https://example.com/flat_new_url",
        canonical_url="https://example.com/flat_new_url",
        raw_title="Flat New Url",
    )
    listing_id, inserted = temp_db.upsert_raw_listing(raw2)

    # Should match on external_id and perform UPDATE instead of INSERT
    assert listing_id == 1
    assert inserted is False

    with temp_db.connection() as conn:
        cursor = conn.execute("SELECT raw_title FROM listings_raw WHERE id = 1;")
        assert cursor.fetchone()["raw_title"] == "Flat New Url"


def test_enriched_listing_upsert(temp_db: Database) -> None:
    source_id = temp_db.upsert_source("sreality", "normal_listing_site")

    raw = RawListing(
        source_id=source_id,
        listing_url="https://example.com/flat",
        canonical_url="https://example.com/flat",
        raw_title="Krásný byt 1+kk",
    )
    raw_id, _ = temp_db.upsert_raw_listing(raw)

    enriched = EnrichedListing(
        raw_listing_id=raw_id,
        property_type="flat",
        offer_type="rent",
        price_value=12000.0,
        currency="CZK",
        total_monthly_value=15000.0,
        district="Libeň",
        layout="1+kk",
        area_m2=30.0,
        suitability_score=80,
    )

    enriched_id = temp_db.insert_enriched_listing(enriched)
    assert enriched_id == 1

    # Check query helper gets raw listings awaiting enrichment
    raw_awaiting = temp_db.get_raw_listings_for_enrichment()
    # Already enriched, so should be empty
    assert len(raw_awaiting) == 0


def test_scrape_run_tracking(temp_db: Database) -> None:
    source_id = temp_db.upsert_source("sreality", "normal_listing_site")

    run = ScrapeRunRecord(source_id=source_id)
    run_id = temp_db.create_scrape_run(run)
    assert run_id == 1

    counts = {"seen": 10, "inserted": 3, "updated": 7}
    temp_db.update_scrape_run(
        run_id=run_id,
        finished_at=datetime.utcnow(),
        status="finished",
        counts=counts,
    )

    with temp_db.connection() as conn:
        cursor = conn.execute("SELECT status, listings_inserted FROM scrape_runs WHERE id = 1;")
        row = cursor.fetchone()
        assert row["status"] == "finished"
        assert row["listings_inserted"] == 3
