import logging
import os
import sys

from enrich import fetch_fulltext_for_pages, prepare_backfill, prepare_items
from feed import get_new_feed_items
from notion import (
    add_feed_item_to_notion,
    delete_old_unread_feed_items_from_notion,
    get_backfill_candidates,
    get_page_plain_text,
    get_recent_pages,
    page_body_is_empty,
    update_reader_page,
)
from summarize import llm_configured
from feishu import send_feed_summary_to_feishu

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


def run_ingest() -> None:
    """Fetch new RSS items, optionally summarize them, and write Notion pages."""
    feed_items = get_new_feed_items()
    logger.info("Fetched %d new feed items", len(feed_items))

    if feed_items:
        send_feed_summary_to_feishu(feed_items)

    if llm_configured():
        logger.info("LLM summarization enabled (%s)", os.getenv("LLM_MODEL", ""))
    else:
        logger.info("LLM env vars missing; fetching full text without 摘要")

    prepared = prepare_items(feed_items)
    success, failed = 0, 0
    for item in prepared:
        if add_feed_item_to_notion(item):
            success += 1
        else:
            failed += 1

    logger.info("Notion write complete: %d success, %d failed", success, failed)
    delete_old_unread_feed_items_from_notion()


def _backfill_days() -> int:
    try:
        days = int(os.getenv("BACKFILL_DAYS", "3"))
    except ValueError:
        days = 3
    return max(1, min(days, 14))


def _backfill_summaries(days: int) -> tuple[int, int, int]:
    """Fill missing 摘要. Returns (updated, write_failures, still_pending)."""
    candidates = get_backfill_candidates(days)
    logger.info("Summary candidates from the last %d days: %d", days, len(candidates))

    status_only: list[dict] = []
    need_summary: list[dict] = []
    for page in candidates:
        if (page.get("summary") or "").strip():
            if page.get("status") == "收录中":
                status_only.append(page)
        else:
            need_summary.append(page)

    updated, failed = 0, 0
    for page in status_only:
        if update_reader_page(page["id"], status="已整理"):
            updated += 1
        else:
            failed += 1

    prepared = prepare_backfill(need_summary, get_page_plain_text)
    still_pending = 0
    for page, payload in zip(need_summary, prepared):
        if not payload.get("llm_ok"):
            still_pending += 1
            continue
        new_title = payload.get("title") or ""
        title_update = new_title if new_title and new_title != page.get("title") else None
        blocks = payload.get("content") if payload.get("fulltext") else None
        if update_reader_page(
            page["id"],
            title=title_update,
            summary=payload.get("summary"),
            status="已整理",
            blocks=blocks,
        ):
            updated += 1
        else:
            failed += 1
    return updated, failed, still_pending


def _backfill_empty_bodies(days: int) -> tuple[int, int, int]:
    """Fetch full text for pages whose body is empty. Returns (updated, failures, still_empty)."""
    pages = get_recent_pages(days)
    empty = [page for page in pages if page.get("link") and page_body_is_empty(page.get("id") or "")]
    logger.info("Empty-body pages from the last %d days: %d", days, len(empty))

    updated, failed, still_empty = 0, 0, 0
    fetched = fetch_fulltext_for_pages(empty)
    for page, blocks in zip(empty, fetched):
        if not blocks:
            still_empty += 1
            continue
        if update_reader_page(page["id"], status="已整理", blocks=blocks):
            updated += 1
        else:
            failed += 1
    return updated, failed, still_empty


def run_backfill() -> None:
    """Fill empty bodies, and summaries when the LLM secrets are set.

    Without an LLM this only rewrites pages from the last few days that have
    no body text. With an LLM it also fills an empty 摘要 or a row stuck at
    收录中. A failed model call stays 收录中. A failed refetch of an empty
    body stays empty so the next run can try again.
    """
    if not os.getenv("NOTION_API_TOKEN") or not os.getenv("NOTION_READER_DATABASE_ID"):
        logger.error("Backfill needs NOTION_API_TOKEN and NOTION_READER_DATABASE_ID")
        sys.exit(1)

    days = _backfill_days()
    summarized = failed = still_pending = 0
    if llm_configured():
        logger.info("LLM summarization enabled (%s)", os.getenv("LLM_MODEL", ""))
        summarized, failed, still_pending = _backfill_summaries(days)
    else:
        logger.info("LLM env vars missing; backfill will only fill empty page bodies")

    # After summaries, so a body written above is not fetched a second time.
    filled, fill_failed, still_empty = _backfill_empty_bodies(days)
    logger.info(
        "Backfill complete: %d summaries, %d bodies, %d write failures, "
        "%d summaries still pending, %d bodies still empty",
        summarized,
        filled,
        failed + fill_failed,
        still_pending,
        still_empty,
    )


def main() -> None:
    mode = os.getenv("RUN_MODE", "ingest").strip().lower()
    if mode == "backfill":
        run_backfill()
    else:
        run_ingest()


if __name__ == "__main__":
    main()
