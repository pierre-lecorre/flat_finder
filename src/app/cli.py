"""Typer CLI ergonomics for Flat Finder.

Supports initializing databases, running scrapes, triggering enrichments,
dumping CSV files, persisting manual Facebook cookies, and testing configurations.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from app.config import AppSettings, load_common_config, discover_site_configs
from app.db import Database
from app.logging_config import setup_logging
from app.pipeline import PipelineOrchestrator
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    name="flat-finder",
    help="Config-driven multi-site real-estate search aggregator.",
    no_args_is_help=True,
)
console = Console()


def _get_orchestrator() -> PipelineOrchestrator:
    """Convenience helper to initialize AppSettings, folders, logging, and Orchestrator."""
    settings = AppSettings.from_env()
    settings.ensure_directories()

    setup_logging(level=settings.log_level, log_file=settings.log_file)
    common_cfg = load_common_config(settings)

    return PipelineOrchestrator(settings, common_cfg)


@app.command("init-db")
def init_db() -> None:
    """Initialize SQLite database, creating standard tables and indexes."""
    orchestrator = _get_orchestrator()
    console.print("[bold cyan]Initializing SQLite database...[/bold cyan]")
    try:
        orchestrator.db.init_db()
        console.print("[bold green]✔ Database initialized successfully.[/bold green]")
    except Exception as exc:
        console.print(f"[bold red]❌ Failed to initialize database: {exc}[/bold red]")
        sys.exit(1)


@app.command("list-sites")
def list_sites() -> None:
    """Display all discovered YAML configurations and their active status."""
    settings = AppSettings.from_env()
    configs = discover_site_configs(settings)

    if not configs:
        console.print("[yellow]No site configs found. Ensure configs/sites/*.yaml files exist.[/yellow]")
        return

    table = Table(title="Discovered Scraping Sources")
    table.add_column("Site Name", style="cyan")
    table.add_column("Source Type", style="magenta")
    table.add_column("Enabled", style="green")
    table.add_column("Extraction Mode", style="blue")
    table.add_column("Domain Rules", style="yellow")

    for c in configs:
        enabled_str = "[bold green]Yes[/bold green]" if c.enabled else "[grey]No[/grey]"
        table.add_row(
            c.site_name,
            c.source_type,
            enabled_str,
            c.extraction_mode,
            ", ".join(c.allowed_domains) if c.allowed_domains else "Any",
        )

    console.print(table)


@app.command("test-site")
def test_site(
    site: str = typer.Option(..., "--site", "-s", help="Name of the site config yaml to validate")
) -> None:
    """Validate a single site configuration file without launching a browser."""
    settings = AppSettings.from_env()
    configs = discover_site_configs(settings)
    cfg = next((c for c in configs if c.site_name == site), None)

    if not cfg:
        console.print(f"[bold red]❌ Config '{site}' not found among active site configurations.[/bold red]")
        sys.exit(1)

    console.print(Panel(
        f"[bold green]✔ Config loaded and validated successfully![/bold green]\n\n"
        f"[bold]Site Name:[/bold] {cfg.site_name}\n"
        f"[bold]Source Type:[/bold] {cfg.source_type}\n"
        f"[bold]Enabled:[/bold] {cfg.enabled}\n"
        f"[bold]Start URLs:[/bold] {cfg.start_urls}\n"
        f"[bold]Extraction Mode:[/bold] {cfg.extraction_mode}\n"
        f"[bold]Card Selector:[/bold] {cfg.listing_card_selector}",
        title=f"Configuration Validation: {cfg.site_name}"
    ))


@app.command("scrape")
def scrape(
    site: str = typer.Option("all", "--site", "-s", help="Specific site name to scrape, or 'all' for all enabled")
) -> None:
    """Run phase 1: Navigate target sites, parse cards, and save raw listings."""
    orchestrator = _get_orchestrator()
    console.print(f"[bold cyan]Launching phase 1 scraper (site: {site})...[/bold cyan]")
    try:
        stats = orchestrator.run_scrape(site)
        console.print(Panel(
            f"[bold green]Scraping phase completed successfully.[/bold green]\n\n"
            f"[bold]Total Listings Discovered:[/bold] {stats['seen']}\n"
            f"[bold]Newly Inserted Raw Rows:[/bold] {stats['inserted']}\n"
            f"[bold]Updated Existing Rows:[/bold] {stats['updated']}",
            title="Scrape Results Summary"
        ))
    except Exception as exc:
        console.print(f"[bold red]❌ Scraping run encountered a fatal error: {exc}[/bold red]")
        sys.exit(1)


@app.command("enrich")
def enrich(
    site: str = typer.Option("all", "--site", "-s", help="Filter raw listings by site name before enriching"),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Maximum listings to enrich in this pass")
) -> None:
    """Run phase 2: Load un-enriched raw database listings and call local Ollama model."""
    orchestrator = _get_orchestrator()
    console.print(f"[bold cyan]Launching phase 2 LLM enrichment (site: {site}, limit: {limit})...[/bold cyan]")
    try:
        stats = orchestrator.run_enrich(site, limit)
        console.print(Panel(
            f"[bold green]LLM enrichment pass completed.[/bold green]\n\n"
            f"[bold]Successfully Enriched Listings:[/bold] {stats['enriched']}\n"
            f"[bold]Failed Validation Attempts:[/bold] {stats['failed']}",
            title="Enrichment Results Summary"
        ))
    except Exception as exc:
        console.print(f"[bold red]❌ Enrichment pass encountered a fatal error: {exc}[/bold red]")
        sys.exit(1)


@app.command("run")
def run(
    site: str = typer.Option("all", "--site", "-s", help="Filter pipeline by site name")
) -> None:
    """Execute full pipeline: Scrape search listings then enrich with Ollama."""
    orchestrator = _get_orchestrator()
    console.print(f"[bold cyan]Executing full Prague aggregator pipeline (site: {site})...[/bold cyan]")
    try:
        stats = orchestrator.run_full(site)
        console.print(Panel(
            f"[bold green]Aggregation pipeline executed successfully.[/bold green]\n\n"
            f"[bold]Scraped (Discovered):[/bold] {stats.get('seen', 0)}\n"
            f"[bold]Scraped (New):[/bold] {stats.get('inserted', 0)}\n"
            f"[bold]Enriched Listings Logged:[/bold] {stats.get('enriched', 0)}\n"
            f"[bold]LLM Failures Encountered:[/bold] {stats.get('failed', 0)}",
            title="Full Aggregate Results Summary"
        ))
    except Exception as exc:
        console.print(f"[bold red]❌ Full aggregate run encountered a fatal error: {exc}[/bold red]")
        sys.exit(1)


@app.command("download-images")
def download_images(
    site: str = typer.Option("all", "--site", "-s", help="Filter by site name"),
    max_images: int = typer.Option(5, "--max-images", "-m", help="Max images to save per listing")
) -> None:
    """Fetch raw database listings and download pictures locally."""
    orchestrator = _get_orchestrator()
    console.print(f"[bold cyan]Executing local image downloader (site: {site}, max-images: {max_images})...[/bold cyan]")
    try:
        count = orchestrator.run_download_images(site, max_images)
        console.print(f"[bold green]✔ Completed downloading images. Saved {count} local image files.[/bold green]")
    except Exception as exc:
        console.print(f"[bold red]❌ Image downloader encountered a fatal error: {exc}[/bold red]")
        sys.exit(1)


@app.command("export-csv")
def export_csv(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Target CSV file path")
) -> None:
    """Query enriched listings, sort by suitability score, and export to CSV."""
    orchestrator = _get_orchestrator()
    settings = orchestrator.settings

    csv_path = Path(output) if output else settings.data_dir / "raw" / "enriched_listings.csv"
    console.print(f"[bold cyan]Exporting enriched database to CSV: {csv_path}...[/bold cyan]")

    try:
        orchestrator.export_csv(csv_path)
        console.print(f"[bold green]✔ Export completed. CSV file saved: {csv_path}[/bold green]")
    except Exception as exc:
        console.print(f"[bold red]❌ Export encountered a fatal error: {exc}[/bold red]")
        sys.exit(1)


@app.command("login-facebook")
def login_facebook() -> None:
    """Launch manual non-headless browser session to log into Facebook and persist session state."""
    orchestrator = _get_orchestrator()
    settings = orchestrator.settings

    # Search for any facebook group source config to fetch state path
    configs = discover_site_configs(settings)
    fb_cfg = next((c for c in configs if c.source_type == "facebook_group"), None)

    state_path = fb_cfg.browser.storage_state_path if fb_cfg else str(settings.browser_state_dir / "facebook.json")

    from app.browser import BrowserManager
    from app.scrapers.facebook_source import FacebookSourceScraper

    bm = BrowserManager(settings)
    try:
        FacebookSourceScraper.login_facebook(bm, state_path)
        console.print("[bold green]✔ Persistent Facebook state successfully saved![/bold green]")
    except Exception as exc:
        console.print(f"[bold red]❌ Manual Facebook login failed: {exc}[/bold red]")
        sys.exit(1)


@app.command("scrape-facebook")
def scrape_facebook() -> None:
    """Explicitly launch fragile Facebook posts scraper using active session cookie."""
    orchestrator = _get_orchestrator()
    settings = orchestrator.settings

    configs = discover_site_configs(settings)
    fb_cfg = next((c for c in configs if c.source_type == "facebook_group"), None)

    if not fb_cfg:
        console.print("[bold red]❌ No Facebook group scraper config found in configs/sites/*.yaml.[/bold red]")
        sys.exit(1)

    if not fb_cfg.enabled:
        console.print("[yellow]WARNING: Facebook config is marked as disabled. Force-executing anyway...[/yellow]")
        fb_cfg.enabled = True

    console.print("[bold cyan]Launching fragile Facebook scraper...[/bold cyan]")
    try:
        # Create database entry for facebook source if needed
        source_id = orchestrator.db.upsert_source(
            site_name=fb_cfg.site_name,
            source_type=fb_cfg.source_type,
            base_url=fb_cfg.facebook.group_urls[0] if fb_cfg.facebook.group_urls else "",
        )

        from app.browser import BrowserManager
        from app.scrapers.facebook_source import FacebookSourceScraper

        with BrowserManager(settings) as bm:
            scraper = FacebookSourceScraper(fb_cfg, settings, bm)
            scraper.source_id = source_id

            posts = scraper.scrape()
            inserted_count = 0
            updated_count = 0

            for raw in posts:
                _, was_inserted = orchestrator.db.upsert_raw_listing(raw)
                if was_inserted:
                    inserted_count += 1
                else:
                    updated_count += 1

            console.print(Panel(
                f"[bold green]Facebook scraping completed successfully.[/bold green]\n\n"
                f"[bold]Total Posts Extracted:[/bold] {len(posts)}\n"
                f"[bold]New Raw Listings Logged:[/bold] {inserted_count}\n"
                f"[bold]Updated Existing Entries:[/bold] {updated_count}",
                title="Facebook Scrape Results"
            ))

    except Exception as exc:
        console.print(f"[bold red]❌ Facebook scraper run failed: {exc}[/bold red]")
        sys.exit(1)


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Binding host address"),
    port: int = typer.Option(8000, "--port", "-p", help="Binding port number")
) -> None:
    """Launch the localhost web app dashboard to explore aggregated results."""
    console.print(f"[bold cyan]Launching Prague housing aggregator dashboard at http://{host}:{port}...[/bold cyan]")
    from app.web import start_web_server
    try:
        start_web_server(host=host, port=port)
    except Exception as exc:
        console.print(f"[bold red]❌ Failed to start dashboard server: {exc}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    app()
