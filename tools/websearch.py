#!/usr/bin/env python3
"""Minimal internet search for the researcher agent (stdlib only).

Usage:
    python3 tools/websearch.py "запрос" [--count 8] [--json]

Fetches the public Bing results page (no API key) and prints the top results.
The researcher then opens useful pages with the built-in `webfetch` tool.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
TITLE_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
SNIPPET_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
BING_REDIRECT_RE = re.compile(r"[?&]u=a1([^&\"]+)")


def _clean(fragment: str) -> str:
    return html.unescape(TAG_RE.sub("", fragment)).strip()


def _decode_bing_url(href: str) -> str:
    """Bing wraps results as /ck/a?...&u=a1<base64url-of-url>. Unwrap it."""
    href = html.unescape(href)
    match = BING_REDIRECT_RE.search(href)
    if not match:
        return href
    chunk = match.group(1)
    chunk += "=" * (-len(chunk) % 4)
    try:
        return base64.urlsafe_b64decode(chunk).decode("utf-8", "replace")
    except Exception:
        return href


def parse_results(page: str, count: int = 8) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for block in BLOCK_RE.findall(page):
        if "b_ad" in block[:200]:
            continue
        title_match = TITLE_RE.search(block)
        if not title_match:
            continue
        url = _decode_bing_url(title_match.group(1))
        if not url.startswith(("http://", "https://")):
            continue
        if "bing.com" in url and "/ck/a" in url:
            continue
        title = _clean(title_match.group(2))
        if not title or url in seen:
            continue
        snippet_match = SNIPPET_RE.search(block)
        snippet = _clean(snippet_match.group(1)) if snippet_match else ""
        snippet = re.sub(r"\s+", " ", snippet)[:400]
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= count:
            break
    return results


def search(query: str, count: int = 8, timeout: float = 20.0) -> list[dict]:
    params = urllib.parse.urlencode(
        {"q": query, "count": str(max(count, 5)), "setlang": "ru", "form": "QBLH"}
    )
    request = urllib.request.Request(
        "https://www.bing.com/search?" + params,
        headers={
            "User-Agent": UA,
            "Accept-Language": "ru,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    page = raw.decode("utf-8", "replace")
    return parse_results(page, count=count)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="websearch.py")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    query = " ".join(args.query).strip()
    if not query:
        print("нужен поисковый запрос", file=sys.stderr)
        return 2
    try:
        results = search(query, count=max(1, min(args.count, 15)))
    except Exception as exc:  # network is best-effort, report clearly
        print(f"ОШИБКА поиска: {exc}", file=sys.stderr)
        return 1
    if not results:
        print("Ничего не найдено (или поисковик вернул антибот-страницу).")
        return 0
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for index, item in enumerate(results, 1):
        print(f"{index}. {item['title']}")
        print(f"   {item['url']}")
        if item["snippet"]:
            print(f"   {item['snippet']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))