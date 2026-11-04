"""Chinese title + 1-2 sentence summary via any OpenAI-compatible chat API.

Configured only through env, so DeepSeek, Qwen, and OpenAI are the same code
path: set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL. When any of them is missing
the caller keeps the previous ingest behavior.
"""

import json
import logging
import os
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_TEASER_PREFIX = "「仅据提要」"

_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


class LlmError(Exception):
    """The summary could not be produced. Callers leave 处理状态 as 收录中."""


def llm_configured() -> bool:
    return bool(
        os.getenv("LLM_API_KEY", "").strip()
        and os.getenv("LLM_BASE_URL", "").strip()
        and os.getenv("LLM_MODEL", "").strip()
    )


def _concurrency() -> threading.Semaphore:
    global _semaphore
    with _semaphore_lock:
        if _semaphore is None:
            try:
                size = int(os.getenv("LLM_CONCURRENCY", "3"))
            except ValueError:
                size = 3
            _semaphore = threading.Semaphore(max(1, min(size, 8)))
        return _semaphore


def reset_semaphore() -> None:
    """Tests change LLM_CONCURRENCY and need a fresh limiter."""
    global _semaphore
    with _semaphore_lock:
        _semaphore = None


def title_needs_zh(title: str) -> bool:
    """True when the title is mostly English and should get a Chinese translation."""
    if not title:
        return False
    if len(_CJK_RE.findall(title)) >= 2:
        return False
    return _LATIN_WORD_RE.search(title) is not None


def compose_bilingual_title(original: str, title_zh: str) -> str:
    """`中文译名｜Original Title` when a translation is warranted, else the original."""
    original = (original or "").strip()
    translated = (title_zh or "").strip().strip("「」\"'“”")
    translated = re.sub(r"\s+", " ", translated)
    if not original:
        return translated[:2000]
    if not translated or not title_needs_zh(original):
        return original[:2000]
    if not _CJK_RE.search(translated):
        return original[:2000]
    if translated == original or original in translated:
        return translated[:2000]
    return f"{translated[:180]}｜{original}"[:2000]


def apply_teaser_prefix(summary: str) -> str:
    text = (summary or "").strip()
    if text.startswith(_TEASER_PREFIX):
        return text[:2000]
    return f"{_TEASER_PREFIX}{text}"[:2000]


def _input_limit() -> int:
    try:
        limit = int(os.getenv("LLM_INPUT_MAX_CHARS", "4000"))
    except ValueError:
        limit = 4000
    return max(500, min(limit, 12000))


def _chat_url(base: str) -> str:
    root = base.strip().rstrip("/")
    if root.endswith("/chat/completions"):
        return root
    return root + "/chat/completions"


def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
        return "".join(parts)
    return str(content or "")


def _parse_payload(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise LlmError("LLM reply had no JSON object")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as err:
        raise LlmError(f"LLM reply was not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise LlmError("LLM JSON was not an object")
    return data


def _build_messages(title: str, source: str, text: str, *, reminder: str = "") -> list[dict]:
    clipped = (text or "").strip()[:_input_limit()]
    user = (
        f"标题：{title or '（无标题）'}\n"
        f"来源：{source or '（未知）'}\n"
        f"材料：\n{clipped or '（无正文，仅有标题）'}\n\n"
        "只输出一个 JSON 对象，不要 Markdown，不要解释。格式："
        '{"summary":"1到2句中文摘要，合计不超过80个汉字，只陈述事实",'
        '"title_zh":"标题主要是英文时给简洁中文译名，标题已是中文时返回空字符串"}\n'
        "摘要必须是中文。不要写“本文介绍”“文章指出”。不要编造材料里没有的数字和引语。"
    )
    if reminder:
        user += "\n" + reminder
    return [
        {"role": "system", "content": "你是资讯编辑。只输出 JSON。"},
        {"role": "user", "content": user},
    ]


def _post_chat(messages: list[dict]) -> str:
    url = _chat_url(os.environ["LLM_BASE_URL"])
    payload = {
        "model": os.environ["LLM_MODEL"].strip(),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 300,
    }
    headers = {
        "Authorization": f"Bearer {os.environ['LLM_API_KEY'].strip()}",
        "Content-Type": "application/json",
    }
    delays = (0, 1.5, 4.0)
    last_error: Exception | None = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=45)
        except requests.exceptions.RequestException as err:
            last_error = err
            logger.warning("LLM request failed (attempt %d): %s", attempt, err)
            continue
        if response.status_code >= 400:
            logger.warning(
                "LLM HTTP %s (attempt %d): %s",
                response.status_code,
                attempt,
                (response.text or "")[:300],
            )
        if response.status_code == 429 or response.status_code >= 500:
            last_error = LlmError(f"LLM HTTP {response.status_code}")
            continue
        if response.status_code >= 400:
            raise LlmError(f"LLM HTTP {response.status_code}")
        try:
            data = response.json()
            return _message_text(data["choices"][0]["message"])
        except (KeyError, IndexError, TypeError, ValueError) as err:
            raise LlmError(f"Unexpected LLM response shape: {err}") from err
    raise LlmError(str(last_error or "LLM request failed"))


def summarize_item(*, title: str, source: str, text: str, teaser_only: bool) -> tuple[str, str]:
    """Return `(summary, title_zh)`. Raises LlmError on failure.

    `title_zh` is "" when the original title is already Chinese. When
    `teaser_only` is set, the summary is prefixed with 「仅据提要」.
    """
    if not llm_configured():
        raise LlmError("LLM env is not configured")

    reminder = ""
    parsed: dict | None = None
    with _concurrency():
        for attempt in range(2):
            raw = _post_chat(_build_messages(title, source, text, reminder=reminder))
            try:
                parsed = _parse_payload(raw)
                break
            except LlmError as err:
                logger.warning("LLM JSON parse failed (attempt %d): %s", attempt + 1, err)
                reminder = "上一次输出不是合法 JSON。这次只输出 JSON 对象本身。"
        if parsed is None:
            raise LlmError("LLM reply was not valid JSON")

        summary = str(parsed.get("summary") or "").strip()
        title_zh = str(parsed.get("title_zh") or "").strip()
        if not summary:
            raise LlmError("LLM returned an empty summary")
        if not _CJK_RE.search(summary):
            # One stricter retry, still inside the concurrency slot.
            raw = _post_chat(_build_messages(
                title,
                source,
                text,
                reminder="summary 必须是中文句子，不能是英文。",
            ))
            parsed = _parse_payload(raw)
            summary = str(parsed.get("summary") or "").strip()
            title_zh = str(parsed.get("title_zh") or "").strip()
            if not summary or not _CJK_RE.search(summary):
                raise LlmError("LLM summary was not Chinese")

    summary = re.sub(r"\s+", " ", summary).strip()
    if teaser_only:
        summary = apply_teaser_prefix(summary)
    else:
        summary = summary[:2000]
    return summary, title_zh
