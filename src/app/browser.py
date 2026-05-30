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

# ---------------------------------------------------------------------------
# Stealth init script — injected before ANY page JS executes.
# Patches every primary signal Facebook's bot-detection pipeline checks:
#   - navigator.webdriver
#   - navigator.plugins / mimeTypes
#   - navigator.languages
#   - window.chrome runtime
#   - Notification.permission spoofing
#   - WebGL vendor / renderer strings
#   - hardware concurrency & device memory
#   - chrome cdc_ / $cdc_ driver handle (Playwright-specific)
#   - Permission query override (headless returns 'denied' by default)
# ---------------------------------------------------------------------------
_STEALTH_SCRIPT = """
// 1. Primary webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. Remove Playwright-injected globals
try { delete window.__playwright; } catch(_) {}
try { delete window.__pw_manual; } catch(_) {}
try { delete window._playwrightChannelHandle; } catch(_) {}

// 3. Plugins — headless Chrome reports [] which is an instant giveaway
const makePlugin = (name, filename, description) => {
    const p = Object.create(Plugin.prototype);
    Object.defineProperty(p, 'name',        { value: name });
    Object.defineProperty(p, 'filename',    { value: filename });
    Object.defineProperty(p, 'description', { value: description });
    return p;
};
const fakePlugins = [
    makePlugin('Chrome PDF Plugin',           'internal-pdf-viewer',   'Portable Document Format'),
    makePlugin('Chrome PDF Viewer',           'mhjfbmdgcfjbbpaeojofohoefgiehjai', ''),
    makePlugin('Native Client',               'internal-nacl-plugin',  ''),
    makePlugin('Widevine Content Decryption Module', 'widevinecdmadapter.dll', 'Enables Widevine licenses for playback'),
    makePlugin('Microsoft Edge PDF Viewer',   'msedgepdfexe',          'Portable Document Format'),
];
Object.defineProperty(navigator, 'plugins', {
    get: () => fakePlugins,
    configurable: true,
});

// 4. Languages — match a typical Czech/English browser
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en', 'cs'],
    configurable: true,
});

// 5. Hardware concurrency + device memory (headless sometimes reports odd values)
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });

// 6. window.chrome — must look exactly like a real installed Chrome
window.chrome = {
    app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
    runtime: {
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
        OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
        PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
        RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
    },
};

// 7. Notification.permission — headless returns 'denied' which FB flags
const origQuery = window.Notification ? window.Notification.requestPermission.bind(Notification) : null;
try {
    Object.defineProperty(Notification, 'permission', { get: () => 'default' });
} catch(_) {}

// 8. Permissions API — navigator.permissions.query({ name: 'notifications' })
//    returns 'denied' in headless; spoof to 'default'
const origPermQuery = navigator.permissions && navigator.permissions.query.bind(navigator.permissions);
if (origPermQuery) {
    navigator.permissions.query = (params) => {
        if (params && params.name === 'notifications') {
            return Promise.resolve({ state: 'default', onchange: null });
        }
        return origPermQuery(params);
    };
}

// 9. WebGL fingerprint — headless often reports SwiftShader which is a bot signal
try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';           // UNMASKED_VENDOR_WEBGL
        if (param === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
        return getParam.call(this, param);
    };
} catch(_) {}

// 10. Remove cdc_ / $cdc_ Playwright driver identifier in DOM
(function removeCDC() {
    try {
        const cdcProp = Object.keys(document).find(k => k.startsWith('cdc_') || k.startsWith('$cdc_'));
        if (cdcProp) delete document[cdcProp];
    } catch (_) {}
})();

// 11. Spoof screen dimensions consistent with a real desktop monitor
Object.defineProperty(screen, 'width',       { get: () => 1920 });
Object.defineProperty(screen, 'height',      { get: () => 1080 });
Object.defineProperty(screen, 'availWidth',  { get: () => 1920 });
Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
Object.defineProperty(screen, 'colorDepth',  { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });
"""


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

        self.browser = self._playwright.chromium.launch(
            headless=is_headless,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--window-size=1280,800",
            ],
        )

        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self.context.set_default_timeout(30000)

    def get_persistent_context(
        self, storage_state_path: str, headless: Optional[bool] = None
    ) -> BrowserContext:
        """Create or launch a persistent browser context for logged-in sessions.

        Uses the user's real Chrome installation (channel='chrome') + a
        comprehensive stealth init-script to bypass Facebook's bot detection.
        """
        if self._playwright:
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

        user_data = self.settings.browser_state_dir / "user_data"
        user_data.mkdir(parents=True, exist_ok=True)

        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            channel="chrome",
            headless=is_headless,
            # Mimic a real 1920x1080 desktop — consistent with screen spoofing below
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Europe/Prague",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            # Critical: strip --enable-automation flag which FB and others check
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-infobars",
                "--start-maximized",
                # Suppress the infobar that says "Chrome is being controlled by..."
                "--disable-notifications",
                # Avoid the obvious "Headless" string in the user-agent via Chrome internals
                "--hide-crash-restore-bubble",
            ],
            # Grant permissions upfront so FB doesn't see a 'denied' Notification permission
            permissions=["notifications", "geolocation"],
        )
        self.context.set_default_timeout(30000)

        # Inject the full stealth patch-set before any page JS can run
        self.context.add_init_script(_STEALTH_SCRIPT)

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
        """Safely extract stripped inner text from a element or locator."""
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
        """Safely extract HTML attribute value from a selector."""
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
        """Scroll page multiple times with human-like jitter to avoid bot patterns."""
        for i in range(n):
            logger.debug("Scroll execution: %d/%d", i + 1, n)
            # Scroll in chunks rather than jumping to absolute bottom — looks more human
            page.evaluate(
                "window.scrollBy({ top: window.innerHeight * 0.85, behavior: 'smooth' })"
            )
            # Randomise pause between scrolls ±30% around the base value
            jitter = pause_ms * random.uniform(0.7, 1.3)
            time.sleep(jitter / 1000.0)

    @staticmethod
    def human_mouse_move(page: Page) -> None:
        """Move mouse to a random screen position to simulate human presence."""
        x = random.randint(200, 1600)
        y = random.randint(200, 900)
        page.mouse.move(x, y)

    @staticmethod
    def polite_delay(min_ms: int = 1000, max_ms: int = 3000) -> None:
        """Polite rate limiting delay using random sleep intervals."""
        delay = random.randint(min_ms, max_ms) / 1000.0
        logger.debug("Polite delay: sleeping for %.2fs", delay)
        time.sleep(delay)

    @staticmethod
    def screenshot_on_error(page: Page, name: str, screenshots_dir: Path) -> None:
        """Capture standard PNG screenshot on scraping failure."""
        from datetime import datetime
        try:
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"error_{name}_{timestamp}.png"
            path = screenshots_dir / filename
            page.screenshot(path=path)
            logger.info("Saved error debug screenshot to %s", path)
        except Exception as exc:
            logger.error("Failed to capture error screenshot: %s", exc)
