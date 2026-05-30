"""Deterministic parsers for listing fields.

Extracts prices, areas, layouts, and amenities from raw text fields
before LLM processing to maximize accuracy.
"""

from __future__ import annotations

import re
from typing import Optional


def parse_price(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Parse a numeric price and currency from a string.

    Handles formats like:
    - '25 000 Kč' -> (25000.0, 'CZK')
    - 'CZK 24,500' -> (24500.0, 'CZK')
    - '€850' -> (850.0, 'EUR')
    - '850 EUR' -> (850.0, 'EUR')
    - '$1,200' -> (1200.0, 'USD')
    - '25000 CZK/month' -> (25000.0, 'CZK')
    """
    if not text:
        return None, None

    # Clean text to remove extra whitespace but preserve digits, dot, comma, and currency symbols
    text_clean = re.sub(r"\s+", " ", text).strip()

    # Detect currency
    currency = "CZK"  # default
    if any(k in text_clean.upper() for k in ("EUR", "€", "EURO")):
        currency = "EUR"
    elif any(k in text_clean.upper() for k in ("USD", "$", "DOLLAR")):
        currency = "USD"
    elif any(k in text_clean.upper() for k in ("KČ", "CZK", "KORUN")):
        currency = "CZK"

    # Extract digits and potential decimal separators
    # Remove grouping characters: if comma followed by 3 digits (like 24,500), strip comma
    # If spaces within digits (like 25 000), strip spaces
    price_str = text_clean
    # Strip currency indicators to avoid confusing regex
    for indicator in ("CZK", "Kč", "EUR", "€", "USD", "$", "korun", "kc", "euro", "/měsíc", "/mesic", "month"):
        price_str = re.sub(re.escape(indicator), "", price_str, flags=re.IGNORECASE)

    # Find the main number
    # Support formats like 25.000 or 25 000 or 25000 or 24,500 or 850
    # Clean standard grouping characters
    price_str = price_str.replace(" ", "")
    # Check if it has a comma as decimal separator vs thousand separator
    # If comma is followed by exactly 3 digits at the end or before currency, and there are no other separators, it could be thousands
    # Let's use a safe regex to capture numbers:
    match = re.search(r"(\d[\d\s,.]*)", price_str)
    if not match:
        return None, None

    num_str = match.group(1)
    # If there is a comma followed by 3 digits, e.g. 24,500, and no period, it's a thousand separator
    if "," in num_str and "." not in num_str:
        parts = num_str.split(",")
        if len(parts) == 2 and len(parts[1]) == 3:
            num_str = num_str.replace(",", "")
        else:
            num_str = num_str.replace(",", ".")  # decimal comma e.g. 24,5
    elif "." in num_str and "," not in num_str:
        # e.g. 25.000 (often Czech thousand separator) or 25.5 (decimal)
        parts = num_str.split(".")
        if len(parts) == 2 and len(parts[1]) == 3:
            # Sreality / Bezrealitky use '.' as thousand separator e.g. 25.000 Kč
            num_str = num_str.replace(".", "")

    # Clean any remaining non-digit/non-dot characters
    num_str = re.sub(r"[^\d.]", "", num_str)

    try:
        val = float(num_str)
        return val, currency
    except ValueError:
        return None, None


def parse_area(text: Optional[str]) -> Optional[float]:
    """Parse numeric area in m² from text.

    Handles formats like:
    - '45 m²' -> 45.0
    - '45m2' -> 45.0
    - '45 sqm' -> 45.0
    """
    if not text:
        return None

    # Matches digit(s) optionally followed by space then m², m2, sqm, sq.m.
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2|sqm|sq\.\s*m|metr)", text, re.IGNORECASE)
    if not match:
        # Fallback to first floating number in text
        match = re.search(r"(\d+(?:[.,]\d+)?)", text)

    if not match:
        return None

    num_str = match.group(1).replace(",", ".")
    try:
        return float(num_str)
    except ValueError:
        return None


def normalize_layout(text: Optional[str]) -> Optional[str]:
    """Normalize flat/room layout strings into standard codes.

    Standard forms:
    - studio, 1+kk, 1+1, 2+kk, 2+1, 3+kk, 3+1, 4+kk, 4+1, room, house
    """
    if not text:
        return None

    text_upper = text.upper().strip()

    # Shared room/flatshare indicators must go FIRST (so Spolubydlení v rodinném domě maps to room instead of house)
    if any(k in text_upper for k in ("ROOM", "POKOJ", "SPOLUBYDLENÍ", "FLATSHARE", "BED", "LŮŽKO")):
        return "room"

    # House / Villa check second
    if any(k in text_upper for k in ("DOM", "DŮM", "VILA", "HOUSE", "CHATA")):
        return "house"

    # Layout regexes
    # e.g. 2+kk, 2+KK, 2 + KK, 2 kk
    layout_kk_match = re.search(r"([1-9])\s*(?:\+|PLUS)?\s*(?:KK|KUTOCH|KUCHYŇSKÝ KOUT)", text_upper)
    if layout_kk_match:
        return f"{layout_kk_match.group(1)}+kk"

    # e.g. 2+1, 2 + 1, 2plus1
    layout_plus_match = re.search(r"([1-9])\s*(?:\+|\s+PLUS\s*)\s*([1-9])", text_upper)
    if layout_plus_match:
        return f"{layout_plus_match.group(1)}+{layout_plus_match.group(2)}"

    # Garsonka / Studio / 1+kk equivalent
    if any(k in text_upper for k in ("GARSONKA", "GARSÓNKA", "GARSONIÉRA", "STUDIO", "GARSON")):
        return "1+kk"

    # Match simple digit kk/1 / block styles like "2 kk" or "3 1"
    simple_kk = re.search(r"\b([1-9])\s*KK\b", text_upper)
    if simple_kk:
        return f"{simple_kk.group(1)}+kk"

    simple_plus = re.search(r"\b([1-9])\s+([1-9])\b", text_upper)
    if simple_plus:
        return f"{simple_plus.group(1)}+{simple_plus.group(2)}"

    return text_upper.lower()


def parse_furnished(text: Optional[str]) -> str:
    """Detect furnished status from text (Czech and English).

    Returns 'yes', 'no', or 'unknown'.
    """
    if not text:
        return "unknown"

    text_lower = text.lower()

    # Furnished patterns
    furnished_kws = (
        "zařízen", "zarizen", "vybaven", "furnished", "fully equipped",
        "zařízený", "vybavený", "zařízeno"
    )
    # Unfurnished patterns
    unfurnished_kws = (
        "nezařízen", "nezarizen", "nevybaven", "unfurnished", "not furnished",
        "empty", "bez vybavení", "bez nabytku", "bez nábytku"
    )

    # Check unfurnished first (subset of furnished keywords often)
    if any(k in text_lower for k in unfurnished_kws):
        return "no"

    if any(k in text_lower for k in furnished_kws):
        return "yes"

    return "unknown"


def extract_boolean_feature(
    text: Optional[str],
    positive_keywords: list[str],
    negative_keywords: list[str],
) -> Optional[bool]:
    """Helper to detect presence of a feature based on keyword occurrence."""
    if not text:
        return None

    text_lower = text.lower()

    # Check negative mentions first e.g. "bez balkónu" / "no elevator"
    if any(k in text_lower for k in negative_keywords):
        return False

    if any(k in text_lower for k in positive_keywords):
        return True

    return None


def parse_balcony(text: Optional[str]) -> Optional[bool]:
    """Detect balcony / terrace presence from text using robust root matching."""
    # Use word roots to handle Czech inflections (e.g., terasa -> terasu, balkón -> balkónu)
    pos = ["balkón", "balkon", "teras", "lodž", "lodzi", "balcon", "terrac", "loggia"]
    neg = ["bez balkón", "bez balkon", "bez teras", "no balcon", "no terrac"]
    return extract_boolean_feature(text, pos, neg)


def parse_elevator(text: Optional[str]) -> Optional[bool]:
    """Detect elevator presence from text."""
    pos = ["výtah", "vytah", "lift", "elevator"]
    neg = ["bez výtah", "bez vytah", "no lift", "no elevator", "walk up"]
    return extract_boolean_feature(text, pos, neg)


def parse_parking(text: Optional[str]) -> Optional[bool]:
    """Detect parking presence from text."""
    pos = ["parkov", "garáž", "garaz", "stán", "stani", "parkin", "garage"]
    neg = ["bez parkov", "no parkin", "bez garáž"]
    return extract_boolean_feature(text, pos, neg)


def parse_pets(text: Optional[str]) -> Optional[bool]:
    """Detect if pets are allowed."""
    pos = ["zvíř", "zvir", "mazlí", "mazli", "pet", "dog", "cat"]
    neg = ["bez zvíř", "bez zvir", "no pet", "no dog", "no cat", "zvířata ne", "zvirata ne"]
    return extract_boolean_feature(text, pos, neg)
