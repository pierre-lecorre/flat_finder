"""Local image downloader and link manager.

Downloads listing images to 'data/images/' and tracks them in the database.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from app.logging_config import get_logger
from app.models import ImageRecord, RawListing

logger = get_logger("app.images")


def download_image(
    url: str,
    save_dir: Path,
    listing_id: int,
    is_primary: bool,
    index: int,
    timeout: int = 15,
) -> Optional[str]:
    """Download an image from a URL and save it locally.

    Returns the relative path string of the saved image within the workspace,
    or None if download fails.
    """
    if not url:
        return None

    try:
        # Determine file extension from URL or fallback to jpg
        ext = ".jpg"
        clean_url = url.split("?")[0]
        for candidate in (".png", ".webp", ".jpeg", ".jpg", ".gif"):
            if clean_url.lower().endswith(candidate):
                ext = candidate
                break

        # Avoid duplicate downloads in the same directory by hashing URL
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
        filename = f"listing_{listing_id}_{index}_{url_hash}{ext}"
        filepath = save_dir / filename

        # If already exists, skip downloading but return relative path
        if filepath.exists():
            return str(filepath.relative_to(save_dir.parent.parent))

        # Perform request
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(resp.content)

        logger.debug("Successfully downloaded image: %s", url)
        # Return path relative to project data directory
        return str(filepath.relative_to(save_dir.parent.parent.parent))

    except Exception as exc:
        logger.warning(
            "Failed to download image %s for listing %d: %s", url, listing_id, exc
        )
        return None


def download_listing_images(
    raw_listing: RawListing,
    images_dir: Path,
    max_images: int = 5,
) -> list[ImageRecord]:
    """Download main and extra images for a raw listing and return ImageRecords."""
    if not raw_listing.id:
        logger.warning("Cannot download images for listing without an ID.")
        return []

    # Setup source specific directory to keep directories clean
    source_dir = images_dir / f"source_{raw_listing.source_id}"
    source_dir.mkdir(parents=True, exist_ok=True)

    records: list[ImageRecord] = []
    urls_to_download: list[tuple[str, bool]] = []

    # 1. Add primary image if present
    if raw_listing.raw_image_url:
        urls_to_download.append((raw_listing.raw_image_url, True))

    # 2. Add extra images
    extra_urls = raw_listing.get_image_urls()
    for url in extra_urls:
        # Don't duplicate the primary image URL
        if url != raw_listing.raw_image_url:
            urls_to_download.append((url, False))

    # Download up to max_images limit
    download_count = 0
    for idx, (url, is_primary) in enumerate(urls_to_download):
        if download_count >= max_images:
            break

        local_path = download_image(
            url=url,
            save_dir=source_dir,
            listing_id=raw_listing.id,
            is_primary=is_primary,
            index=idx,
        )

        if local_path:
            records.append(
                ImageRecord(
                    raw_listing_id=raw_listing.id,
                    image_url=url,
                    local_path=local_path,
                    is_primary=is_primary,
                    downloaded_at=datetime.utcnow(),
                )
            )
            download_count += 1

    return records
