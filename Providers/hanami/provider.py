from __future__ import annotations

from app.Services.MD_Scrapper.Providers.hanami.core import (
    HANAMI_PROVIDER_KEY,
    get_hanami_project,
    get_hanami_project_by_url,
    is_hanami_url,
    search_hanami_projects,
)
from app.Services.MD_Scrapper.Providers.hanami.runner import run as run_hanami_scraper_download
from app.Services.MD_Scrapper.registry import ScraperProviderDefinition

PROVIDER_DEFINITION = ScraperProviderDefinition(
    key=HANAMI_PROVIDER_KEY,
    label="Hanami Heaven",
    search_projects=search_hanami_projects,
    get_project=get_hanami_project,
    get_project_by_url=get_hanami_project_by_url,
    is_project_url=is_hanami_url,
    runner=run_hanami_scraper_download,
    min_app_version="1.3.0",
)
