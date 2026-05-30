-- Flat Finder — SQLite schema
-- Run via: python -m app.cli init-db

-- =========================================================================
-- Sources (registered websites / facebook groups)
-- =========================================================================
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_name       TEXT    NOT NULL UNIQUE,
    source_type     TEXT    NOT NULL DEFAULT 'normal_listing_site',
    base_url        TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sources_site_name ON sources(site_name);

-- =========================================================================
-- Raw listings — exactly as scraped from the site
-- =========================================================================
CREATE TABLE IF NOT EXISTS listings_raw (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    external_id         TEXT,
    listing_url         TEXT    NOT NULL,
    canonical_url       TEXT    NOT NULL,
    raw_title           TEXT,
    raw_price_text      TEXT,
    raw_location_text   TEXT,
    raw_description     TEXT,
    raw_layout_text     TEXT,
    raw_area_text       TEXT,
    raw_fees_text       TEXT,
    raw_deposit_text    TEXT,
    raw_image_url       TEXT,
    raw_image_urls_json TEXT,
    raw_metadata_json   TEXT,
    listing_hash        TEXT    NOT NULL DEFAULT '',
    first_seen_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    scraped_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    is_active           INTEGER NOT NULL DEFAULT 1
);

-- Primary dedup: same source + same canonical URL = same listing
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_source_url
    ON listings_raw(source_id, canonical_url);

-- Secondary lookup by external_id when available
CREATE INDEX IF NOT EXISTS idx_raw_external_id
    ON listings_raw(source_id, external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_listing_hash
    ON listings_raw(listing_hash);

CREATE INDEX IF NOT EXISTS idx_raw_is_active
    ON listings_raw(is_active);

CREATE INDEX IF NOT EXISTS idx_raw_scraped_at
    ON listings_raw(scraped_at);

-- =========================================================================
-- Enriched listings — normalized by deterministic parsers + LLM
-- =========================================================================
CREATE TABLE IF NOT EXISTS listings_enriched (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_listing_id      INTEGER NOT NULL UNIQUE REFERENCES listings_raw(id),
    property_type       TEXT,
    offer_type          TEXT,
    price_value         REAL,
    currency            TEXT,
    fees_value          REAL,
    total_monthly_value REAL,
    deposit_value       REAL,
    district            TEXT,
    city                TEXT,
    address             TEXT,
    layout              TEXT,
    area_m2             REAL,
    furnished_status    TEXT,
    balcony             INTEGER,
    elevator            INTEGER,
    parking             INTEGER,
    pets_allowed        INTEGER,
    flatshare           INTEGER,
    room_private        INTEGER,
    owner_or_agency     TEXT,
    available_from      TEXT,
    validation_status   TEXT,
    suitability_score   INTEGER,
    commute_note        TEXT,
    llm_summary         TEXT,
    llm_json            TEXT,
    enriched_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_enriched_raw_id
    ON listings_enriched(raw_listing_id);

CREATE INDEX IF NOT EXISTS idx_enriched_district
    ON listings_enriched(district);

CREATE INDEX IF NOT EXISTS idx_enriched_price
    ON listings_enriched(price_value);

CREATE INDEX IF NOT EXISTS idx_enriched_score
    ON listings_enriched(suitability_score);

CREATE INDEX IF NOT EXISTS idx_enriched_layout
    ON listings_enriched(layout);

CREATE INDEX IF NOT EXISTS idx_enriched_validation
    ON listings_enriched(validation_status);

-- =========================================================================
-- Images — downloaded locally and linked to raw listings
-- =========================================================================
CREATE TABLE IF NOT EXISTS images (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_listing_id  INTEGER NOT NULL REFERENCES listings_raw(id),
    image_url       TEXT    NOT NULL,
    local_path      TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    downloaded_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_images_raw_id
    ON images(raw_listing_id);

-- =========================================================================
-- Scrape runs — audit trail for each scraping session
-- =========================================================================
CREATE TABLE IF NOT EXISTS scrape_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at       TEXT,
    source_id         INTEGER NOT NULL REFERENCES sources(id),
    status            TEXT    NOT NULL DEFAULT 'running',
    listings_seen     INTEGER NOT NULL DEFAULT 0,
    listings_inserted INTEGER NOT NULL DEFAULT 0,
    listings_updated  INTEGER NOT NULL DEFAULT 0,
    error_text        TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_source
    ON scrape_runs(source_id);

CREATE INDEX IF NOT EXISTS idx_runs_status
    ON scrape_runs(status);
