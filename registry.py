from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from typing import Any, Callable

from app.Services.md_scrapper_provider_fs import list_provider_keys
from app.Services.versioning import is_version_at_least, read_app_version

ScraperRunnerType = Callable[[dict[str, Any], Callable[[int | None], bool]], None]
SearchProjectsType = Callable[[str], list[dict[str, Any]]]
GetProjectType = Callable[[int | str], dict[str, Any]]
GetProjectByUrlType = Callable[[str], dict[str, Any]]
IsProjectUrlType = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class ScraperProviderDefinition:
    key: str
    label: str
    search_projects: SearchProjectsType
    get_project: GetProjectType
    get_project_by_url: GetProjectByUrlType
    is_project_url: IsProjectUrlType
    runner: ScraperRunnerType
    min_app_version: str | None = None


def _load_provider_definition(provider_key: str) -> ScraperProviderDefinition:
    module = import_module(f"app.Services.MD_Scrapper.Providers.{provider_key}.provider")
    definition = getattr(module, "PROVIDER_DEFINITION", None)
    if not isinstance(definition, ScraperProviderDefinition):
        raise RuntimeError(
            f"O provider '{provider_key}' precisa expor PROVIDER_DEFINITION como ScraperProviderDefinition."
        )
    if definition.key != provider_key:
        raise RuntimeError(
            f"O provider '{provider_key}' retornou uma definição inconsistente ({definition.key})."
        )
    return definition


@lru_cache(maxsize=1)
def _load_provider_map() -> dict[str, ScraperProviderDefinition]:
    providers: dict[str, ScraperProviderDefinition] = {}
    for provider_key in list_provider_keys():
        providers[provider_key] = _load_provider_definition(provider_key)
    return providers


def list_scraper_providers() -> list[ScraperProviderDefinition]:
    return list(_load_provider_map().values())


def get_scraper_provider(provider_key: str) -> ScraperProviderDefinition | None:
    normalized_provider = str(provider_key or "").strip().lower()
    if not normalized_provider:
        return None
    return _load_provider_map().get(normalized_provider)


def get_scraper_provider_compatibility(
    provider: ScraperProviderDefinition,
    *,
    current_app_version: str | None = None,
) -> dict[str, Any]:
    resolved_current_version = current_app_version or read_app_version()
    supported = is_version_at_least(resolved_current_version, provider.min_app_version)
    detail = None
    if not supported and provider.min_app_version:
        detail = (
            f"O provedor {provider.label} requer a versão {provider.min_app_version} "
            f"ou superior do app. Versão atual: {resolved_current_version}."
        )

    return {
        "supported": supported,
        "current_app_version": resolved_current_version,
        "min_app_version": provider.min_app_version,
        "detail": detail,
    }


def resolve_scraper_provider_from_url(url: str) -> ScraperProviderDefinition | None:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        return None

    for provider in list_scraper_providers():
        try:
            if provider.is_project_url(normalized_url):
                return provider
        except Exception:
            continue
    return None
