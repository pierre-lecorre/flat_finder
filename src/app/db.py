"""SQLite Database Manager for Flat Finder.

Handles connection pooling, initialization, thread-safe raw/enriched upserts,
and scrape run logging.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from app.config import AppSettings
from app.logging_config import get_logger
from app.models import (
    EnrichedListing,
    ImageRecord,
    RawListing,
    ScrapeRunRecord,
)

logger = get_logger("app.db")


class Database:
    """SQLite Database manager using context managers for safe transactions."""

    def __init__(self, settings: AppSettings) -> None:
        self.db_path = settings.db_path
        self.schema_path = Path(__file__).parent / "schema.sql"

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for SQLite connections with foreign keys and WAL mode."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            yield conn
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error("Database transaction error: %s", exc)
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """Create directories and run schema.sql DDL to initialize database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.schema_path.exists():
            raise FileNotFoundError(f"Schema DDL file not found at {self.schema_path}")

        logger.info("Initializing database at %s", self.db_path)
        with open(self.schema_path, "r", encoding="utf-8") as f:
            ddl = f.read()

        with self.connection() as conn:
            conn.executescript(ddl)
        logger.info("Database initialized successfully.")

    def upsert_source(self, site_name: str, source_type: str, base_url: str = "") -> int:
        """Upsert a listing source and return its database ID."""
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sources (site_name, source_type, base_url)
                VALUES (?, ?, ?)
                ON CONFLICT(site_name) DO UPDATE SET
                    source_type = excluded.source_type,
                    base_url = excluded.base_url
                RETURNING id;
                """,
                (site_name, source_type, base_url),
            )
            row = cursor.fetchone()
            if row:
                return row["id"]

            # Fallback if RETURNING has edge-case issues
            cursor = conn.execute("SELECT id FROM sources WHERE site_name = ?;", (site_name,))
            return cursor.fetchone()["id"]

    def upsert_raw_listing(self, raw: RawListing) -> tuple[int, bool]:
        """Upsert a raw listing.

        Dedups based on:
        1. (source_id, external_id) if external_id is present
        2. (source_id, canonical_url) otherwise

        Returns (listing_id, was_inserted).
        """
        was_inserted = False
        now_str = datetime.now(timezone.utc).isoformat()

        with self.connection() as conn:
            # Check by external_id if present
            existing_id = None
            if raw.external_id:
                cursor = conn.execute(
                    """
                    SELECT id FROM listings_raw
                    WHERE source_id = ? AND external_id = ?;
                    """,
                    (raw.source_id, raw.external_id),
                )
                row = cursor.fetchone()
                if row:
                    existing_id = row["id"]

            # Check by canonical_url if not found by external_id
            if not existing_id:
                cursor = conn.execute(
                    """
                    SELECT id FROM listings_raw
                    WHERE source_id = ? AND canonical_url = ?;
                    """,
                    (raw.source_id, raw.canonical_url),
                )
                row = cursor.fetchone()
                if row:
                    existing_id = row["id"]

            if existing_id:
                # Update existing row
                conn.execute(
                    """
                    UPDATE listings_raw SET
                        raw_title = ?,
                        raw_price_text = ?,
                        raw_location_text = ?,
                        raw_description = ?,
                        raw_layout_text = ?,
                        raw_area_text = ?,
                        raw_fees_text = ?,
                        raw_deposit_text = ?,
                        raw_image_url = ?,
                        raw_image_urls_json = ?,
                        raw_metadata_json = ?,
                        listing_hash = ?,
                        last_seen_at = ?,
                        scraped_at = ?,
                        is_active = 1
                    WHERE id = ?;
                    """,
                    (
                        raw.raw_title,
                        raw.raw_price_text,
                        raw.raw_location_text,
                        raw.raw_description,
                        raw.raw_layout_text,
                        raw.raw_area_text,
                        raw.raw_fees_text,
                        raw.raw_deposit_text,
                        raw.raw_image_url,
                        raw.raw_image_urls_json,
                        raw.raw_metadata_json,
                        raw.listing_hash,
                        now_str,
                        now_str,
                        existing_id,
                    ),
                )
                listing_id = existing_id
            else:
                # Insert brand new row
                cursor = conn.execute(
                    """
                    INSERT INTO listings_raw (
                        source_id, external_id, listing_url, canonical_url,
                        raw_title, raw_price_text, raw_location_text, raw_description,
                        raw_layout_text, raw_area_text, raw_fees_text, raw_deposit_text,
                        raw_image_url, raw_image_urls_json, raw_metadata_json,
                        listing_hash, first_seen_at, last_seen_at, scraped_at, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    RETURNING id;
                    """,
                    (
                        raw.source_id,
                        raw.external_id,
                        raw.listing_url,
                        raw.canonical_url,
                        raw.raw_title,
                        raw.raw_price_text,
                        raw.raw_location_text,
                        raw.raw_description,
                        raw.raw_layout_text,
                        raw.raw_area_text,
                        raw.raw_fees_text,
                        raw.raw_deposit_text,
                        raw.raw_image_url,
                        raw.raw_image_urls_json,
                        raw.raw_metadata_json,
                        raw.listing_hash,
                        now_str,
                        now_str,
                        now_str,
                    ),
                )
                listing_id = cursor.fetchone()["id"]
                was_inserted = True

        return listing_id, was_inserted

    def insert_enriched_listing(self, enriched: EnrichedListing) -> int:
        """Upsert/replace an enriched listing record."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO listings_enriched (
                    raw_listing_id, property_type, offer_type, price_value, currency,
                    fees_value, total_monthly_value, deposit_value, district, city,
                    address, layout, area_m2, furnished_status, balcony, elevator,
                    parking, pets_allowed, flatshare, room_private, owner_or_agency,
                    available_from, validation_status, suitability_score, commute_note,
                    llm_summary, llm_json, enriched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id;
                """,
                (
                    enriched.raw_listing_id,
                    enriched.property_type,
                    enriched.offer_type,
                    enriched.price_value,
                    enriched.currency,
                    enriched.fees_value,
                    enriched.total_monthly_value,
                    enriched.deposit_value,
                    enriched.district,
                    enriched.city,
                    enriched.address,
                    enriched.layout,
                    enriched.area_m2,
                    enriched.furnished_status,
                    1 if enriched.balcony else (0 if enriched.balcony is False else None),
                    1 if enriched.elevator else (0 if enriched.elevator is False else None),
                    1 if enriched.parking else (0 if enriched.parking is False else None),
                    1 if enriched.pets_allowed else (0 if enriched.pets_allowed is False else None),
                    1 if enriched.flatshare else (0 if enriched.flatshare is False else None),
                    1 if enriched.room_private else (0 if enriched.room_private is False else None),
                    enriched.owner_or_agency,
                    enriched.available_from,
                    enriched.validation_status,
                    enriched.suitability_score,
                    enriched.commute_note,
                    enriched.llm_summary,
                    enriched.llm_json,
                    now_str,
                ),
            )
            return cursor.fetchone()["id"]

    def insert_image(self, img: ImageRecord) -> int:
        """Insert a downloaded image record."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO images (raw_listing_id, image_url, local_path, is_primary, downloaded_at)
                VALUES (?, ?, ?, ?, ?)
                RETURNING id;
                """,
                (
                    img.raw_listing_id,
                    img.image_url,
                    img.local_path,
                    1 if img.is_primary else 0,
                    now_str,
                ),
            )
            return cursor.fetchone()["id"]

    def create_scrape_run(self, run: ScrapeRunRecord) -> int:
        """Create a scrape run record in DB to track progress."""
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO scrape_runs (started_at, source_id, status, listings_seen, listings_inserted, listings_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                RETURNING id;
                """,
                (
                    run.started_at.isoformat(),
                    run.source_id,
                    run.status,
                    run.listings_seen,
                    run.listings_inserted,
                    run.listings_updated,
                ),
            )
            return cursor.fetchone()["id"]

    def update_scrape_run(
        self,
        run_id: int,
        finished_at: datetime,
        status: str,
        counts: dict[str, int],
        error_text: Optional[str] = None,
    ) -> None:
        """Update finished scrape run record with final statistics."""
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE scrape_runs SET
                    finished_at = ?,
                    status = ?,
                    listings_seen = ?,
                    listings_inserted = ?,
                    listings_updated = ?,
                    error_text = ?
                WHERE id = ?;
                """,
                (
                    finished_at.isoformat(),
                    status,
                    counts.get("seen", 0),
                    counts.get("inserted", 0),
                    counts.get("updated", 0),
                    error_text,
                    run_id,
                ),
            )

    def get_raw_listings_for_enrichment(
        self, source_id: Optional[int] = None, limit: Optional[int] = None
    ) -> list[RawListing]:
        """Fetch raw listings that do not have an enriched listing counterpart yet."""
        query = """
            SELECT r.* FROM listings_raw r
            LEFT JOIN listings_enriched e ON r.id = e.raw_listing_id
            WHERE e.id IS NULL AND r.is_active = 1
        """
        params: list[Any] = []
        if source_id is not None:
            query += " AND r.source_id = ?"
            params.append(source_id)

        query += " ORDER BY r.scraped_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        listings = []
        with self.connection() as conn:
            cursor = conn.execute(query, params)
            for row in cursor.fetchall():
                # Convert SQLite row to Pydantic RawListing model
                data = dict(row)
                # Map SQLite text timestamps to datetime if present
                for key in ("first_seen_at", "last_seen_at", "scraped_at"):
                    if data.get(key) and isinstance(data[key], str):
                        try:
                            data[key] = datetime.fromisoformat(data[key])
                        except ValueError:
                            pass
                data["is_active"] = bool(data["is_active"])
                listings.append(RawListing(**data))

        return listings

    def get_all_enriched_for_export(self, source_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Fetch all enriched listings with raw fields merged for CSV/Excel export."""
        query = """
            SELECT
                r.canonical_url,
                r.listing_url,
                s.site_name,
                e.*
            FROM listings_enriched e
            JOIN listings_raw r ON e.raw_listing_id = r.id
            JOIN sources s ON r.source_id = s.id
        """
        params = []
        if source_id is not None:
            query += " WHERE r.source_id = ?"
            params.append(source_id)

        query += " ORDER BY e.suitability_score DESC, e.total_monthly_value ASC"

        with self.connection() as conn:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
