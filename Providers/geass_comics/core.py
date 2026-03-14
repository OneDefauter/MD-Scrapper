from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

GEASS_COMICS_PROVIDER_KEY = "geass_comics"
GEASS_COMICS_SITE_URL = "https://geasscomics.xyz/"
GEASS_COMICS_API_BASE_URL = "https://api.skkyscan.fun"
GEASS_COMICS_MANGAS_ENDPOINT = f"{GEASS_COMICS_API_BASE_URL}/api/mangas"
GEASS_COMICS_MANGA_SEARCH_ENDPOINT = f"{GEASS_COMICS_MANGAS_ENDPOINT}/search"
GEASS_COMICS_CHAPTERS_ENDPOINT = f"{GEASS_COMICS_API_BASE_URL}/api/chapters"
GEASS_COMICS_PROJECT_URL_PATTERN = re.compile(r"^/(?:obra|manga)/(?P<slug>[^/?#]+)/?$", re.I)
GEASS_COMICS_READER_URL_PATTERN = re.compile(r"^/(?:ler|reader)/(?P<slug>[^/?#]+)/(?P<chapter>[^/?#]+)/?$", re.I)
GEASS_COMICS_READER_SHORTHAND_PATTERN = re.compile(
    r"^(?P<slug>[a-z0-9][a-z0-9-]*)[@:](?P<chapter>[^/@:]+)$",
    re.I,
)
GEASS_COMICS_API_MANGA_PATH_PATTERN = re.compile(r"^/api/mangas/(?P<lookup>[^/?#]+)/?$", re.I)
GEASS_COMICS_API_CHAPTER_PATH_PATTERN = re.compile(r"^/api/chapters/(?P<lookup>[^/?#]+)/?$", re.I)
GEASS_COMICS_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
REQUEST_TIMEOUT = 30.0
CACHE_TTL_SECONDS = 600

_CACHE_LOCK = Lock()
_MANGA_CACHE: dict[str, Any] = {"expires_at": 0.0, "items": {}}
_CHAPTER_LIST_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


class MDScrapperProviderError(RuntimeError):
    pass


def create_geass_session() -> httpx.Client:
    return httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": GEASS_COMICS_SITE_URL,
        },
    )


def _cache_valid(expires_at: float) -> bool:
    return expires_at > time.time()


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sanitize_path_component(value: Any, *, fallback: str = "_") -> str:
    raw = _normalize_text(value) or fallback
    sanitized = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in raw)
    sanitized = sanitized.strip(" .")
    return sanitized or fallback


def _is_uuid(value: str) -> bool:
    return bool(GEASS_COMICS_UUID_PATTERN.fullmatch(str(value or "").strip()))


def _normalize_chapter_number(value: str | int | float | None) -> str:
    text = _normalize_text(value)
    if not text:
        return ""

    numeric_text = text.replace(",", ".")
    try:
        number = Decimal(numeric_text)
    except (InvalidOperation, ValueError):
        return text

    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _request_json(session: httpx.Client, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = session.get(url, params=params)
    except httpx.HTTPError as exc:
        raise MDScrapperProviderError(f"Falha ao acessar o provedor Geass Comics: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        response.raise_for_status()
        raise MDScrapperProviderError(f"JSON invalido retornado por {url}") from exc

    if response.is_error or payload.get("success") is False:
        message = payload.get("error") or payload.get("message") or response.text[:200]
        raise MDScrapperProviderError(str(message))

    if not isinstance(payload, dict):
        raise MDScrapperProviderError("Payload inesperado do Geass Comics.")
    return payload


def _normalize_image_url(url: str | None) -> str | None:
    raw = _normalize_text(url)
    if not raw:
        return None

    if "/api/cdn/" in raw:
        if raw.startswith("http"):
            return raw
        return f"{GEASS_COMICS_API_BASE_URL}{raw if raw.startswith('/') else f'/{raw}'}"

    if "r2.dev/" in raw or "r2.cloudflarestorage.com/" in raw:
        parts = re.split(r"r2\.dev/|r2\.cloudflarestorage\.com/[^/]+/", raw)
        key = parts[-1] if parts else ""
        if key:
            return f"{GEASS_COMICS_API_BASE_URL}/api/cdn/{key.lstrip('/')}"

    if raw.startswith("http"):
        return raw
    return f"{GEASS_COMICS_API_BASE_URL}/api/cdn/{raw.lstrip('/')}"


def _build_manga_url(slug: str | None) -> str | None:
    if not slug:
        return None
    return urljoin(GEASS_COMICS_SITE_URL, f"obra/{slug}")


def _build_legacy_manga_url(slug: str | None) -> str | None:
    if not slug:
        return None
    return urljoin(GEASS_COMICS_SITE_URL, f"manga/{slug}")


def _build_reader_url(slug: str | None, chapter_number: str | None) -> str | None:
    if not slug or not chapter_number:
        return None
    return urljoin(
        GEASS_COMICS_SITE_URL,
        f"ler/{slug}/{_normalize_chapter_number(chapter_number)}",
    )


def is_geass_comics_url(value: str) -> bool:
    host = urlparse(str(value or "")).netloc.lower()
    return "geasscomics.xyz" in host or "api.skkyscan.fun" in host


def _remember_manga(manga: dict[str, Any], *aliases: str) -> None:
    with _CACHE_LOCK:
        cache = dict(_MANGA_CACHE.get("items") or {})
        cache[str(manga["id"])] = manga
        cache[str(manga["slug"])] = manga
        for alias in aliases:
            normalized_alias = _normalize_text(alias)
            if normalized_alias:
                cache[normalized_alias] = manga
        _MANGA_CACHE["items"] = cache
        _MANGA_CACHE["expires_at"] = time.time() + CACHE_TTL_SECONDS


def _cached_manga(lookup: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        if not _cache_valid(float(_MANGA_CACHE.get("expires_at") or 0.0)):
            _MANGA_CACHE["items"] = {}
            return None
        cache = _MANGA_CACHE.get("items") or {}
        item = cache.get(_normalize_text(lookup))
        return dict(item) if isinstance(item, dict) else None


def _normalize_manga(data: dict[str, Any]) -> dict[str, Any]:
    title = _normalize_text(data.get("title"))
    original_title = None
    alternative_titles = data.get("alternativeTitles") or []
    if isinstance(alternative_titles, list):
        for value in alternative_titles:
            normalized = _normalize_text(value)
            if normalized and normalized != title:
                original_title = normalized
                break

    slug = _normalize_text(data.get("slug"))
    manga_id = _normalize_text(data.get("id"))
    return {
        "id": manga_id,
        "slug": slug,
        "provider": GEASS_COMICS_PROVIDER_KEY,
        "title": title or slug or manga_id,
        "original_title": original_title,
        "description": data.get("description"),
        "cover_url": _normalize_image_url(data.get("coverImage")),
        "banner_url": _normalize_image_url(data.get("bannerImage")),
        "status": data.get("status"),
        "type": data.get("type"),
        "author": data.get("author"),
        "artist": data.get("artist"),
        "release_year": data.get("releaseYear"),
        "chapter_count": data.get("chapterCount"),
        "rating": data.get("rating"),
        "views": data.get("views"),
        "latest_chapter_at": data.get("lastChapterCreatedAt"),
        "site_url": _build_manga_url(slug),
        "legacy_url": _build_legacy_manga_url(slug),
        "api_url": f"{GEASS_COMICS_MANGAS_ENDPOINT}/{manga_id}" if manga_id else None,
        "genres": data.get("genres") or [],
        "tags": data.get("tags") or [],
    }


def _extract_manga_lookup(manga: str) -> str:
    value = _normalize_text(manga)
    if not value:
        raise MDScrapperProviderError("Informe um slug, ID ou URL de obra do Geass Comics.")

    path = urlparse(value).path if "://" in value else value
    path = _normalize_text(path)

    for pattern in (GEASS_COMICS_PROJECT_URL_PATTERN, GEASS_COMICS_API_MANGA_PATH_PATTERN):
        match = pattern.fullmatch(path)
        if match is not None:
            groups = match.groupdict()
            return groups.get("slug") or groups.get("lookup") or value

    candidate = path.strip("/").rsplit("/", 1)[-1]
    if candidate:
        return candidate
    raise MDScrapperProviderError("Nao foi possivel identificar a obra do Geass Comics.")


def _extract_direct_chapter_id(chapter: str) -> str | None:
    value = _normalize_text(chapter)
    if not value:
        return None

    path = urlparse(value).path if "://" in value else value
    path = _normalize_text(path)

    match = GEASS_COMICS_API_CHAPTER_PATH_PATTERN.fullmatch(path)
    if match is not None:
        lookup = _normalize_text(match.group("lookup"))
        return lookup if _is_uuid(lookup) else None

    candidate = path.strip("/").rsplit("/", 1)[-1]
    return candidate if _is_uuid(candidate) else None


def _parse_reader_reference(chapter: str) -> tuple[str, str] | None:
    value = _normalize_text(chapter)
    if not value:
        return None

    path = urlparse(value).path if "://" in value else value
    path = _normalize_text(path)

    match = GEASS_COMICS_READER_URL_PATTERN.fullmatch(path)
    if match is not None:
        return match.group("slug"), match.group("chapter")

    match = GEASS_COMICS_READER_SHORTHAND_PATTERN.fullmatch(path)
    if match is not None:
        return match.group("slug"), match.group("chapter")
    return None


def _fetch_chapter_page(
    session: httpx.Client,
    manga_id: str,
    *,
    limit: int,
    page: int,
    order: str,
    search: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    params: dict[str, Any] = {
        "mangaId": manga_id,
        "limit": limit,
        "page": page,
        "order": order,
    }
    if search:
        params["search"] = search

    payload = _request_json(session, GEASS_COMICS_CHAPTERS_ENDPOINT, params=params)
    data = payload.get("data") or []
    pagination = payload.get("pagination")
    if not isinstance(data, list):
        raise MDScrapperProviderError("Lista de capitulos do Geass Comics veio em formato invalido.")
    return [item for item in data if isinstance(item, dict)], pagination if isinstance(pagination, dict) else None


def _normalize_chapter_summary(data: dict[str, Any], *, manga_slug: str | None, manga_id: str) -> dict[str, Any]:
    chapter_number = _normalize_text(data.get("chapterNumber"))
    chapter_number_display = _normalize_chapter_number(chapter_number) or None
    title = _normalize_text(data.get("title"))
    default_label = f"Capitulo {chapter_number_display}" if chapter_number_display else _normalize_text(data.get("slug"))
    label = title or default_label or _normalize_text(data.get("id"))
    reader_url = _build_reader_url(manga_slug, chapter_number)

    return {
        "id": _normalize_text(data.get("id")),
        "manga_id": manga_id,
        "provider": GEASS_COMICS_PROVIDER_KEY,
        "url": reader_url or f"{GEASS_COMICS_CHAPTERS_ENDPOINT}/{_normalize_text(data.get('id'))}",
        "number": chapter_number_display,
        "title": title or default_label,
        "label": label,
        "published_at": data.get("publishedAt"),
        "folder_name": _sanitize_path_component(chapter_number_display or _normalize_text(data.get("id")), fallback="chapter"),
        "slug": data.get("slug"),
        "page_count": data.get("pageCount"),
        "views": data.get("views"),
        "api_url": f"{GEASS_COMICS_CHAPTERS_ENDPOINT}/{_normalize_text(data.get('id'))}",
    }


def _fetch_all_chapter_pages(
    session: httpx.Client,
    manga_id: str,
    manga_slug: str,
    *,
    order: str,
    page_size: int,
    search: str | None = None,
) -> list[dict[str, Any]]:
    cache_key = (manga_id, order, search or "")
    with _CACHE_LOCK:
        cached = _CHAPTER_LIST_CACHE.get(cache_key)
        if cached and _cache_valid(float(cached.get("expires_at") or 0.0)):
            return list(cached.get("items") or [])

    page = 1
    chapters: list[dict[str, Any]] = []
    while True:
        raw_items, pagination = _fetch_chapter_page(
            session,
            manga_id,
            limit=page_size,
            page=page,
            order=order,
            search=search,
        )
        chapters.extend(
            _normalize_chapter_summary(item, manga_slug=manga_slug, manga_id=manga_id)
            for item in raw_items
        )
        if not pagination or not pagination.get("hasNext"):
            break
        page += 1
        if page > 1000:
            raise MDScrapperProviderError("Paginacao inesperada ao buscar capitulos do Geass Comics.")

    with _CACHE_LOCK:
        _CHAPTER_LIST_CACHE[cache_key] = {
            "expires_at": time.time() + CACHE_TTL_SECONDS,
            "items": list(chapters),
        }
    return chapters


def _resolve_chapter_reference(session: httpx.Client, chapter: str) -> tuple[str, dict[str, Any] | None]:
    direct_id = _extract_direct_chapter_id(chapter)
    if direct_id is not None:
        return direct_id, None

    reader_reference = _parse_reader_reference(chapter)
    if reader_reference is None:
        raise MDScrapperProviderError(
            "Informe um ID/URL de capitulo ou uma URL do leitor no formato /ler/<slug>/<capitulo>."
        )

    manga_slug, chapter_number = reader_reference
    manga_info = get_geass_manga(manga_slug, session=session)
    chapters = _fetch_all_chapter_pages(
        session,
        str(manga_info["id"]),
        str(manga_info["slug"]),
        order="asc",
        page_size=500,
    )

    target_number = _normalize_chapter_number(chapter_number)
    for item in chapters:
        if _normalize_chapter_number(item.get("number")) == target_number:
            return str(item["id"]), manga_info

    raise MDScrapperProviderError(
        f"Capitulo {chapter_number} nao encontrado para a obra {manga_info['slug']}."
    )


def search_geass_projects(query: str, *, limit: int = 24) -> list[dict[str, Any]]:
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return []

    with create_geass_session() as session:
        payload = _request_json(
            session,
            GEASS_COMICS_MANGA_SEARCH_ENDPOINT,
            params={"q": normalized_query, "limit": max(1, min(limit, 100))},
        )

    raw_results = payload.get("data") or []
    if not isinstance(raw_results, list):
        raise MDScrapperProviderError("Busca do Geass Comics retornou payload invalido.")

    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_manga(item)
        manga_id = str(normalized["id"])
        if manga_id in seen_ids:
            continue
        seen_ids.add(manga_id)
        _remember_manga(normalized, manga_id, normalized.get("slug") or "", normalized.get("site_url") or "")
        results.append(
            {
                "id": normalized["id"],
                "provider": GEASS_COMICS_PROVIDER_KEY,
                "title": normalized["title"],
                "original_title": normalized.get("original_title"),
                "description": normalized.get("description"),
                "cover_url": normalized.get("cover_url"),
                "url": normalized.get("site_url") or normalized.get("legacy_url"),
                "chapter_count": normalized.get("chapter_count"),
                "latest_chapter_at": normalized.get("latest_chapter_at"),
                "status": normalized.get("status"),
                "type": normalized.get("type"),
                "author": normalized.get("author"),
                "artist": normalized.get("artist"),
                "rating": normalized.get("rating"),
            }
        )
    return results[: max(1, min(limit, 100))]


def get_geass_manga(manga: str, *, session: httpx.Client | None = None) -> dict[str, Any]:
    lookup = _extract_manga_lookup(manga)
    cached = _cached_manga(lookup)
    if cached is not None:
        return cached

    active_session = session or create_geass_session()
    owns_session = session is None
    try:
        payload = _request_json(active_session, f"{GEASS_COMICS_MANGAS_ENDPOINT}/{lookup}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MDScrapperProviderError("Detalhe de obra do Geass Comics veio em formato invalido.")
        normalized = _normalize_manga(data)
        _remember_manga(normalized, lookup, normalized.get("site_url") or "", normalized.get("legacy_url") or "")
        return normalized
    finally:
        if owns_session:
            active_session.close()


def get_geass_project(project_lookup: int | str) -> dict[str, Any]:
    with create_geass_session() as session:
        manga_info = get_geass_manga(str(project_lookup), session=session)
        manga_id = str(manga_info["id"])
        manga_slug = str(manga_info.get("slug") or "")
        chapters = _fetch_all_chapter_pages(
            session,
            manga_id,
            manga_slug,
            order="desc",
            page_size=500,
        )

    return {
        "provider": GEASS_COMICS_PROVIDER_KEY,
        "project": {
            "id": manga_info["id"],
            "provider": GEASS_COMICS_PROVIDER_KEY,
            "title": manga_info["title"],
            "original_title": manga_info.get("original_title"),
            "description": manga_info.get("description"),
            "cover_url": manga_info.get("cover_url"),
            "url": manga_info.get("site_url") or manga_info.get("legacy_url"),
            "chapter_count": manga_info.get("chapter_count") or len(chapters),
            "latest_chapter_at": manga_info.get("latest_chapter_at"),
            "status": manga_info.get("status"),
            "type": manga_info.get("type"),
            "author": manga_info.get("author"),
            "artist": manga_info.get("artist"),
            "rating": manga_info.get("rating"),
            "release_year": manga_info.get("release_year"),
            "genres_raw": manga_info.get("genres"),
            "tags_raw": manga_info.get("tags"),
        },
        "chapters": chapters,
    }


def get_geass_project_by_url(project_url: str) -> dict[str, Any]:
    if not is_geass_comics_url(project_url):
        raise MDScrapperProviderError("A URL informada nao pertence ao Geass Comics.")
    return get_geass_project(project_url)


def fetch_geass_chapter_manifest(chapter: str) -> dict[str, Any]:
    with create_geass_session() as session:
        chapter_id, manga_info = _resolve_chapter_reference(session, chapter)
        payload = _request_json(session, f"{GEASS_COMICS_CHAPTERS_ENDPOINT}/{chapter_id}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise MDScrapperProviderError("Detalhe de capitulo do Geass Comics veio em formato invalido.")

    if manga_info is None:
        manga_info = get_geass_manga(str(data.get("mangaId")))

    chapter_number = _normalize_text(data.get("chapterNumber"))
    pages = data.get("pages") or []
    if not isinstance(pages, list):
        raise MDScrapperProviderError("Lista de paginas do Geass Comics veio em formato invalido.")

    normalized_pages = sorted(
        [item for item in pages if isinstance(item, dict)],
        key=lambda item: item.get("pageNumber") or 0,
    )
    image_urls = [
        _normalize_image_url(page.get("imageUrl"))
        for page in normalized_pages
        if _normalize_image_url(page.get("imageUrl"))
    ]
    if not image_urls:
        raise MDScrapperProviderError("Nao encontrei imagens no capitulo do Geass Comics.")

    return {
        "chapter_id": _normalize_text(data.get("id")) or chapter_id,
        "chapter_number": _normalize_chapter_number(chapter_number) or None,
        "reader_url": _build_reader_url(str(manga_info.get("slug") or ""), chapter_number),
        "files": image_urls,
    }
