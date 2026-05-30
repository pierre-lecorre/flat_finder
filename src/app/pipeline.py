"""Orchestrator pipeline running scrapers, enrichment passes, and downloads.

Integrates database connection, browser wrapper, Ollama clients,
image downloaders, and CSV exports.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.browser import BrowserManager
from app.config import AppSettings, CommonConfig, load_common_config, get_enabled_site_configs
from app.db import Database
from app.images import download_listing_images
from app.llm.extractors import enrich_listing_with_llm
from app.llm.ollama_client import OllamaClient
from app.logging_config import get_logger
from app.models import EnrichedListing, ScrapeRunRecord
from app.normalization import normalize_raw_listing
from app.scrapers.site_registry import default_registry

logger = get_logger("app.pipeline")


class PipelineOrchestrator:
    """Manages full real-estate aggregation lifecycle."""

    def __init__(self, settings: AppSettings, common_config: CommonConfig) -> None:
        self.settings = settings
        self.common_config = common_config
        self.db = Database(settings)

    def run_scrape(self, site_name: Optional[str] = None) -> dict[str, int]:
        """Execute scraping phase for enabled or single selected site configs.

        Inserts raw rows to database, tracking scrape run metadata.
        """
        self.db.init_db()
        configs = get_enabled_site_configs(self.settings)

        if site_name and site_name != "all":
            # Filter specifically by name
            configs = [c for c in configs if c.site_name == site_name]
            if not configs:
                logger.error("No enabled site config found with name '%s'", site_name)
                return {"seen": 0, "inserted": 0, "updated": 0}

        stats = {"seen": 0, "inserted": 0, "updated": 0}

        # Initialize browser context
        with BrowserManager(self.settings) as bm:
            for site_cfg in configs:
                logger.info("Starting scrape run for site: %s", site_cfg.site_name)

                # 1. Upsert source registry record
                source_id = self.db.upsert_source(
                    site_name=site_cfg.site_name,
                    source_type=site_cfg.source_type,
                    base_url=site_cfg.start_urls[0] if site_cfg.start_urls else "",
                )

                # 2. Log scrape run started
                run_record = ScrapeRunRecord(source_id=source_id)
                run_id = self.db.create_scrape_run(run_record)

                run_stats = {"seen": 0, "inserted": 0, "updated": 0}
                status = "finished"
                error_text = None

                try:
                    # Resolve appropriate scraper adapter and inject context
                    scraper = default_registry.get_scraper(site_cfg, self.settings, bm)
                    scraper.source_id = source_id

                    # Execute scrape
                    raw_listings = scraper.scrape()
                    run_stats["seen"] = len(raw_listings)

                    for raw in raw_listings:
                        # SQLite Upsert
                        _, was_inserted = self.db.upsert_raw_listing(raw)
                        if was_inserted:
                            run_stats["inserted"] += 1
                        else:
                            run_stats["updated"] += 1

                    logger.info(
                        "Scraped '%s' completed successfully. Seen: %d, New: %d, Updated: %d",
                        site_cfg.site_name,
                        run_stats["seen"],
                        run_stats["inserted"],
                        run_stats["updated"],
                    )

                except Exception as exc:
                    logger.exception("Scraping execution failed for %s", site_cfg.site_name)
                    status = "failed"
                    error_text = str(exc)
                finally:
                    # Update audit run record
                    self.db.update_scrape_run(
                        run_id=run_id,
                        finished_at=datetime.utcnow(),
                        status=status,
                        counts=run_stats,
                        error_text=error_text,
                    )

                # Update global stats accumulator
                for k in stats:
                    stats[k] += run_stats[k]

        return stats

    def run_enrich(self, site_name: Optional[str] = None, limit: Optional[int] = None) -> dict[str, int]:
        """Execute phase 2: read un-enriched raw rows, apply rules, run Ollama, save enriched."""
        # 1. Load active Ollama credentials
        host = self.settings.ollama_host
        model = self.common_config.ollama_model or self.settings.ollama_model
        timeout = self.common_config.ollama_timeout or self.settings.ollama_timeout
        temp = self.common_config.ollama_temperature or self.settings.ollama_temperature

        client = OllamaClient(host=host, model=model, timeout=timeout, temperature=temp)

        if not client.is_available():
            logger.error(
                "Ollama server is unreachable or requested model '%s' is not pulled. "
                "Ensure Ollama is running ('ollama serve') and pull the model ('ollama pull %s').",
                model,
                model,
            )
            return {"enriched": 0, "failed": 0}

        # Resolve optional source_id filter
        source_id = None
        if site_name and site_name != "all":
            with self.db.connection() as conn:
                cursor = conn.execute("SELECT id FROM sources WHERE site_name = ?;", (site_name,))
                row = cursor.fetchone()
                if row:
                    source_id = row["id"]
                else:
                    logger.error("No source record found in DB for '%s'", site_name)
                    return {"enriched": 0, "failed": 0}

        # 2. Fetch raw listings needing enrichment
        raw_list = self.db.get_raw_listings_for_enrichment(source_id=source_id, limit=limit)
        logger.info("Found %d raw listings awaiting LLM enrichment.", len(raw_list))

        stats = {"enriched": 0, "failed": 0}

        for raw in raw_list:
            logger.info("Enriching listing #%d: %s", raw.id, raw.raw_title)

            # A. Execute deterministic normalization + Prague commute scoring first
            enriched = normalize_raw_listing(
                raw=raw,
                weights=self.common_config.scoring,
                default_city=self.common_config.default_city,
                default_currency=self.common_config.default_currency,
            )

            # B. Package dictionary of raw parameters for LLM context
            raw_data = {
                "title": raw.raw_title,
                "price_text": raw.raw_price_text,
                "location": raw.raw_location_text,
                "layout": raw.raw_layout_text,
                "area": raw.raw_area_text,
                "fees": raw.raw_fees_text,
                "deposit": raw.raw_deposit_text,
                "description": raw.raw_description,
            }

            # C. Call Ollama Client for JSON analysis
            llm_res = enrich_listing_with_llm(client, raw_data)

            if llm_res:
                # Merge LLM attributes into EnrichedListing, prioritizing LLM values on ambiguity
                if llm_res.property_type:
                    enriched.property_type = llm_res.property_type
                if llm_res.offer_type:
                    enriched.offer_type = llm_res.offer_type
                if llm_res.price_value:
                    enriched.price_value = llm_res.price_value
                if llm_res.currency:
                    enriched.currency = llm_res.currency
                if llm_res.fees_value:
                    enriched.fees_value = llm_res.fees_value
                if llm_res.total_monthly_cost:
                    enriched.total_monthly_value = llm_res.total_monthly_cost
                if llm_res.deposit_value:
                    enriched.deposit_value = llm_res.deposit_value
                if llm_res.district:
                    enriched.district = llm_res.district
                if llm_res.city:
                    enriched.city = llm_res.city
                if llm_res.address:
                    enriched.address = llm_res.address
                if llm_res.layout:
                    enriched.layout = llm_res.layout
                if llm_res.area_m2:
                    enriched.area_m2 = llm_res.area_m2
                if llm_res.furnished_status:
                    enriched.furnished_status = llm_res.furnished_status
                if llm_res.balcony is not None:
                    enriched.balcony = llm_res.balcony
                if llm_res.elevator is not None:
                    enriched.elevator = llm_res.elevator
                if llm_res.parking is not None:
                    enriched.parking = llm_res.parking
                if llm_res.pets_allowed is not None:
                    enriched.pets_allowed = llm_res.pets_allowed
                if llm_res.flatshare is not None:
                    enriched.flatshare = llm_res.flatshare
                if llm_res.room_private is not None:
                    enriched.room_private = llm_res.room_private
                if llm_res.owner_or_agency:
                    enriched.owner_or_agency = llm_res.owner_or_agency
                if llm_res.available_from:
                    enriched.available_from = llm_res.available_from
                if llm_res.validation_status:
                    enriched.validation_status = llm_res.validation_status

                enriched.llm_summary = llm_res.summary
                enriched.llm_json = llm_res.model_dump_json()

                # Re-calculate suitability score with LLM corrected attributes
                from app.normalization import compute_suitability_score
                enriched.suitability_score = compute_suitability_score(enriched, self.common_config.scoring)

                # Create commute notes dynamically
                from app.normalization import compute_commute_note
                enriched.commute_note = compute_commute_note(enriched.district)

            else:
                logger.warning(
                    "Listing enrichment failed to validate via LLM. "
                    "Saving intermediate deterministic fields."
                )
                stats["failed"] += 1

            # Save to listings_enriched
            self.db.insert_enriched_listing(enriched)
            stats["enriched"] += 1

        return stats

    def run_full(self, site_name: Optional[str] = None) -> dict[str, int]:
        """Execute full pipelined phase: Scrape -> LLM Enrich."""
        logger.info("Executing full real-estate pipelined aggregator...")
        scrape_stats = self.run_scrape(site_name)
        enrich_stats = self.run_enrich(site_name)
        return {**scrape_stats, **enrich_stats}

    def run_download_images(self, site_name: Optional[str] = None, max_images: int = 5) -> int:
        """Query DB for listings, download images to data folder, and persist links."""
        # Resolve source filter
        source_id = None
        if site_name and site_name != "all":
            with self.db.connection() as conn:
                cursor = conn.execute("SELECT id FROM sources WHERE site_name = ?;", (site_name,))
                row = cursor.fetchone()
                if row:
                    source_id = row["id"]

        # Fetch active raw listings that do not have downloaded images logged yet
        query = """
            SELECT r.* FROM listings_raw r
            LEFT JOIN images i ON r.id = i.raw_listing_id
            WHERE i.id IS NULL AND r.is_active = 1
        """
        params = []
        if source_id is not None:
            query += " AND r.source_id = ?"
            params.append(source_id)

        from app.models import RawListing
        raw_listings: list[RawListing] = []

        with self.db.connection() as conn:
            cursor = conn.execute(query, params)
            for row in cursor.fetchall():
                data = dict(row)
                data["is_active"] = bool(data["is_active"])
                raw_listings.append(RawListing(**data))

        logger.info("Found %d listings requiring image downloads.", len(raw_listings))
        download_count = 0

        for raw in raw_listings:
            logger.info("Downloading images for listing #%d...", raw.id)
            img_records = download_listing_images(
                raw_listing=raw,
                images_dir=self.settings.images_dir,
                max_images=max_images,
            )

            for rec in img_records:
                self.db.insert_image(rec)
                download_count += 1

        logger.info("Completed image downloads. Total files saved: %d", download_count)
        return download_count

    def export_csv(self, output_path: Path) -> None:
        """Query all Prague enriched listings, format columns, and export to CSV."""
        logger.info("Fetching enriched data for CSV export...")
        rows = self.db.get_all_enriched_for_export()

        if not rows:
            logger.warning("No enriched listing records found in database to export.")
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        headers = list(rows[0].keys())

        logger.info("Writing %d rows to CSV: %s", len(rows), output_path)
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

        logger.info("Export completed successfully.")
