"""Optional keyword and source blocklist. Empty by default."""

import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_BLOCKLIST_PATH = Path(__file__).with_name("blocklist.txt")


def _split_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse(text: str) -> tuple[list[str], list[str]]:
    keywords: list[str] = []
    sources: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("source:"):
            source = stripped.split(":", 1)[1].strip()
            if source:
                sources.append(source)
        else:
            keywords.append(stripped)
    return keywords, sources


@lru_cache(maxsize=1)
def _file_rules() -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        text = _BLOCKLIST_PATH.read_text(encoding="utf-8")
    except OSError as err:
        logger.warning("Could not read %s: %s", _BLOCKLIST_PATH, err)
        return (), ()
    keywords, sources = _parse(text)
    return tuple(keywords), tuple(sources)


def rules() -> tuple[list[str], list[str]]:
    """File rules plus optional BLOCKLIST_KEYWORDS / BLOCKLIST_SOURCES env extras."""
    file_keywords, file_sources = _file_rules()
    keywords = list(file_keywords) + _split_env("BLOCKLIST_KEYWORDS")
    sources = list(file_sources) + _split_env("BLOCKLIST_SOURCES")
    return keywords, sources


def is_blocked(title: str, content: str, source: str) -> bool:
    """True when the title, RSS body, or feed name hits a configured rule."""
    keywords, sources = rules()
    if not keywords and not sources:
        return False

    hay_source = (source or "").casefold()
    for name in sources:
        if name.casefold() in hay_source:
            return True

    if not keywords:
        return False
    hay = f"{title or ''}\n{content or ''}".casefold()
    return any(keyword.casefold() in hay for keyword in keywords)


def reset_cache() -> None:
    """Drop the cached file parse. Tests use this after rewriting env or file."""
    _file_rules.cache_clear()
