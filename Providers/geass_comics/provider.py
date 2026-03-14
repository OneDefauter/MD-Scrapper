from __future__ import annotations

from app.Services.MD_Scrapper.Providers.geass_comics.core import (
    GEASS_COMICS_PROVIDER_KEY,
    get_geass_project,
    get_geass_project_by_url,
    is_geass_comics_url,
    search_geass_projects,
)
from app.Services.MD_Scrapper.Providers.geass_comics.runner import run as run_geass_comics_scraper_download
from app.Services.MD_Scrapper.registry import ScraperProviderDefinition

PROVIDER_DEFINITION = ScraperProviderDefinition(
    key=GEASS_COMICS_PROVIDER_KEY,
    label="Geass Comics",
    search_projects=search_geass_projects,
    get_project=get_geass_project,
    get_project_by_url=get_geass_project_by_url,
    is_project_url=is_geass_comics_url,
    runner=run_geass_comics_scraper_download,
    min_app_version="1.3.0",
)
