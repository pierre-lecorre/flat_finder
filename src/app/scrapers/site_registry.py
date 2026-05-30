"""Scraper registration manager.

Resolves configured source_type strings into active Scraper class instances.
"""

from __future__ import annotations

from typing import Type

from app.browser import BrowserManager
from app.config import AppSettings, SiteConfig
from app.logging_config import get_logger
from app.scrapers.base import BaseScraper
from app.scrapers.facebook_source import FacebookSourceScraper
from app.scrapers.generic_listing import GenericListingScraper

logger = get_logger("app.scrapers.registry")


class SiteRegistry:
    """Registry pattern mapping source_type strings to Python Scraper classes."""

    def __init__(self) -> None:
        self._registry: dict[str, Type[BaseScraper]] = {}

    def register(self, source_type: str, scraper_class: Type[BaseScraper]) -> None:
        """Register a new scraper class for a source type."""
        self._registry[source_type] = scraper_class
        logger.debug("Registered scraper class '%s' for source_type '%s'", scraper_class.__name__, source_type)

    def get_scraper(
        self,
        site_config: SiteConfig,
        settings: AppSettings,
        browser_manager: BrowserManager,
    ) -> BaseScraper:
        """Resolve and instantiate the correct scraper instance."""
        source_type = site_config.source_type
        scraper_cls = self._registry.get(source_type)

        if not scraper_cls:
            logger.warning(
                "Source type '%s' is not registered. Defaulting to GenericListingScraper.",
                source_type,
            )
            scraper_cls = GenericListingScraper

        return scraper_cls(site_config, settings, browser_manager)


# Global singleton instance
default_registry = SiteRegistry()

# Register core scraper adapters
default_registry.register("normal_listing_site", GenericListingScraper)
default_registry.register("facebook_group", FacebookSourceScraper)
default_registry.register("custom", GenericListingScraper)
