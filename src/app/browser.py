"""Playwright Sync API browser manager wrapper.

Provides convenient thread-safe wrappers for navigation, polite pauses,
element text/attribute extraction, scroll management, and screenshots.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Optional

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
# Full stealth init script — injected before ANY page JS executes.
#
# IMPORTANT: Do NOT use viewport=None in launch_persistent_context.
# That combination triggers a known Playwright bug where page.goto() hangs
# indefinitely (https://github.com/microsoft/playwright/issues/29572).
# Instead we keep a real viewport (1920x1080) and spoof window.outerWidth /
# window.outerHeight via this init script so they match. Facebook reads these
# four values and flags a mismatch as a bot signal.
# ---------------------------------------------------------------------------
_STEALTH_SCRIPT = """
// ── 1. navigator.webdriver ──────────────────────────────────────────────
// Must be `undefined`, NOT `false`.
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// ── 2. Remove Playwright-injected globals ────────────────────────────────
try { delete window.__playwright; } catch(_) {}
try { delete window.__pw_manual; } catch(_) {}
try { delete window._playwrightChannelHandle; } catch(_) {}
(function() {
    try {
        const k = Object.keys(document).find(k => k.startsWith('cdc_') || k.startsWith('$cdc_'));
        if (k) delete document[k];
    } catch(_) {}
})();

// ── 3. window.outerWidth / outerHeight ───────────────────────────────────
// Playwright sets the viewport (innerWidth/innerHeight) correctly but does
// NOT set outerWidth/outerHeight — they expose the real OS window size which
// may differ. Facebook reads all four values and flags the mismatch.
// We spoof them to match our 1920x1080 viewport.
try {
    Object.defineProperty(window, 'outerWidth',  { get: () => 1920, configurable: true });
    Object.defineProperty(window, 'outerHeight', { get: () => 1080, configurable: true });
} catch(_) {}

// ── 4. navigator.plugins + mimeTypes ────────────────────────────────────
const _pluginData = [
    { name: 'PDF Viewer',                description: 'Portable Document Format', filename: 'internal-pdf-viewer' },
    { name: 'Chrome PDF Viewer',         description: '',                          filename: 'internal-pdf-viewer' },
    { name: 'Chromium PDF Viewer',       description: '',                          filename: 'internal-pdf-viewer' },
    { name: 'Microsoft Edge PDF Viewer', description: '',                          filename: 'internal-pdf-viewer' },
    { name: 'WebKit built-in PDF',       description: '',                          filename: 'internal-pdf-viewer' },
];
const _plugins = _pluginData.map(d => {
    const p = Object.create(Plugin.prototype);
    Object.defineProperty(p, 'name',        { value: d.name,        enumerable: true });
    Object.defineProperty(p, 'description', { value: d.description, enumerable: true });
    Object.defineProperty(p, 'filename',    { value: d.filename,    enumerable: true });
    Object.defineProperty(p, 'length',      { value: 0,             enumerable: true });
    return p;
});
Object.defineProperty(navigator, 'plugins', {
    get: () => Object.assign(_plugins, {
        item:      i => _plugins[i] || null,
        namedItem: n => _plugins.find(p => p.name === n) || null,
        refresh:   () => {},
        length:    _plugins.length,
    }),
    configurable: true,
});
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => ({ length: 2, item: () => null, namedItem: () => null }),
    configurable: true,
});

// ── 5. navigator.languages / language ───────────────────────────────────
Object.defineProperty(navigator, 'language',  { get: () => 'en-US', configurable: true });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'cs'], configurable: true });

// ── 6. Hardware concurrency + device memory ──────────────────────────────
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8, configurable: true });

// ── 7. window.chrome (loadTimes + csi are critical — absence is a top FB signal)
window.chrome = {
    app: {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
    },
    runtime: {
        id: undefined,
        connect:     () => {},
        sendMessage: () => {},
        OnInstalledReason:       { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
        OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
        PlatformArch:            { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformOs:              { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
        RequestUpdateCheckStatus:{ NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
    },
    loadTimes: function() {
        return {
            requestTime:             Date.now() / 1000,
            startLoadTime:           Date.now() / 1000,
            commitLoadTime:          Date.now() / 1000,
            finishDocumentLoadTime:  0,
            finishLoadTime:          0,
            firstPaintTime:          0,
            firstPaintAfterLoadTime: 0,
            navigationType:          'Other',
            wasFetchedViaSpdy:              false,
            wasNpnNegotiated:               false,
            npnNegotiatedProtocol:          'unknown',
            wasAlternateProtocolAvailable:  false,
            connectionInfo:                 'http/1.1',
        };
    },
    csi: function() {
        return {
            startE:  Date.now(),
            onloadT: Date.now(),
            pageT:   3000 + Math.random() * 1000,
            tran:    15,
        };
    },
};

// ── 8. Notification.permission + permissions.query ──────────────────────
try {
    Object.defineProperty(Notification, 'permission', { get: () => 'default', configurable: true });
} catch(_) {}
const _origPermQuery = navigator.permissions && navigator.permissions.query.bind(navigator.permissions);
if (_origPermQuery) {
    navigator.permissions.query = (params) => {
        if (params && params.name === 'notifications') {
            return Promise.resolve({ state: 'default', onchange: null });
        }
        return _origPermQuery(params);
    };
}

// ── 9. WebGL fingerprint (SwiftShader is a direct bot signal) ─────────────
try {
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return _getParam.call(this, param);
    };
} catch(_) {}

// ── 10. Canvas fingerprint noise ─────────────────────────────────────────
try {
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, ...args) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const imgData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imgData.data.length; i += 100) {
                imgData.data[i] ^= Math.floor(Math.random() * 2);
            }
            ctx.putImageData(imgData, 0, 0);
        }
        return _toDataURL.call(this, type, ...args);
    };
} catch(_) {}

// ── 11. screen dimensions ────────────────────────────────────────────────
Object.defineProperty(screen, 'width',       { get: () => 1920, configurable: true });
Object.defineProperty(screen, 'height',      { get: () => 1080, configurable: true });
Object.defineProperty(screen, 'availWidth',  { get: () => 1920, configurable: true });
Object.defineProperty(screen, 'availHeight', { get: () => 1040, configurable: true });
Object.defineProperty(screen, 'colorDepth',  { get: () => 24,   configurable: true });
Object.defineProperty(screen, 'pixelDepth',  { get: () => 24,   configurable: true });

// ── 12. iframe contentWindow isolation ───────────────────────────────────
try {
    const _origCreateElement = document.createElement.bind(document);
    document.createElement = function(...args) {
        const el = _origCreateElement(...args);
        if (args[0] && args[0].toLowerCase() === 'iframe') {
            Object.defineProperty(el, 'contentWindow', {
                get: function() {
                    const win = HTMLIFrameElement.prototype.__lookupGetter__
                        ? HTMLIFrameElement.prototype.__lookupGetter__('contentWindow').call(this)
                        : null;
                    if (win) {
                        try { Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined, configurable: true }); } catch(_) {}
                        try { Object.defineProperty(win, 'outerWidth',  { get: () => 1920, configurable: true }); } catch(_) {}
                        try { Object.defineProperty(win, 'outerHeight', { get: () => 1080, configurable: true }); } catch(_) {}
                    }
                    return win;
                },
                configurable: true,
            });
        }
        return el;
    };
} catch(_) {}

// ── 13. Cloak toString() on patched functions ─────────────────────────────
const _cloakFn = (fn) => {
    if (!fn) return;
    try {
        Object.defineProperty(fn, 'toString', {
            value: () => `function ${fn.name || 'get'}() { [native code] }`,
            configurable: true,
            writable: true,
        });
    } catch(_) {}
};
try { _cloakFn(WebGLRenderingContext.prototype.getParameter); } catch(_) {}
try { _cloakFn(HTMLCanvasElement.prototype.toDataURL); } catch(_) {}
try { _cloakFn(document.createElement); } catch(_) {}
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

        Uses the real Chrome installation (channel='chrome') + a full stealth
        init-script to bypass Facebook's bot detection.

        NOTE: We intentionally pass a real viewport={1920, 1080} instead of
        viewport=None. Using viewport=None with launch_persistent_context +
        channel='chrome' triggers a known Playwright bug where page.goto()
        hangs indefinitely. The outerWidth/outerHeight mismatch is handled
        purely via the JS init script spoofing, which is sufficient.
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
            # Keep a real viewport — do NOT use viewport=None here (causes goto hang).
            # outerWidth/outerHeight are spoofed to match via the init script.
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
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
                "--window-size=1920,1080",
                "--window-position=0,0",
                "--disable-notifications",
                "--hide-crash-restore-bubble",
            ],
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
        """Safely extract stripped inner text from an element."""
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
        """Scroll page multiple times with human-like jitter."""
        for i in range(n):
            logger.debug("Scroll execution: %d/%d", i + 1, n)
            fraction = random.uniform(0.6, 1.0)
            page.evaluate(
                f"window.scrollBy({{ top: window.innerHeight * {fraction:.2f}, behavior: 'smooth' }})"
            )
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
