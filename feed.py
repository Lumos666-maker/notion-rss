import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import feedparser
from dotenv import load_dotenv

from blocklist import is_blocked
from notion import get_feed_urls_from_notion, get_existing_items_since
from urlnorm import normalize_url

load_dotenv()

logger = logging.getLogger(__name__)
RUN_FREQUENCY = int(os.getenv("RUN_FREQUENCY", "259200"))
_MAX_FETCH_WORKERS = 5


def _parse_struct_time_to_timestamp(st) -> float:
    if st:
        return time.mktime(st)
    return 0.0


def _extract_content(item) -> str:
    content_list = item.get("content", [])
    if content_list:
        return content_list[0].get("value", "") or item.get("summary", "")
    return item.get("summary", "")


def _get_new_feed_items_from(
    feed_url: str,
    existing_titles: set[str],
    existing_links: set[str],
    source: str = "",
) -> list[dict]:
    try:
        rss = feedparser.parse(feed_url)
    except Exception as e:
        logger.error("Error parsing feed %s: %s", feed_url, e)
        return []

    current_time_struct = rss.get("updated_parsed") or rss.get("published_parsed")
    current_time = (
        _parse_struct_time_to_timestamp(current_time_struct)
        if current_time_struct
        else time.time()
    )

    new_items: list[dict] = []
    for item in rss.entries:
        pub_date = item.get("published_parsed") or item.get("updated_parsed")
        if not pub_date:
            continue

        blog_published_time = _parse_struct_time_to_timestamp(pub_date)
        if (current_time - blog_published_time) >= RUN_FREQUENCY:
            continue

        title = item.get("title", "")
        link = item.get("link", "")
        normalized = normalize_url(link)

        if title in existing_titles or link in existing_links or (
            normalized and normalized in existing_links
        ):
            continue

        published_date = datetime(*pub_date[:6]).strftime("%Y-%m-%d")

        new_items.append({
            "title": title,
            "link": link,
            "content": _extract_content(item),
            "published_parsed": pub_date,
            "published_date": published_date,
            "source": source,
        })

    return new_items


def select_new_items(
    items: list[dict],
    existing_titles: set[str],
    existing_links: set[str],
) -> list[dict]:
    """Drop in-batch duplicates and blocklisted rows. First occurrence wins.

    A row is a duplicate when its exact title or its normalized URL is already
    known. The stored link is the normalized URL so later runs compare equal.
    """
    seen_titles = set(existing_titles)
    seen_links = set(existing_links)
    kept: list[dict] = []
    dropped_dupes = 0
    dropped_blocked = 0

    for item in items:
        title = (item.get("title") or "").strip()
        raw_link = item.get("link") or ""
        normalized = normalize_url(raw_link)
        if (title and title in seen_titles) or (normalized and normalized in seen_links) or (
            raw_link and raw_link in seen_links
        ):
            dropped_dupes += 1
            continue
        if is_blocked(title, item.get("content") or "", item.get("source") or ""):
            dropped_blocked += 1
            logger.info("Blocklist drop: %s [%s]", title, item.get("source") or "")
            continue
        if title:
            seen_titles.add(title)
        if normalized:
            seen_links.add(normalized)
        if raw_link:
            seen_links.add(raw_link)
        kept.append({
            **item,
            "title": title,
            "link": normalized or raw_link,
        })

    if dropped_dupes or dropped_blocked:
        logger.info(
            "Filtered items: %d duplicates, %d blocklisted, %d kept",
            dropped_dupes,
            dropped_blocked,
            len(kept),
        )
    return kept


def get_new_feed_items() -> list[dict]:
    feeds = get_feed_urls_from_notion()
    existing_titles, existing_links = get_existing_items_since(days=5)

    jobs = [(f["feedUrl"], f.get("title") or "") for f in feeds if f.get("feedUrl")]
    all_new_feed_items: list[dict] = []

    with ThreadPoolExecutor(max_workers=_MAX_FETCH_WORKERS) as executor:
        futures = {
            executor.submit(
                _get_new_feed_items_from, url, existing_titles, existing_links, source
            ): url
            for url, source in jobs
        }
        for future in as_completed(futures):
            try:
                all_new_feed_items.extend(future.result())
            except Exception as e:
                logger.error("Error fetching feed %s: %s", futures[future], e)

    all_new_feed_items.sort(
        key=lambda x: _parse_struct_time_to_timestamp(x.get("published_parsed"))
    )
    return select_new_items(all_new_feed_items, existing_titles, existing_links)
