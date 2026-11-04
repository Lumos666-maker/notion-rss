"""Unit tests for dedupe, blocklist, Notion payload shape, and the LLM client."""

import os
import unittest
from unittest import mock

from blocklist import is_blocked, reset_cache
from enrich import enrich_one, fulltext_enabled
import notion
from feed import select_new_items
from notion import _minimal_properties, _reader_properties
from fulltext import _is_interstitial
from parser import text_to_notion_blocks
from summarize import (
    LlmError,
    apply_teaser_prefix,
    compose_bilingual_title,
    summarize_item,
    title_needs_zh,
)
from urlnorm import normalize_url


class NormalizeUrlTest(unittest.TestCase):
    def test_strips_utm_and_rss_flag(self):
        raw = "https://Example.com/news/1/?utm_source=rss&utm_medium=feed&f=rss&id=9"
        self.assertEqual(normalize_url(raw), "https://example.com/news/1?id=9")

    def test_keeps_real_query_and_non_rss_f(self):
        raw = "https://news.site/a?f=html&id=3&fbclid=abc&spm=1.2"
        self.assertEqual(normalize_url(raw), "https://news.site/a?f=html&id=3")

    def test_strips_bbc_campaign_params(self):
        raw = "https://www.bbc.co.uk/news/articles/abc?at_medium=RSS&at_campaign=rss"
        self.assertEqual(normalize_url(raw), "https://www.bbc.co.uk/news/articles/abc")

    def test_strips_from_rss_and_fragment_tracking(self):
        raw = "https://a.com/p/?from=rss#utm_campaign=x"
        self.assertEqual(normalize_url(raw), "https://a.com/p")

    def test_empty(self):
        self.assertEqual(normalize_url(""), "")
        self.assertEqual(normalize_url("not a url"), "not a url")


class DedupeTest(unittest.TestCase):
    def test_skips_normalized_link_and_exact_title(self):
        items = [
            {"title": "Same", "link": "https://a.com/x?utm_source=rss", "content": "a", "source": "S"},
            {"title": "Other", "link": "https://a.com/x?f=rss", "content": "b", "source": "S"},
            {"title": "Same", "link": "https://b.com/y", "content": "c", "source": "S"},
            {"title": "Fresh", "link": "https://c.com/z?id=1", "content": "d", "source": "S"},
        ]
        kept = select_new_items(items, existing_titles=set(), existing_links=set())
        self.assertEqual([item["title"] for item in kept], ["Same", "Fresh"])
        self.assertEqual(kept[0]["link"], "https://a.com/x")
        self.assertEqual(kept[1]["link"], "https://c.com/z?id=1")

    def test_existing_normalized_link_and_title(self):
        items = [
            {"title": "Known title", "link": "https://a.com/new", "content": "", "source": ""},
            {"title": "New", "link": "https://a.com/old?utm_medium=email", "content": "", "source": ""},
        ]
        kept = select_new_items(
            items,
            existing_titles={"Known title"},
            existing_links={"https://a.com/old"},
        )
        self.assertEqual(kept, [])


class BlocklistTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("BLOCKLIST_KEYWORDS", None)
        os.environ.pop("BLOCKLIST_SOURCES", None)

    def test_default_file_blocks_nothing(self):
        reset_cache()
        self.assertFalse(is_blocked("股价大涨", "<p>美股收盘</p>", "行情"))

    def test_env_keyword_and_source(self):
        os.environ["BLOCKLIST_KEYWORDS"] = "美股收盘"
        os.environ["BLOCKLIST_SOURCES"] = "行情快讯"
        self.assertTrue(is_blocked("今日", "开盘 美股收盘 走高", "别处"))
        self.assertTrue(is_blocked("无关标题", "", "早间行情快讯"))
        self.assertFalse(is_blocked("芯片发布", "正文", "科技"))


class TitleAndBlocksTest(unittest.TestCase):
    def test_bilingual_title_only_for_english(self):
        self.assertTrue(title_needs_zh("OpenAI ships a new model"))
        self.assertFalse(title_needs_zh("华为发布新机"))
        self.assertEqual(
            compose_bilingual_title("OpenAI ships a new model", "OpenAI 发布新模型"),
            "OpenAI 发布新模型｜OpenAI ships a new model",
        )
        self.assertEqual(compose_bilingual_title("华为发布新机", "不该出现"), "华为发布新机")

    def test_teaser_prefix_once(self):
        self.assertTrue(apply_teaser_prefix("公司发布了新品").startswith("「仅据提要」"))
        self.assertEqual(
            apply_teaser_prefix("「仅据提要」已经有了"),
            "「仅据提要」已经有了",
        )

    def test_blocks_respect_notion_limits(self):
        text = ("段落甲。\n\n" + ("乙" * 4500) + "\n\n丙")
        blocks = text_to_notion_blocks(text)
        self.assertGreaterEqual(len(blocks), 3)
        for block in blocks:
            content = block["paragraph"]["rich_text"][0]["text"]["content"]
            self.assertLessEqual(len(content), 2000)
            self.assertTrue(content.strip())


class NotionPropertyTest(unittest.TestCase):
    def tearDown(self):
        notion._schema_loaded = False
        notion._reader_schema = None

    def test_only_known_properties_are_written(self):
        notion._schema_loaded = True
        notion._reader_schema = {
            "名称": "title",
            "听原文": "url",
            "发布日期": "date",
            "处理状态": "status",
            "摘要": "rich_text",
        }
        props = _reader_properties("标题", "https://a.com/x", "2026-11-01", "一句摘要。", "已整理")
        self.assertEqual(set(props), {"名称", "听原文", "发布日期", "处理状态", "摘要"})
        self.assertEqual(props["处理状态"]["status"]["name"], "已整理")
        self.assertEqual(props["摘要"]["rich_text"][0]["text"]["content"], "一句摘要。")
        minimal = _minimal_properties(props)
        self.assertEqual(set(minimal), {"名称", "听原文", "发布日期", "处理状态"})
        self.assertEqual(minimal["处理状态"]["status"]["name"], "收录中")

    def test_missing_summary_property_does_not_mark_done(self):
        notion._schema_loaded = True
        notion._reader_schema = {"名称": "title", "听原文": "url", "处理状态": "status"}
        props = _reader_properties("标题", "https://a.com/x", None, "一句摘要。", "已整理")
        self.assertNotIn("摘要", props)
        self.assertEqual(props["处理状态"]["status"]["name"], "收录中")


class InterstitialTest(unittest.TestCase):
    def test_script_captcha_on_a_long_page_is_kept(self):
        html = "<html><script>loadCaptcha()</script><article>" + ("正文段落。" * 400) + "</article></html>"
        self.assertFalse(_is_interstitial(html))

    def test_short_security_check_is_blocked(self):
        html = "<html><body><p>火山引擎 正在进行安全检测... 请耐心等待</p></body></html>"
        self.assertTrue(_is_interstitial(html))


class EnrichTest(unittest.TestCase):
    def tearDown(self):
        for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "FULLTEXT_FETCH"):
            os.environ.pop(key, None)

    def test_without_llm_keeps_rss_body_and_status(self):
        self.assertFalse(fulltext_enabled())
        item = {
            "title": "OpenAI ships a new model",
            "link": "https://example.com/a",
            "content": "<p>Just a teaser from the feed.</p>",
            "published_date": "2026-11-01",
            "source": "Example",
        }
        with mock.patch("enrich.fetch_article_text") as fetch:
            payload = enrich_one(item)
        fetch.assert_not_called()
        self.assertEqual(payload["status"], "收录中")
        self.assertIsNone(payload["summary"])
        self.assertEqual(payload["title"], item["title"])
        self.assertTrue(payload["content"])
        self.assertFalse(payload["llm_ok"])

    def test_teaser_prefix_when_fetch_fails(self):
        os.environ["LLM_API_KEY"] = "test-key"
        os.environ["LLM_BASE_URL"] = "https://llm.example/v1"
        os.environ["LLM_MODEL"] = "test-model"
        item = {
            "title": "OpenAI ships a new model",
            "link": "https://example.com/a",
            "content": "<p>A short teaser.</p>",
            "published_date": "2026-11-01",
            "source": "Example",
        }
        with mock.patch("enrich.fetch_article_text", return_value=None), mock.patch(
            "enrich.summarize_item",
            return_value=("「仅据提要」OpenAI 发布了新模型。", "OpenAI 发布新模型"),
        ) as summarize:
            payload = enrich_one(item)
        self.assertTrue(summarize.call_args.kwargs["teaser_only"])
        self.assertEqual(payload["status"], "已整理")
        self.assertTrue(payload["summary"].startswith("「仅据提要」"))
        self.assertIn("｜", payload["title"])
        self.assertFalse(payload["fulltext"])

    def test_llm_failure_stays_collecting(self):
        os.environ["LLM_API_KEY"] = "test-key"
        os.environ["LLM_BASE_URL"] = "https://llm.example/v1"
        os.environ["LLM_MODEL"] = "test-model"
        item = {
            "title": "已经是中文标题",
            "link": "https://example.com/b",
            "content": "<p>提要</p>",
            "source": "源",
        }
        with mock.patch("enrich.fetch_article_text", return_value="甲" * 200), mock.patch(
            "enrich.summarize_item", side_effect=LlmError("down")
        ):
            payload = enrich_one(item)
        self.assertEqual(payload["status"], "收录中")
        self.assertIsNone(payload["summary"])
        self.assertTrue(payload["fulltext"])
        self.assertEqual(payload["title"], "已经是中文标题")

    def test_full_text_is_written_as_paragraphs(self):
        os.environ["LLM_API_KEY"] = "test-key"
        os.environ["LLM_BASE_URL"] = "https://llm.example/v1"
        os.environ["LLM_MODEL"] = "test-model"
        article = "第一段正文，足够长。\n\n第二段继续说事实。" + "补充" * 40
        item = {
            "title": "已经是中文标题",
            "link": "https://example.com/c",
            "content": "<p>短提要</p>",
            "source": "源",
        }
        with mock.patch("enrich.fetch_article_text", return_value=article), mock.patch(
            "enrich.summarize_item", return_value=("这是一句中文摘要。", "")
        ):
            payload = enrich_one(item)
        self.assertEqual(payload["status"], "已整理")
        self.assertEqual(payload["summary"], "这是一句中文摘要。")
        self.assertFalse(payload["summary"].startswith("「仅据提要」"))
        joined = "\n".join(
            block["paragraph"]["rich_text"][0]["text"]["content"] for block in payload["content"]
        )
        self.assertIn("第一段正文", joined)
        self.assertNotIn("<p>", joined)


class SummarizeClientTest(unittest.TestCase):
    def tearDown(self):
        for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
            os.environ.pop(key, None)

    def test_parses_fenced_json_and_prefixes_teaser(self):
        os.environ["LLM_API_KEY"] = "test-key"
        os.environ["LLM_BASE_URL"] = "https://api.deepseek.com/v1"
        os.environ["LLM_MODEL"] = "deepseek-chat"
        body = {
            "choices": [
                {
                    "message": {
                        "content": '```json\n{"summary":"公司宣布了新计划。","title_zh":"公司新计划"}\n```'
                    }
                }
            ]
        }
        response = mock.Mock(status_code=200)
        response.json.return_value = body
        with mock.patch("summarize.requests.post", return_value=response) as post:
            summary, title_zh = summarize_item(
                title="A company announces a plan",
                source="Wire",
                text="teaser",
                teaser_only=True,
            )
        self.assertEqual(summary, "「仅据提要」公司宣布了新计划。")
        self.assertEqual(title_zh, "公司新计划")
        called_url = post.call_args.args[0]
        self.assertEqual(called_url, "https://api.deepseek.com/v1/chat/completions")
        self.assertTrue(post.call_args.kwargs["headers"]["Authorization"].startswith("Bearer "))
        # The prompt is truncated and does not include the key.
        self.assertNotIn("test-key", post.call_args.kwargs["json"]["messages"][1]["content"])

    def test_rejects_english_summary(self):
        os.environ["LLM_API_KEY"] = "test-key"
        os.environ["LLM_BASE_URL"] = "https://llm.example/v1"
        os.environ["LLM_MODEL"] = "m"
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "choices": [{"message": {"content": '{"summary":"English only.","title_zh":""}'}}]
        }
        with mock.patch("summarize.requests.post", return_value=response):
            with self.assertRaises(LlmError):
                summarize_item(title="标题", source="", text="正文", teaser_only=False)


if __name__ == "__main__":
    unittest.main()
