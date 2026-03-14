from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.Services.MD_Scrapper.registry import get_scraper_provider


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

    scraper_provider = get_scraper_provider(provider)
    if scraper_provider is None:
        raise RuntimeError(f"Provider de scraper não suportado: {provider}")

    scraper_provider.runner(job, hb)
