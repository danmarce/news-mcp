"""MCP query server: structured, read-only tools over the SQLite news store."""

from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import Settings
from .db import connect, fold

MAX_ITEMS = 50

INSTRUCTIONS = """\
Curated current-news store: headlines + short summaries from a hand-picked set of RSS feeds in several
languages and countries (Ecuador, Spain, Germany, France, UK, US, Qatar, Japan, Argentina, ...), refreshed
every ~30-60 min and kept for about two weeks. Use it for "what's happening / latest news / how did outlets
cover X". For older or encyclopedic facts use other tools.

Items are stored in their ORIGINAL language (field `lang`). Translate and summarize them faithfully into the
user's language: do not add facts, opinions, or loaded wording that is not in the item. Always attribute each
claim to its outlet (field `outlet`) and include the date. Different outlets covering the same story is
intentional - it lets you compare framing. Only title + summary are available (no full article text);
give the `link` when the user wants more.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _out(obj: dict[str, Any]) -> str:
    """One compact JSON text block: no duplicate structuredContent, no escaped Arabic/Japanese (tokens matter for a 12B model)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _like_escape(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def topic_filter(query: str) -> tuple[str, list[str]]:
    """'a b | c' -> items containing (a AND b) OR c; case/accent-insensitive substring match."""
    alternatives = [alt.split() for alt in fold(query).split("|")]
    alternatives = [terms for terms in alternatives if terms]
    if not alternatives:
        raise ValueError("query must contain at least one word")
    clauses, params = [], []
    for terms in alternatives:
        clauses.append("(" + " AND ".join("fold(n.title || ' ' || n.summary) LIKE ? ESCAPE '\\'" for _ in terms) + ")")
        params.extend(_like_escape(t) for t in terms)
    return "(" + " OR ".join(clauses) + ")", params


def _since(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(row: sqlite3.Row, summary_chars: int) -> dict[str, Any]:
    summary = row["summary"] or ""
    if len(summary) > summary_chars:
        summary = summary[:summary_chars].rsplit(" ", 1)[0] + "…"
    return {
        "outlet": row["name"],
        "source": row["source"],
        "country": row["country"],
        "lang": row["lang"],
        "published_utc": row["published_utc"],
        "title": row["title"],
        "summary": summary,
        "link": row["link"],
    }


ITEM_SELECT = """SELECT n.*, s.name, s.country, s.category
                 FROM news n JOIN sources s USING (source)"""


def build_server(settings: Settings) -> MCPServer:
    mcp = MCPServer(name="news", instructions=INSTRUCTIONS)

    def db() -> closing[sqlite3.Connection]:
        return closing(connect(settings.db_path))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def latest_news(
        source: str | None = None,
        lang: str | None = None,
        country: str | None = None,
        category: str | None = None,
        hours: float = 24,
        n: int = 15,
    ) -> str:
        """Most recent headlines, newest first. Use for "what's the news", "latest from Ecuador",
        "what does DW say today", "tech news". All filters are optional and combine with AND.

        Args:
            source: feed id from `sources()` (e.g. "elcomercio", "bbc-mundo", "dw-de").
            lang: ISO 639-1 language of the original items (e.g. "es", "en", "de", "fr", "ar", "ja").
            country: ISO 3166 alpha-2 country of the outlet (e.g. "EC", "US", "DE"); "INT" = international.
            category: one of general, politics, economy, science, tech, sport.
            hours: look-back window in hours (default 24; up to ~336 = two weeks).
            n: max items (default 15, max 50).
        """
        where, params = ["n.published_utc >= ?"], [_since(hours)]
        for col, val in (("s.source", source), ("n.lang", lang), ("s.country", country), ("s.category", category)):
            if val:
                where.append(f"lower({col}) = lower(?)")
                params.append(val.strip())
        sql = f"{ITEM_SELECT} WHERE {' AND '.join(where)} ORDER BY n.published_utc DESC LIMIT ?"
        with db() as conn:
            rows = conn.execute(sql, [*params, min(max(n, 1), MAX_ITEMS)]).fetchall()
        return _out({"count": len(rows), "items": [_item(r, 280) for r in rows]})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def news_by_topic(query: str, days: float = 7, lang: str | None = None, n: int = 20) -> str:
        """Search recent headlines + summaries by keyword, newest first. Use for "news about X",
        "what happened with X this week".

        Matching is case- and accent-insensitive substring search on the ORIGINAL-language text, so a
        Spanish word will not find German or Japanese items. Words separated by spaces must ALL appear;
        separate alternatives with "|". To search across languages give the key name/term in each
        language, e.g. "Noboa | ノボア" or "elecciones | Wahl | élection | election".
        Prefer short distinctive terms (names, places) over full sentences.

        Args:
            query: keywords; spaces = AND, "|" = OR between alternatives.
            days: look-back window in days (default 7, max ~14).
            lang: optional ISO 639-1 filter on the original language.
            n: max items (default 20, max 50).
        """
        clause, params = topic_filter(query)
        where, params = [clause, "n.published_utc >= ?"], [*params, _since(days * 24)]
        if lang:
            where.append("lower(n.lang) = lower(?)")
            params.append(lang.strip())
        sql = f"{ITEM_SELECT} WHERE {' AND '.join(where)} ORDER BY n.published_utc DESC LIMIT ?"
        with db() as conn:
            rows = conn.execute(sql, [*params, min(max(n, 1), MAX_ITEMS)]).fetchall()
        return _out({"query": query, "count": len(rows), "items": [_item(r, 500) for r in rows]})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def compare_coverage(topic: str, days: float = 7, per_source: int = 3) -> str:
        """How different outlets/countries covered one story - for framing comparison ("compare how the
        German, Qatari, US and Ecuadorian press covered X"). Returns matching items grouped by outlet, plus
        the active outlets with NO matching item (silence can be telling, but may also just mean the
        keywords were in another language).

        Same matching as `news_by_topic`: include the key term in every relevant language separated by
        "|", e.g. "Ecuador | Équateur | エクアドル | الإكوادور".
        When answering, compare emphasis, word choice and what each outlet includes or omits, citing each
        outlet by name; translate faithfully and do not inject your own view.

        Args:
            topic: keywords; spaces = AND, "|" = OR between alternatives.
            days: look-back window in days (default 7).
            per_source: max items per outlet (default 3, max 10).
        """
        clause, params = topic_filter(topic)
        sql = f"{ITEM_SELECT} WHERE {clause} AND n.published_utc >= ? ORDER BY n.published_utc DESC"
        per_source = min(max(per_source, 1), 10)
        with db() as conn:
            rows = conn.execute(sql, [*params, _since(days * 24)]).fetchall()
            active = conn.execute("SELECT source, name, country, lang FROM sources WHERE active=1").fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for r in rows:
            g = groups.setdefault(
                r["source"],
                {"outlet": r["name"], "source": r["source"], "country": r["country"], "lang": r["lang"],
                 "matches": 0, "items": []},
            )
            g["matches"] += 1
            if len(g["items"]) < per_source:
                g["items"].append(_item(r, 400))
        silent = [
            {"outlet": s["name"], "source": s["source"], "country": s["country"], "lang": s["lang"]}
            for s in active if s["source"] not in groups
        ]
        return _out({"topic": topic, "outlets_covering": len(groups), "coverage": list(groups.values()),
                     "no_matching_items": silent})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def sources() -> str:
        """List the curated feeds: id, outlet name, language, country, category, whether active, when it was
        last refreshed successfully, and how many items it currently holds. Use to know which ids/filters
        exist or to check how fresh the data is."""
        with db() as conn:
            rows = conn.execute(
                """SELECT s.*, COUNT(n.guid) AS items, MAX(n.published_utc) AS newest_item_utc
                   FROM sources s LEFT JOIN news n USING (source)
                   GROUP BY s.source ORDER BY s.active DESC, s.country, s.source"""
            ).fetchall()
        return _out({"sources": [
            {"source": r["source"], "outlet": r["name"], "lang": r["lang"], "country": r["country"],
             "category": r["category"], "active": bool(r["active"]), "items": r["items"],
             "newest_item_utc": r["newest_item_utc"], "last_ok_refresh_utc": r["last_ok"],
             "last_status": r["last_status"]}
            for r in rows
        ]})

    return mcp


class BearerAuth:
    """Minimal ASGI gate for streamable-http: requires `Authorization: Bearer <token>`. /healthz stays open."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            if scope["path"] == "/healthz":
                await _respond(send, 200, b"ok")
                return
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await _respond(send, 401, b"unauthorized")
                return
        await self.app(scope, receive, send)


async def _respond(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
