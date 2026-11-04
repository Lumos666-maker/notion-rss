"""Live fetch + extraction against a few real feeds.

Skipped unless RUN_LIVE_FETCH=1. The LLM is not called; a mock shows how a
successful extract is turned into a Notion payload.
"""

import os
import unittest

import feedparser

from enrich import enrich_one
from fulltext import fetch_article_text

_FEEDS = (
    "https://sspai.com/feed",
    "https://www.ifanr.com/feed",
    "https://blog.python.org/feeds/posts/default",
)


@unittest.skipUnless(os.getenv("RUN_LIVE_FETCH") == "1", "set RUN_LIVE_FETCH=1 to hit the network")
class LiveExtractTest(unittest.TestCase):
    def test_extracts_first_item_of_real_feeds(self):
        for feed_url in _FEEDS:
            parsed = feedparser.parse(feed_url)
            self.assertGreater(len(parsed.entries), 0, feed_url)
            entry = parsed.entries[0]
            link = entry.get("link") or ""
            text = fetch_article_text(link)
            print(f"\n{feed_url}\n  {entry.get('title')}\n  {link}\n  chars={0 if text is None else len(text)}")
            self.assertIsNotNone(text, link)
            self.assertGreater(len(text), 200, link)
            self.assertNotIn("<html", text.lower())

    def test_paywall_and_security_check_return_none(self):
        # NYT responds 403. 36kr serves a short "安全检测" page to this client.
        for url in (
            "https://www.nytimes.com/2024/01/15/technology/ai-regulation.html",
            "https://36kr.com/p/2935482198483200",
        ):
            text = fetch_article_text(url)
            print(f"\n{url}\n  extracted={None if text is None else len(text)}")
            self.assertIsNone(text, url)

    def test_mocked_llm_on_a_real_extract(self):
        parsed = feedparser.parse("https://sspai.com/feed")
        entry = parsed.entries[0]
        os.environ["FULLTEXT_FETCH"] = "1"
        os.environ["LLM_API_KEY"] = "mock"
        os.environ["LLM_BASE_URL"] = "https://llm.example/v1"
        os.environ["LLM_MODEL"] = "mock"
        try:
            from unittest import mock

            def _fake_summary(*, title, source, text, teaser_only):
                self.assertGreater(len(text), 200)
                self.assertFalse(teaser_only)
                return "少数派这篇讲的是一款把诗装进手机的应用。", ""

            item = {
                "title": entry.get("title") or "",
                "link": entry.get("link") or "",
                "content": entry.get("summary") or "",
                "source": "少数派",
            }
            with mock.patch("enrich.summarize_item", side_effect=_fake_summary):
                payload = enrich_one(item)
            self.assertTrue(payload["fulltext"])
            self.assertEqual(payload["status"], "已整理")
            self.assertIn("少数派", payload["summary"])
            joined = "\n".join(
                block["paragraph"]["rich_text"][0]["text"]["content"] for block in payload["content"]
            )
            self.assertGreater(len(joined), 200)
            self.assertNotIn("<p>", joined)
        finally:
            for key in ("FULLTEXT_FETCH", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
                os.environ.pop(key, None)


if __name__ == "__main__":
    unittest.main()
