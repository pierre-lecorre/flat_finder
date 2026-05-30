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
from datetime import datetime
from typing import Any

from app.browser import BrowserManager
from app.config import AppSettings, SiteConfig
from app.logging_config import get_logger
from app.models import RawListing
from app.scrapers.base import BaseScraper
from playwright.sync_api import Page

logger = get_logger("app.scrapers.facebook")

# ---------------------------------------------------------------------------
# URL patterns that indicate Facebook has NOT yet landed on an authenticated
# feed page.  We keep polling until the URL matches none of these.
# ---------------------------------------------------------------------------
_BLOCKED_URL_PATTERNS: tuple[str, ...] = (
    "facebook.com/login",
    "facebook.com/checkpoint",
    "facebook.com/recover",
    "facebook.com/two_step_verification",
    "facebook.com/ajax/",
    "facebook.com/identity",
    "about:blank",
)

# URL fragments that confirm the user is on their authenticated home feed
_SUCCESS_URL_PATTERNS: tuple[str, ...] = (
    "facebook.com/?sk=",
    "facebook.com/home",
    "facebook.com/groups",
    "facebook.com/profile",
    # Bare homepage after redirect is just "https://www.facebook.com/"
    # We match it via the absence of /login and /checkpoint (see _is_authenticated).
)

# How many seconds to keep polling before giving up
_MAX_WAIT_SECONDS = 300  # 5 minutes — enough time for MFA
_POLL_INTERVAL_SECONDS = 3


def _is_authenticated(url: str) -> bool:
    """Return True when the browser has landed on a genuine FB feed page."""
    if any(pat in url for pat in _BLOCKED_URL_PATTERNS):
        return False
    # The bare homepage redirect after login ends at https://www.facebook.com/
    # which contains none of the blocked patterns.
    if "facebook.com" in url:
        return True
    return False


def _is_checkpoint(url: str) -> bool:
    """Return True when the browser is stuck on a security checkpoint page."""
    return any(p in url for p in (
        "checkpoint",
        "recover",
        "two_step_verification",
        "identity",
    ))


class FacebookSourceScraper(BaseScraper):
    """Scrapes visible post texts from specified Facebook groups using persistent contexts."""

    def scrape(self) -> list[RawListing]:
        if not self.source_id:
            raise ValueError("Facebook scraper launched without source_id configured.")

        state_path = self.site_config.browser.storage_state_path
        if not state_path:
            state_path = str(self.settings.browser_state_dir / "facebook.json")

        logger.info("Initializing persistent context for Facebook scraping...")
        # Start persistent browser session
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
        """Navigate to group, scroll N times to trigger posts rendering, and extract content."""
        logger.info("Loading Facebook group URL: %s", group_url)
        page.goto(group_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)  # settle pause

        # Check if login prompt blocking screen
        if "login" in page.url or page.query_selector("input[name='email']"):
            logger.error(
                "Facebook login screen detected. Please execute standard login first "
                "via command: python -m app.cli login-facebook"
            )
            return []

        # Perform scroll iterations to render dynamic posts
        scrolls = self.site_config.facebook.scroll_iterations
        logger.info("Scrolling Facebook group page %d times...", scrolls)
        self.browser_manager.scroll_n_times(page, scrolls, pause_ms=1800)

        # Standard post card selector candidates
        # Facebook updates selectors constantly.
        # Typically post blocks are wrapped inside divs with role="feed" or feed elements
        # We search standard div with role="article" or generic post classes
        post_sel = self.site_config.facebook.post_selector
        if not post_sel:
            post_sel = "div[role='article']"

        posts = page.query_selector_all(post_sel)
        logger.info("Found %d visible post articles on Facebook group.", len(posts))

        group_listings: list[RawListing] = []

        for idx, post in enumerate(posts):
            try:
                # Extract main text body
                # Typical FB text block is nested inside dir="auto" divs
                text_elements = post.query_selector_all("div[dir='auto']")
                text_content = ""
                for te in text_elements:
                    txt = (te.inner_text() or "").strip()
                    if txt and len(txt) > len(text_content):
                        text_content = txt  # grab longest text chunk as main body

                if not text_content:
                    continue  # skip image-only posts with no text

                # Check inclusion/exclusion keywords
                desc_lower = text_content.lower()
                is_filtered = False
                for kw in self.site_config.filters.exclude_keywords:
                    if kw.lower() in desc_lower:
                        is_filtered = True
                        break

                if is_filtered:
                    continue

                # Extract post permalink
                # FB post links are typically found inside standard anchor tags pointing to /groups/.../posts/...
                link_elem = post.query_selector("a[role='link'][href*='/posts/']")
                permalink = None
                if link_elem:
                    href = link_elem.get_attribute("href")
                    if href:
                        # Clean query parameters to avoid tracking tokens
                        permalink = href.split("?")[0]
                        if not permalink.startswith("http"):
                            permalink = "https://www.facebook.com" + permalink

                if not permalink:
                    permalink = f"{group_url}#post_{idx}"

                # Extract images inside post
                img_elements = post.query_selector_all("img[src]")
                image_urls = []
                for img in img_elements:
                    src = img.get_attribute("src")
                    # Exclude tiny UI icon buttons/avatars
                    if src and "emoji" not in src and "/rsrc.php/" not in src and "fbcdn" in src:
                        image_urls.append(src)

                primary_img = image_urls[0] if image_urls else None

                # Extract metadata: poster name, relative timestamp
                poster_name = None
                poster_elem = post.query_selector("strong span, a[role='link'] span")
                if poster_elem:
                    poster_name = (poster_elem.inner_text() or "").strip()

                timestamp_text = None
                time_elem = post.query_selector("a[role='link'] span[id]")
                if time_elem:
                    timestamp_text = (time_elem.inner_text() or "").strip()

                # Title is the first line of the description
                lines = [line.strip() for line in text_content.split("\n") if line.strip()]
                title = lines[0][:80] if lines else "Facebook Post"

                # Standard build
                # Generate unique hash for deduplication
                from app.utils import generate_listing_hash
                listing_hash = generate_listing_hash(
                    self.source_id,
                    title,
                    None,
                    group_url
                )

                raw = RawListing(
                    source_id=self.source_id,
                    external_id=permalink.split("/posts/")[-1].replace("/", "") if "/posts/" in permalink else None,
                    listing_url=permalink,
                    canonical_url=permalink,
                    raw_title=title,
                    raw_price_text=text_content,  # pass description to price text to extract numeric price if mentioned in post
                    raw_location_text=group_url,
                    raw_description=text_content,
                    raw_layout_text=text_content,  # pass to parsing heuristics
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
                    "source_group": group_url
                })

                group_listings.append(raw)

            except Exception as exc:
                logger.warning("Failed to parse Facebook post element: %s", exc)
                continue

        return group_listings

    @classmethod
    def login_facebook(cls, browser_manager: BrowserManager, storage_state_path: str) -> None:
        """Open a non-headless browser and guide the user through manual Facebook login.

        After the user submits their credentials Facebook sometimes shows a
        checkpoint/security-challenge page (CAPTCHA, SMS code, identity
        confirmation) or simply a white blank screen while it processes the
        session.  The old implementation saved the storage state immediately
        after the first ENTER press, capturing an unauthenticated state.

        This version polls the browser URL every few seconds and only saves
        the session once Facebook has redirected to a genuine authenticated
        landing page.  If a checkpoint is detected the user is prompted to
        resolve it in the browser window before the script continues.
        """
        logger.info("Initializing Playwright manual session launch...")
        context = browser_manager.get_persistent_context(
            storage_state_path=storage_state_path,
            headless=False,
        )
        page = context.new_page()

        try:
            # Navigate directly to the login form so credentials are immediately
            # visible without an extra redirect step.
            logger.info("Navigating to facebook.com/login...")
            page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")

            print("\n" + "=" * 80)
            print("FACEBOOK MANUAL LOGIN — INSTRUCTIONS")
            print("=" * 80)
            print("1. A browser window has opened on the Facebook login page.")
            print("2. Enter your email / phone and password, then click Log In.")
            print("3. Complete any 2-factor auth or security challenge in the browser.")
            print("4. Wait until you can see your Facebook home feed (news feed).")
            print("5. Once the feed is fully loaded, come back here and press ENTER.")
            print("="  * 80)
            print()
            print("NOTE: If you see a white/blank screen after entering credentials,")
            print("      Facebook is running a security check. Wait for it to finish")
            print("      or follow any on-screen prompts before pressing ENTER.")
            print("=" * 80)
            input("\n--> Press ENTER once you are fully logged in and see your news feed...")

            # ------------------------------------------------------------------
            # Poll until the browser has landed on an authenticated page.
            # This handles the checkpoint / white-screen scenario where the user
            # presses ENTER too early (or Facebook is still loading).
            # ------------------------------------------------------------------
            print("\nVerifying login state, please wait...")
            elapsed = 0
            last_reported_url = ""

            while elapsed < _MAX_WAIT_SECONDS:
                current_url = page.url

                if _is_authenticated(current_url):
                    print(f"\n✔  Authenticated!  Current page: {current_url}")
                    break

                if current_url != last_reported_url:
                    last_reported_url = current_url

                    if _is_checkpoint(current_url):
                        print()
                        print("⚠  Facebook checkpoint detected!")
                        print(f"   Current URL: {current_url}")
                        print("   Please complete the security challenge in the browser window.")
                        print("   This script will continue automatically once you land on the feed.")
                    elif "about:blank" in current_url or not current_url:
                        print("   Browser shows a blank page — Facebook is loading, please wait...")
                    elif "login" in current_url:
                        print("   Still on login page — credentials may have been rejected. Please try again.")
                    else:
                        print(f"   Waiting for feed page... (current: {current_url})")

                time.sleep(_POLL_INTERVAL_SECONDS)
                elapsed += _POLL_INTERVAL_SECONDS
            else:
                raise RuntimeError(
                    f"Timed out after {_MAX_WAIT_SECONDS}s waiting for Facebook authentication. "
                    "The browser may be stuck on a checkpoint or login page. "
                    "Try running login-facebook again."
                )

            # Save storage state as JSON backup (best-effort)
            try:
                browser_manager.save_storage_state(storage_state_path)
                print(f"\n✔  Session state saved to: {storage_state_path}")
            except Exception as exc:
                logger.warning("Could not export storage state JSON backup: %s", exc)

            logger.info("Facebook authentication successfully persisted in user_data_dir.")

        finally:
            page.close()
            browser_manager.stop()
