"""Shared utility helpers — hashing, URL canonicalization, text cleanup."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse, urljoin


def generate_listing_hash(
    source_id: int,
    title: str | None,
    price_text: str | None,
    location_text: str | None,
) -> str:
    """Produce a deterministic hash for dedup from core listing fields.

    The hash is computed from (source_id, title, price_text, location_text).
    All inputs are lowered and stripped before hashing.
    """
    parts = [
        str(source_id),
        (title or "").strip().lower(),
        (price_text or "").strip().lower(),
        (location_text or "").strip().lower(),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def canonicalize_url(url: str, strip_query: bool = False) -> str:
    """Normalize a URL for dedup comparison.

    - Strips fragment
    - Optionally strips query string
    - Lowercases scheme and host
    - Removes trailing slash
    """
    if not url:
        return ""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    query = "" if strip_query else parsed.query
    return urlunparse((scheme, netloc, path, "", query, ""))


def absolutize_url(url: str, base_url: str) -> str:
    """Make a relative URL absolute using a base URL."""
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base_url, url)


def clean_whitespace(text: str | None) -> str | None:
    """Collapse runs of whitespace and strip."""
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned if cleaned else None


def safe_int(value: str | None) -> int | None:
    """Try to parse an integer; return None on failure."""
    if value is None:
        return None
    try:
        return int(re.sub(r"[^\d\-]", "", value))
    except (ValueError, TypeError):
        return None


def safe_float(value: str | None) -> float | None:
    """Try to parse a float; return None on failure."""
    if value is None:
        return None
    try:
        cleaned = re.sub(r"[^\d.\-,]", "", value).replace(",", ".")
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def truncate(text: str | None, max_len: int = 500) -> str | None:
    """Truncate text to max_len characters, appending '…' if cut."""
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
