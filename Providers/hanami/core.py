from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import cloudscraper
from bs4 import BeautifulSoup, Tag

HANAMI_PROVIDER_KEY = "hanami"
HANAMI_SITE_URL = "https://hanamiheaven.org/"
HANAMI_WORKS_URL = urljoin(HANAMI_SITE_URL, "todas-as-obras/")
HANAMI_AJAX_URL = urljoin(HANAMI_SITE_URL, "wp-admin/admin-ajax.php")
HANAMI_PROJECT_URL_PATTERN = re.compile(r"/manga/(?P<slug>[^/?#]+)/?", re.I)
HANAMI_CHAPTER_URL_PATTERN = re.compile(r"/manga/(?P<project_slug>[^/?#]+)/(?P<chapter_slug>[^/?#]+)/?", re.I)
HANAMI_SEARCH_NONCE_RE = re.compile(
    r"action:\s*['\"]search_grid_todas['\"].{0,300}?nonce:\s*['\"]([a-f0-9]+)['\"]",
    re.I | re.S,
)
HANAMI_ALTERNATIVE_TITLE_RE = re.compile(r'"_wp_manga_alternative":\["(?P<value>(?:\\.|[^"])*)"\]', re.I)
REQUEST_TIMEOUT = 30


class MDScrapperProviderError(RuntimeError):
    pass


def create_hanami_session():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )


def _normalize_text(value: Any) -> str:
    if isinstance(value, Tag):
        return " ".join(value.get_text(" ", strip=True).split())
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sanitize_path_component(value: Any, *, fallback: str = "_") -> str:
    raw = _normalize_text(value) or fallback
    sanitized = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in raw)
    sanitized = sanitized.strip(" .")
    return sanitized or fallback


def _attr(node: Tag | None, name: str) -> str | None:
    if node is None:
        return None
    value = node.get(name)
    if not value:
        return None
    return " ".join(str(value).split())


def _extract_image_url(node: Tag | None) -> str | None:
    for attr_name in ("data-src", "data-lazy-src", "data-original", "src"):
        value = _attr(node, attr_name)
        if value:
            return value
    return None


def _request_response(session, method: str, url: str, **kwargs):
    try:
        response = session.request(method.upper(), url, timeout=REQUEST_TIMEOUT, **kwargs)
    except Exception as exc:
        raise MDScrapperProviderError(f"Falha ao acessar o provedor Hanami: {exc}") from exc

    try:
        response.raise_for_status()
    except Exception as exc:
        detail = response.text.strip() or "Sem corpo de resposta."
        raise MDScrapperProviderError(
            f"Hanami respondeu com erro ({response.status_code}): {detail}"
        ) from exc
    return response


def warm_hanami_session(session) -> None:
    _request_response(session, "get", HANAMI_SITE_URL)


def is_hanami_url(value: str) -> bool:
    host = urlparse(str(value or "")).netloc.lower()
    return "hanamiheaven.org" in host


def _normalize_project_url(project_slug_or_url: str) -> str:
    raw = str(project_slug_or_url or "").strip()
    if not raw:
        raise MDScrapperProviderError("Projeto do Hanami inválido.")

    candidate = raw
    if not candidate.startswith(("http://", "https://")) and (
        candidate.startswith("/") or candidate.lower().startswith("manga/")
    ):
        candidate = urljoin(HANAMI_SITE_URL, candidate)

    if candidate.startswith(("http://", "https://")):
        if not is_hanami_url(candidate):
            raise MDScrapperProviderError("A URL informada não pertence ao Hanami.")
        match = HANAMI_PROJECT_URL_PATTERN.search(urlparse(candidate).path)
        if match is None:
            raise MDScrapperProviderError("URL do projeto Hanami inválida.")
        slug = match.group("slug").strip().strip("/")
    else:
        slug = candidate.strip().strip("/")

    if not slug:
        raise MDScrapperProviderError("Slug do projeto Hanami inválido.")
    return urljoin(HANAMI_SITE_URL, f"manga/{slug}/")


def _project_slug_from_url(project_url: str) -> str:
    match = HANAMI_PROJECT_URL_PATTERN.search(urlparse(project_url).path)
    if match is None:
        raise MDScrapperProviderError("URL do projeto Hanami inválida.")
    return match.group("slug").strip()


def _chapter_slug_from_url(chapter_url: str) -> str:
    match = HANAMI_CHAPTER_URL_PATTERN.search(urlparse(chapter_url).path)
    if match is None:
        raise MDScrapperProviderError("URL do capítulo Hanami inválida.")
    return match.group("chapter_slug").strip()


def _parse_pt_br_date(value: str | None) -> str | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return normalized or None


def _parse_date_score(value: str | None) -> float:
    normalized = _normalize_text(value)
    if not normalized:
        return 0.0
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(normalized, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _extract_numeric_chapter(value: str | None) -> str | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", normalized)
    if match is None:
        return None
    return match.group(0).replace(",", ".")


def _extract_alternative_title(html_text: str) -> str | None:
    match = HANAMI_ALTERNATIVE_TITLE_RE.search(html_text)
    if match is None:
        return None

    raw_value = match.group("value")
    try:
        decoded = json.loads(f'"{raw_value}"')
    except json.JSONDecodeError:
        decoded = raw_value
    normalized = _normalize_text(decoded)
    return normalized or None


def _parse_summary_map(soup: BeautifulSoup) -> dict[str, Tag]:
    summary_map: dict[str, Tag] = {}
    for item in soup.select(".post-content_item"):
        heading = _normalize_text(item.select_one(".summary-heading"))
        content = item.select_one(".summary-content")
        if heading and content and heading.casefold() not in summary_map:
            summary_map[heading.casefold()] = content
    return summary_map


def _tag_texts(node: Tag | None) -> list[str]:
    if node is None:
        return []
    return [_normalize_text(item) for item in node.select("a") if _normalize_text(item)]


def _build_search_project(item: Tag) -> dict[str, Any] | None:
    link = item.select_one("a.manga-image-container[href]") or item.select_one(".manga-title a[href]")
    project_url = _attr(link, "href")
    if not project_url:
        return None

    normalized_url = _normalize_project_url(project_url)
    slug = _project_slug_from_url(normalized_url)
    title = _normalize_text(item.select_one(".info-title") or item.select_one(".manga-title a"))
    if not title:
        title = slug

    project_type = _normalize_text(item.select_one(".manga-type")) or None
    rating = _normalize_text(item.select_one(".info-rating")) or None
    if rating:
        rating = re.sub(r"^[^:]+:\s*", "", rating).strip() or None
    author = _normalize_text(item.select_one(".info-author")) or None
    if author:
        author = re.sub(r"^[^:]+:\s*", "", author).strip() or None
    artist = _normalize_text(item.select_one(".info-artist")) or None
    if artist:
        artist = re.sub(r"^[^:]+:\s*", "", artist).strip() or None
    release = _normalize_text(item.select_one(".info-release")) or None
    if release:
        release = re.sub(r"^[^:]+:\s*", "", release).strip() or None

    return {
        "id": slug,
        "provider": HANAMI_PROVIDER_KEY,
        "title": title,
        "original_title": None,
        "description": None,
        "cover_url": _extract_image_url(item.select_one("img")),
        "url": normalized_url,
        "chapter_count": None,
        "latest_chapter_at": None,
        "project_type": project_type,
        "rating": rating,
        "author": author,
        "artist": artist,
        "release": release,
    }


def search_hanami_projects(query: str, *, limit: int = 24) -> list[dict[str, Any]]:
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return []

    session = create_hanami_session()
    works_response = _request_response(session, "get", HANAMI_WORKS_URL)
    nonce_match = HANAMI_SEARCH_NONCE_RE.search(works_response.text)
    if nonce_match is None:
        raise MDScrapperProviderError("Não foi possível localizar o nonce de busca do Hanami.")

    search_response = _request_response(
        session,
        "post",
        HANAMI_AJAX_URL,
        data={
            "action": "search_grid_todas",
            "term": normalized_query,
            "nonce": nonce_match.group(1),
        },
    )
    try:
        payload = search_response.json()
    except ValueError as exc:
        raise MDScrapperProviderError("O Hanami retornou JSON inválido na busca.") from exc

    if not bool(payload.get("success")):
        raise MDScrapperProviderError("A busca do Hanami retornou um payload sem sucesso.")

    html = str((payload.get("data") or {}).get("html") or "")
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in soup.select(".manga-item"):
        project = _build_search_project(item)
        if project is None:
            continue
        project_id = str(project["id"])
        if project_id in seen_ids:
            continue
        seen_ids.add(project_id)
        results.append(project)

    return results[: max(1, min(limit, 100))]


def _build_chapter_payload(project_slug: str, chapter_link: Tag) -> dict[str, Any] | None:
    chapter_url = _attr(chapter_link, "href")
    if not chapter_url:
        return None

    normalized_url = urljoin(HANAMI_SITE_URL, str(chapter_url).strip())
    chapter_slug = _chapter_slug_from_url(normalized_url)
    chapter_label = _normalize_text(chapter_link) or chapter_slug
    chapter_number = _extract_numeric_chapter(chapter_label)
    parent = chapter_link.find_parent("li")
    date_text = _normalize_text(parent.select_one(".chapter-release-date i")) if parent else ""
    published_at = _parse_pt_br_date(date_text)

    return {
        "id": chapter_slug,
        "manga_id": project_slug,
        "provider": HANAMI_PROVIDER_KEY,
        "url": normalized_url,
        "number": chapter_number,
        "title": chapter_label,
        "label": chapter_label,
        "published_at": published_at,
        "folder_name": _sanitize_path_component(chapter_number or chapter_slug, fallback=chapter_slug),
    }


def _chapter_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    number_value = item.get("number")
    numeric = float(str(number_value)) if number_value not in (None, "") else None
    if numeric is not None:
        return (0, -numeric, -_parse_date_score(item.get("published_at")), str(item.get("id") or ""))
    return (
        1,
        -_parse_date_score(item.get("published_at")),
        _normalize_text(item.get("label") or item.get("title") or item.get("id")),
        str(item.get("id") or ""),
    )


def get_hanami_project(project_slug_or_url: int | str) -> dict[str, Any]:
    project_url = _normalize_project_url(str(project_slug_or_url))
    session = create_hanami_session()
    response = _request_response(session, "get", project_url)
    soup = BeautifulSoup(response.text, "html.parser")
    project_slug = _project_slug_from_url(project_url)

    title = (
        _normalize_text(soup.select_one(".post-content h1"))
        or _normalize_text(soup.select_one(".summary-heading h1"))
        or project_slug
    )
    description = _normalize_text(soup.select_one(".manga-excerpt")) or None
    cover_url = _extract_image_url(soup.select_one(".summary_image img"))
    summary_map = _parse_summary_map(soup)
    authors = _tag_texts(summary_map.get("autor(es)"))
    artists = _tag_texts(summary_map.get("artista(s)"))
    genres = _tag_texts(summary_map.get("gênero(s)"))
    tags = _tag_texts(summary_map.get("tag(s)"))
    release = _normalize_text(summary_map.get("lançamento")) or None
    status = _normalize_text(summary_map.get("status")) or None
    project_type = _normalize_text(summary_map.get("tipo")) or None
    rating = _normalize_text(soup.select_one("#averagerate")) or None
    rating_count = _normalize_text(soup.select_one("#countrate")) or None
    original_title = _extract_alternative_title(response.text)
    if original_title == title:
        original_title = None

    chapters_response = _request_response(session, "post", urljoin(project_url, "ajax/chapters/"))
    chapters_soup = BeautifulSoup(chapters_response.text, "html.parser")
    chapters: list[dict[str, Any]] = []
    seen_chapter_ids: set[str] = set()
    for item in chapters_soup.select("li.wp-manga-chapter a[href]"):
        chapter = _build_chapter_payload(project_slug, item)
        if chapter is None:
            continue
        chapter_id = str(chapter["id"])
        if chapter_id in seen_chapter_ids:
            continue
        seen_chapter_ids.add(chapter_id)
        chapters.append(chapter)

    if not chapters:
        raise MDScrapperProviderError("Não encontrei capítulos no Hanami.")

    chapters.sort(key=_chapter_sort_key)
    latest_chapter_at = next((item.get("published_at") for item in chapters if item.get("published_at")), None)

    return {
        "provider": HANAMI_PROVIDER_KEY,
        "project": {
            "id": project_slug,
            "provider": HANAMI_PROVIDER_KEY,
            "title": title,
            "original_title": original_title,
            "description": description,
            "cover_url": cover_url,
            "url": project_url,
            "chapter_count": len(chapters),
            "latest_chapter_at": latest_chapter_at,
            "authors_raw": authors,
            "artists_raw": artists,
            "genres_raw": genres,
            "tags_raw": tags,
            "release": release,
            "status": status,
            "project_type": project_type,
            "rating": rating,
            "rating_count": rating_count,
        },
        "chapters": chapters,
    }


def get_hanami_project_by_url(project_url: str) -> dict[str, Any]:
    if not is_hanami_url(project_url):
        raise MDScrapperProviderError("A URL informada não pertence ao Hanami.")
    return get_hanami_project(project_url)


def build_hanami_chapter_list_url(chapter_url: str) -> str:
    normalized_url = str(chapter_url or "").strip()
    if not normalized_url:
        raise MDScrapperProviderError("URL do capítulo Hanami inválida.")
    if not normalized_url.startswith(("http://", "https://")):
        normalized_url = urljoin(HANAMI_SITE_URL, normalized_url)

    url_parts = list(urlparse(normalized_url))
    query = dict(parse_qs(url_parts[4]))
    query["style"] = ["list"]
    url_parts[4] = urlencode(query, doseq=True)
    return urlunparse(url_parts)


def fetch_hanami_chapter_manifest(chapter_url: str, *, session=None) -> dict[str, Any]:
    normalized_url = str(chapter_url or "").strip()
    if not normalized_url:
        raise MDScrapperProviderError("URL do capítulo Hanami inválida.")
    if not normalized_url.startswith(("http://", "https://")):
        normalized_url = urljoin(HANAMI_SITE_URL, normalized_url)

    active_session = session or create_hanami_session()
    list_url = build_hanami_chapter_list_url(normalized_url)
    response = _request_response(
        active_session,
        "get",
        list_url,
        headers={"Referer": normalized_url},
    )
    soup = BeautifulSoup(response.text, "html.parser")
    image_urls = [
        str(url).strip()
        for url in (_extract_image_url(node) for node in soup.select("div.page-break img"))
        if str(url or "").strip() and "/WP-manga/data/" in str(url)
    ]
    if not image_urls:
        raise MDScrapperProviderError("Não encontrei imagens no capítulo do Hanami.")

    current_chapter = soup.select_one("#wp-manga-current-chap")
    chapter_slug = _attr(current_chapter, "value") or _chapter_slug_from_url(normalized_url)
    return {
        "chapter_slug": chapter_slug,
        "list_url": list_url,
        "image_urls": image_urls,
    }
