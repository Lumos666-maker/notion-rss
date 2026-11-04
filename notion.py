import datetime
import logging
import os

import requests
from dotenv import load_dotenv

from urlnorm import normalize_url

load_dotenv()

logger = logging.getLogger(__name__)

NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_READER_DATABASE_ID = os.getenv("NOTION_READER_DATABASE_ID")
NOTION_FEEDS_DATABASE_ID = os.getenv("NOTION_FEEDS_DATABASE_ID")

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"
_MAX_BLOCKS_PER_REQUEST = 100
_REQUEST_TIMEOUT = 30
_ARCHIVE_AFTER_DAYS = 30

# Populated by _load_reader_schema(). None means "could not check".
_schema_loaded = False
_reader_schema: dict[str, str] | None = None


def _get_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }


def _load_reader_schema() -> dict[str, str] | None:
    """Map reader property name -> Notion type. None if the lookup didn't run."""
    global _schema_loaded, _reader_schema
    if _schema_loaded:
        return _reader_schema
    _schema_loaded = True
    if not NOTION_API_TOKEN or not NOTION_READER_DATABASE_ID:
        return None
    try:
        response = requests.get(
            f"{NOTION_BASE_URL}/databases/{NOTION_READER_DATABASE_ID}",
            headers=_get_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        props = response.json().get("properties", {})
    except requests.exceptions.RequestException as err:
        logger.warning("Could not read reader database schema: %s", err)
        return None
    _reader_schema = {
        name: (meta or {}).get("type", "")
        for name, meta in props.items()
    }
    logger.info("Reader properties: %s", ", ".join(sorted(_reader_schema)))
    return _reader_schema


def _summary_supported() -> bool:
    """Whether 摘要 can be written. Unknown schemas are tried, then retried without it."""
    schema = _load_reader_schema()
    if schema is None:
        return True
    return schema.get("摘要") == "rich_text"


def _query_database_with_pagination(
    database_id: str, payload: dict
) -> list[dict]:
    url = f"{NOTION_BASE_URL}/databases/{database_id}/query"
    all_results: list[dict] = []
    has_more = True
    start_cursor: str | None = None

    while has_more:
        body = dict(payload)
        if start_cursor:
            body["start_cursor"] = start_cursor

        try:
            response = requests.post(
                url, headers=_get_headers(), json=body, timeout=_REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as err:
            detail = ""
            resp = getattr(err, "response", None)
            if resp is not None and getattr(resp, "text", None):
                detail = " " + resp.text[:400]
            logger.error("Error querying database %s: %s%s", database_id, err, detail)
            break

        all_results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return all_results


def get_feed_urls_from_notion() -> list[dict]:
    payload = {
        "filter": {
            "property": "Rss地址",
            "url": {"is_not_empty": True},
        }
    }

    results = _query_database_with_pagination(NOTION_FEEDS_DATABASE_ID, payload)

    feeds: list[dict] = []
    for item in results:
        props = item.get("properties", {})
        title_prop = props.get("名称", {}).get("title", [])
        link_prop = props.get("Rss地址", {}).get("url")

        title = "".join(part.get("plain_text", "") for part in title_prop)
        feeds.append({"title": title, "feedUrl": link_prop})

    return feeds


def _plain_rich_text(parts: list[dict] | None) -> str:
    return "".join(part.get("plain_text", "") for part in (parts or []))


def get_existing_items_since(days: int = 5) -> tuple[set[str], set[str]]:
    since_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

    payload = {
        "filter": {
            "timestamp": "created_time",
            "created_time": {"on_or_after": since_date.isoformat()},
        }
    }

    results = _query_database_with_pagination(NOTION_READER_DATABASE_ID, payload)

    titles: set[str] = set()
    links: set[str] = set()

    for item in results:
        props = item.get("properties", {})
        plain_title = _plain_rich_text(props.get("名称", {}).get("title"))
        if plain_title:
            titles.add(plain_title)
            # Bilingual titles are stored as "中文译名｜Original Title".
            if "｜" in plain_title:
                original = plain_title.split("｜", 1)[1].strip()
                if original:
                    titles.add(original)
        link_val = props.get("听原文", {}).get("url")
        if link_val:
            links.add(link_val)
            normalized = normalize_url(link_val)
            if normalized:
                links.add(normalized)

    return titles, links


def _title_property(title: str) -> dict:
    return {"title": [{"text": {"content": (title or "")[:2000]}}]}


def _summary_property(summary: str) -> dict:
    return {"rich_text": [{"text": {"content": summary[:2000]}}]}


def _status_property(name: str) -> dict:
    return {"status": {"name": name}}


def _reader_properties(
    title: str,
    link: str,
    published_date: str | None,
    summary: str | None,
    status: str,
) -> dict:
    """Properties this repo is allowed to write.

    Confirmed by the existing ingest path: 名称, 听原文, 处理状态, 发布日期.
    摘要 is added only when the database schema says it is rich text (or when
    the schema could not be read, in which case a 400 is retried without it).
    """
    resolved_status = status or "收录中"
    properties = {
        "名称": _title_property(title),
        "听原文": {"url": link},
        "处理状态": _status_property(resolved_status),
    }
    if published_date:
        properties["发布日期"] = {"date": {"start": published_date}}

    if summary:
        if _summary_supported():
            properties["摘要"] = _summary_property(summary)
        else:
            logger.warning("Reader database has no rich_text 摘要; leaving 处理状态 as 收录中")
            properties["处理状态"] = _status_property("收录中")
    return properties


def _minimal_properties(properties: dict) -> dict:
    """Historical payload: title, link, 收录中, and the publish date when present."""
    minimal = {
        "名称": properties["名称"],
        "听原文": properties["听原文"],
        "处理状态": _status_property("收录中"),
    }
    if "发布日期" in properties:
        minimal["发布日期"] = properties["发布日期"]
    return minimal


def _post_json(url: str, payload: dict) -> requests.Response | None:
    try:
        return requests.post(
            url, headers=_get_headers(), json=payload, timeout=_REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException as err:
        logger.error("Notion request error: %s", err)
        return None


def _patch_json(url: str, payload: dict) -> requests.Response | None:
    try:
        return requests.patch(
            url, headers=_get_headers(), json=payload, timeout=_REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException as err:
        logger.error("Notion request error: %s", err)
        return None


def _log_notion_error(action: str, response: requests.Response) -> None:
    logger.error(
        "%s failed: HTTP %s %s",
        action,
        response.status_code,
        (response.text or "")[:500],
    )


def _create_reader_page(properties: dict, children: list[dict]) -> str | None:
    url = f"{NOTION_BASE_URL}/pages"
    payload = {
        "parent": {"database_id": NOTION_READER_DATABASE_ID},
        "properties": properties,
        "children": children,
    }
    response = _post_json(url, payload)
    if response is None:
        return None
    if response.ok:
        return response.json().get("id")

    _log_notion_error("Create page", response)
    if response.status_code != 400:
        return None

    fallback = _minimal_properties(properties)
    if fallback == properties:
        return None
    logger.warning("Retrying Notion create with the original property set")
    payload["properties"] = fallback
    response = _post_json(url, payload)
    if response is None:
        return None
    if not response.ok:
        _log_notion_error("Create page retry", response)
        return None
    return response.json().get("id")


def _append_children(page_id: str, blocks: list[dict]) -> bool:
    """Append blocks. Stop on the first failed chunk so callers can avoid deleting the old body."""
    for index in range(0, len(blocks), _MAX_BLOCKS_PER_REQUEST):
        chunk = blocks[index:index + _MAX_BLOCKS_PER_REQUEST]
        response = _patch_json(
            f"{NOTION_BASE_URL}/blocks/{page_id}/children",
            {"children": chunk},
        )
        if response is None or not response.ok:
            if response is not None:
                _log_notion_error(f"Append blocks to {page_id}", response)
            return False
    return True


def add_feed_item_to_notion(notion_item: dict) -> bool:
    title = notion_item.get("title", "")
    link = notion_item.get("link", "")
    content: list[dict] = notion_item.get("content") or []
    published_date = notion_item.get("published_date")
    summary = notion_item.get("summary") or None
    status = notion_item.get("status") or "收录中"

    first_chunk = content[:_MAX_BLOCKS_PER_REQUEST]
    remaining = content[_MAX_BLOCKS_PER_REQUEST:]
    properties = _reader_properties(title, link, published_date, summary, status)

    page_id = _create_reader_page(properties, first_chunk)
    if not page_id:
        return False
    if remaining:
        _append_children(page_id, remaining)
    return True


def _reader_page(item: dict) -> dict:
    props = item.get("properties", {})
    date_value = (props.get("发布日期", {}).get("date") or {}).get("start")
    status_value = (props.get("处理状态", {}).get("status") or {}).get("name") or ""
    return {
        "id": item.get("id"),
        "title": _plain_rich_text(props.get("名称", {}).get("title")),
        "link": props.get("听原文", {}).get("url") or "",
        "summary": _plain_rich_text(props.get("摘要", {}).get("rich_text")),
        "status": status_value,
        "published_date": date_value,
    }


def _created_since(days: int) -> str:
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return since.isoformat()


def get_recent_pages(days: int = 3) -> list[dict]:
    """Every reader page created in the last `days`, regardless of summary or status."""
    payload = {
        "filter": {
            "timestamp": "created_time",
            "created_time": {"on_or_after": _created_since(days)},
        }
    }
    results = _query_database_with_pagination(NOTION_READER_DATABASE_ID, payload)
    return [_reader_page(item) for item in results]


def get_backfill_candidates(days: int = 3) -> list[dict]:
    """Pages created in the last `days` with an empty 摘要 or 处理状态=收录中."""
    schema = _load_reader_schema()
    if schema is not None and schema.get("摘要") != "rich_text":
        logger.warning("Reader database has no rich_text 摘要 property; summary backfill skipped")
        return []

    payload = {
        "filter": {
            "and": [
                {
                    "timestamp": "created_time",
                    "created_time": {"on_or_after": _created_since(days)},
                },
                {
                    "or": [
                        {"property": "摘要", "rich_text": {"is_empty": True}},
                        {"property": "处理状态", "status": {"equals": "收录中"}},
                    ]
                },
            ]
        }
    }
    results = _query_database_with_pagination(NOTION_READER_DATABASE_ID, payload)
    return [_reader_page(item) for item in results]


_TEXT_BLOCK_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "callout",
    "toggle",
    "code",
}


def _block_plain(block: dict) -> str:
    block_type = block.get("type")
    body = block.get(block_type) or {}
    return _plain_rich_text(body.get("rich_text")).strip()


def body_blocks_are_empty(blocks: list[dict]) -> bool:
    """True when the page has no text and no non-text block such as an image."""
    for block in blocks:
        block_type = block.get("type")
        if block_type not in _TEXT_BLOCK_TYPES:
            return False
        if _block_plain(block):
            return False
    return True


def page_body_is_empty(page_id: str) -> bool:
    """True when the page has no readable body. A failed read is treated as not empty."""
    if not page_id:
        return False
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        try:
            response = requests.get(
                f"{NOTION_BASE_URL}/blocks/{page_id}/children",
                headers=_get_headers(),
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as err:
            logger.error("Error reading page %s: %s", page_id, err)
            return False
        blocks = data.get("results", [])
        if not body_blocks_are_empty(blocks):
            return False
        if not data.get("has_more"):
            return True
        cursor = data.get("next_cursor")
        if not cursor:
            return True


def get_page_plain_text(page_id: str, *, max_chars: int = 6000) -> str:
    """Read paragraph text already stored on a page. Used when a refetch fails."""
    if not page_id:
        return ""
    parts: list[str] = []
    total = 0
    cursor: str | None = None
    while total < max_chars:
        params: dict[str, str | int] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        try:
            response = requests.get(
                f"{NOTION_BASE_URL}/blocks/{page_id}/children",
                headers=_get_headers(),
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as err:
            logger.error("Error reading page %s: %s", page_id, err)
            break

        for block in data.get("results", []):
            text = _block_plain(block)
            if not text:
                continue
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
        if not data.get("has_more") or total >= max_chars:
            break
        cursor = data.get("next_cursor")
    return "\n".join(parts)[:max_chars]


def _list_child_ids(block_id: str) -> list[str]:
    ids: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        response = _post_safe_get_children(block_id, params)
        if response is None or not response.ok:
            if response is not None:
                _log_notion_error(f"List blocks {block_id}", response)
            break
        data = response.json()
        ids.extend(block.get("id") for block in data.get("results", []) if block.get("id"))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return ids


def _post_safe_get_children(block_id: str, params: dict) -> requests.Response | None:
    try:
        return requests.get(
            f"{NOTION_BASE_URL}/blocks/{block_id}/children",
            headers=_get_headers(),
            params=params,
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as err:
        logger.error("Error listing blocks %s: %s", block_id, err)
        return None


def _replace_children(page_id: str, blocks: list[dict]) -> bool:
    """Append the new body first, then archive the previous blocks.

    Appending before deleting means a failed append leaves the old article in place.
    """
    if not blocks:
        return True
    old_ids = _list_child_ids(page_id)
    if not _append_children(page_id, blocks):
        logger.error("New body was not saved; existing blocks left in place on %s", page_id)
        return False
    archived = True
    for block_id in old_ids:
        response = _patch_json(
            f"{NOTION_BASE_URL}/blocks/{block_id}",
            {"archived": True},
        )
        if response is None or not response.ok:
            archived = False
            if response is not None:
                _log_notion_error(f"Archive block {block_id}", response)
    return archived


def update_reader_page(
    page_id: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    status: str | None = None,
    blocks: list[dict] | None = None,
) -> bool:
    """Patch properties and, when `blocks` is given, replace the page body."""
    properties: dict = {}
    if title:
        properties["名称"] = _title_property(title)
    if summary:
        if _summary_supported():
            properties["摘要"] = _summary_property(summary)
        else:
            logger.warning("Skipping summary update for %s; 摘要 is unavailable", page_id)
            status = None
    if status:
        properties["处理状态"] = _status_property(status)

    if properties:
        response = _patch_json(
            f"{NOTION_BASE_URL}/pages/{page_id}",
            {"properties": properties},
        )
        if response is None:
            return False
        if not response.ok:
            _log_notion_error(f"Update page {page_id}", response)
            if response.status_code == 400 and ("摘要" in properties or status == "已整理"):
                fallback = {}
                if title:
                    fallback["名称"] = properties["名称"]
                fallback["处理状态"] = _status_property("收录中")
                response = _patch_json(
                    f"{NOTION_BASE_URL}/pages/{page_id}",
                    {"properties": fallback},
                )
                if response is None or not response.ok:
                    if response is not None:
                        _log_notion_error(f"Update page retry {page_id}", response)
                    return False
            else:
                return False

    if blocks:
        return _replace_children(page_id, blocks)
    return True


def delete_old_unread_feed_items_from_notion() -> None:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=_ARCHIVE_AFTER_DAYS)

    payload = {
        "filter": {
            "and": [
                {
                    "timestamp": "created_time",
                    "created_time": {"on_or_before": cutoff.isoformat()},
                },
                {
                    "property": "处理状态",
                    "status": {"equals": "收录中"},
                },
            ]
        }
    }

    results = _query_database_with_pagination(NOTION_READER_DATABASE_ID, payload)
    archived = 0

    for item in results:
        page_id = item.get("id")
        try:
            response = requests.patch(
                f"{NOTION_BASE_URL}/pages/{page_id}",
                headers=_get_headers(),
                json={"archived": True},
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            archived += 1
        except requests.exceptions.RequestException as err:
            logger.error("Error archiving page %s: %s", page_id, err)

    if archived:
        logger.info("Archived %d old unread items", archived)
