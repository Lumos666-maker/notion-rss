"""Fetch a page and extract the article body with trafilatura."""

import logging
import os
import re

import requests
import trafilatura

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_MAX_BYTES = 1_500_000
_MIN_CHARS = 80

# Visible text of a challenge page, not words that merely appear in site scripts.
# Wikipedia's HTML mentions "captcha" inside scripts; that must not reject the article.
_BLOCK_MARKERS = (
    "security check",
    "安全验证",
    "安全检测",
    "请完成验证",
    "访问验证",
    "异常访问",
    "access denied",
    "just a moment",
    "cf-browser-verification",
    "enable javascript",
    "please enable cookies",
    "please enable js",
    "are you a robot",
    "captcha",
)
# Challenge pages are small. Real articles are not rejected for a script mention.
_INTERSTITIAL_HTML_MAX = 120_000


def _timeout() -> tuple[float, float]:
    try:
        read = float(os.getenv("FULLTEXT_TIMEOUT", "12"))
    except ValueError:
        read = 12.0
    read = min(max(read, 3.0), 30.0)
    return (5.0, read)


def _decode(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=[\"']?([\w.-]+)", content_type, re.I)
    if not match:
        head = raw[:2500].decode("ascii", errors="ignore")
        match = re.search(r"charset=[\"']?([\w.-]+)", head, re.I)
    if match:
        try:
            return raw.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    return raw.decode("utf-8", errors="replace")


def _read_limited(response: requests.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(65536):
        if not chunk:
            continue
        room = _MAX_BYTES - total
        if room <= 0:
            break
        if len(chunk) > room:
            chunks.append(chunk[:room])
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _looks_blocked(text: str) -> bool:
    sample = text[:4000].casefold()
    return any(marker.casefold() in sample for marker in _BLOCK_MARKERS)


def _visible_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_interstitial(html: str) -> bool:
    """True for small challenge pages. Large documents are left to the extractor."""
    if len(html) > _INTERSTITIAL_HTML_MAX:
        return False
    return _looks_blocked(_visible_text(html))


def fetch_article_text(url: str) -> str | None:
    """Return cleaned article text, or None when the page can't be used.

    None covers timeouts, HTTP errors (including paywall 403), anti-bot
    interstitials, and extracts that are too short to be the article.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=_timeout(),
            allow_redirects=True,
            stream=True,
        )
    except requests.exceptions.RequestException as err:
        logger.info("Full text fetch failed for %s: %s", url, err)
        return None

    try:
        status = response.status_code
        if status >= 400:
            logger.info("Full text HTTP %s for %s", status, url)
            return None
        content_type = response.headers.get("Content-Type", "")
        if content_type and not any(kind in content_type.lower() for kind in ("html", "xml", "text")):
            logger.info("Skip non-HTML %s for %s", content_type, url)
            return None
        raw = _read_limited(response)
    finally:
        response.close()

    if not raw:
        return None
    html = _decode(raw, content_type)
    if _is_interstitial(html):
        logger.info("Anti-bot or paywall interstitial for %s", url)
        return None

    try:
        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            deduplicate=True,
        )
    except Exception as err:
        logger.info("trafilatura failed for %s: %s", url, err)
        return None

    text = (extracted or "").strip()
    if len(text) < _MIN_CHARS:
        logger.info("Extract too short (%d chars) for %s", len(text), url)
        return None
    if _looks_blocked(text[:1500]) and len(text) < 800:
        logger.info("Extract looks like a challenge page for %s", url)
        return None
    return text
