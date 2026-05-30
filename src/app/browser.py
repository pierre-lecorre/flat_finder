"""Playwright Sync API browser manager wrapper.

Provides convenient thread-safe wrappers for navigation, polite pauses,
element text/attribute extraction, scroll management, and screenshots.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Generator, Optional

from app.config import AppSettings, WaitRules
from app.logging_config import get_logger
from playwright.sync_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Locator,
    Page,
    sync_playwright,
)

logger = get_logger("app.browser")


class BrowserManager:
    """Manages Playwright browser lifecycle, persistent contexts, and actions."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None

    def __enter__(self) -> BrowserManager:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def start(self, headless: Optional[bool] = None) -> None:
        """Launch Playwright sync browser and build standard context."""
        if self._playwright:
            return

        is_headless = headless if headless is not None else self.settings.browser_headless
        logger.info("Starting Playwright browser (headless=%s)...", is_headless)
        self._playwright = sync_playwright().start()

        # Launch chromium as standard engine
        self.browser = self._playwright.chromium.launch(
            headless=is_headless,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--window-size=1280,800",
            ],
        )

        # Standard context configuration
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # 30 second global timeout default
        self.context.set_default_timeout(30000)

    def get_persistent_context(
        self, storage_state_path: str, headless: Optional[bool] = None
    ) -> BrowserContext:
        """Create or launch a persistent browser context (e.g. for logged-in sessions).

        Uses the user's real Chrome installation (channel='chrome') to avoid
        Facebook's anti-automation detection of Playwright's bundled Chromium.
        """
        if self._playwright:
            # Persistent context cannot be created once browser is already launched
            self.stop()

        is_headless = headless if headless is not None else self.settings.browser_headless
        state_path = Path(storage_state_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Starting Playwright persistent context (headless=%s, state=%s)...",
            is_headless,
            state_path.name,
        )
        self._playwright = sync_playwright().start()

        # Persistent contexts store all state (cookies, localStorage, sessions)
        # directly inside user_data_dir. Do NOT pass storage_state here —
        # launch_persistent_context does not support that parameter.
        user_data = self.settings.browser_state_dir / "user_data"
        user_data.mkdir(parents=True, exist_ok=True)

        # Use the real Chrome install via channel="chrome" so Facebook sees a
        # genuine browser fingerprint instead of the stripped-down Chromium
        # that Playwright ships (which FB actively blocks).
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            channel="chrome",
            headless=is_headless,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="Europe/Prague",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-infobars",
                "--start-maximized",
            ],
        )
        self.context.set_default_timeout(30000)

        # Inject stealth script on every new page to hide automation markers.
        # This runs before any page JS and patches the primary signals that
        # Facebook (and other anti-bot systems) use for detection.
        self.context.add_init_script("""
            // Hide navigator.webdriver (primary bot detection signal)
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Hide the Playwright-injected window properties
            delete window.__playwright;
            delete window.__pw_manual;

            // Spoof plugins array (headless Chromium reports 0 plugins)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });

            // Spoof languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en', 'cs'],
            });

            // Pass Chrome-specific runtime check
            window.chrome = { runtime: {} };
        """)
        return self.context

    def save_storage_state(self, path: str) -> None:
        """Serialize session state cookies/storage to disk."""
        if not self.context:
            logger.warning("No context available to save storage state.")
            return
        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(state_path))
        logger.info("Storage state successfully saved to %s", path)

    def stop(self) -> None:
        """Close context and browser cleanly."""
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None

        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        logger.info("Playwright browser stopped.")

    def new_page(self) -> Page:
        """Create a fresh Page inside the current context."""
        if not self.context:
            self.start()
        return self.context.new_page()

    # -----------------------------------------------------------------------
    # Page Helpers
    # -----------------------------------------------------------------------

    def navigate(self, page: Page, url: str, wait_rules: WaitRules) -> None:
        """Navigate to URL and wait for configured selectors or state."""
        logger.info("Navigating to: %s", url)
        # Using string representation of load state
        page.goto(url, wait_until=wait_rules.load_state)  # type: ignore

        if wait_rules.wait_for_selector:
            logger.debug("Waiting for selector: %s", wait_rules.wait_for_selector)
            page.wait_for_selector(
                wait_rules.wait_for_selector,
                state="visible",
                timeout=wait_rules.timeout_ms,
            )

    @staticmethod
    def wait_for(page: Page, selector: str, timeout_ms: int = 10000) -> None:
        """Explicitly wait for a selector to appear on the page."""
        page.wait_for_selector(selector, state="visible", timeout=timeout_ms)

    @staticmethod
    def extract_text(parent: Any, selector: str) -> Optional[str]:
        """Safely extract stripped inner text from a element or locator.

        Returns None if element is not found.
        """
        try:
            elem = parent.query_selector(selector)
            if elem:
                txt = elem.inner_text()
                return txt.strip() if txt else None
        except Exception:
            pass
        return None

    @staticmethod
    def extract_attr(parent: Any, selector: str, attr: str) -> Optional[str]:
        """Safely extract HTML attribute value from a selector.

        Returns None if element or attribute is not found.
        """
        try:
            elem = parent.query_selector(selector)
            if elem:
                val = elem.get_attribute(attr)
                return val.strip() if val else None
        except Exception:
            pass
        return None

    @staticmethod
    def extract_all_texts(parent: Any, selector: str) -> list[str]:
        """Safely extract stripped texts from all matching nodes."""
        try:
            elements = parent.query_selector_all(selector)
            return [t for elem in elements if (t := (elem.inner_text() or "").strip())]
        except Exception:
            return []

    @staticmethod
    def extract_all_attrs(parent: Any, selector: str, attr: str) -> list[str]:
        """Safely extract attributes from all matching nodes."""
        try:
            elements = parent.query_selector_all(selector)
            return [a for elem in elements if (a := (elem.get_attribute(attr) or "").strip())]
        except Exception:
            return []

    @staticmethod
    def scroll_to_bottom(page: Page, pause_ms: int = 1500) -> None:
        """Scroll to the bottom of the page to trigger dynamic loads."""
        logger.debug("Scrolling to bottom of page...")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(pause_ms / 1000.0)

    @staticmethod
    def scroll_n_times(page: Page, n: int, pause_ms: int = 1500) -> None:
        """Scroll page multiple times to load infinite scrolling elements."""
        for i in range(n):
            logger.debug("Scroll execution: %d/%d", i + 1, n)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(pause_ms / 1000.0)

    @staticmethod
    def polite_delay(min_ms: int = 1000, max_ms: int = 3000) -> None:
        """Polite rate limiting delay using random sleep intervals."""
        delay = random.randint(min_ms, max_ms) / 1000.0
        logger.debug("Polite delay: sleeping for %.2fs", delay)
        time.sleep(delay)

    @staticmethod
    def screenshot_on_error(page: Page, name: str, screenshots_dir: Path) -> None:
        """Capture standard PNG screenshot on scraping failure."""
        try:
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"error_{name}_{timestamp}.png"
            path = screenshots_dir / filename
            page.screenshot(path=path)
            logger.info("Saved error debug screenshot to %s", path)
        except Exception as exc:
            logger.error("Failed to capture error screenshot: %s", exc)
