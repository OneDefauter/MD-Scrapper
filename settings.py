from __future__ import annotations

from typing import Any

from app.Services.Database import session_scope
from app.Services.Database.settings_store import get_settings, settings_to_grouped_payload

_SCRAPER_WORKER_SCOPE = "md_scrapper"
_LEGACY_WORKER_SCOPES = ("md_scrapper_manhastro",)


def get_md_scrapper_settings() -> dict[str, dict[str, Any]]:
    with session_scope() as session:
        grouped = settings_to_grouped_payload(get_settings(session))
    if isinstance(grouped, dict):
        return grouped
    return {}


def get_provider_settings(provider_key: str) -> dict[str, Any]:
    normalized_provider = str(provider_key or "").strip().lower()
    if not normalized_provider:
        return {}

    grouped = get_md_scrapper_settings()
    scoped = grouped.get(f"md_scrapper_{normalized_provider}")
    if isinstance(scoped, dict):
        return scoped
    return {}


def _coerce_int(value: Any, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _resolve_worker_setting_int(key: str, default: int, *, minimum: int) -> int:
    grouped = get_md_scrapper_settings()
    for scope in (_SCRAPER_WORKER_SCOPE, *_LEGACY_WORKER_SCOPES):
        scoped = grouped.get(scope)
        if not isinstance(scoped, dict) or key not in scoped:
            continue
        return _coerce_int(scoped.get(key), default, minimum=minimum)
    return default


def get_scraper_worker_chapters_concurrent(default: int = 1) -> int:
    return _resolve_worker_setting_int("chapters_concurrent", default, minimum=1)


def get_scraper_worker_max_retries(default: int = 3) -> int:
    return _resolve_worker_setting_int("max_retries", default, minimum=0)
