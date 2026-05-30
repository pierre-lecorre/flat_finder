"""Application settings and YAML site-config models.

Settings are loaded from environment variables and .env file.
Site configs are loaded from YAML files in the configs/sites/ directory
and validated with Pydantic at load time.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load .env from project root (if present)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Application-wide settings (from env vars / .env)
# ---------------------------------------------------------------------------

class AppSettings(BaseModel):
    """Global settings populated from environment variables."""

    # Ollama
    ollama_host: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3.1")
    ollama_timeout: int = Field(default=120)
    ollama_temperature: float = Field(default=0.1)

    # Paths
    project_root: Path = Field(default=_PROJECT_ROOT)
    data_dir: Path = Field(default=_PROJECT_ROOT / "data")
    configs_dir: Path = Field(default=_PROJECT_ROOT / "configs")
    db_path: Path = Field(default=_PROJECT_ROOT / "data" / "db" / "flat_finder.db")
    log_file: Path = Field(default=_PROJECT_ROOT / "data" / "logs" / "flat_finder.log")
    images_dir: Path = Field(default=_PROJECT_ROOT / "data" / "images")
    raw_dir: Path = Field(default=_PROJECT_ROOT / "data" / "raw")
    browser_state_dir: Path = Field(default=_PROJECT_ROOT / "data" / "browser_state")
    screenshots_dir: Path = Field(default=_PROJECT_ROOT / "data" / "screenshots")

    # Logging
    log_level: str = Field(default="INFO")

    # Browser defaults
    browser_headless: bool = Field(default=False)

    # Rate limiting defaults
    default_min_delay_ms: int = Field(default=1000)
    default_max_delay_ms: int = Field(default=3000)

    @classmethod
    def from_env(cls) -> AppSettings:
        """Create settings from current environment variables."""
        data_dir_str = os.getenv("DATA_DIR", str(_PROJECT_ROOT / "data"))
        data_dir = Path(data_dir_str) if Path(data_dir_str).is_absolute() else _PROJECT_ROOT / data_dir_str

        return cls(
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1"),
            ollama_timeout=int(os.getenv("OLLAMA_TIMEOUT", "120")),
            ollama_temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0.1")),
            data_dir=data_dir,
            db_path=data_dir / "db" / "flat_finder.db",
            log_file=data_dir / "logs" / "flat_finder.log",
            images_dir=data_dir / "images",
            raw_dir=data_dir / "raw",
            browser_state_dir=data_dir / "browser_state",
            screenshots_dir=data_dir / "screenshots",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            browser_headless=os.getenv("BROWSER_HEADLESS", "false").lower() == "true",
            default_min_delay_ms=int(os.getenv("DEFAULT_MIN_DELAY_MS", "1000")),
            default_max_delay_ms=int(os.getenv("DEFAULT_MAX_DELAY_MS", "3000")),
        )

    def ensure_directories(self) -> None:
        """Create all required data directories if they don't exist."""
        for d in [
            self.data_dir,
            self.data_dir / "db",
            self.data_dir / "raw",
            self.data_dir / "images",
            self.data_dir / "logs",
            self.data_dir / "browser_state",
            self.data_dir / "screenshots",
        ]:
            d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# YAML site-config Pydantic models
# ---------------------------------------------------------------------------

class FieldSelector(BaseModel):
    """CSS selector + attribute to extract for a single field."""
    selector: str = ""
    attr: str = "text"  # "text", "href", "src", or any HTML attribute


class DetailPageConfig(BaseModel):
    enabled: bool = False
    link_selector: str = ""


class PaginationType(str, Enum):
    NONE = "none"
    NEXT_BUTTON = "next_button"
    PAGE_PARAM = "page_param"
    INFINITE_SCROLL = "infinite_scroll"


class PaginationConfig(BaseModel):
    type: PaginationType = PaginationType.NONE
    max_pages: int = 5
    next_button_selector: str = ""
    page_param_name: str = "page"
    start_page: int = 1


class WaitRules(BaseModel):
    load_state: str = "domcontentloaded"
    wait_for_selector: str = ""
    timeout_ms: int = 30000


class FieldsConfig(BaseModel):
    """Selectors for all extractable fields on listing cards or detail pages."""
    title: Optional[FieldSelector] = None
    price_text: Optional[FieldSelector] = None
    image_url: Optional[FieldSelector] = None
    listing_url: Optional[FieldSelector] = None
    location: Optional[FieldSelector] = None
    description: Optional[FieldSelector] = None
    area: Optional[FieldSelector] = None
    layout: Optional[FieldSelector] = None
    fees: Optional[FieldSelector] = None
    deposit: Optional[FieldSelector] = None
    external_id: Optional[FieldSelector] = None


class TransformsConfig(BaseModel):
    strip_query_params_from_url: bool = False
    absolutize_urls: bool = True
    clean_whitespace: bool = True


class FiltersConfig(BaseModel):
    required_fields: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)


class RateLimitConfig(BaseModel):
    min_delay_ms: int = 1000
    max_delay_ms: int = 3000


class BrowserConfig(BaseModel):
    headless: bool = False
    use_persistent_context: bool = False
    storage_state_path: str = ""


class ExtractionMode(str, Enum):
    LISTING_CARDS_ONLY = "listing_cards_only"
    DETAIL_PAGES = "detail_pages"
    HYBRID = "hybrid"


class FacebookConfig(BaseModel):
    """Facebook-specific configuration — separate because it is fragile."""
    group_urls: list[str] = Field(default_factory=list)
    use_manual_login: bool = True
    scroll_iterations: int = 10
    post_selector: str = ""


class ScoringWeights(BaseModel):
    """Weights for suitability scoring.  Loaded from common.yaml."""
    commute_weight: float = 0.25
    price_weight: float = 0.20
    layout_weight: float = 0.15
    area_weight: float = 0.10
    furnished_weight: float = 0.10
    flatshare_quality_weight: float = 0.10
    extras_weight: float = 0.10

    # Target values for scoring
    max_price_czk: float = 25000
    ideal_price_czk: float = 18000
    min_area_m2: float = 25.0
    preferred_layouts: list[str] = Field(
        default_factory=lambda: ["1+kk", "1+1", "2+kk", "2+1", "studio", "room"]
    )
    preferred_districts: list[str] = Field(
        default_factory=lambda: [
            "Praha 8", "Karlín", "Libeň", "Palmovka", "Invalidovna",
            "Florenc", "Praha 3", "Žižkov", "Praha 1", "Praha 7",
            "Holešovice", "Letná",
        ]
    )
    exclude_keywords: list[str] = Field(
        default_factory=lambda: ["women only", "only girls", "jen ženy", "studentky"]
    )


class SiteConfig(BaseModel):
    """Full validated configuration for one scraping source.

    Loaded from a YAML file in configs/sites/.
    """

    site_name: str
    source_type: str = "normal_listing_site"
    enabled: bool = True
    start_urls: list[str] = Field(default_factory=list)
    search_url_templates: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    listing_card_selector: str = ""
    detail_page: DetailPageConfig = Field(default_factory=DetailPageConfig)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    wait_rules: WaitRules = Field(default_factory=WaitRules)
    fields: FieldsConfig = Field(default_factory=FieldsConfig)
    detail_fields: FieldsConfig = Field(default_factory=FieldsConfig)
    transforms: TransformsConfig = Field(default_factory=TransformsConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    extraction_mode: str = "listing_cards_only"
    facebook: FacebookConfig = Field(default_factory=FacebookConfig)
    notes: str = ""


# ---------------------------------------------------------------------------
# Common / global config (from configs/common.yaml)
# ---------------------------------------------------------------------------

class CommonConfig(BaseModel):
    """Settings from configs/common.yaml that apply across all sites."""
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    ollama_model: Optional[str] = None
    ollama_timeout: Optional[int] = None
    ollama_temperature: Optional[float] = None
    default_city: str = "Prague"
    default_currency: str = "CZK"


# ---------------------------------------------------------------------------
# Config loader functions
# ---------------------------------------------------------------------------

def load_common_config(settings: AppSettings) -> CommonConfig:
    """Load configs/common.yaml and return validated CommonConfig."""
    path = settings.configs_dir / "common.yaml"
    if not path.exists():
        return CommonConfig()
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return CommonConfig(**data)


def load_site_config(path: Path) -> SiteConfig:
    """Load and validate a single site YAML config."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return SiteConfig(**data)


def discover_site_configs(settings: AppSettings) -> list[SiteConfig]:
    """Discover and load all site configs from configs/sites/*.yaml."""
    sites_dir = settings.configs_dir / "sites"
    if not sites_dir.exists():
        return []

    configs: list[SiteConfig] = []
    for yaml_path in sorted(sites_dir.glob("*.yaml")):
        try:
            cfg = load_site_config(yaml_path)
            configs.append(cfg)
        except Exception as exc:
            # Logged by caller — we don't swallow silently but collect valid ones
            import logging
            logging.getLogger("app.config").error(
                "Failed to load site config %s: %s", yaml_path.name, exc
            )
    return configs


def get_enabled_site_configs(settings: AppSettings) -> list[SiteConfig]:
    """Return only enabled site configs."""
    return [c for c in discover_site_configs(settings) if c.enabled]
