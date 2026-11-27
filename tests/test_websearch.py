"""Offline tests for the Bing results parser used by tools/websearch.py."""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import websearch  # noqa: E402


def encoded(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


class ParseResultsTest(unittest.TestCase):
    def test_parses_direct_and_redirect_urls(self) -> None:
        page = f"""
        <html><body><ol>
        <li class="b_algo" data-id iid=SERP.1>
          <h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u=a1{encoded('https://arxiv.org/abs/1234.5678')}&amp;ntb=1">Survey &amp; paper</a></h2>
          <p>An <b>AutoML</b> survey.</p>
        </li>
        <li class="b_algo">
          <h2><a href="https://example.org/direct">Direct result</a></h2>
          <p>Second snippet.</p>
        </li>
        </ol></body></html>
        """
        results = websearch.parse_results(page, count=8)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://arxiv.org/abs/1234.5678")
        self.assertEqual(results[0]["title"], "Survey & paper")
        self.assertIn("AutoML", results[0]["snippet"])
        self.assertEqual(results[1]["url"], "https://example.org/direct")

    def test_count_and_dedup(self) -> None:
        block = (
            '<li class="b_algo"><h2><a href="https://example.org/a">A</a></h2>'
            "<p>x</p></li>"
        )
        page = block * 3
        results = websearch.parse_results(page, count=8)
        self.assertEqual(len(results), 1)
        results = websearch.parse_results(page.replace("apple", "x"), count=1)
        self.assertEqual(len(results), 1)

    def test_skips_ads_and_non_http(self) -> None:
        page = """
        <li class="b_algo b_ad"><h2><a href="https://ad.example/">Ad</a></h2><p>x</p></li>
        <li class="b_algo"><h2><a href="/relative">Relative</a></h2><p>x</p></li>
        <li class="b_algo"><h2><a href="https://ok.example/">Ok</a></h2><p>good</p></li>
        """
        results = websearch.parse_results(page)
        self.assertEqual([r["url"] for r in results], ["https://ok.example/"])

    def test_empty_page(self) -> None:
        self.assertEqual(websearch.parse_results("<html>no results</html>"), [])


if __name__ == "__main__":
    unittest.main()