import html
import logging
import os
import re

from markdownify import markdownify as turndown

logger = logging.getLogger(__name__)
NOTION_MAX_TEXT_LENGTH = 2000
# A long article is split across blocks; cap the body so a single page stays
# well under Notion's request size. ~30k Chinese characters is several screens.
_DEFAULT_BODY_CHARS = 30000
_MAX_BODY_BLOCKS = 120
_LINK_RE = re.compile(r'\[(.+?)]\((.+?)\)')
_NUMBERED_LIST_RE = re.compile(r'^\d+\.\s')
_TAG_RE = re.compile(r"(?is)<(script|style|noscript).*?>.*?</\1>")
_ANY_TAG_RE = re.compile(r"(?s)<[^>]+>")


def html_to_markdown(html_content: str) -> str:
    """Convert HTML content to Markdown."""
    try:
        return turndown(html_content)
    except Exception as e:
        logger.error("Error converting HTML to Markdown: %s", e)
        return ""


def _truncate(text: str, max_len: int = NOTION_MAX_TEXT_LENGTH) -> str:
    """Truncate text to fit Notion's per-block character limit."""
    return text[:max_len] if len(text) > max_len else text


def _make_rich_text(content: str, *, link: str | None = None, **annotations) -> list[dict]:
    text_obj: dict = {"content": _truncate(content)}
    if link:
        text_obj["link"] = {"url": link}
    entry: dict = {"type": "text", "text": text_obj}
    if annotations:
        entry["annotations"] = annotations
    return [entry]


def _make_block(block_type: str, rich_text: list[dict]) -> dict:
    return {"type": block_type, block_type: {"rich_text": rich_text}}


def markdown_to_notion_blocks(markdown_content: str) -> list[dict]:
    """Convert Markdown content to Notion blocks."""
    blocks: list[dict] = []

    for line in markdown_content.split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.startswith("### "):
            blocks.append(_make_block("heading_3", _make_rich_text(line[4:])))
        elif line.startswith("## "):
            blocks.append(_make_block("heading_2", _make_rich_text(line[3:])))
        elif line.startswith("# "):
            blocks.append(_make_block("heading_1", _make_rich_text(line[2:])))
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append(_make_block("bulleted_list_item", _make_rich_text(line[2:])))
        elif _NUMBERED_LIST_RE.match(line):
            text = _NUMBERED_LIST_RE.sub("", line, count=1)
            blocks.append(_make_block("numbered_list_item", _make_rich_text(text)))
        elif line.startswith("**") and line.endswith("**") and len(line) > 4:
            blocks.append(_make_block("paragraph", _make_rich_text(line[2:-2], bold=True)))
        elif line.startswith("*") and line.endswith("*") and len(line) > 2:
            blocks.append(_make_block("paragraph", _make_rich_text(line[1:-1], italic=True)))
        elif line.startswith("`") and line.endswith("`") and len(line) > 2:
            blocks.append(_make_block("paragraph", _make_rich_text(line[1:-1], code=True)))
        elif line.startswith("http://") or line.startswith("https://"):
            blocks.append(_make_block("paragraph", _make_rich_text(line, link=line)))
        else:
            link_match = _LINK_RE.match(line)
            if link_match:
                text_part, url_part = link_match.groups()
                blocks.append(_make_block("paragraph", _make_rich_text(text_part, link=url_part)))
            else:
                blocks.append(_make_block("paragraph", _make_rich_text(line)))

    return blocks


def html_to_notion_blocks(html_content: str) -> list[dict]:
    """Convert HTML content to Notion blocks."""
    markdown = html_to_markdown(html_content)
    return markdown_to_notion_blocks(markdown)


def html_to_plain_text(html_content: str) -> str:
    """Strip markup for summaries and blocklist checks. Plain text is returned as-is."""
    if not html_content:
        return ""
    text = _TAG_RE.sub(" ", html_content)
    text = _ANY_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _body_char_limit() -> int:
    try:
        limit = int(os.getenv("FULLTEXT_BODY_MAX_CHARS", str(_DEFAULT_BODY_CHARS)))
    except ValueError:
        limit = _DEFAULT_BODY_CHARS
    return max(2000, min(limit, 100000))


def _split_chunks(text: str, limit: int = NOTION_MAX_TEXT_LENGTH) -> list[str]:
    """Split a paragraph into Notion rich-text sized pieces, preferring sentence breaks."""
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + limit, length)
        if end < length:
            window = text[start:end]
            cuts = [window.rfind(mark) for mark in ("\n", "。", "！", "？", ".", " ")]
            cut = max(cuts)
            if cut >= limit // 2:
                end = start + cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end
    return chunks


def text_to_notion_blocks(text: str, *, max_chars: int | None = None) -> list[dict]:
    """Turn cleaned article text into paragraph blocks, truncated to Notion limits."""
    if not text:
        return []
    limit = max_chars if max_chars is not None else _body_char_limit()
    clipped = text.replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]
    blocks: list[dict] = []
    for para in re.split(r"\n+", clipped):
        para = para.strip()
        if not para:
            continue
        for chunk in _split_chunks(para):
            blocks.append(_make_block("paragraph", _make_rich_text(chunk)))
            if len(blocks) >= _MAX_BODY_BLOCKS:
                return blocks
    return blocks
