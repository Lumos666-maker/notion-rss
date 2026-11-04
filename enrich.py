"""Attach full text and, when configured, a Chinese summary to each new item.

Full text is on unless FULLTEXT_FETCH is 0/false/off. The LLM is optional:
without LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL the page still gets a body
and 处理状态=已整理, but no 摘要 and no translated title.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from fulltext import fetch_article_text
from parser import html_to_notion_blocks, html_to_plain_text, text_to_notion_blocks
from summarize import (
    LlmError,
    compose_bilingual_title,
    llm_configured,
    summarize_item,
    title_needs_zh,
)

logger = logging.getLogger(__name__)


def fulltext_enabled() -> bool:
    """Full text is the default. Set FULLTEXT_FETCH=0 to keep the RSS HTML only."""
    flag = os.getenv("FULLTEXT_FETCH", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _worker_count() -> int:
    try:
        workers = int(os.getenv("ENRICH_WORKERS", "4"))
    except ValueError:
        workers = 4
    return max(1, min(workers, 8))


def _rss_payload(item: dict) -> dict:
    """The pre-enrichment Notion payload: RSS HTML body, original title, 收录中."""
    return {
        "title": item.get("title") or "",
        "link": item.get("link") or "",
        "content": html_to_notion_blocks(item.get("content") or ""),
        "published_date": item.get("published_date"),
        "summary": None,
        "status": "收录中",
        "source": item.get("source") or "",
        "fulltext": False,
        "teaser_only": False,
        "llm_ok": False,
    }


def _original_title(stored_title: str) -> str:
    """Recover the RSS title from a bilingual `中文｜Original` value."""
    if "｜" in stored_title:
        original = stored_title.split("｜", 1)[1].strip()
        if original:
            return original
    return stored_title


def enrich_one(item: dict, *, fallback_text: str = "") -> dict:
    """Build one Notion payload. Never raises; LLM failure leaves 收录中."""
    payload = _rss_payload(item)
    title = _original_title(payload["title"])
    source = payload["source"]
    link = payload["link"]

    full_text = None
    if fulltext_enabled() and link:
        try:
            full_text = fetch_article_text(link)
        except Exception as err:
            logger.info("Full text error for %s: %s", link, err)
            full_text = None

    if full_text:
        payload["content"] = text_to_notion_blocks(full_text)
        payload["fulltext"] = True
        material = full_text
        teaser_only = False
    else:
        rss_text = html_to_plain_text(item.get("content") or "")
        # Backfill passes the page body when the network fetch fails. A long body
        # is a previous successful extract, not a teaser.
        if len(fallback_text) >= 800:
            material = fallback_text
            teaser_only = False
        else:
            material = rss_text or fallback_text or title
            teaser_only = True
        payload["teaser_only"] = teaser_only

    if not llm_configured():
        # Body is either the article or the RSS HTML. Both are finished ingest.
        payload["summary"] = None
        payload["status"] = "已整理"
        return payload

    try:
        summary, title_zh = summarize_item(
            title=title,
            source=source,
            text=material,
            teaser_only=teaser_only,
        )
    except LlmError as err:
        logger.warning("LLM failed for %s: %s", link or title, err)
        return payload
    except Exception as err:
        logger.warning("LLM error for %s: %s", link or title, err)
        return payload

    payload["summary"] = summary
    payload["llm_ok"] = True
    payload["status"] = "已整理"
    payload["title"] = _display_title(payload["title"], title, title_zh)
    return payload


def _display_title(stored: str, original: str, title_zh: str) -> str:
    """Keep an existing bilingual title; otherwise add a translation once."""
    if "｜" in stored:
        return stored
    if title_needs_zh(original):
        return compose_bilingual_title(original, title_zh)
    return original or stored


def _run_pool(jobs: list) -> list:
    if not jobs:
        return []
    if len(jobs) == 1 or _worker_count() == 1:
        out = []
        for fn in jobs:
            out.append(fn())
        return out
    results: list = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=_worker_count()) as executor:
        futures = {executor.submit(fn): index for index, fn in enumerate(jobs)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception:
                logger.exception("Enrich worker failed")
                results[index] = None
    return results


def prepare_items(items: list[dict]) -> list[dict]:
    """Enrich RSS items, preserving input order."""
    if not items:
        return []

    def _job(item: dict):
        def run() -> dict:
            try:
                return enrich_one(item)
            except Exception:
                logger.exception("Enrich failed for %s", item.get("link"))
                return _rss_payload(item)
        return run

    prepared = _run_pool([_job(item) for item in items])
    for index, payload in enumerate(prepared):
        if payload is None:
            prepared[index] = _rss_payload(items[index])
    _log_stats(prepared)
    return prepared


def prepare_backfill(pages: list[dict], page_text) -> list[dict]:
    """Enrich existing Notion pages. `page_text(page_id)` supplies the current body.

    Each result includes `page_id`. `content` is None when the body should stay
    as it is (the article URL could not be fetched).
    """
    if not pages:
        return []
    return _prepare_backfill(pages, page_text)


def _prepare_backfill(pages: list[dict], page_text) -> list[dict]:
    def _job(page: dict):
        def run() -> dict:
            return _enrich_page(page, page_text)
        return run

    prepared = _run_pool([_job(page) for page in pages])
    for index, payload in enumerate(prepared):
        if payload is None:
            payload = _rss_payload(pages[index])
            payload["content"] = None
            payload["page_id"] = pages[index].get("id")
            prepared[index] = payload
    _log_stats(prepared)
    return prepared


def _enrich_page(page: dict, page_text) -> dict:
    """Like enrich_one, but a failed fetch can fall back to the stored page body."""
    item = {
        "title": page.get("title") or "",
        "link": page.get("link") or "",
        "content": "",
        "published_date": page.get("published_date"),
        "source": page.get("source") or "",
    }
    payload = _rss_payload(item)
    payload["page_id"] = page.get("id")
    title = _original_title(payload["title"])
    link = payload["link"]

    full_text = None
    if fulltext_enabled() and link:
        try:
            full_text = fetch_article_text(link)
        except Exception as err:
            logger.info("Full text error for %s: %s", link, err)

    fallback = ""
    if not full_text:
        try:
            fallback = page_text(page.get("id") or "") or ""
        except Exception as err:
            logger.info("Could not read page body %s: %s", page.get("id"), err)

    if full_text:
        payload["content"] = text_to_notion_blocks(full_text)
        payload["fulltext"] = True
        material = full_text
        teaser_only = False
    else:
        payload["content"] = None
        if len(fallback) >= 800:
            material = fallback
            teaser_only = False
        else:
            material = fallback or title
            teaser_only = True
        payload["teaser_only"] = teaser_only

    if not llm_configured():
        payload["summary"] = None
        if payload["fulltext"]:
            payload["status"] = "已整理"
        else:
            # Nothing new to store. Leave the existing body and status alone.
            payload["content"] = None
            payload["status"] = None
        return payload

    try:
        summary, title_zh = summarize_item(
            title=title,
            source=item["source"],
            text=material,
            teaser_only=teaser_only,
        )
    except LlmError as err:
        logger.warning("LLM failed for %s: %s", link or title, err)
        return payload
    except Exception as err:
        logger.warning("LLM error for %s: %s", link or title, err)
        return payload

    payload["summary"] = summary
    payload["llm_ok"] = True
    payload["status"] = "已整理"
    payload["title"] = _display_title(payload["title"], title, title_zh)
    return payload


def fetch_fulltext_for_pages(pages: list[dict]) -> list[list[dict] | None]:
    """Fetch article blocks for each page, in input order. None means the fetch failed."""
    def _job(page: dict):
        url = page.get("link") or ""

        def run() -> list[dict] | None:
            return fulltext_blocks(url)
        return run

    return _run_pool([_job(page) for page in pages])


def fulltext_blocks(url: str) -> list[dict] | None:
    """Notion blocks for a URL, or None when the article could not be fetched."""
    if not fulltext_enabled() or not url:
        return None
    try:
        text = fetch_article_text(url)
    except Exception as err:
        logger.info("Full text error for %s: %s", url, err)
        return None
    if not text:
        return None
    blocks = text_to_notion_blocks(text)
    return blocks or None


def _log_stats(prepared: list[dict]) -> None:
    fulltext = sum(1 for item in prepared if item.get("fulltext"))
    teaser = sum(1 for item in prepared if item.get("teaser_only"))
    summarized = sum(1 for item in prepared if item.get("llm_ok"))
    logger.info(
        "Enrich complete: %d items, %d full text, %d teaser-only, %d summarized",
        len(prepared),
        fulltext,
        teaser,
        summarized,
    )
