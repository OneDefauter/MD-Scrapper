from __future__ import annotations

import re
import time
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import requests

MANHASTRO_PROVIDER_KEY = "manhastro"
MANHASTRO_SITE_URL = "https://manhastro.net"
MANHASTRO_CATALOG_URL = "https://api2.manhastro.net/dados"
MANHASTRO_PROJECT_URL_PATTERN = re.compile(r"/manga/(?P<manga_id>\d+)", re.I)
MANHASTRO_CHAPTER_URL_PATTERN = re.compile(
    r"/(?:leitura/(?P<manga_id_a>\d+)/(?P<chapter_id_a>\d+)|manga/(?P<manga_id_b>\d+)/chapter/(?P<chapter_id_b>\d+))",
    re.I,
)
REQUEST_TIMEOUT = 30
CACHE_TTL_SECONDS = 600

_CACHE_LOCK = Lock()
_CATALOG_CACHE: dict[str, Any] = {"expires_at": 0.0, "items": None, "by_id": None}
_CHAPTERS_CACHE: dict[int, dict[str, Any]] = {}


class MDScrapperProviderError(RuntimeError):
    pass


def _cache_valid(expires_at: float) -> bool:
    return expires_at > time.time()


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sanitize_path_component(value: Any, *, fallback: str = "_") -> str:
    raw = _normalize_text(value) or fallback
    sanitized = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in raw)
    sanitized = sanitized.strip(" .")
    return sanitized or fallback


def _with_scheme(url_or_host: str | None) -> str | None:
    raw = str(url_or_host or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    return f"https://{raw.lstrip('/')}"


def is_manhastro_url(value: str) -> bool:
    host = urlparse(str(value or "")).netloc.lower()
    return "manhastro.net" in host or "manhastro.com" in host


def _request_json(url: str) -> dict[str, Any]:
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        raise MDScrapperProviderError(f"Falha ao acessar o provedor Manhastro: {exc}") from exc

    if not response.ok:
        detail = response.text.strip() or "Sem corpo de resposta."
        raise MDScrapperProviderError(
            f"Manhastro respondeu com erro ({response.status_code}): {detail}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise MDScrapperProviderError("O Manhastro retornou JSON inválido.") from exc

    if not isinstance(payload, dict) or "data" not in payload:
        raise MDScrapperProviderError("O Manhastro retornou um payload inesperado.")
    return payload


def _get_catalog() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    with _CACHE_LOCK:
        if (
            _CATALOG_CACHE["items"] is not None
            and _CATALOG_CACHE["by_id"] is not None
            and _cache_valid(float(_CATALOG_CACHE["expires_at"]))
        ):
            return _CATALOG_CACHE["items"], _CATALOG_CACHE["by_id"]

    payload = _request_json(MANHASTRO_CATALOG_URL)
    items = payload.get("data") or []
    if not isinstance(items, list):
        raise MDScrapperProviderError("O catálogo do Manhastro veio em formato inválido.")

    normalized_items = [item for item in items if isinstance(item, dict)]
    by_id = {
        int(item["manga_id"]): item
        for item in normalized_items
        if str(item.get("manga_id", "")).isdigit()
    }

    with _CACHE_LOCK:
        _CATALOG_CACHE["expires_at"] = time.time() + CACHE_TTL_SECONDS
        _CATALOG_CACHE["items"] = normalized_items
        _CATALOG_CACHE["by_id"] = by_id
    return normalized_items, by_id


def _get_chapters(manga_id: int) -> list[dict[str, Any]]:
    with _CACHE_LOCK:
        cached = _CHAPTERS_CACHE.get(manga_id)
        if cached and _cache_valid(float(cached.get("expires_at") or 0.0)):
            return list(cached.get("items") or [])

    payload = _request_json(f"{MANHASTRO_CATALOG_URL}/{manga_id}")
    items = payload.get("data") or []
    if not isinstance(items, list):
        raise MDScrapperProviderError("A lista de capítulos do Manhastro veio em formato inválido.")
    normalized_items = [item for item in items if isinstance(item, dict)]

    with _CACHE_LOCK:
        _CHAPTERS_CACHE[manga_id] = {
            "expires_at": time.time() + CACHE_TTL_SECONDS,
            "items": normalized_items,
        }
    return normalized_items


def _chapter_number_from_name(chapter_name: str, chapter_id: int) -> str:
    match = re.search(r"\d+(?:\.\d+)?", chapter_name)
    if match:
        return match.group(0)
    return str(chapter_id)


def _normalize_project_item(item: dict[str, Any]) -> dict[str, Any]:
    manga_id = int(item["manga_id"])
    title = _normalize_text(item.get("titulo_brasil") or item.get("titulo") or manga_id)
    original_title = _normalize_text(item.get("titulo") or "")
    description = _normalize_text(item.get("descricao_brasil") or item.get("descricao") or "")
    image_url = _with_scheme(item.get("imagem"))
    chapters_count = int(item.get("qnt_capitulo") or 0)

    return {
        "id": str(manga_id),
        "provider": MANHASTRO_PROVIDER_KEY,
        "title": title,
        "original_title": original_title or None,
        "description": description or None,
        "cover_url": image_url,
        "url": f"{MANHASTRO_SITE_URL}/manga/{manga_id}",
        "chapter_count": chapters_count,
        "latest_chapter_at": item.get("ultimo_capitulo"),
        "scan_name": item.get("scan_atual"),
        "genres_raw": item.get("generos"),
        "views_month": item.get("views_mes"),
    }


def search_manhastro_projects(query: str, *, limit: int = 24) -> list[dict[str, Any]]:
    normalized_query = _normalize_text(query).casefold()
    if not normalized_query:
        return []

    catalog, _ = _get_catalog()
    matches: list[tuple[int, dict[str, Any]]] = []
    for item in catalog:
        searchable_values = [
            _normalize_text(item.get("titulo_brasil")),
            _normalize_text(item.get("titulo")),
        ]
        haystack = " | ".join(value.casefold() for value in searchable_values if value)
        if normalized_query not in haystack:
            continue

        rank = 3
        for value in searchable_values:
            candidate = value.casefold()
            if candidate == normalized_query:
                rank = 0
                break
            if candidate.startswith(normalized_query):
                rank = min(rank, 1)
            elif normalized_query in candidate:
                rank = min(rank, 2)
        matches.append((rank, _normalize_project_item(item)))

    matches.sort(key=lambda item: (item[0], item[1]["title"].casefold()))
    return [item for _, item in matches[: max(1, min(limit, 100))]]


def _parse_manga_id_from_url(project_url: str) -> int:
    match = MANHASTRO_PROJECT_URL_PATTERN.search(urlparse(project_url).path)
    if not match:
        raise MDScrapperProviderError("URL do projeto Manhastro inválida.")
    return int(match.group("manga_id"))


def get_manhastro_project_by_url(project_url: str) -> dict[str, Any]:
    if not is_manhastro_url(project_url):
        raise MDScrapperProviderError("A URL informada não pertence ao Manhastro.")
    return get_manhastro_project(_parse_manga_id_from_url(project_url))


def get_manhastro_project(manga_id: int | str) -> dict[str, Any]:
    try:
        normalized_manga_id = int(str(manga_id))
    except (TypeError, ValueError) as exc:
        raise MDScrapperProviderError("manga_id inválido para o Manhastro.") from exc

    _, catalog_by_id = _get_catalog()
    item = catalog_by_id.get(normalized_manga_id)
    if item is None:
        raise MDScrapperProviderError("Projeto não encontrado no catálogo do Manhastro.")

    project = _normalize_project_item(item)
    chapter_items = _get_chapters(normalized_manga_id)
    chapters: list[dict[str, Any]] = []

    for chapter in chapter_items:
        chapter_id = int(chapter["capitulo_id"])
        chapter_name = _normalize_text(chapter.get("capitulo_nome") or f"Capítulo {chapter_id}")
        chapter_number = _chapter_number_from_name(chapter_name, chapter_id)
        chapters.append(
            {
                "id": str(chapter_id),
                "manga_id": str(normalized_manga_id),
                "provider": MANHASTRO_PROVIDER_KEY,
                "url": f"{MANHASTRO_SITE_URL}/leitura/{normalized_manga_id}/{chapter_id}",
                "number": chapter_number,
                "title": chapter_name,
                "label": chapter_name,
                "published_at": chapter.get("capitulo_data"),
                "folder_name": _sanitize_path_component(chapter_name, fallback=f"capitulo-{chapter_id}"),
            }
        )

    chapters.sort(
        key=lambda item: (
            -float(item["number"]) if re.fullmatch(r"\d+(?:\.\d+)?", item["number"]) else float("-inf"),
            item["title"].casefold(),
        ),
        reverse=False,
    )
    chapters = list(reversed(chapters))

    return {
        "provider": MANHASTRO_PROVIDER_KEY,
        "project": project,
        "chapters": chapters,
    }


def fetch_manhastro_chapter_manifest(chapter_id: int | str) -> dict[str, Any]:
    try:
        normalized_chapter_id = int(str(chapter_id))
    except (TypeError, ValueError) as exc:
        raise MDScrapperProviderError("chapter_id inválido para o Manhastro.") from exc

    payload = _request_json(f"https://api2.manhastro.net/paginas/{normalized_chapter_id}")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise MDScrapperProviderError("O Manhastro retornou dados de capítulo inválidos.")
    if bool(data.get("text")):
        raise MDScrapperProviderError("Este capítulo do Manhastro foi entregue em modo texto.")

    chapter = data.get("chapter") or {}
    if not isinstance(chapter, dict):
        raise MDScrapperProviderError("O Manhastro não retornou o bloco chapter.")

    base_url = str(chapter.get("baseUrl") or "").strip().rstrip("/")
    chapter_hash = str(chapter.get("hash") or "").strip()
    file_names = chapter.get("data") or []
    if not base_url or not chapter_hash or not isinstance(file_names, list) or not file_names:
        raise MDScrapperProviderError("O manifesto do capítulo veio incompleto.")

    normalized_files = [str(item).strip() for item in file_names if str(item).strip()]
    return {
        "base_url": base_url,
        "chapter_hash": chapter_hash,
        "files": normalized_files,
    }
