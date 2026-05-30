"""Config-driven generic real-estate listing scraper.

Operates over multi-site search configurations, handles paginated cards,
loads detail pages for deeper fields, and produces RawListings.
"""

from __future__ import annotations

import re
import time
from typing import Any

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from app.config import ExtractionMode, PaginationType
from app.logging_config import get_logger
from app.models import RawListing
from app.scrapers.base import BaseScraper

logger = get_logger("app.scrapers.generic")


class GenericListingScraper(BaseScraper):
    """Universal scraper that navigates search pages and cards based on CSS selectors."""

    def scrape(self) -> list[RawListing]:
        if not self.source_id:
            raise ValueError("Scraper launched without source_id configured.")

        urls = list(self.site_config.start_urls)
        if not urls and self.site_config.search_url_templates:
            for tmpl in self.site_config.search_url_templates:
                start_page = self.site_config.pagination.start_page
                urls.append(tmpl.replace("{page}", str(start_page)))

        if not urls:
            logger.warning("No search URLs or templates configured for %s.", self.site_config.site_name)
            return []

        listings: list[RawListing] = []
        # new_page() opens a new tab and returns the driver
        driver = self.browser_manager.new_page()

        try:
            for base_search_url in urls:
                current_listings = self._scrape_search_url(driver, base_search_url)
                listings.extend(current_listings)
        finally:
            # Close only the extra tab we opened, if multiple handles exist
            try:
                handles = driver.window_handles
                if len(handles) > 1:
                    driver.close()
                    driver.switch_to.window(handles[0])
            except Exception:
                pass

        logger.info("Scraping completed. Found %d valid listings.", len(listings))
        return listings

    def _scrape_search_url(self, driver: uc.Chrome, start_url: str) -> list[RawListing]:
        """Iterate through pages for a specific search root and extract listings."""
        listings: list[RawListing] = []
        pag_cfg = self.site_config.pagination
        max_pages = pag_cfg.max_pages

        current_page_idx = pag_cfg.start_page
        current_url = start_url

        for page_num in range(1, max_pages + 1):
            logger.info("Scraping page %d for %s: %s", page_num, self.site_config.site_name, current_url)
            try:
                self.browser_manager.navigate(driver, current_url, self.site_config.wait_rules)
            except Exception as exc:
                logger.error("Navigation failed to search URL %s: %s", current_url, exc)
                self.browser_manager.screenshot_on_error(
                    driver, f"nav_error_{self.site_config.site_name}", self.settings.screenshots_dir
                )
                break

            self.browser_manager.polite_delay(
                self.site_config.rate_limit.min_delay_ms,
                self.site_config.rate_limit.max_delay_ms,
            )

            if pag_cfg.type == PaginationType.INFINITE_SCROLL:
                self.browser_manager.scroll_n_times(driver, 5, pause_ms=1000)

            card_selector = self.site_config.listing_card_selector
            if not card_selector:
                logger.error("No listing_card_selector configured for %s", self.site_config.site_name)
                break

            cards = driver.find_elements(By.CSS_SELECTOR, card_selector)
            logger.info("Discovered %d listing cards on page %d", len(cards), page_num)

            if not cards:
                break

            page_listings = self._process_cards(driver, cards, current_url)
            listings.extend(page_listings)

            if pag_cfg.type == PaginationType.NONE or page_num >= max_pages:
                break

            if pag_cfg.type == PaginationType.PAGE_PARAM:
                current_page_idx += 1
                if self.site_config.search_url_templates:
                    current_url = self.site_config.search_url_templates[0].replace("{page}", str(current_page_idx))
                else:
                    if f"{pag_cfg.page_param_name}=" in start_url:
                        current_url = re.sub(
                            rf"({pag_cfg.page_param_name}=)\d+",
                            rf"\g<1>{current_page_idx}",
                            start_url,
                        )
                    elif "?" in start_url:
                        current_url = f"{start_url}&{pag_cfg.page_param_name}={current_page_idx}"
                    else:
                        current_url = f"{start_url}?{pag_cfg.page_param_name}={current_page_idx}"

            elif pag_cfg.type == PaginationType.NEXT_BUTTON:
                next_btn_sel = pag_cfg.next_button_selector
                if not next_btn_sel:
                    logger.warning("next_button_selector is empty.")
                    break
                try:
                    from selenium.webdriver.common.by import By as _By
                    next_btn = driver.find_element(_By.CSS_SELECTOR, next_btn_sel)
                    if next_btn.is_displayed() and next_btn.is_enabled():
                        prev_url = driver.current_url
                        next_btn.click()
                        time.sleep(2)
                        current_url = driver.current_url
                        if prev_url == current_url:
                            logger.info("URL unchanged after next-click. Ending pagination.")
                            break
                    else:
                        logger.info("Next button not visible/enabled. Ending pagination.")
                        break
                except Exception as exc:
                    logger.warning("Failed to navigate to next page: %s", exc)
                    break
            else:
                break

        return listings

    def _process_cards(
        self, driver: uc.Chrome, cards: list[Any], base_url: str
    ) -> list[RawListing]:
        """Convert listing cards to RawListings, optionally fetching detail pages."""
        listings: list[RawListing] = []
        mode = self.site_config.extraction_mode

        use_detail = (
            mode in (ExtractionMode.DETAIL_PAGES, ExtractionMode.HYBRID)
            and self.site_config.detail_page.enabled
        )

        # Open a second tab for detail pages
        detail_driver = None
        if use_detail:
            detail_driver = self.browser_manager.new_page()

        try:
            for idx, card in enumerate(cards):
                card_data = self.extract_fields_from_card(card, self.site_config.fields, base_url)
                card_data = self.apply_transforms(card_data, base_url)

                listing_url = card_data.get("listing_url")

                if detail_driver and listing_url:
                    logger.info("Loading detail page (%d/%d): %s", idx + 1, len(cards), listing_url)
                    try:
                        self.browser_manager.navigate(
                            detail_driver, listing_url, self.site_config.wait_rules
                        )
                        self.browser_manager.polite_delay(
                            self.site_config.rate_limit.min_delay_ms,
                            self.site_config.rate_limit.max_delay_ms,
                        )
                        detail_data = self.extract_fields_from_card(
                            detail_driver, self.site_config.detail_fields, listing_url
                        )
                        detail_data = self.apply_transforms(detail_data, listing_url)
                        for k, v in detail_data.items():
                            if v is not None and card_data.get(k) is None:
                                card_data[k] = v
                    except Exception as exc:
                        logger.warning("Failed to load detail page for %s: %s", listing_url, exc)

                if not self.apply_filters(card_data):
                    continue

                raw_listing = self.build_raw_listing(card_data, self.source_id or 0)
                listings.append(raw_listing)
        finally:
            if detail_driver:
                try:
                    handles = detail_driver.window_handles
                    if len(handles) > 1:
                        detail_driver.close()
                        detail_driver.switch_to.window(handles[0])
                except Exception:
                    pass

        return listings
