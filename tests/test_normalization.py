"""Unit tests for deterministic parsers and scoring normalization."""

from __future__ import annotations

import pytest
from app.config import ScoringWeights
from app.models import EnrichedListing, RawListing, ValidationStatus
from app.normalization import (
    compute_commute_note,
    compute_suitability_score,
    normalize_raw_listing,
)
from app.parsers import (
    normalize_layout,
    parse_area,
    parse_balcony,
    parse_elevator,
    parse_furnished,
    parse_parking,
    parse_pets,
    parse_price,
)


def test_parse_price() -> None:
    # Czech korunas
    assert parse_price("25 000 Kč") == (25000.0, "CZK")
    assert parse_price("CZK 24,500") == (24500.0, "CZK")
    assert parse_price("25.000 Kč") == (25000.0, "CZK")
    assert parse_price("18000 CZK/month") == (18000.0, "CZK")

    # Euros
    assert parse_price("€850") == (850.0, "EUR")
    assert parse_price("850 EUR") == (850.0, "EUR")

    # Dollars
    assert parse_price("$1,200") == (1200.0, "USD")

    # Empty / Bad formats
    assert parse_price(None) == (None, None)
    assert parse_price("") == (None, None)
    assert parse_price("Negotiable price") == (None, None)


def test_parse_area() -> None:
    assert parse_area("45 m²") == 45.0
    assert parse_area("45m2") == 45.0
    assert parse_area("52.5 sqm") == 52.5
    assert parse_area("45 sq. m.") == 45.0
    assert parse_area("Velikost bytu je 60 metrů čtverečních") == 60.0
    assert parse_area(None) is None
    assert parse_area("no size mentioned") is None


def test_normalize_layout() -> None:
    assert normalize_layout("2+kk") == "2+kk"
    assert normalize_layout("2+KK") == "2+kk"
    assert normalize_layout("2 + kk") == "2+kk"
    assert normalize_layout("2 plus kk") == "2+kk"
    assert normalize_layout("1+1") == "1+1"
    assert normalize_layout("2 + 1") == "2+1"
    assert normalize_layout("Garsonka") == "1+kk"
    assert normalize_layout("Studio apartment") == "1+kk"
    assert normalize_layout("pokoj") == "room"
    assert normalize_layout("Spolubydlení v rodinném domě") == "room"
    assert normalize_layout("Vila 5+1") == "house"
    assert normalize_layout(None) is None


def test_parse_furnished() -> None:
    assert parse_furnished("Byt je kompletně zařízený nábytkem") == "yes"
    assert parse_furnished("Equipped with kitchen and bed, fully furnished") == "yes"
    assert parse_furnished("Byt se pronajímá jako nezařízený") == "no"
    assert parse_furnished("unfurnished flat share") == "no"
    assert parse_furnished("Krásný zrekonstruovaný byt") == "unknown"
    assert parse_furnished(None) == "unknown"


def test_boolean_amenities() -> None:
    # Balcony
    assert parse_balcony("Byt má krásnou prostornou terasu s výhledem") is True
    assert parse_balcony("bez balkónu") is False
    assert parse_balcony("krásná lokalita") is None

    # Elevator
    assert parse_elevator("Cihlový dům s výtahem") is True
    assert parse_elevator("bez výtahu") is False

    # Parking
    assert parse_parking("možnost garáže v domě") is True
    assert parse_parking("bez parkování") is False

    # Pets
    assert parse_pets("zvířata povolena po dohodě") is True
    assert parse_pets("bez zvířat") is False


def test_commute_note() -> None:
    assert "Excellent" in compute_commute_note("Libeň")
    assert "Excellent" in compute_commute_note("Karlín")
    assert "Very good" in compute_commute_note("Holešovice")
    assert "Very good" in compute_commute_note("Žižkov")
    assert "Far" in compute_commute_note("Praha 5 - Stodůlky")
    assert "unknown" in compute_commute_note(None)


def test_suitability_score() -> None:
    weights = ScoringWeights()
    # Verified listing near Karlin with good price
    enriched = EnrichedListing(
        raw_listing_id=1,
        property_type="flat",
        offer_type="rent",
        price_value=15000.0,
        currency="CZK",
        fees_value=3000.0,
        total_monthly_value=18000.0,
        district="Karlín, Praha 8",
        layout="2+kk",
        area_m2=45.0,
        furnished_status="yes",
        balcony=True,
        elevator=True,
        parking=False,
        pets_allowed=True,
        flatshare=False,
        room_private=None,
        validation_status=ValidationStatus.VERIFIED.value,
        suitability_score=0,
    )

    score = compute_suitability_score(enriched, weights)
    assert score > 70  # highly suitable for Palmovka target and budget

    # Excluded listing should return 0 score
    enriched.validation_status = ValidationStatus.EXCLUDED.value
    assert compute_suitability_score(enriched, weights) == 0


def test_normalize_raw_listing() -> None:
    weights = ScoringWeights()
    raw = RawListing(
        source_id=1,
        listing_url="https://example.com/flat",
        canonical_url="https://example.com/flat",
        raw_title="Krásný byt 2+kk, 45 m2 s balkónem",
        raw_price_text="17.000 Kč",
        raw_location_text="Karlín",
        raw_description="Byt je plně vybavený nábytkem. Výtah v domě.",
    )

    enriched = normalize_raw_listing(raw, weights)
    assert enriched.price_value == 17000.0
    assert enriched.layout == "2+kk"
    assert enriched.area_m2 == 45.0
    assert enriched.balcony is True
    assert enriched.elevator is True
    assert enriched.furnished_status == "yes"
    assert enriched.district == "Karlín"
    assert enriched.suitability_score > 60
