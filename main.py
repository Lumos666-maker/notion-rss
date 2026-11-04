import logging
import os
import sys

from enrich import prepare_backfill, prepare_items
from feed import get_new_feed_items
from notion import (
    add_feed_item_to_notion,
    delete_old_unread_feed_items_from_notion,
    get_backfill_candidates,
    get_page_plain_text,
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
        logger.info("LLM env vars missing; writing items without 摘要")

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


def run_backfill() -> None:
    """Fill 摘要 for recent rows that are still empty or stuck at 收录中.

    Rows that already have a summary but are still 收录中 are only flipped to
    已整理. Rows with an empty summary are fetched and summarized. LLM failures
    stay 收录中 so the next run can retry them.
    """
    if not os.getenv("NOTION_API_TOKEN") or not os.getenv("NOTION_READER_DATABASE_ID"):
        logger.error("Backfill needs NOTION_API_TOKEN and NOTION_READER_DATABASE_ID")
        sys.exit(1)
    if not llm_configured():
        logger.info("LLM env vars missing; backfill skipped")
        return

    days = _backfill_days()
    candidates = get_backfill_candidates(days)
    logger.info("Backfill candidates from the last %d days: %d", days, len(candidates))

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

    logger.info(
        "Backfill complete: %d updated, %d write failures, %d left at 收录中",
        updated,
        failed,
        still_pending,
    )


def main() -> None:
    mode = os.getenv("RUN_MODE", "ingest").strip().lower()
    if mode == "backfill":
        run_backfill()
    else:
        run_ingest()


if __name__ == "__main__":
    main()
