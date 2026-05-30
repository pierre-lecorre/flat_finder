"""Facebook groups raw-text post scraper.

=============================================================================
WARNING:
Facebook scraping is extremely fragile, policy-sensitive, and prone to breakage.
Facebook actively detects automated agents, utilises anti-scraping protections,
and changes markup daily. This source adapter uses manual-login session-saves
and handles visible post rendering best-effort. Respect Facebook's Terms of
Service and local laws. Use with caution.
=============================================================================
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

from app.browser import BrowserManager
from app.config import AppSettings, SiteConfig
from app.logging_config import get_logger
from app.models import RawListing
from app.scrapers.base import BaseScraper
from playwright.sync_api import Page

logger = get_logger("app.scrapers.facebook")

# Selectors tried in order — FB changes these regularly; we cascade through them
_POST_SELECTOR_CANDIDATES = [
    "div[role='article']",
    "div[data-pagelet^='FeedUnit']",
    "div[data-testid='fbfeed_story']",
]


class FacebookSourceScraper(BaseScraper):
    """Scrapes visible post texts from specified Facebook groups using persistent contexts."""

    def scrape(self) -> list[RawListing]:
        if not self.source_id:
            raise ValueError("Facebook scraper launched without source_id configured.")

        state_path = self.site_config.browser.storage_state_path
        if not state_path:
            state_path = str(self.settings.browser_state_dir / "facebook.json")

        logger.info("Initializing persistent context for Facebook scraping...")
        context = self.browser_manager.get_persistent_context(
            storage_state_path=state_path,
            headless=self.site_config.browser.headless,
        )

        page = context.new_page()
        listings: list[RawListing] = []

        try:
            urls = self.site_config.facebook.group_urls
            if not urls:
                logger.warning("No facebook group URLs configured.")
                return []

            for group_url in urls:
                group_listings = self._scrape_group(page, group_url)
                listings.extend(group_listings)
        finally:
            page.close()

        logger.info("Facebook scraping done. Discovered %d posts.", len(listings))
        return listings

    def _scrape_group(self, page: Page, group_url: str) -> list[RawListing]:
        """Navigate to group, scroll to load posts, and extract content."""
        logger.info("Loading Facebook group URL: %s", group_url)

        # Navigate with a realistic referrer so FB doesn't see a cold direct hit
        page.set_extra_http_headers({
            "Referer": "https://www.google.com/",
            "Accept-Language": "en-US,en;q=0.9,cs;q=0.8",
        })
        page.goto(group_url, wait_until="domcontentloaded")

        # Simulate a human pause before doing anything
        self._human_pause(2500, 4500)

        # Move the mouse to a random position to look alive
        self.browser_manager.human_mouse_move(page)

        # Check for login wall
        if self._is_login_wall(page):
            logger.error(
                "Facebook login screen detected. Please run: python -m app.cli login-facebook"
            )
            return []

        # Check for CAPTCHA / bot-challenge page
        if self._is_captcha_page(page):
            logger.error(
                "Facebook CAPTCHA / human-verification page detected. "
                "The session may be flagged. Try: (1) re-run login-facebook, "
                "(2) reduce scroll_iterations in config, (3) add longer delays."
            )
            return []

        # Scroll to trigger lazy-loaded posts
        scrolls = self.site_config.facebook.scroll_iterations
        logger.info("Scrolling Facebook group page %d times...", scrolls)
        self._human_scroll(page, scrolls)

        # Try each post selector in priority order
        posts = self._find_posts(page)
        logger.info("Found %d visible post articles on Facebook group.", len(posts))

        group_listings: list[RawListing] = []
        for idx, post in enumerate(posts):
            try:
                listing = self._parse_post(post, idx, group_url)
                if listing:
                    group_listings.append(listing)
            except Exception as exc:
                logger.warning("Failed to parse Facebook post element: %s", exc)

        return group_listings

    # ------------------------------------------------------------------
    # Detection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_login_wall(page: Page) -> bool:
        """Return True if Facebook is showing the login gate."""
        if "login" in page.url:
            return True
        if page.query_selector("input[name='email'], input[name='pass']"):
            return True
        return False

    @staticmethod
    def _is_captcha_page(page: Page) -> bool:
        """Return True if Facebook is showing a CAPTCHA / bot-check challenge."""
        url = page.url
        if "checkpoint" in url or "captcha" in url or "twostepverification" in url:
            return True
        # Check for the 'Confirm you are human' text in the page body
        try:
            content = page.content()
            if "Confirm you are human" in content or "confirm you are human" in content:
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Human-behaviour helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _human_pause(min_ms: int = 1500, max_ms: int = 4000) -> None:
        """Sleep for a random duration to mimic a human reading the page."""
        time.sleep(random.uniform(min_ms, max_ms) / 1000.0)

    @staticmethod
    def _human_scroll(page: Page, n: int) -> None:
        """Scroll in a human-like way: variable chunk sizes + random pauses."""
        for i in range(n):
            # Vary scroll distance between 60-100% of viewport height
            fraction = random.uniform(0.6, 1.0)
            page.evaluate(
                f"window.scrollBy({{ top: window.innerHeight * {fraction:.2f}, behavior: 'smooth' }})"
            )
            # Occasionally wiggle the mouse mid-scroll
            if random.random() < 0.4:
                x = random.randint(300, 1500)
                y = random.randint(200, 800)
                page.mouse.move(x, y)
            # Pause between 1.2 s and 3.5 s
            time.sleep(random.uniform(1.2, 3.5))

    # ------------------------------------------------------------------
    # Post discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_posts(page: Page) -> list:
        """Try multiple selector candidates and return the first non-empty result."""
        configured_sel = None  # could be passed via site_config in future
        candidates = ([configured_sel] if configured_sel else []) + _POST_SELECTOR_CANDIDATES
        for sel in candidates:
            posts = page.query_selector_all(sel)
            if posts:
                logger.debug("Post selector '%s' matched %d elements.", sel, len(posts))
                return posts
        logger.warning("No post selector matched — page structure may have changed.")
        return []

    # ------------------------------------------------------------------
    # Post parsing
    # ------------------------------------------------------------------

    def _parse_post(
        self, post: Any, idx: int, group_url: str
    ) -> RawListing | None:
        """Extract all relevant fields from a single post element."""
        # Extract main text — grab the longest dir=auto div as the body
        text_elements = post.query_selector_all("div[dir='auto']")
        text_content = ""
        for te in text_elements:
            txt = (te.inner_text() or "").strip()
            if txt and len(txt) > len(text_content):
                text_content = txt

        if not text_content:
            return None  # skip image-only posts

        # Keyword exclusion filter
        desc_lower = text_content.lower()
        for kw in self.site_config.filters.exclude_keywords:
            if kw.lower() in desc_lower:
                return None

        # Extract permalink
        permalink = self._extract_permalink(post, group_url, idx)

        # Extract images (skip tiny UI assets)
        image_urls = [
            src
            for img in post.query_selector_all("img[src]")
            if (src := img.get_attribute("src"))
            and "emoji" not in src
            and "/rsrc.php/" not in src
            and "fbcdn" in src
        ]
        primary_img = image_urls[0] if image_urls else None

        # Poster name
        poster_name = None
        poster_elem = post.query_selector("strong span, a[role='link'] span")
        if poster_elem:
            poster_name = (poster_elem.inner_text() or "").strip()

        # Timestamp text
        timestamp_text = None
        time_elem = post.query_selector("a[role='link'] span[id]")
        if time_elem:
            timestamp_text = (time_elem.inner_text() or "").strip()

        # Title = first non-empty line, capped at 80 chars
        lines = [line.strip() for line in text_content.split("\n") if line.strip()]
        title = lines[0][:80] if lines else "Facebook Post"

        from app.utils import generate_listing_hash
        listing_hash = generate_listing_hash(self.source_id, title, None, group_url)

        raw = RawListing(
            source_id=self.source_id,
            external_id=(
                permalink.split("/posts/")[-1].replace("/", "")
                if "/posts/" in permalink
                else None
            ),
            listing_url=permalink,
            canonical_url=permalink,
            raw_title=title,
            raw_price_text=text_content,
            raw_location_text=group_url,
            raw_description=text_content,
            raw_layout_text=text_content,
            raw_area_text=text_content,
            raw_fees_text=None,
            raw_deposit_text=None,
            raw_image_url=primary_img,
            listing_hash=listing_hash,
        )
        raw.set_image_urls(image_urls)
        raw.set_metadata({
            "poster_name": poster_name,
            "timestamp_text": timestamp_text,
            "scraped_at": datetime.utcnow().isoformat(),
            "source_group": group_url,
        })
        return raw

    @staticmethod
    def _extract_permalink(post: Any, group_url: str, idx: int) -> str:
        """Best-effort permalink extraction from a post element."""
        # Primary: anchor with /posts/ in href
        link_elem = post.query_selector("a[role='link'][href*='/posts/']")
        if link_elem:
            href = link_elem.get_attribute("href")
            if href:
                clean = href.split("?")[0]
                if not clean.startswith("http"):
                    clean = "https://www.facebook.com" + clean
                return clean
        # Fallback: any anchor containing /groups/ in href
        link_elem = post.query_selector("a[href*='/groups/']")
        if link_elem:
            href = link_elem.get_attribute("href")
            if href:
                clean = href.split("?")[0]
                if not clean.startswith("http"):
                    clean = "https://www.facebook.com" + clean
                return clean
        return f"{group_url}#post_{idx}"

    # ------------------------------------------------------------------
    # Manual login helper
    # ------------------------------------------------------------------

    @classmethod
    def login_facebook(cls, browser_manager: BrowserManager, storage_state_path: str) -> None:
        """Open persistent chrome browser and halt execution for manual credential login."""
        logger.info("Initializing Playwright manual session launch...")
        context = browser_manager.get_persistent_context(
            storage_state_path=storage_state_path,
            headless=False,
        )
        page = context.new_page()

        try:
            logger.info("Navigating to facebook.com...")
            page.goto("https://www.facebook.com/")
            print("\n" + "=" * 80)
            print("FACEBOOK MANUAL LOGIN ASSISTANCE:")
            print("1. A browser window has opened to facebook.com.")
            print("2. Enter your credentials and complete multi-factor auth.")
            print("3. Verify you have loaded your home page feed correctly.")
            print("4. Switch back to this console window.")
            print("=" * 80)
            input("\n--> Press ENTER in this console once you are fully logged in...")

            try:
                browser_manager.save_storage_state(storage_state_path)
            except Exception as exc:
                logger.warning("Could not export storage state JSON backup: %s", exc)

            logger.info("Facebook authentication successfully persisted in user_data_dir.")
        finally:
            page.close()
            browser_manager.stop()
