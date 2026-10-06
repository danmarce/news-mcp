"""Offline tests: parsing, storage, and tools against a seeded DB (no network)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import feedparser
import pytest

from news_mcp.config import Feed, Settings
from news_mcp.db import connect, fold
from news_mcp.refresh import entry_rows, html_to_text, iso, sync_sources
from news_mcp.server import build_server

NOW = datetime.now(UTC)
RDF = f"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://example.de/"><title>Beispiel</title></channel>
  <item rdf:about="https://example.de/a">
    <title>Wahl in Ecuador: Noboa gewinnt</title>
    <link>https://example.de/a</link>
    <description>&lt;p&gt;Die &lt;b&gt;Präsidentschaftswahl&lt;/b&gt; ist entschieden.&lt;/p&gt;</description>
    <dc:date>{iso(NOW - timedelta(hours=2))}</dc:date>
  </item>
  <item rdf:about="https://example.de/old">
    <title>Uralte Meldung</title>
    <link>https://example.de/old</link>
    <dc:date>{iso(NOW - timedelta(days=30))}</dc:date>
  </item>
</rdf:RDF>"""

FEEDS = [
    Feed("dw-de", "DW Deutsch", "https://x/de", "de", "DE", "general"),
    Feed("elcomercio", "El Comercio", "https://x/ec", "es", "EC", "general"),
    Feed("dead", "Dead Feed", "https://x/dead", "en", "US", "general", active=False),
]


def test_fold_is_case_and_accent_insensitive():
    assert fold("Elección ÉQUATEUR") == "eleccion equateur"
    assert fold("エクアドル") == fold("エクアドル")


def test_html_to_text():
    assert html_to_text("<p>Die <b>Wahl</b> &amp; mehr</p>") == "Die Wahl & mehr"


def test_rdf_parse_and_retention_cutoff():
    parsed = feedparser.parse(RDF.encode())
    rows = entry_rows(parsed, "dw-de", "de", NOW, NOW - timedelta(days=14))
    assert len(rows) == 1  # the 30-day-old item is outside the window
    source, guid, lang, title, summary, link, published, _ = rows[0]
    assert (source, guid, lang, link) == ("dw-de", "https://example.de/a", "de", "https://example.de/a")
    assert summary == "Die Präsidentschaftswahl ist entschieden."


@pytest.fixture
def server(tmp_path):
    settings = Settings(db_path=tmp_path / "news.db")
    conn = connect(settings.db_path)
    sync_sources(conn, FEEDS)
    rows = entry_rows(feedparser.parse(RDF.encode()), "dw-de", "de", NOW, NOW - timedelta(days=14))
    rows.append(("elcomercio", "ec-1", "es", "Elecciones en Ecuador: Noboa reelecto", "Resultados del CNE.",
                 "https://x/ec/1", iso(NOW - timedelta(hours=1)), iso(NOW)))
    rows.append(("elcomercio", "ec-2", "es", "Clima en Quito", "", "https://x/ec/2",
                 iso(NOW - timedelta(days=3)), iso(NOW)))
    conn.executemany("INSERT INTO news VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return build_server(settings)


def call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    content = result.content if hasattr(result, "content") else result[0]
    return json.loads(content[0].text)


def test_every_tool_declares_title_and_all_four_hints(server):
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"latest_news", "news_by_topic", "compare_coverage", "sources"}
    for t in tools:
        wire = t.annotations.model_dump(by_alias=True)  # the camelCase JSON a client/directory actually reads
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert isinstance(wire.get(hint), bool), f"{t.name}.{hint} unset"
        assert t.title and wire.get("title") == t.title, f"{t.name} missing title"
        # all tools only read the local store; feeds are pulled by the refresh job, never by a tool
        assert wire["readOnlyHint"] and wire["idempotentHint"], t.name
        assert not wire["destructiveHint"] and not wire["openWorldHint"], t.name


def test_latest_news_filters_and_window(server):
    assert [i["source"] for i in call(server, "latest_news", {})["items"]] == ["elcomercio", "dw-de"]
    assert call(server, "latest_news", {"lang": "DE"})["items"][0]["outlet"] == "DW Deutsch"
    assert call(server, "latest_news", {"hours": 24 * 7, "country": "ec"})["count"] == 2


def test_news_by_topic_and_or_accents(server):
    assert call(server, "news_by_topic", {"query": "ecuador noboa"})["count"] == 2
    assert call(server, "news_by_topic", {"query": "ecuador quito"})["count"] == 0
    assert call(server, "news_by_topic", {"query": "quito | praesident | präsidentschaftswahl"})["count"] == 2
    assert call(server, "news_by_topic", {"query": "ELECCIÓN"})["count"] == 1  # folds to a substring of "Elecciones"
    assert call(server, "news_by_topic", {"query": "electoral"})["count"] == 0  # substring, not stemming
    assert call(server, "news_by_topic", {"query": "100%_"})["count"] == 0  # LIKE wildcards escaped


def test_compare_coverage_groups_and_silence(server):
    out = call(server, "compare_coverage", {"topic": "Wahl | elecciones"})
    assert {g["source"] for g in out["coverage"]} == {"dw-de", "elcomercio"}
    assert out["no_matching_items"] == []  # inactive feeds are not reported as silent
    out = call(server, "compare_coverage", {"topic": "Quito"})
    assert [s["source"] for s in out["no_matching_items"]] == ["dw-de"]


def test_sources_lists_inactive(server):
    srcs = {s["source"]: s for s in call(server, "sources", {})["sources"]}
    assert srcs["dead"]["active"] is False and srcs["dw-de"]["items"] == 1
