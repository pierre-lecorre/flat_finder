"""Facebook groups raw-text post scraper.

=============================================================================
WARNING:
Facebook scraping is extremely fragile, policy-sensitive, and prone to breakage.
Facebook actively detects automated agents, utilizes anti-scraping protections,
and changes markup daily. This source adapter uses manual-login session-saves
and handles visible post rendering best-effort. Respect Facebook's Terms of
Service and local laws. Use with caution.
=============================================================================
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

from app.browser import BrowserManager
from app.config import AppSettings, SiteConfig
from app.logging_config import get_logger
from app.models import RawListing
from app.scrapers.base import BaseScraper

logger = get_logger("app.scrapers.facebook")


class FacebookSourceScraper(BaseScraper):
    """Scrapes visible post texts from specified Facebook groups using persistent contexts."""

    def scrape(self) -> list[RawListing]:
        if not self.source_id:
            raise ValueError("Facebook scraper launched without source_id configured.")

        state_path = self.site_config.browser.storage_state_path
        if not state_path:
            state_path = str(self.settings.browser_state_dir / "facebook.json")

        logger.info("Initializing persistent context for Facebook scraping...")
        driver = self.browser_manager.get_persistent_context(
            storage_state_path=state_path,
            headless=self.site_config.browser.headless,
        )

        listings: list[RawListing] = []

        try:
            urls = self.site_config.facebook.group_urls
            if not urls:
                logger.warning("No facebook group URLs configured.")
                return []

            for group_url in urls:
                group_listings = self._scrape_group(driver, group_url)
                listings.extend(group_listings)
        finally:
            self.browser_manager.stop()

        logger.info("Facebook scraping done. Discovered %d posts.", len(listings))
        return listings

    def _is_flat_offer(self, text: str) -> bool:
        """Ask the LLM to classify whether this post is a flat/room rental offer.

        Returns True if the LLM considers this a genuine rental listing.
        Falls back to True (include) if LLM is unavailable, to avoid silent data loss.
        """
        try:
            from app.llm.ollama_client import OllamaClient
            from app.llm.prompts import build_is_flat_offer_prompt

            client = OllamaClient(
                host=self.settings.ollama_host,
                model=self.settings.ollama_model,
            )
            if not client.is_available():
                logger.debug("Ollama unavailable; skipping flat-offer classification.")
                return True

            prompt = build_is_flat_offer_prompt(text)
            raw_response = client.generate(prompt)
            parsed = json.loads(raw_response)
            result = bool(parsed.get("is_flat_offer", True))
            logger.debug("LLM flat-offer classification: %s", result)
            return result
        except Exception as exc:
            logger.warning("LLM flat-offer check failed (%s); defaulting to include.", exc)
            return True

    def _enrich_text_with_llm(self, text: str) -> str:
        """Ask the LLM to extend / clean the raw post text into a richer description.

        Returns the enriched text, or the original text if LLM is unavailable.
        """
        try:
            from app.llm.ollama_client import OllamaClient
            from app.llm.prompts import build_text_enrichment_prompt

            client = OllamaClient(
                host=self.settings.ollama_host,
                model=self.settings.ollama_model,
            )
            if not client.is_available():
                logger.debug("Ollama unavailable; skipping text enrichment.")
                return text

            prompt = build_text_enrichment_prompt(text)
            raw_response = client.generate(prompt)
            parsed = json.loads(raw_response)
            enriched = parsed.get("enriched_text", "").strip()
            return enriched if enriched else text
        except Exception as exc:
            logger.warning("LLM text enrichment failed (%s); keeping original.", exc)
            return text

    def _scrape_group(self, driver: Any, group_url: str) -> list[RawListing]:
        """Navigate to group, scroll N times to trigger post rendering, and extract content."""
        logger.info("Loading Facebook group URL: %s", group_url)
        driver.get(group_url)
        time.sleep(3)  # settle pause

        # Login check
        current_url = driver.current_url
        login_inputs = driver.find_elements(By.CSS_SELECTOR, "input[name='email']")
        if "login" in current_url or login_inputs:
            logger.error(
                "Facebook login screen detected. Please execute standard login first "
                "via command: python -m app.cli login-facebook"
            )
            return []

        scrolls = self.site_config.facebook.scroll_iterations
        logger.info("Scrolling Facebook group page %d times...", scrolls)
        self.browser_manager.scroll_n_times(driver, scrolls, pause_ms=1800)

        # ---------------------------------------------------------------------------
        # Selector strategy (updated for 2026 Facebook markup):
        #
        # Facebook group feed posts are rendered as div[aria-posinset] elements
        # inside div[role='feed']. The old div[role='article'] elements are still
        # present in the DOM but are empty lazy-load placeholder shells — they
        # contain no text content and should not be used.
        #
        # Text extraction priority:
        #   1. div[data-ad-rendering-role='story_message']  — always present, full text
        #   2. div[data-ad-comet-preview='message']         — secondary content block
        #   3. div[dir='auto'] / span[dir='auto']           — last resort fallback
        #
        # Post permalinks: /posts/ or story_fbid links may not be present until the
        # post is clicked/expanded. Fall back to group URL + posinset index.
        # ---------------------------------------------------------------------------
        post_sel = self.site_config.facebook.post_selector
        if not post_sel:
            feeds = driver.find_elements(By.CSS_SELECTOR, "div[role='feed']")
            if feeds:
                posts = feeds[0].find_elements(By.CSS_SELECTOR, "div[aria-posinset]")
            else:
                # Fallback: try aria-posinset globally, then old article selector
                posts = driver.find_elements(By.CSS_SELECTOR, "div[aria-posinset]")
                if not posts:
                    posts = driver.find_elements(By.CSS_SELECTOR, "div[role='article']")
        else:
            posts = driver.find_elements(By.CSS_SELECTOR, post_sel)

        logger.info("Found %d visible post containers on Facebook group.", len(posts))

        group_listings: list[RawListing] = []

        for idx, post in enumerate(posts):
            try:
                # ------------------------------------------------------------------
                # Text extraction — story_message is the primary source
                # ------------------------------------------------------------------
                text_content = ""

                # Priority 1: data-ad-rendering-role='story_message'
                story_els = post.find_elements(
                    By.CSS_SELECTOR, "[data-ad-rendering-role='story_message']"
                )
                if story_els:
                    text_content = (story_els[0].text or "").strip()

                # Priority 2: data-ad-comet-preview='message'
                if not text_content:
                    comet_els = post.find_elements(
                        By.CSS_SELECTOR, "[data-ad-comet-preview='message']"
                    )
                    if comet_els:
                        text_content = (comet_els[0].text or "").strip()

                # Priority 3: longest dir=auto text node (last resort)
                if not text_content:
                    for te in post.find_elements(
                        By.CSS_SELECTOR, "div[dir='auto'], span[dir='auto']"
                    ):
                        try:
                            txt = (te.text or "").strip()
                            if len(txt) > len(text_content):
                                text_content = txt
                        except StaleElementReferenceException:
                            continue

                if not text_content or len(text_content) < 20:
                    continue  # skip image-only or near-empty posts

                # ------------------------------------------------------------------
                # Keyword exclusion filter
                # ------------------------------------------------------------------
                desc_lower = text_content.lower()
                is_filtered = False
                for kw in self.site_config.filters.exclude_keywords:
                    if kw.lower() in desc_lower:
                        is_filtered = True
                        break
                if is_filtered:
                    continue

                # ------------------------------------------------------------------
                # LLM flat-offer classification
                # ------------------------------------------------------------------
                if not self._is_flat_offer(text_content):
                    logger.debug("Post #%d classified as non-flat-offer; skipping.", idx)
                    continue

                # ------------------------------------------------------------------
                # LLM text enrichment
                # ------------------------------------------------------------------
                enriched_text = self._enrich_text_with_llm(text_content)

                # ------------------------------------------------------------------
                # Permalink extraction
                # ------------------------------------------------------------------
                permalink = None
                for link_sel in (
                    "a[href*='/posts/']",
                    "a[href*='story_fbid']",
                    "a[href*='?fbid']",
                    "a[href*='/permalink/']",
                ):
                    link_els = post.find_elements(By.CSS_SELECTOR, link_sel)
                    if link_els:
                        href = (link_els[0].get_attribute("href") or "").strip()
                        if href:
                            permalink = href.split("?")[0]
                            if not permalink.startswith("http"):
                                permalink = "https://www.facebook.com" + permalink
                            break

                if not permalink:
                    permalink = f"{group_url}#post_{idx}"

                # ------------------------------------------------------------------
                # Image URLs — filter out tiny sprites and UI avatars
                # ------------------------------------------------------------------
                image_urls = []
                for img in post.find_elements(By.CSS_SELECTOR, "img[src]"):
                    try:
                        src = (img.get_attribute("src") or "").strip()
                        if (
                            src
                            and "emoji" not in src
                            and "/rsrc.php/" not in src
                            and "fbcdn" in src
                            and "_s." not in src
                        ):
                            image_urls.append(src)
                    except StaleElementReferenceException:
                        continue

                primary_img = image_urls[0] if image_urls else None

                # ------------------------------------------------------------------
                # Poster name
                # ------------------------------------------------------------------
                poster_name = None
                for name_sel in (
                    "strong",
                    "strong span",
                    "h2 span",
                    "h3 span",
                    "a[role='link'] strong",
                ):
                    name_els = post.find_elements(By.CSS_SELECTOR, name_sel)
                    if name_els:
                        name_txt = (name_els[0].text or "").strip()
                        if name_txt:
                            poster_name = name_txt
                            break

                # ------------------------------------------------------------------
                # Timestamp
                # ------------------------------------------------------------------
                timestamp_text = None
                for ts_sel in (
                    "a[aria-label] span",
                    "abbr[data-utime]",
                    "span[id] a span",
                ):
                    ts_els = post.find_elements(By.CSS_SELECTOR, ts_sel)
                    if ts_els:
                        ts_txt = (ts_els[0].text or "").strip()
                        if ts_txt:
                            timestamp_text = ts_txt
                            break

                # Title = first non-empty line of the enriched text (capped at 80 chars)
                lines = [line.strip() for line in enriched_text.split("\n") if line.strip()]
                title = lines[0][:80] if lines else "Facebook Post"

                from app.utils import generate_listing_hash
                listing_hash = generate_listing_hash(
                    self.source_id,
                    title,
                    None,
                    group_url,
                )

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
                    raw_price_text=enriched_text,
                    raw_location_text=group_url,
                    raw_description=enriched_text,
                    raw_layout_text=enriched_text,
                    raw_area_text=enriched_text,
                    raw_fees_text=None,
                    raw_deposit_text=None,
                    raw_image_url=primary_img,
                    listing_hash=listing_hash,
                )
                raw.set_image_urls(image_urls)
                raw.set_metadata({
                    "poster_name": poster_name,
                    "timestamp_text": timestamp_text,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "source_group": group_url,
                    "original_text": text_content,
                })

                group_listings.append(raw)

            except Exception as exc:
                logger.warning("Failed to parse Facebook post element: %s", exc)
                continue

        return group_listings

    @classmethod
    def login_facebook(cls, browser_manager: BrowserManager, storage_state_path: str) -> None:
        """Open persistent Chrome browser and halt execution for manual credential login."""
        logger.info("Opening persistent Chrome for manual Facebook login...")
        driver = browser_manager.get_persistent_context(
            storage_state_path=storage_state_path,
            headless=False,
        )

        try:
            logger.info("Navigating to facebook.com...")
            driver.get("https://www.facebook.com/")
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
            browser_manager.stop()
