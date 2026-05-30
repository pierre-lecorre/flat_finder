"""Selenium + undetected-chromedriver browser manager.

Replaces the previous Playwright implementation which suffered from
irreproducible goto() hangs when using launch_persistent_context +
channel='chrome' + viewport=None on Windows.

undetected-chromedriver (uc) patches the Chrome binary at runtime to
remove every automation fingerprint that Facebook and other sites check.
It wraps the standard Selenium WebDriver API, so all helper methods
(navigate, extract_text, scroll, etc.) continue to work unchanged.
"""

from __future__ import annotations

import random
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from app.config import AppSettings, WaitRules
from app.logging_config import get_logger

logger = get_logger("app.browser")


def _detect_chrome_major_version() -> Optional[int]:
    """Return the installed Chrome major version number, or None if undetectable."""
    candidates = [
        # Windows — typical install paths
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        # Also try via registry / PATH
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
    ]
    for exe in candidates:
        try:
            result = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            match = re.search(r"(\d+)\.\d+\.\d+", result.stdout or result.stderr)
            if match:
                version = int(match.group(1))
                logger.debug("Detected Chrome major version: %d", version)
                return version
        except Exception:
            continue
    logger.warning("Could not auto-detect Chrome version; letting uc pick automatically.")
    return None


_CHROME_VERSION: Optional[int] = _detect_chrome_major_version()


class BrowserManager:
    """Manages undetected-chromedriver lifecycle, persistent profiles, and page helpers."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.driver: Optional[uc.Chrome] = None

    def __enter__(self) -> BrowserManager:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _build_options(
        self,
        headless: bool,
        user_data_dir: Optional[str] = None,
    ) -> uc.ChromeOptions:
        opts = uc.ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        if user_data_dir:
            opts.add_argument(f"--user-data-dir={user_data_dir}")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--start-maximized")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-infobars")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--lang=en-US")
        return opts

    def _make_driver(self, opts: uc.ChromeOptions) -> uc.Chrome:
        """Instantiate uc.Chrome, passing version_main when we know the Chrome version."""
        kwargs: dict[str, Any] = {"options": opts, "use_subprocess": True}
        if _CHROME_VERSION is not None:
            kwargs["version_main"] = _CHROME_VERSION
        return uc.Chrome(**kwargs)

    def start(self, headless: Optional[bool] = None) -> None:
        """Launch a standard (non-persistent) undetected-chromedriver instance."""
        if self.driver:
            return
        is_headless = headless if headless is not None else self.settings.browser_headless
        logger.info("Starting undetected-chromedriver (headless=%s)...", is_headless)
        opts = self._build_options(is_headless)
        self.driver = self._make_driver(opts)
        self.driver.implicitly_wait(10)

    def get_persistent_context(
        self, storage_state_path: str, headless: Optional[bool] = None
    ) -> uc.Chrome:
        """Launch Chrome with a persistent user-data-dir for logged-in sessions."""
        if self.driver:
            self.stop()

        is_headless = headless if headless is not None else self.settings.browser_headless
        user_data = self.settings.browser_state_dir / "user_data"
        user_data.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Starting persistent undetected-chromedriver (headless=%s, profile=%s)...",
            is_headless,
            user_data,
        )
        opts = self._build_options(is_headless, user_data_dir=str(user_data))
        self.driver = self._make_driver(opts)
        self.driver.implicitly_wait(10)
        return self.driver

    def save_storage_state(self, path: str) -> None:
        """Export cookies to a JSON file."""
        if not self.driver:
            logger.warning("No driver available to save storage state.")
            return
        import json
        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        cookies = self.driver.get_cookies()
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"cookies": cookies}, f, indent=2)
        logger.info("Storage state (cookies) saved to %s", path)

    def stop(self) -> None:
        """Quit driver cleanly."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        logger.info("Browser driver stopped.")

    def new_page(self) -> uc.Chrome:
        """Open a new tab and switch to it; return the driver."""
        if not self.driver:
            self.start()
        self.driver.execute_script("window.open('');")
        self.driver.switch_to.window(self.driver.window_handles[-1])
        return self.driver

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def navigate(self, page: uc.Chrome, url: str, wait_rules: WaitRules) -> None:
        """Navigate to URL and wait for selector; logs a warning on timeout (non-fatal)."""
        logger.info("Navigating to: %s", url)
        page.get(url)
        if wait_rules.wait_for_selector:
            logger.debug("Waiting for selector: %s", wait_rules.wait_for_selector)
            try:
                WebDriverWait(page, wait_rules.timeout_ms / 1000).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_rules.wait_for_selector))
                )
            except TimeoutException:
                logger.warning(
                    "Selector '%s' not found within %.1fs — continuing anyway.",
                    wait_rules.wait_for_selector,
                    wait_rules.timeout_ms / 1000,
                )

    @staticmethod
    def wait_for(page: uc.Chrome, selector: str, timeout_ms: int = 10_000) -> None:
        """Explicitly wait for a CSS selector to appear."""
        WebDriverWait(page, timeout_ms / 1000).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def extract_text(parent: Any, selector: str) -> Optional[str]:
        try:
            elem = parent.find_element(By.CSS_SELECTOR, selector)
            txt = elem.text
            return txt.strip() if txt else None
        except (NoSuchElementException, Exception):
            return None

    @staticmethod
    def extract_attr(parent: Any, selector: str, attr: str) -> Optional[str]:
        try:
            elem = parent.find_element(By.CSS_SELECTOR, selector)
            val = elem.get_attribute(attr)
            return val.strip() if val else None
        except (NoSuchElementException, Exception):
            return None

    @staticmethod
    def extract_all_texts(parent: Any, selector: str) -> list[str]:
        try:
            elements = parent.find_elements(By.CSS_SELECTOR, selector)
            return [t for elem in elements if (t := (elem.text or "").strip())]
        except Exception:
            return []

    @staticmethod
    def extract_all_attrs(parent: Any, selector: str, attr: str) -> list[str]:
        try:
            elements = parent.find_elements(By.CSS_SELECTOR, selector)
            return [a for elem in elements if (a := (elem.get_attribute(attr) or "").strip())]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Scroll / interaction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def scroll_to_bottom(page: uc.Chrome, pause_ms: int = 1500) -> None:
        logger.debug("Scrolling to bottom of page...")
        page.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause_ms / 1000.0)

    @staticmethod
    def scroll_n_times(page: uc.Chrome, n: int, pause_ms: int = 1500) -> None:
        for i in range(n):
            logger.debug("Scroll %d/%d", i + 1, n)
            fraction = random.uniform(0.6, 1.0)
            page.execute_script(
                f"window.scrollBy({{ top: window.innerHeight * {fraction:.2f}, behavior: 'smooth' }});"
            )
            jitter = pause_ms * random.uniform(0.7, 1.3)
            time.sleep(jitter / 1000.0)

    @staticmethod
    def human_mouse_move(page: uc.Chrome) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        x = random.randint(200, 1600)
        y = random.randint(200, 900)
        ActionChains(page).move_by_offset(x, y).perform()

    @staticmethod
    def polite_delay(min_ms: int = 1000, max_ms: int = 3000) -> None:
        delay = random.randint(min_ms, max_ms) / 1000.0
        logger.debug("Polite delay: %.2fs", delay)
        time.sleep(delay)

    @staticmethod
    def screenshot_on_error(page: uc.Chrome, name: str, screenshots_dir: Path) -> None:
        """Capture a PNG screenshot on scraping failure."""
        try:
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            path = screenshots_dir / f"error_{name}_{timestamp}.png"
            page.save_screenshot(str(path))
            logger.info("Saved error screenshot to %s", path)
        except Exception as exc:
            logger.error("Failed to capture screenshot: %s", exc)
