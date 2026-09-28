"""Refresh pipeline: pull each active feed (conditional GET) -> parse (feedparser) -> upsert -> prune."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import unescape
from html.parser import HTMLParser

import feedparser
import httpx

from .config import Feed, Settings, load_feeds
from .db import connect

log = logging.getLogger("news_mcp.refresh")

SUMMARY_MAX_CHARS = 1500
MAX_CONCURRENCY = 6


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """Feed summaries are often HTML; the model wants plain text (and fewer tokens)."""
    if "<" not in html:
        return " ".join(unescape(html).split())
    p = _TextExtractor()
    p.feed(html)
    p.close()
    return " ".join(" ".join(p.parts).split())


def sync_sources(conn: sqlite3.Connection, feeds: list[Feed]) -> None:
    """Mirror the curated list into `sources`, keeping per-feed poll state. Feeds dropped from config go inactive."""
    for f in feeds:
        conn.execute(
            """INSERT INTO sources (source, name, url, lang, country, category, active)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                 name=excluded.name, lang=excluded.lang, country=excluded.country,
                 category=excluded.category, active=excluded.active,
                 -- a new URL invalidates the cached validators
                 etag=CASE WHEN url=excluded.url THEN etag END,
                 last_modified=CASE WHEN url=excluded.url THEN last_modified END,
                 url=excluded.url""",
            (f.source, f.name, f.url, f.lang, f.country, f.category, int(f.active)),
        )
    known = [f.source for f in feeds]
    conn.execute(
        f"UPDATE sources SET active=0 WHERE source NOT IN ({','.join('?' * len(known))})", known
    )
    conn.commit()


def entry_rows(feed: dict, source: str, lang: str, now: datetime, cutoff: datetime) -> list[tuple]:
    rows = []
    for e in feed.get("entries", []):
        title = html_to_text(e.get("title", "") or "")
        link = e.get("link")
        if not title and not link:
            continue
        raw_summary = e.get("summary") or ""
        if not raw_summary and e.get("content"):
            raw_summary = e["content"][0].get("value", "")
        summary = html_to_text(raw_summary)
        if summary == title:
            summary = ""
        if len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"

        parsed = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime.fromtimestamp(calendar.timegm(parsed), UTC) if parsed else now
        if published > now + timedelta(hours=1):  # clock-skewed / scheduled items
            published = now
        if published < cutoff:
            continue

        guid = e.get("id") or link or hashlib.sha1(f"{source}|{title}".encode()).hexdigest()
        rows.append((source, guid, lang, title, summary, link, iso(published), iso(now)))
    return rows


@dataclass
class FeedResult:
    source: str
    status: str  # "ok" | "not-modified" | "error: ..."
    items: int = 0


async def fetch_feed(
    client: httpx.AsyncClient, conn: sqlite3.Connection, src: sqlite3.Row, now: datetime, cutoff: datetime
) -> FeedResult:
    headers = {}
    if src["etag"]:
        headers["If-None-Match"] = src["etag"]
    if src["last_modified"]:
        headers["If-Modified-Since"] = src["last_modified"]

    etag, last_modified, last_ok = src["etag"], src["last_modified"], src["last_ok"]
    try:
        resp = await client.get(src["url"], headers=headers)
        if resp.status_code == 304:
            result = FeedResult(src["source"], "not-modified")
            last_ok = iso(now)
        elif resp.status_code != 200:
            result = FeedResult(src["source"], f"error: HTTP {resp.status_code}")
        else:
            parsed = feedparser.parse(resp.content, response_headers={k.lower(): v for k, v in resp.headers.items()})
            if not parsed.entries and parsed.bozo:
                result = FeedResult(src["source"], f"error: unparseable ({type(parsed.bozo_exception).__name__})")
            else:
                rows = entry_rows(parsed, src["source"], src["lang"], now, cutoff)
                conn.executemany(
                    """INSERT INTO news (source, guid, lang, title, summary, link, published_utc, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, guid) DO UPDATE SET
                         title=excluded.title, summary=excluded.summary, link=excluded.link""",
                    rows,
                )
                etag = resp.headers.get("etag")
                last_modified = resp.headers.get("last-modified")
                last_ok = iso(now)
                result = FeedResult(src["source"], "ok", len(rows))
    except (httpx.HTTPError, OSError) as exc:  # a down feed is skipped, never fatal
        result = FeedResult(src["source"], f"error: {type(exc).__name__}: {exc}"[:300])

    conn.execute(
        "UPDATE sources SET etag=?, last_modified=?, last_fetch=?, last_status=?, last_ok=? WHERE source=?",
        (etag, last_modified, iso(now), result.status, last_ok, src["source"]),
    )
    conn.commit()
    return result


async def refresh(settings: Settings) -> list[FeedResult]:
    conn = connect(settings.db_path)
    try:
        sync_sources(conn, load_feeds(settings.feeds_path))
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=settings.retention_days)
        active = conn.execute("SELECT * FROM sources WHERE active=1 ORDER BY source").fetchall()

        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, "Accept": "application/rss+xml, application/atom+xml, application/rdf+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"},
            timeout=settings.fetch_timeout,
            follow_redirects=True,
        ) as client:

            async def one(src: sqlite3.Row) -> FeedResult:
                async with sem:
                    return await fetch_feed(client, conn, src, now, cutoff)

            results = await asyncio.gather(*(one(s) for s in active))

        pruned = conn.execute("DELETE FROM news WHERE published_utc < ?", (iso(cutoff),)).rowcount
        conn.commit()
        for r in results:
            log.info("%-16s %-14s %d items", r.source, r.status, r.items)
        log.info("pruned %d items older than %d days", pruned, settings.retention_days)
        return list(results)
    finally:
        conn.close()
