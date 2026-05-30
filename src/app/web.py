"""FastAPI backend server for the Flat Finder localhost dashboard.

Queries raw/enriched databases, applies dynamic filters (price, district,
layouts, amenities), and handles user exclusion POST triggers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from app.config import AppSettings, load_common_config
from app.db import Database
from app.logging_config import get_logger, setup_logging
from app.models import ValidationStatus
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

logger = get_logger("app.web")

# 1. Initialize core contexts
settings = AppSettings.from_env()
settings.ensure_directories()
setup_logging(level=settings.log_level, log_file=settings.log_file)

common_cfg = load_common_config(settings)
db = Database(settings)

app = FastAPI(
    title="Flat Finder Local Dashboard",
    description="Locally-hosted aggregation dashboard for Prague flat hunting.",
    version="0.1.0",
)


# Serve primary/extra images locally via static mount
images_abs_path = settings.images_dir.resolve()
if images_abs_path.exists():
    app.mount("/images", StaticFiles(directory=str(images_abs_path)), name="images")


@app.get("/", response_class=HTMLResponse)
def get_dashboard() -> str:
    """Serve the central single-page dashboard HTML."""
    html_path = Path(__file__).parent / "web" / "index.html"
    if not html_path.exists():
        logger.error("Dashboard static HTML file not found at %s", html_path)
        raise HTTPException(
            status_code=404,
            detail="Dashboard static files not found. Ensure src/app/web/index.html exists.",
        )

    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/listings")
def get_listings(
    q: Optional[str] = Query(None, description="Search term in title, location, or description"),
    site_name: Optional[str] = Query(None, description="Filter by source site name"),
    property_type: Optional[str] = Query(None, description="flat, room, house"),
    offer_type: Optional[str] = Query(None, description="rent, sale, flatshare"),
    min_price: Optional[float] = Query(None, description="Minimum price value"),
    max_price: Optional[float] = Query(None, description="Maximum price value"),
    min_area: Optional[float] = Query(None, description="Minimum square meters area"),
    layout: Optional[list[str]] = Query(None, description="Multi-selection filter for standard layouts"),
    district: Optional[str] = Query(None, description="Praha 1, Praha 8, Karlín etc."),
    furnished: Optional[str] = Query(None, description="yes, no, unknown"),
    balcony: Optional[bool] = Query(None, description="Has balcony/terrace"),
    elevator: Optional[bool] = Query(None, description="Building has elevator"),
    parking: Optional[bool] = Query(None, description="Parking available"),
    pets: Optional[bool] = Query(None, description="Pets allowed"),
    flatshare: Optional[bool] = Query(None, description="Is shared apartment"),
    exclude_inactive: bool = Query(True, description="Hide listings with EXCLUDED status"),
    sort_by: str = Query("score_desc", description="score_desc, price_asc, price_desc, area_desc, date_desc"),
) -> list[dict]:
    """Fetch enriched real-estate listings with dynamically built search/filter SQL queries."""
    # Base query combining enriched variables with source names and raw images
    query = """
        SELECT
            e.*,
            r.canonical_url,
            r.listing_url,
            r.raw_title,
            r.raw_price_text,
            r.raw_location_text,
            r.raw_description,
            r.raw_image_url,
            r.raw_image_urls_json,
            s.site_name,
            s.source_type
        FROM listings_enriched e
        JOIN listings_raw r ON e.raw_listing_id = r.id
        JOIN sources s ON r.source_id = s.id
        WHERE r.is_active = 1
    """
    params: list = []

    # Dynamic search keyword parsing
    if q:
        # Search title, location, description, or layout
        query += " AND (r.raw_title LIKE ? OR r.raw_description LIKE ? OR r.raw_location_text LIKE ? OR e.layout LIKE ?)"
        term = f"%{q}%"
        params.extend([term, term, term, term])

    # Category filters
    if site_name:
        query += " AND s.site_name = ?"
        params.append(site_name)

    if property_type:
        query += " AND e.property_type = ?"
        params.append(property_type)

    if offer_type:
        query += " AND e.offer_type = ?"
        params.append(offer_type)

    if min_price is not None:
        # Check against monthly total first, fallback to raw price
        query += " AND COALESCE(e.total_monthly_value, e.price_value) >= ?"
        params.append(min_price)

    if max_price is not None:
        query += " AND COALESCE(e.total_monthly_value, e.price_value) <= ?"
        params.append(max_price)

    if min_area is not None:
        query += " AND e.area_m2 >= ?"
        params.append(min_area)

    if layout:
        # Match standard layout list (e.g. 1+kk, 2+kk)
        placeholders = ", ".join("?" for _ in layout)
        query += f" AND e.layout IN ({placeholders})"
        params.extend(layout)

    if district:
        query += " AND (e.district LIKE ? OR r.raw_location_text LIKE ?)"
        params.extend([f"%{district}%", f"%{district}%"])

    if furnished:
        query += " AND e.furnished_status = ?"
        params.append(furnished)

    # Boolean filters
    if balcony is not None:
        query += " AND e.balcony = ?"
        params.append(1 if balcony else 0)

    if elevator is not None:
        query += " AND e.elevator = ?"
        params.append(1 if elevator else 0)

    if parking is not None:
        query += " AND e.parking = ?"
        params.append(1 if parking else 0)

    if pets is not None:
        query += " AND e.pets_allowed = ?"
        params.append(1 if pets else 0)

    if flatshare is not None:
        query += " AND e.flatshare = ?"
        params.append(1 if flatshare else 0)

    if exclude_inactive:
        # Hide listings that LLM or User marked as EXCLUDED
        query += " AND e.validation_status != ?"
        params.append(ValidationStatus.EXCLUDED.value)

    # Sort rules
    if sort_by == "score_desc":
        query += " ORDER BY e.suitability_score DESC, e.total_monthly_value ASC"
    elif sort_by == "price_asc":
        query += " ORDER BY COALESCE(e.total_monthly_value, e.price_value) ASC"
    elif sort_by == "price_desc":
        query += " ORDER BY COALESCE(e.total_monthly_value, e.price_value) DESC"
    elif sort_by == "area_desc":
        query += " ORDER BY e.area_m2 DESC"
    elif sort_by == "date_desc":
        query += " ORDER BY e.enriched_at DESC"
    else:
        query += " ORDER BY e.suitability_score DESC"

    # Execute search query
    results = []
    with db.connection() as conn:
        cursor = conn.execute(query, params)
        for row in cursor.fetchall():
            item = dict(row)
            # Map booleans cleanly
            for b_key in ("balcony", "elevator", "parking", "pets_allowed", "flatshare", "room_private"):
                if item.get(b_key) is not None:
                    item[b_key] = bool(item[b_key])

            # Get downloaded local image links
            raw_id = item["raw_listing_id"]
            img_query = "SELECT image_url, local_path, is_primary FROM images WHERE raw_listing_id = ?"
            img_cursor = conn.execute(img_query, (raw_id,))
            img_records = [dict(img_row) for img_row in img_cursor.fetchall()]

            # Enforce local images lookup URLs
            item["downloaded_images"] = img_records
            results.append(item)

    return results


@app.post("/api/listings/{enriched_id}/exclude")
def exclude_listing(enriched_id: int) -> dict:
    """Let user manually exclude a listing directly from the UI dashboard."""
    with db.connection() as conn:
        # Verify exists
        cursor = conn.execute("SELECT raw_listing_id FROM listings_enriched WHERE id = ?;", (enriched_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Enriched listing not found.")

        raw_listing_id = row["raw_listing_id"]

        # 1. Update listings_enriched validation status to EXCLUDED, set suitability score to 0
        conn.execute(
            """
            UPDATE listings_enriched SET
                validation_status = ?,
                suitability_score = 0
            WHERE id = ?;
            """,
            (ValidationStatus.EXCLUDED.value, enriched_id),
        )

        # 2. De-activate raw listing
        conn.execute(
            """
            UPDATE listings_raw SET
                is_active = 0
            WHERE id = ?;
            """,
            (raw_listing_id,),
        )

    logger.info("Successfully excluded listing enriched_id=%d from active aggregator results.", enriched_id)
    return {"status": "success", "message": f"Listing {enriched_id} excluded and deactivated."}


@app.get("/api/sources")
def get_sources() -> list[dict]:
    """Fetch registered sources with listing counts for source filters."""
    query = """
        SELECT
            s.id,
            s.site_name,
            s.source_type,
            (SELECT COUNT(*) FROM listings_raw r WHERE r.source_id = s.id AND r.is_active = 1) as active_count
        FROM sources s
        ORDER BY s.site_name ASC
    """
    with db.connection() as conn:
        cursor = conn.execute(query)
        return [dict(row) for row in cursor.fetchall()]


def start_web_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Boot FastAPI app using Uvicorn locally."""
    import uvicorn
    logger.info("Starting localhost dashboard server at http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
