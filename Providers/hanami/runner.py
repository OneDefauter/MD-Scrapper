from __future__ import annotations

import concurrent.futures as cf
import json
import shutil
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

import requests

from app.Services.Database import session_scope
from app.Services.Database.scraper_download_store import (
    get_scraper_download_by_id,
    update_scraper_download,
)
from app.Services.MD_Scrapper.Providers.hanami.core import (
    HANAMI_PROVIDER_KEY,
    build_hanami_chapter_list_url,
    create_hanami_session,
    fetch_hanami_chapter_manifest,
    warm_hanami_session,
)
from app.Services.MD_Scrapper.settings import get_provider_settings
from app.Services.Records import WorkerRecordLogger
from app.Services.Workers.core import Cancelled

REQUEST_TIMEOUT = (10, 60)
CHUNK_SIZE = 1024 * 256


def _load_worker_context() -> dict[str, Any]:
    return get_provider_settings(HANAMI_PROVIDER_KEY)


def _extract_project_title(project: dict[str, Any]) -> str | None:
    attributes = project.get("attributes") if isinstance(project, dict) else {}
    title_map = attributes.get("title") if isinstance(attributes, dict) else {}
    if isinstance(title_map, dict):
        for value in title_map.values():
            if value:
                return str(value)
    if isinstance(project, dict) and project.get("id"):
        return str(project["id"])
    return None


def _manifest_from_job(job: dict[str, Any]) -> tuple[str, list[str]] | None:
    files = job.get("files") or {}
    chapter_slug = str(files.get("chapter_slug") or "").strip()
    image_urls = files.get("image_urls") or []
    if not chapter_slug or not isinstance(image_urls, list):
        return None

    normalized_urls = [str(item).strip() for item in image_urls if str(item).strip()]
    if not normalized_urls:
        return None
    return chapter_slug, normalized_urls


def _store_manifest(job_id: str, chapter_slug: str, image_urls: list[str]) -> None:
    with session_scope() as session:
        scraper_download = get_scraper_download_by_id(session, UUID(str(job_id)))
        if scraper_download is None:
            return

        files = dict(scraper_download.files or {})
        files["chapter_slug"] = chapter_slug
        files["image_urls"] = list(image_urls)
        files["count"] = len(image_urls)
        update_scraper_download(session, scraper_download, files=files)


def _resolve_target_dir(job: dict[str, Any]) -> Path:
    files = job.get("files") or {}
    target = str(files.get("path") or files.get("target_dir") or "").strip()
    if not target:
        raise RuntimeError("Scraper download job sem path de destino.")
    return Path(target)


def _prepare_target_dir(target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)


def _cleanup_target_dir(target_dir: Path) -> None:
    shutil.rmtree(target_dir, ignore_errors=True)


def _build_output_name(index: int, total: int, source_url: str) -> str:
    path = Path(source_url.split("?", 1)[0])
    suffix = path.suffix
    pad = max(2, len(str(total)))
    return f"{index:0{pad}d}{suffix}"


def _write_metadata_file(target_dir: Path, job: dict[str, Any], *, page_count: int) -> None:
    metadata = {
        "provider": HANAMI_PROVIDER_KEY,
        "job_id": str(job.get("id") or ""),
        "metadata": job.get("metadata") or {},
        "files": {
            key: value
            for key, value in dict(job.get("files") or {}).items()
            if key in {"source_url", "target_dir", "path", "chapter_slug", "image_urls"}
        },
        "page_count": page_count,
    }
    (target_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _download_file(
    url: str,
    destination: Path,
    *,
    referer_url: str,
    user_agent: str,
    cookies: dict[str, str],
    cancel_event: threading.Event,
) -> None:
    tmp_path = destination.with_suffix(destination.suffix + ".part")
    if cancel_event.is_set():
        raise Cancelled()

    try:
        with requests.get(
            url,
            stream=True,
            timeout=REQUEST_TIMEOUT,
            headers={
                "Referer": referer_url,
                "Accept": "image/*",
                "User-Agent": user_agent,
            },
            cookies=cookies,
        ) as response:
            response.raise_for_status()
            with tmp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if cancel_event.is_set():
                        raise Cancelled()
                    if not chunk:
                        continue
                    handle.write(chunk)
        tmp_path.replace(destination)
    except Cancelled:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except requests.RequestException as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"Falha ao baixar imagem do Hanami: {exc}") from exc
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def run(job: dict[str, Any], hb) -> None:
    metadata = job.get("metadata") or {}
    project = metadata.get("project") if isinstance(metadata, dict) else {}
    chapter = metadata.get("chapter") if isinstance(metadata, dict) else {}
    source = metadata.get("source") if isinstance(metadata, dict) else {}
    project_title = _extract_project_title(project)
    chapter_id = str(chapter.get("id") or "").strip()
    chapter_number = str(chapter.get("num") or "").strip() or None
    chapter_title = str(chapter.get("title") or "").strip() or None
    source_url = str(chapter.get("url") or (job.get("files") or {}).get("source_url") or "").strip()
    if not chapter_id:
        raise RuntimeError("Scraper download job sem chapter id.")
    if not source_url:
        raise RuntimeError("Scraper download job sem source_url.")

    record_logger = WorkerRecordLogger(
        kind="download",
        job_id=str(job["id"]),
        project_id=str(project.get("id") or "").strip() or None,
        project_title=project_title,
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        chapter_title=chapter_title,
        language="pt-br",
        summary={
            "provider": source.get("provider") or HANAMI_PROVIDER_KEY,
            "source_url": source_url,
            "target_dir": (job.get("files") or {}).get("target_dir"),
        },
    )
    record_logger.start(stage="claim")
    record_logger.event(
        stage="claim",
        message="Hanami scraper runner claimed job.",
        data={"job_id": str(job["id"]), "chapter_id": chapter_id, "source_url": source_url},
    )

    current_stage = "manifest"
    settings = _load_worker_context()
    images_concurrent = max(1, int(settings.get("images_concurrent", 4) or 4))
    record_logger.event(
        stage="context",
        message="Hanami scraper runner loaded worker context.",
        data={"images_concurrent": images_concurrent},
    )

    if not hb(0):
        raise Cancelled()

    target_dir: Path | None = None
    cancel_signal = threading.Event()
    executor: cf.ThreadPoolExecutor | None = None
    futures: dict[Any, int] = {}

    try:
        session = create_hanami_session()
        warm_hanami_session(session)
        user_agent = str(session.headers.get("User-Agent") or "Mozilla/5.0")
        cookies = requests.utils.dict_from_cookiejar(session.cookies)
        referer_url = build_hanami_chapter_list_url(source_url)

        manifest = _manifest_from_job(job)
        if manifest is None:
            manifest_payload = fetch_hanami_chapter_manifest(source_url, session=session)
            manifest = (
                str(manifest_payload["chapter_slug"]).strip(),
                list(manifest_payload["image_urls"]),
            )
            _store_manifest(str(job["id"]), *manifest)
            referer_url = str(manifest_payload.get("list_url") or referer_url)
            record_logger.event(
                stage=current_stage,
                message="Fetched Hanami chapter manifest.",
                data={"file_count": len(manifest[1])},
            )
        else:
            record_logger.event(
                stage=current_stage,
                message="Using cached Hanami chapter manifest.",
                data={"file_count": len(manifest[1])},
            )

        chapter_slug, image_urls = manifest
        current_stage = "prepare_target"
        target_dir = _resolve_target_dir(job)
        _prepare_target_dir(target_dir)
        record_logger.event(
            stage=current_stage,
            message="Prepared scraper target directory.",
            data={"target_dir": str(target_dir)},
        )

        total = len(image_urls)
        download_jobs: list[tuple[str, Path]] = []
        for index, image_url in enumerate(image_urls, start=1):
            file_name = _build_output_name(index, total, image_url)
            download_jobs.append((image_url, target_dir / file_name))

        executor = cf.ThreadPoolExecutor(max_workers=min(images_concurrent, max(1, total)))
        futures = {
            executor.submit(
                _download_file,
                image_url,
                destination,
                referer_url=referer_url,
                user_agent=user_agent,
                cookies=cookies,
                cancel_event=cancel_signal,
            ): position
            for position, (image_url, destination) in enumerate(download_jobs, start=1)
        }

        current_stage = "download_pages"
        record_logger.event(
            stage=current_stage,
            message="Starting Hanami page downloads.",
            data={"total_pages": total, "chapter_slug": chapter_slug},
        )
        completed = 0
        for future in cf.as_completed(futures):
            future.result()
            completed += 1
            page_url, destination = download_jobs[futures[future] - 1]
            record_logger.event(
                stage=current_stage,
                level="debug",
                message="Downloaded scraper page.",
                data={"page_index": futures[future], "destination": str(destination), "source_url": page_url},
            )
            progress = int(completed * 10_000 / max(1, total))
            if not hb(progress):
                cancel_signal.set()
                raise Cancelled()

        if not hb(10_000):
            raise Cancelled()
        _write_metadata_file(target_dir, job, page_count=total)
        record_logger.event(
            stage="finalize",
            message="Hanami chapter download completed.",
            data={"target_dir": str(target_dir), "page_count": total, "chapter_slug": chapter_slug},
        )
        record_logger.finish(
            status="done",
            stage="done",
            progress_bp_final=10_000,
            summary={
                "provider": HANAMI_PROVIDER_KEY,
                "target_dir": str(target_dir),
                "page_count": total,
                "chapter_slug": chapter_slug,
            },
        )
    except Cancelled as exc:
        record_logger.event(
            stage=current_stage,
            level="warning",
            code="CANCELLED",
            message="Hanami scraper runner detected cancellation.",
            data={"detail": str(exc) or "Cancelled"},
        )
        record_logger.finish(
            status="cancelled",
            stage=current_stage,
            last_error={"code": "CANCELLED", "message": str(exc) or "Cancelled"},
        )
        cancel_signal.set()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        if target_dir is not None:
            _cleanup_target_dir(target_dir)
        raise
    except Exception as exc:
        record_logger.event(
            stage=current_stage,
            level="error",
            code="RUNNER_FAILED",
            message="Hanami scraper runner failed.",
            data={"error": str(exc)},
        )
        record_logger.finish(
            status="error",
            stage=current_stage,
            last_error={"code": "RUNNER_FAILED", "message": str(exc)},
        )
        cancel_signal.set()
        for future in futures:
            future.cancel()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        if target_dir is not None:
            _cleanup_target_dir(target_dir)
        raise
    else:
        if executor is not None:
            executor.shutdown(wait=True)
