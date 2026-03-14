from __future__ import annotations

from app.Services.MD_Scrapper.Providers.manhastro.core import (
    MANHASTRO_PROVIDER_KEY,
    get_manhastro_project,
    get_manhastro_project_by_url,
    is_manhastro_url,
    search_manhastro_projects,
)
from app.Services.MD_Scrapper.Providers.manhastro.runner import run as run_manhastro_scraper_download
from app.Services.MD_Scrapper.registry import ScraperProviderDefinition

PROVIDER_DEFINITION = ScraperProviderDefinition(
    key=MANHASTRO_PROVIDER_KEY,
    label="Manhastro",
    search_projects=search_manhastro_projects,
    get_project=get_manhastro_project,
    get_project_by_url=get_manhastro_project_by_url,
    is_project_url=is_manhastro_url,
    runner=run_manhastro_scraper_download,
    min_app_version="1.3.0",
)
