"""Config-driven generic real-estate listing scraper.

Operates over multi-site search configurations, handles paginated cards,
loads detail pages for deeper fields, and produces RawListings.
"""

from __future__ import annotations

from typing import Any

from app.config import ExtractionMode, PaginationType
from app.logging_config import get_logger
from app.models import RawListing
from app.scrapers.base import BaseScraper
from playwright.sync_api import Page

logger = get_logger("app.scrapers.generic")


class GenericListingScraper(BaseScraper):
    """Universal scraper that navigates search pages and cards based on CSS selectors."""

    def scrape(self) -> list[RawListing]:
        if not self.source_id:
            raise ValueError("Scraper launched without source_id configured.")

        # Determine start pages
        urls = list(self.site_config.start_urls)
        if not urls and self.site_config.search_url_templates:
            # Generate start URLs from templates using page=1
            for tmpl in self.site_config.search_url_templates:
                start_page = self.site_config.pagination.start_page
                urls.append(tmpl.replace("{page}", str(start_page)))

        if not urls:
            logger.warning("No search URLs or templates configured for %s.", self.site_config.site_name)
            return []

        listings: list[RawListing] = []
        page = self.browser_manager.new_page()

        try:
            for base_search_url in urls:
                current_listings = self._scrape_search_url(page, base_search_url)
                listings.extend(current_listings)
        finally:
            page.close()

        logger.info("Scraping completed. Found %d valid listings.", len(listings))
        return listings

    def _scrape_search_url(self, page: Page, start_url: str) -> list[RawListing]:
        """Iterate through pages for a specific search root and extract listings."""
        listings: list[RawListing] = []
        pag_cfg = self.site_config.pagination
        max_pages = pag_cfg.max_pages

        current_page_idx = pag_cfg.start_page
        current_url = start_url

        for page_num in range(1, max_pages + 1):
            logger.info("Scraping page %d for %s: %s", page_num, self.site_config.site_name, current_url)
            try:
                self.browser_manager.navigate(page, current_url, self.site_config.wait_rules)
            except Exception as exc:
                logger.error("Navigation failed to search URL %s: %s", current_url, exc)
                self.browser_manager.screenshot_on_error(page, f"nav_error_{self.site_config.site_name}", self.settings.screenshots_dir)
                break

            # Polite pause for load-settle
            self.browser_manager.polite_delay(
                self.site_config.rate_limit.min_delay_ms,
                self.site_config.rate_limit.max_delay_ms,
            )

            # Handle infinite scroll if pagination type is scroll
            if pag_cfg.type == PaginationType.INFINITE_SCROLL:
                self.browser_manager.scroll_n_times(page, 5, pause_ms=1000)

            # Find card nodes
            card_selector = self.site_config.listing_card_selector
            if not card_selector:
                logger.error("No listing_card_selector configured for %s", self.site_config.site_name)
                break

            cards = page.query_selector_all(card_selector)
            logger.info("Discovered %d listing cards on page %d", len(cards), page_num)

            if not cards:
                # No more listings, break pagination loop
                break

            page_listings = self._process_cards(page, cards, current_url)
            listings.extend(page_listings)

            # Handle next page navigation
            if pag_cfg.type == PaginationType.NONE or page_num >= max_pages:
                break

            if pag_cfg.type == PaginationType.PAGE_PARAM:
                current_page_idx += 1
                # Check if template replacement is possible
                if self.site_config.search_url_templates:
                    current_url = self.site_config.search_url_templates[0].replace("{page}", str(current_page_idx))
                else:
                    # Modify query param manually
                    # A basic replacement helper for simplicity:
                    if "?" in start_url:
                        # Append or replace page param
                        if f"{pag_cfg.page_param_name}=" in start_url:
                            import re
                            current_url = re.sub(
                                rf"({pag_cfg.page_param_name}=)\d+",
                                rf"\g<1>{current_page_idx}",
                                start_url
                            )
                        else:
                            current_url = f"{start_url}&{pag_cfg.page_param_name}={current_page_idx}"
                    else:
                        current_url = f"{start_url}?{pag_cfg.page_param_name}={current_page_idx}"

            elif pag_cfg.type == PaginationType.NEXT_BUTTON:
                next_btn_sel = pag_cfg.next_button_selector
                if not next_btn_sel:
                    logger.warning("Next button pagination selected but next_button_selector is empty.")
                    break

                try:
                    next_btn = page.query_selector(next_btn_sel)
                    if next_btn and next_btn.is_visible() and next_btn.is_enabled():
                        # Save current URL to verify navigation occurred
                        prev_url = page.url
                        next_btn.click()
                        # Wait for URL to change or timeout
                        page.wait_for_timeout(2000)
                        current_url = page.url
                        if prev_url == current_url:
                            logger.info("URL didn't change after next-click. Ending page iteration.")
                            break
                    else:
                        logger.info("Next page button not visible or disabled. Ending pagination loop.")
                        break
                except Exception as exc:
                    logger.warning("Failed to navigate to next page: %s", exc)
                    break
            else:
                # Custom / infinite scroll already handled
                break

        return listings

    def _process_cards(self, search_page: Page, cards: list[Any], base_url: str) -> list[RawListing]:
        """Convert listing cards to RawListings, optionally fetching details pages."""
        listings: list[RawListing] = []
        mode = self.site_config.extraction_mode

        # We create a separate detail page to keep context clear
        detail_page = None
        if mode in (ExtractionMode.DETAIL_PAGES, ExtractionMode.HYBRID) and self.site_config.detail_page.enabled:
            detail_page = self.browser_manager.new_page()

        try:
            for idx, card in enumerate(cards):
                # 1. Scrape listing fields from search page listing card
                card_data = self.extract_fields_from_card(card, self.site_config.fields, base_url)
                card_data = self.apply_transforms(card_data, base_url)

                listing_url = card_data.get("listing_url")

                # 2. Hybrid / Detail page parsing
                if detail_page and listing_url:
                    logger.info("Loading detail page (%d/%d): %s", idx + 1, len(cards), listing_url)
                    try:
                        self.browser_manager.navigate(detail_page, listing_url, self.site_config.wait_rules)
                        self.browser_manager.polite_delay(
                            self.site_config.rate_limit.min_delay_ms,
                            self.site_config.rate_limit.max_delay_ms,
                        )

                        # Scrape additional detail page fields
                        detail_data = self.extract_fields_from_card(
                            detail_page, self.site_config.detail_fields, listing_url
                        )
                        detail_data = self.apply_transforms(detail_data, listing_url)

                        # Merge detail fields, keeping card fields on conflict (cards are more robust usually)
                        for k, v in detail_data.items():
                            if v is not None and card_data.get(k) is None:
                                card_data[k] = v
                    except Exception as exc:
                        logger.warning("Failed to load detail page for %s: %s", listing_url, exc)
                        # We still continue with card_data

                # Apply post-scraping filters
                if not self.apply_filters(card_data):
                    continue

                # Build RawListing model
                raw_listing = self.build_raw_listing(card_data, self.source_id or 0)
                listings.append(raw_listing)
        finally:
            if detail_page:
                detail_page.close()

        return listings
