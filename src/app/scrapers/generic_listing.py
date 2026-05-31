"""Config-driven generic real-estate listing scraper.

Two-phase approach for hybrid/detail-page mode:
  Phase 1 – collect all listing URLs across paginated search results
             (no detail pages touched, no stale element risk).
  Phase 2 – visit each collected URL individually and extract detail fields.

For card-only mode (extraction_mode: card_only) the original single-pass
behaviour is preserved.
"""

from __future__ import annotations

import re
import time
from typing import Any

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

from app.config import ExtractionMode, PaginationType
from app.logging_config import get_logger
from app.models import RawListing
from app.scrapers.base import BaseScraper

logger = get_logger("app.scrapers.generic")

# Both known listing-path fragments (cz + com domains)
_LISTING_PATH_RE = re.compile(
    r"/(?:properties-flats-houses|nemovitosti-byty-domy)/\d+-"
)


class GenericListingScraper(BaseScraper):
    """Universal scraper driven by CSS selectors from a YAML config."""

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def scrape(self) -> list[RawListing]:
        if not self.source_id:
            raise ValueError("Scraper launched without source_id configured.")

        start_urls = list(self.site_config.start_urls)
        if not start_urls and self.site_config.search_url_templates:
            tpl = self.site_config.search_url_templates[0]
            start_urls.append(
                tpl.replace("{page}", str(self.site_config.pagination.start_page))
            )

        if not start_urls:
            logger.warning(
                "No search URLs or templates configured for %s.",
                self.site_config.site_name,
            )
            return []

        driver = self.browser_manager.new_page()
        listings: list[RawListing] = []

        try:
            for base_url in start_urls:
                listings.extend(self._scrape_search_url(driver, base_url))
        finally:
            self._safe_close_tab(driver)

        logger.info(
            "Scraping completed. Found %d valid listings.", len(listings)
        )
        return listings

    # ------------------------------------------------------------------
    # Per-search-URL orchestration
    # ------------------------------------------------------------------

    def _scrape_search_url(
        self, driver: uc.Chrome, start_url: str
    ) -> list[RawListing]:
        mode = self.site_config.extraction_mode
        use_detail = (
            mode in (ExtractionMode.DETAIL_PAGES, ExtractionMode.HYBRID)
            and self.site_config.detail_page.enabled
        )

        if use_detail:
            # --- TWO-PHASE: collect URLs first, then visit each detail page ---
            listing_urls = self._collect_listing_urls(driver, start_url)
            logger.info(
                "Phase 1 complete: collected %d unique listing URLs.",
                len(listing_urls),
            )
            return self._visit_detail_pages(driver, listing_urls)
        else:
            # --- SINGLE-PASS: extract everything from cards only ---
            return self._scrape_cards_only(driver, start_url)

    # ------------------------------------------------------------------
    # Phase 1: collect all listing URLs across paginated search results
    # ------------------------------------------------------------------

    def _collect_listing_urls(
        self, driver: uc.Chrome, start_url: str
    ) -> list[str]:
        """Navigate all search pages and return deduplicated listing URLs.

        Only the href attribute is read while the listing page is still
        loaded — no detail-page navigation happens here.
        """
        pag = self.site_config.pagination
        link_selector = self.site_config.detail_page.link_selector
        if not link_selector:
            logger.error(
                "detail_page.link_selector is empty for %s — cannot collect URLs.",
                self.site_config.site_name,
            )
            return []

        collected: list[str] = []
        seen: set[str] = set()
        current_url = start_url
        current_page_idx = pag.start_page

        for page_num in range(1, pag.max_pages + 1):
            logger.info(
                "[Phase 1] Page %d/%d — %s",
                page_num,
                pag.max_pages,
                current_url,
            )

            try:
                self.browser_manager.navigate(
                    driver, current_url, self.site_config.wait_rules
                )
            except Exception as exc:
                logger.error(
                    "Navigation failed to search URL %s: %s", current_url, exc
                )
                self.browser_manager.screenshot_on_error(
                    driver,
                    f"nav_error_{self.site_config.site_name}_p{page_num}",
                    self.settings.screenshots_dir,
                )
                break

            self.browser_manager.polite_delay(
                self.site_config.rate_limit.min_delay_ms,
                self.site_config.rate_limit.max_delay_ms,
            )

            if pag.type == PaginationType.INFINITE_SCROLL:
                self.browser_manager.scroll_n_times(driver, 5, pause_ms=1000)

            # Collect all matching hrefs from this page
            # link_selector may be a comma-separated list of selectors
            page_urls: list[str] = []
            for sel in [s.strip() for s in link_selector.split(",") if s.strip()]:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in elements:
                        href = el.get_attribute("href") or ""
                        href = href.strip()
                        # Validate it's a real listing URL (not header/footer nav)
                        if href and _LISTING_PATH_RE.search(href):
                            page_urls.append(href)
                except Exception as exc:
                    logger.warning(
                        "Selector %r failed on page %d: %s", sel, page_num, exc
                    )

            # Deduplicate and strip query params if configured
            for url in page_urls:
                clean = url
                if self.site_config.transforms.strip_query_params_from_url:
                    clean = clean.split("?")[0]
                if clean not in seen:
                    seen.add(clean)
                    collected.append(clean)

            logger.info(
                "[Phase 1] Page %d: found %d new URLs (total so far: %d)",
                page_num,
                len([u for u in page_urls if u.split("?")[0] not in seen - {u.split("?")[0]}]),
                len(collected),
            )

            # Advance to next page
            if page_num >= pag.max_pages:
                break

            next_url = self._next_page_url(
                driver, current_url, current_page_idx, pag
            )
            if next_url is None:
                break
            current_url = next_url
            current_page_idx += 1

        return collected

    # ------------------------------------------------------------------
    # Phase 2: visit each collected URL and extract detail fields
    # ------------------------------------------------------------------

    def _visit_detail_pages(
        self, driver: uc.Chrome, urls: list[str]
    ) -> list[RawListing]:
        """Navigate to each listing URL in turn and build RawListings."""
        listings: list[RawListing] = []

        for idx, url in enumerate(urls, start=1):
            logger.info(
                "[Phase 2] Detail page %d/%d: %s", idx, len(urls), url
            )
            try:
                self.browser_manager.navigate(
                    driver, url, self.site_config.wait_rules
                )
                self.browser_manager.polite_delay(
                    self.site_config.rate_limit.min_delay_ms,
                    self.site_config.rate_limit.max_delay_ms,
                )

                # Extract detail fields from the loaded page
                data = self.extract_fields_from_card(
                    driver, self.site_config.detail_fields, url
                )
                data = self.apply_transforms(data, url)

                # listing_url comes from the URL we navigated to
                if not data.get("listing_url"):
                    data["listing_url"] = url

                # Extract external_id from URL path if not already set
                if not data.get("external_id"):
                    m = _LISTING_PATH_RE.search(url)
                    if m:
                        id_match = re.search(
                            r"/(?:properties-flats-houses|nemovitosti-byty-domy)/(\d+)-",
                            url,
                        )
                        if id_match:
                            data["external_id"] = id_match.group(1)

                if not self.apply_filters(data):
                    logger.debug("Listing filtered out: %s", url)
                    continue

                raw = self.build_raw_listing(data, self.source_id or 0)
                listings.append(raw)

            except Exception as exc:
                logger.warning(
                    "Failed to process detail page %s: %s", url, exc
                )

        return listings

    # ------------------------------------------------------------------
    # Card-only single-pass (extraction_mode: card_only)
    # ------------------------------------------------------------------

    def _scrape_cards_only(
        self, driver: uc.Chrome, start_url: str
    ) -> list[RawListing]:
        """Legacy single-pass card extraction (no detail pages)."""
        pag = self.site_config.pagination
        card_selector = self.site_config.listing_card_selector
        if not card_selector:
            logger.error(
                "No listing_card_selector configured for %s",
                self.site_config.site_name,
            )
            return []

        listings: list[RawListing] = []
        current_url = start_url
        current_page_idx = pag.start_page

        for page_num in range(1, pag.max_pages + 1):
            logger.info(
                "Scraping cards page %d — %s", page_num, current_url
            )
            try:
                self.browser_manager.navigate(
                    driver, current_url, self.site_config.wait_rules
                )
            except Exception as exc:
                logger.error("Navigation failed: %s", exc)
                break

            self.browser_manager.polite_delay(
                self.site_config.rate_limit.min_delay_ms,
                self.site_config.rate_limit.max_delay_ms,
            )

            if pag.type == PaginationType.INFINITE_SCROLL:
                self.browser_manager.scroll_n_times(driver, 5, pause_ms=1000)

            cards = driver.find_elements(By.CSS_SELECTOR, card_selector)
            logger.info("Found %d cards on page %d", len(cards), page_num)
            if not cards:
                break

            for card in cards:
                card_data = self.extract_fields_from_card(
                    card, self.site_config.fields, current_url
                )
                card_data = self.apply_transforms(card_data, current_url)
                if not self.apply_filters(card_data):
                    continue
                listings.append(self.build_raw_listing(card_data, self.source_id or 0))

            if page_num >= pag.max_pages:
                break

            next_url = self._next_page_url(
                driver, current_url, current_page_idx, pag
            )
            if next_url is None:
                break
            current_url = next_url
            current_page_idx += 1

        return listings

    # ------------------------------------------------------------------
    # Pagination helpers
    # ------------------------------------------------------------------

    def _next_page_url(
        self,
        driver: uc.Chrome,
        current_url: str,
        current_page_idx: int,
        pag,
    ) -> str | None:
        """Return the URL for the next page, or None if pagination ends."""
        next_idx = current_page_idx + 1

        if pag.type == PaginationType.PAGE_PARAM:
            param = pag.page_param_name
            # If a template is defined, always prefer it
            if self.site_config.search_url_templates:
                return self.site_config.search_url_templates[0].replace(
                    "{page}", str(next_idx)
                )
            # Otherwise mutate the current URL's query string
            if f"{param}=" in current_url:
                return re.sub(
                    rf"({re.escape(param)}=)\d+",
                    rf"\g<1>{next_idx}",
                    current_url,
                )
            # Append the param (handles both ?existing=1 and bare URLs)
            sep = "&" if "?" in current_url else "?"
            return f"{current_url}{sep}{param}={next_idx}"

        elif pag.type == PaginationType.NEXT_BUTTON:
            next_btn_sel = pag.next_button_selector
            if not next_btn_sel:
                logger.warning("next_button_selector is empty.")
                return None
            try:
                btn = driver.find_element(By.CSS_SELECTOR, next_btn_sel)
                if btn.is_displayed() and btn.is_enabled():
                    prev_url = driver.current_url
                    btn.click()
                    time.sleep(2)
                    new_url = driver.current_url
                    if new_url == prev_url:
                        logger.info("URL unchanged after next-click — pagination ended.")
                        return None
                    return new_url
                logger.info("Next button not visible/enabled — pagination ended.")
                return None
            except Exception as exc:
                logger.warning("Failed to click next-page button: %s", exc)
                return None

        return None  # PaginationType.NONE or unknown

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_close_tab(driver: uc.Chrome) -> None:
        try:
            handles = driver.window_handles
            if len(handles) > 1:
                driver.close()
                driver.switch_to.window(handles[0])
        except Exception:
            pass
