"""
HTML snapshot tests for the bezrealitky.yaml config selectors.

These tests load a local HTML snapshot of the Bezrealitky listing/detail page and
verify that the CSS selectors defined in configs/sites/bezrealitky.yaml
actually find the expected data.

Run with:  pytest tests/test_bezrealitky_selectors.py -v

Snapshot files expected at:
  tests/fixtures/bezrealitky_listing.html   (save from .cz or .com search results)
  tests/fixtures/bezrealitky_detail.html    (save from any individual listing page)

Notes on URL paths:
  .cz domain: /nemovitosti-byty-domy/<id>-<slug>
  .com domain: /properties-flats-houses/<id>-<slug>
  Tests accept both patterns.
"""

import re
from pathlib import Path

import pytest

try:
    from bs4 import BeautifulSoup
except ImportError:
    pytest.skip("beautifulsoup4 not installed", allow_module_level=True)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
LISTING_HTML = FIXTURES_DIR / "bezrealitky_listing.html"
DETAIL_HTML = FIXTURES_DIR / "bezrealitky_detail.html"

# Both .cz and .com path fragments — tested together everywhere.
_LISTING_PATH_FRAGMENTS = ["/nemovitosti-byty-domy", "/properties-flats-houses"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_soup(path: Path) -> BeautifulSoup:
    return BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")


def attr_selector(soup, css: str, attr: str) -> list[str]:
    """Return a list of (text or attribute) values for all matching elements."""
    results = []
    for el in soup.select(css):
        if attr == "text":
            results.append(el.get_text(strip=True))
        else:
            val = el.get(attr, "")
            if val:
                results.append(val)
    return results


def listing_urls(soup) -> list[str]:
    """Collect listing hrefs matching either .cz or .com path pattern."""
    urls = []
    for frag in _LISTING_PATH_FRAGMENTS:
        urls.extend(attr_selector(soup, f"a[href*='{frag}']", "href"))
    return list(dict.fromkeys(urls))  # deduplicate, preserve order


def extract_listing_id(href: str) -> re.Match | None:
    """Extract numeric ID from either /properties-flats-houses/<id>- or /nemovitosti-byty-domy/<id>-."""
    return re.search(r"/(?:properties-flats-houses|nemovitosti-byty-domy)/(\d+)-", href)


# ---------------------------------------------------------------------------
# Listing page tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not LISTING_HTML.exists(), reason="Snapshot not present — save page HTML to tests/fixtures/bezrealitky_listing.html")
class TestBezrealitkyListingSelectors:
    """Validate selectors against a saved HTML snapshot of the search results page."""

    @pytest.fixture(scope="class")
    def soup(self):
        return load_soup(LISTING_HTML)

    # --- Card container ---

    def test_listing_cards_found(self, soup):
        cards = soup.select("article.propertyCard")
        assert len(cards) > 0, (
            "No article.propertyCard elements found. "
            "The site may have changed its markup. "
            "Check the listing_card_selector in bezrealitky.yaml."
        )

    def test_listing_cards_count_reasonable(self, soup):
        cards = soup.select("article.propertyCard")
        assert 5 <= len(cards) <= 50, f"Unexpected card count: {len(cards)}"

    # --- Address / title ---

    def test_address_selector(self, soup):
        cards = soup.select("article.propertyCard")
        assert cards, "No cards found"
        first_card = cards[0]
        addresses = attr_selector(first_card, "span[class*='propertyCardAddress']", "text")
        assert addresses, "Address selector matched nothing on first card"
        assert len(addresses[0]) > 3, f"Address looks empty: {addresses[0]!r}"

    def test_address_contains_prague(self, soup):
        """At least one listing address should mention Prague."""
        all_addresses = attr_selector(soup, "span[class*='propertyCardAddress']", "text")
        prague_count = sum(1 for a in all_addresses if "Prague" in a or "Praha" in a or "Prag" in a)
        assert prague_count > 0, "None of the address fields mention Prague"

    # --- Listing URL ---

    def test_listing_url_selector(self, soup):
        """Cards must contain a link to a listing detail page (either .cz or .com path)."""
        cards = soup.select("article.propertyCard")
        assert cards, "No cards found"
        first_card = cards[0]
        urls = listing_urls(first_card)
        assert urls, (
            "Listing URL selector matched nothing on first card. "
            "Expected href containing '/properties-flats-houses' or '/nemovitosti-byty-domy'."
        )
        assert any(frag in urls[0] for frag in _LISTING_PATH_FRAGMENTS), f"Unexpected URL: {urls[0]!r}"

    def test_listing_urls_are_unique(self, soup):
        urls = listing_urls(soup)
        assert len(urls) >= 3, f"Expected multiple unique listing URLs, got: {urls}"

    # --- Price ---

    def test_price_selector(self, soup):
        prices = attr_selector(soup, "span[class*='propertyPriceAmount']", "text")
        assert prices, "Price selector matched nothing"

    def test_price_contains_czk(self, soup):
        prices = attr_selector(soup, "span[class*='propertyPriceAmount']", "text")
        czk_prices = [p for p in prices if "CZK" in p or "Kč" in p or re.search(r"\d", p)]
        assert czk_prices, f"No price contains a number or CZK. Got: {prices}"

    def test_price_additional_selector(self, soup):
        """Additional charges field (service costs) should be present on most listings."""
        additional = attr_selector(soup, "span[class*='propertyPriceAdditional']", "text")
        assert isinstance(additional, list)

    # --- Label ---

    def test_listing_type_label_selector(self, soup):
        """Card label uses offerCardLabel class on .com HTML."""
        # Try both known class fragments (site serves different class names on .cz vs .com)
        labels = (
            attr_selector(soup, "span[class*='offerCardLabel']", "text") or
            attr_selector(soup, "span[class*='propertyCardLabel']", "text")
        )
        assert labels, "Listing type label selector matched nothing (tried offerCardLabel and propertyCardLabel)"
        rental_labels = [
            l for l in labels
            if "rent" in l.lower() or "pronajem" in l.lower() or "pronájem" in l.lower()
        ]
        assert rental_labels, f"No label mentions rent. Got: {labels[:5]}"

    # --- Image ---

    def test_image_url_selector(self, soup):
        imgs = attr_selector(soup, "div[class*='propertyCardImageHolder'] img", "src")
        assert isinstance(imgs, list)

    # --- Pagination ---

    def test_pagination_links_present(self, soup):
        pagination = soup.select("ul.pagination li a")
        assert len(pagination) >= 1, "Expected pagination links on a full listing page"


# ---------------------------------------------------------------------------
# Detail page tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DETAIL_HTML.exists(), reason="Detail snapshot not present — save a listing detail page to tests/fixtures/bezrealitky_detail.html")
class TestBezrealitkyDetailSelectors:
    """Validate detail-page selectors against a saved HTML snapshot."""

    @pytest.fixture(scope="class")
    def soup(self):
        return load_soup(DETAIL_HTML)

    def test_detail_description_selector(self, soup):
        """Description text block — tries multiple known class fragments."""
        descs = (
            attr_selector(soup, "div[class*='offerDetail']", "text") or
            attr_selector(soup, "div[class*='OfferDetail']", "text") or
            attr_selector(soup, "div[class*='description']", "text") or
            attr_selector(soup, "div[class*='Description']", "text")
        )
        # Fall back: any long paragraph-like div (>80 chars)
        if not descs:
            for div in soup.find_all("div"):
                txt = div.get_text(strip=True)
                if len(txt) > 80 and len(txt) < 5000:
                    classes = " ".join(div.get("class", []))
                    if any(k in classes.lower() for k in ["detail", "content", "text", "body", "info"]):
                        descs = [txt]
                        break
        assert descs, "Could not find a description/detail text block on the detail page"
        assert len(descs[0]) > 10, "Description text suspiciously short"

    def test_detail_id_from_url_pattern(self, soup):
        """Numeric listing ID must be extractable from the canonical URL.

        Supports both:
          .cz  /nemovitosti-byty-domy/<id>-<slug>
          .com /properties-flats-houses/<id>-<slug>
        """
        canonical = soup.find("link", rel="canonical")
        if not canonical or not canonical.get("href"):
            pytest.skip("No canonical link found in detail snapshot")
        href = canonical["href"]
        match = extract_listing_id(href)
        # If canonical points to /search (listing page saved by mistake), skip gracefully
        if "/search" in href or "/vyhledat" in href:
            pytest.skip(
                f"Detail snapshot canonical URL points to search page: {href!r}. "
                "Re-save a proper listing detail page."
            )
        assert match, (
            f"Could not extract numeric ID from canonical URL: {href!r}. "
            "Expected pattern: /properties-flats-houses/<id>- or /nemovitosti-byty-domy/<id>-"
        )


# ---------------------------------------------------------------------------
# Config sanity tests (always run — no fixture file needed)
# ---------------------------------------------------------------------------

class TestBezrealitkyConfigSanity:
    """Verify the YAML config loads and contains required keys."""

    @pytest.fixture(scope="class")
    def config(self):
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")
        config_path = Path(__file__).parent.parent / "configs" / "sites" / "bezrealitky.yaml"
        assert config_path.exists(), f"Config not found: {config_path}"
        return yaml.safe_load(config_path.read_text(encoding="utf-8"))

    def test_site_name(self, config):
        assert config["site_name"] == "bezrealitky"

    def test_enabled(self, config):
        assert config["enabled"] is True

    def test_listing_card_selector(self, config):
        assert config["listing_card_selector"] == "article.propertyCard"

    def test_wait_for_selector(self, config):
        assert config["wait_rules"]["wait_for_selector"] == "article.propertyCard"

    def test_detail_link_selector(self, config):
        """detail_page.link_selector must cover both .cz and .com URL patterns."""
        selector = config["detail_page"]["link_selector"]
        assert "/properties-flats-houses" in selector or "/nemovitosti-byty-domy" in selector, (
            f"link_selector must contain at least one of the known listing path fragments, got: {selector!r}"
        )

    def test_required_fields_present(self, config):
        fields = config.get("fields", {})
        assert "title" in fields
        assert "price_text" in fields
        assert "listing_url" in fields

    def test_price_selector_uses_partial_match(self, config):
        """Selectors must use [class*='...'] because Bezrealitky uses hashed class names."""
        price_selector = config["fields"]["price_text"]["selector"]
        assert "class*=" in price_selector, (
            f"Price selector should use [class*='...'] for hashed class names, got: {price_selector!r}"
        )

    def test_address_selector_uses_partial_match(self, config):
        title_selector = config["fields"]["title"]["selector"]
        assert "class*=" in title_selector, (
            f"Address selector should use [class*='...'], got: {title_selector!r}"
        )

    def test_start_urls_not_empty(self, config):
        assert config["start_urls"], "start_urls is empty"
        assert "bezrealitky" in config["start_urls"][0]
