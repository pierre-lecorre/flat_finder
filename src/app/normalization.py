"""Listing normalization and Prague suitability scoring.

Combines deterministic parsing results and computes a suitability score (0-100)
for Czech flats/flatshares based on proximity to New Palmovka / The Docks
and other housing preferences.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.config import ScoringWeights
from app.models import EnrichedListing, RawListing, ValidationStatus
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
from app.utils import canonicalize_url, clean_whitespace


def compute_commute_note(district: Optional[str]) -> str:
    """Assess commute to New Palmovka / The Docks (Praha 8) based on district."""
    if not district:
        return "Commute status unknown: no district provided."

    dist_lower = district.lower()

    # Direct / extremely fast districts
    if any(k in dist_lower for k in ("palmovka", "libeň", "liben", "karlín", "karlin", "praha 8", "invalidovna")):
        return "Excellent! In or adjacent to Praha 8. Direct commute (<15 mins) to Palmovka."

    # Very good districts
    if any(k in dist_lower for k in ("vysočany", "vysocany", "holešovice", "holesovice", "praha 7", "žižkov", "zizkov", "praha 3", "florenc")):
        return "Very good. Adjacent to Praha 8. Fast commute (15-25 mins) via tram/metro."

    # Moderate districts
    if any(k in dist_lower for k in ("praha 1", "praha 2", "vinohrady", "vršovice", "vrsovice", "praha 10", "strašnice", "strasnice", "kobylisy", "prosek")):
        return "Moderate. Core Prague area. Standard commute (25-35 mins) with one transfer."

    # Far districts
    return "Far. Commute to Palmovka likely exceeds 35-40 mins."


def compute_suitability_score(enriched: EnrichedListing, weights: ScoringWeights) -> int:
    """Calculate flat suitability score (0 to 100) for Prague housing context.

    Rules:
    - Commute proximity to Palmovka (up to 25 pts)
    - Price within budget (up to 20 pts)
    - Preferred layouts (up to 15 pts)
    - Area size >= 25m2 (up to 10 pts)
    - Furnished preference match (up to 10 pts)
    - Professional adult vs student share (up to 10 pts)
    - Extra amenities (balcony, elevator, parking, pets) (up to 10 pts)
    - Critical exclusions (women-only, student heavy) -> Penalty (reduces score to 0)
    """
    score = 0.0

    # 1. Commute/District Proximity (25 pts)
    if enriched.district:
        dist_lower = enriched.district.lower()
        # Direct
        if any(k in dist_lower for k in ("palmovka", "libeň", "liben", "karlín", "karlin", "praha 8", "invalidovna")):
            score += 25.0
        # Very Good
        elif any(k in dist_lower for k in ("vysočany", "vysocany", "holešovice", "holesovice", "praha 7", "žižkov", "zizkov", "praha 3", "florenc")):
            score += 18.0
        # Moderate
        elif any(k in dist_lower for k in ("praha 1", "praha 2", "vinohrady", "vršovice", "vrsovice", "praha 10", "strašnice", "strasnice", "kobylisy", "prosek")):
            score += 10.0
        else:
            score += 2.0

    # 2. Price Suitability (20 pts)
    price = enriched.total_monthly_value or enriched.price_value
    if price:
        if price <= weights.ideal_price_czk:
            score += 20.0
        elif price <= weights.max_price_czk:
            # Linear decay from ideal to max price
            ratio = (weights.max_price_czk - price) / (weights.max_price_czk - weights.ideal_price_czk)
            score += 20.0 * ratio
        else:
            # Over budget gets 0 for this category
            pass

    # 3. Layout Matching (15 pts)
    if enriched.layout:
        layout_norm = enriched.layout.lower()
        if layout_norm in [l.lower() for l in weights.preferred_layouts]:
            score += 15.0
        elif "room" in layout_norm or "pokoj" in layout_norm:
            score += 10.0  # room share is acceptable
        else:
            score += 5.0

    # 4. Area (10 pts)
    # Target full flat min size is 25m2. If flatshare, room size doesn't need to be 25m2.
    if enriched.area_m2:
        if enriched.area_m2 >= weights.min_area_m2:
            score += 10.0
        else:
            # Linear drop for smaller flats
            ratio = enriched.area_m2 / weights.min_area_m2
            score += 10.0 * ratio
    else:
        score += 5.0  # neutral default

    # 5. Furnished Status Match (10 pts)
    if enriched.furnished_status:
        status = enriched.furnished_status.lower()
        if status == "yes":
            score += 10.0
        elif status == "partially":
            score += 7.0
        elif status == "no":
            score += 4.0
        else:
            score += 5.0

    # 6. Flatshare / Professional adult bonus (10 pts)
    # Extra points for professional adult shares.
    if enriched.flatshare:
        if enriched.room_private:
            score += 10.0  # private room in flatshare is highly preferred
        else:
            score += 5.0
    else:
        score += 8.0  # private full flats are also great

    # 7. Extras (Balcony, Elevator, Garage) (10 pts)
    extras = 0.0
    if enriched.balcony:
        extras += 4.0
    if enriched.elevator:
        extras += 3.0
    if enriched.parking:
        extras += 3.0
    score += min(extras, 10.0)

    # 8. Exclusions & Penalties
    # Check validation status
    if enriched.validation_status == ValidationStatus.EXCLUDED:
        return 0

    return max(0, min(100, int(round(score))))


def normalize_raw_listing(
    raw: RawListing,
    weights: ScoringWeights,
    default_city: str = "Prague",
    default_currency: str = "CZK",
) -> EnrichedListing:
    """Run deterministic extraction pipeline on raw listing to produce EnrichedListing.

    This operates BEFORE LLM pass and extracts all easy/obvious elements.
    """
    # Deterministic price parsing
    price_val, currency = parse_price(raw.raw_price_text)
    if not currency:
        currency = default_currency

    fees_val, _ = parse_price(raw.raw_fees_text)
    deposit_val, _ = parse_price(raw.raw_deposit_text)

    # Total monthly value estimate
    total_val = None
    if price_val:
        total_val = price_val
        if fees_val:
            total_val += fees_val

    # Parse layout, area, and other fields
    layout_norm = normalize_layout(raw.raw_layout_text)
    if not layout_norm and raw.raw_title:
        # Fallback to layout from title
        layout_norm = normalize_layout(raw.raw_title)

    area_val = parse_area(raw.raw_area_text)
    if not area_val and raw.raw_title:
        area_val = parse_area(raw.raw_title)

    # Contextual fields parsed from combined title and description
    desc = raw.raw_description or ""
    search_text = f"{(raw.raw_title or '')} {desc}"
    
    furnished = parse_furnished(search_text)
    if furnished == "unknown" and raw.raw_metadata_json:
        # Check raw metadata
        meta = raw.get_metadata()
        furnished = parse_furnished(str(meta))

    balcony = parse_balcony(search_text)
    elevator = parse_elevator(search_text)
    parking = parse_parking(search_text)
    pets = parse_pets(search_text)

    # Simple flatshare heuristics
    is_flatshare = False
    is_room_private = None
    if layout_norm == "room" or "spolubydl" in desc.lower() or "flatshare" in desc.lower() or "room" in desc.lower():
        is_flatshare = True
        is_room_private = True  # standard assumption unless stated

    # Commute assessment
    district = clean_whitespace(raw.raw_location_text)
    commute_note = compute_commute_note(district)

    # Validation check for exclusions
    validation = ValidationStatus.UTILITIES_UNCONFIRMED.value
    if fees_val:
        validation = ValidationStatus.VERIFIED.value

    # Check immediate exclusion keywords
    for kw in weights.exclude_keywords:
        if kw.lower() in desc.lower() or kw.lower() in (raw.raw_title or "").lower():
            validation = ValidationStatus.EXCLUDED.value
            break

    # Build intermediate enriched model
    enriched = EnrichedListing(
        raw_listing_id=raw.id or 0,
        property_type="flat" if not is_flatshare else "room",
        offer_type="rent",
        price_value=price_val,
        currency=currency,
        fees_value=fees_val,
        total_monthly_value=total_val,
        deposit_value=deposit_val,
        district=district,
        city=default_city,
        address=None,
        layout=layout_norm,
        area_m2=area_val,
        furnished_status=furnished,
        balcony=balcony,
        elevator=elevator,
        parking=parking,
        pets_allowed=pets,
        flatshare=is_flatshare,
        room_private=is_room_private,
        owner_or_agency="unknown",
        available_from=None,
        validation_status=validation,
        suitability_score=0,  # calculated below
        commute_note=commute_note,
        llm_summary=None,
        llm_json=None,
        enriched_at=datetime.utcnow(),
    )

    # Compute final score
    enriched.suitability_score = compute_suitability_score(enriched, weights)

    return enriched
