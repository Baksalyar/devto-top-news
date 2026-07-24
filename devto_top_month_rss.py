import argparse
import datetime as dt
import html
import json
import random
import sys
import threading
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from email.utils import format_datetime

import requests
from bs4 import BeautifulSoup

TOP_URL_TEMPLATE = "https://dev.to/api/articles?top={top_days}&per_page={limit}&page={page}"
ARTICLE_URL_TEMPLATE = "https://dev.to/api/articles/{article_id}"
FEED_TITLE = "DEV.to Top Posts This Month"
FEED_LINK = "https://dev.to/top/month"
FEED_DESCRIPTION = "Top DEV.to posts from the last 30 days."
RATE_LIMIT_EXIT_CODE = 75


class RateLimitError(RuntimeError):
    """The upstream API asked the collector to back off."""


def fetch_json(session: requests.Session, url: str, retries: int = 3):
    last_response: requests.Response | None = None
    for attempt in range(retries + 1):
        response = session.get(url, timeout=30)
        last_response = response
        if response.status_code != 429:
            response.raise_for_status()
            return response.json()

        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            delay = int(retry_after)
        else:
            delay = 2 ** attempt
        jitter = random.uniform(0, 1)
        time.sleep(delay + jitter)

    if last_response is not None:
        raise RateLimitError(
            f"DEV.to rate limit after {retries + 1} attempts "
            f"for {last_response.url}"
        )
    raise RuntimeError("Request failed without a response.")


def extract_paragraphs(body_html: str, fallback_text: str | None) -> list[str]:
    soup = BeautifulSoup(body_html, "html.parser")
    paragraphs: list[str] = []

    for node in soup.find_all(["p", "blockquote", "li"]):
        text = " ".join(node.stripped_strings)
        if not text:
            continue
        paragraphs.append(text)
        if len(paragraphs) >= 20:
            break

    if len(paragraphs) < 2 and fallback_text:
        paragraphs.append(fallback_text)

    if len(paragraphs) < 2:
        paragraphs.append("Top DEV.to article snippet unavailable.")

    total_paragraphs = len(
        [
            node
            for node in soup.find_all(["p", "blockquote", "li"])
            if " ".join(node.stripped_strings)
        ]
    )
    if total_paragraphs > 20 and len(paragraphs) >= 20:
        paragraphs.append("Read the rest of the article at the source.")

    return paragraphs[:21]


def paragraphs_to_html(paragraphs: list[str]) -> str:
    return "".join(f"<p>{html.escape(text)}</p>" for text in paragraphs)


def build_rss(items: list[dict]) -> str:
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    channel_parts = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
        "<rss version=\"2.0\" xmlns:content=\"http://purl.org/rss/1.0/modules/content/\">",
        "  <channel>",
        f"    <title>{html.escape(FEED_TITLE)}</title>",
        f"    <link>{html.escape(FEED_LINK)}</link>",
        f"    <description>{html.escape(FEED_DESCRIPTION)}</description>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
    ]

    for item in items:
        channel_parts.append("    <item>")
        channel_parts.append(f"      <title>{html.escape(item['title'])}</title>")
        channel_parts.append(f"      <link>{html.escape(item['link'])}</link>")
        channel_parts.append(
            f"      <guid isPermaLink=\"true\">{html.escape(item['link'])}</guid>"
        )
        channel_parts.append(f"      <pubDate>{item['pub_date']}</pubDate>")
        channel_parts.append(
            f"      <content:encoded><![CDATA[{item['content']}]]></content:encoded>"
        )
        channel_parts.append("    </item>")

    channel_parts.append("  </channel>")
    channel_parts.append("</rss>")

    return "\n".join(channel_parts)


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def collect_items(session: requests.Session, limit: int, top_days: int) -> list[dict]:
    items: list[dict] = []
    # The endpoint is already sorted by DEV.to's ranking. Walking pages until
    # enough articles pass the likes threshold turns a normal refresh into
    # hundreds of API calls and reliably triggers the anonymous rate limit.
    fetch_limit = min(max(limit * 3, limit), 100)
    top_url = TOP_URL_TEMPLATE.format(top_days=top_days, limit=fetch_limit, page=1)
    top_articles = fetch_json(session, top_url)

    for article in top_articles:
        if len(items) >= limit:
            break
        reactions = article.get("positive_reactions_count", 0)
        if reactions < 100:
            continue

        article_id = article["id"]
        detail_url = ARTICLE_URL_TEMPLATE.format(article_id=article_id)
        detail = fetch_json(session, detail_url)

        paragraphs = extract_paragraphs(detail.get("body_html", ""), article.get("description"))

        published = detail.get("published_at") or detail.get("created_at")
        reactions = detail.get("positive_reactions_count", article.get("positive_reactions_count", 0))
        if published:
            published_dt = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
            pub_date = format_datetime(published_dt)
            date_str = published_dt.strftime("%-d %B %Y at %H:%M UTC")
        else:
            pub_date = format_datetime(dt.datetime.now(dt.timezone.utc))
            date_str = "unknown date"

        likes_word = "like" if reactions == 1 else "likes"
        info_line = f"Originally published on {date_str} \u2014 {reactions} {likes_word}"
        content_html = f"<p><em>{html.escape(info_line)}</em></p>" + paragraphs_to_html(paragraphs)

        items.append(
            {
                "id": article_id,
                "title": detail.get("title", article.get("title", "Untitled")),
                "link": detail.get("url", article.get("url")),
                "pub_date": pub_date,
                "content": content_html,
            }
        )

    return items


def write_feed(output_path: str, items: list[dict]) -> None:
    rss = build_rss(items)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(rss)


def start_server(port: int) -> ThreadingHTTPServer:
    handler = SimpleHTTPRequestHandler
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Serving feed on http://localhost:{port}/")
    return server


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an RSS feed for DEV.to top posts this month."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of top posts to include (default: 20).",
    )
    parser.add_argument(
        "--top-days",
        type=int,
        default=30,
        help="Window for top posts in days (default: 30).",
    )
    parser.add_argument(
        "--output",
        default="devto_top_month.xml",
        help="Output RSS file path (default: devto_top_month.xml).",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously and refresh the feed on an interval.",
    )
    parser.add_argument(
        "--min-interval",
        type=int,
        default=900,
        help="Minimum sleep time in seconds for daemon mode (default: 900).",
    )
    parser.add_argument(
        "--max-interval",
        type=int,
        default=1200,
        help="Maximum sleep time in seconds for daemon mode (default: 1200).",
    )
    parser.add_argument(
        "--state-file",
        default="devto_top_month_state.json",
        help="JSON file to store the latest seen article IDs.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Serve the output directory over HTTP.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the HTTP server (default: 8000).",
    )
    args = parser.parse_args()

    if args.limit <= 0:
        raise ValueError("--limit must be greater than zero.")

    if args.min_interval <= 0 or args.max_interval <= 0:
        raise ValueError("Intervals must be positive seconds.")
    if args.min_interval > args.max_interval:
        raise ValueError("--min-interval cannot exceed --max-interval.")
    if args.port <= 0:
        raise ValueError("--port must be a positive integer.")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "devto-top-month-rss/1.0",
            "Accept": "application/json",
        }
    )

    def refresh() -> int:
        items = collect_items(session, args.limit, args.top_days)
        write_feed(args.output, items)

        state = load_state(args.state_file)
        seen_ids = set(state.get("seen_ids", []))
        current_ids = [item["id"] for item in items]
        new_ids = [article_id for article_id in current_ids if article_id not in seen_ids]

        seen_ids.update(current_ids)
        state["seen_ids"] = sorted(seen_ids)
        state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save_state(args.state_file, state)

        if new_ids:
            print(f"Found {len(new_ids)} new articles. Feed updated.")
        else:
            print("No new articles. Feed refreshed.")
        return len(items)

    server = None
    if args.serve:
        server = start_server(args.port)

    if args.daemon or args.serve:
        print("Starting daemon mode. Press Ctrl+C to stop.")
        try:
            while True:
                refresh()
                sleep_for = random.uniform(args.min_interval, args.max_interval)
                print(f"Sleeping for {int(sleep_for)}s...")
                time.sleep(sleep_for)
        finally:
            if server:
                server.shutdown()
    else:
        count = refresh()
        print(f"Wrote RSS feed with {count} items to {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RateLimitError as exc:
        print(f"Rate limited by DEV.to: {exc}. Keeping the previous feed.", file=sys.stderr)
        raise SystemExit(RATE_LIMIT_EXIT_CODE)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise
