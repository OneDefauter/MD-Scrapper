from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.Services.MD_Scrapper.Providers.manhastro.core import MANHASTRO_PROVIDER_KEY
from app.Services.MD_Scrapper.Providers.manhastro.runner import run as run_manhastro_scraper_download

ScraperRunnerType = Callable[[dict[str, Any], Callable[[int | None], bool]], None]

_SCRAPER_PROVIDER_RUNNERS: dict[str, ScraperRunnerType] = {
    MANHASTRO_PROVIDER_KEY: run_manhastro_scraper_download,
}


def get_scraper_job_provider(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") or {}
    if not isinstance(metadata, dict):
        return ""

    provider = str(metadata.get("provider") or "").strip().lower()
    if provider:
        return provider

    source = metadata.get("source") or {}
    if not isinstance(source, dict):
        return ""

    return str(source.get("provider") or "").strip().lower()


def run_scraper_download_job(job: dict[str, Any], hb: Callable[[int | None], bool]) -> None:
    provider = get_scraper_job_provider(job)
    if not provider:
        raise RuntimeError("Scraper download job sem provider.")

    runner = _SCRAPER_PROVIDER_RUNNERS.get(provider)
    if runner is None:
        raise RuntimeError(f"Provider de scraper não suportado: {provider}")

    runner(job, hb)
